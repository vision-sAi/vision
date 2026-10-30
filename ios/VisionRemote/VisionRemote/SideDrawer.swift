import SwiftUI

/// The screen's own corner radius, so the pushed chat looks like the display shrunk sideways.
enum DisplayCorner {
    static let radius: CGFloat = {
        let screen = UIApplication.shared.connectedScenes.compactMap { ($0 as? UIWindowScene)?.screen }.first
        let r = screen?.value(forKey: "_displayCornerRadius") as? CGFloat ?? 0
        return r > 0 ? r : 44
    }()
}

/// Open/close motion. Applied explicitly to `isOpen` changes only: an implicit `.animation` on the
/// container would leak into the chat screen's own updates (a new transcript scroll view mid-swap).
enum SideDrawerMotion {
    static let duration: TimeInterval = 0.35
    static let animation: Animation = .spring(duration: duration, bounce: 0)

    /// The snap after a swipe carries on at the finger's speed instead of starting from rest, so a flick
    /// doesn't stall at release. `velocity` is relative: fractions of the remaining travel per second.
    /// Capped at the spring's own frequency, past which an unbouncy spring still overshoots.
    static func release(velocity: Double) -> Animation {
        let cap = 2 * .pi / duration
        return .interpolatingSpring(Spring(duration: duration, bounce: 0), initialVelocity: max(0, min(velocity, cap)))
    }

    /// How much smaller Panes draws the chat once the drawer is fully out.
    static let shrinkScale: CGFloat = 0.08
    /// The screen the chat slides across, unscaled: what `shrink(of:)` measures the chat against.
    static let screen = "SideDrawer.screen"
    /// The chat card's own coordinates, which a slide or shrink doesn't move; at rest they are the screen's. Frames the
    /// chat keeps for lining things up are measured here: measured on the screen, they changed every frame of a slide
    /// and redrew the whole chat screen each time.
    static let card = "SideDrawer.card"

    /// How far Panes has shrunk the chat for the drawer, 0 (home) to 1 (out), read off how much smaller a view in the
    /// chat now draws on the screen than it lays out. For `visualEffect`, which runs every frame of a slide without
    /// redrawing anything: handed down the environment, the same number changed every view in the chat each frame.
    static func shrink(of geo: GeometryProxy) -> CGFloat {
        guard geo.size.width > 0 else { return 0 }
        let scale = geo.frame(in: .named(screen)).width / geo.size.width
        return max(0, min(1, (1 - scale) / shrinkScale))
    }
}

extension EnvironmentValues {
    /// The screen's bars, for the chat screen to keep room for as padding: in the drawer it lays out with no safe area
    /// (see SideDrawer's `content`). nil outside the drawer.
    @Entry var drawerBars: EdgeInsets? = nil
}

/// Buttons on the chat screen, and anything there that scrolls sideways itself. A pull starting inside one of these is theirs; the drawer keeps its hands off.
struct DrawerSwipeGuardKey: PreferenceKey {
    static var defaultValue: [CGRect] = []
    static func reduce(value: inout [CGRect], nextValue: () -> [CGRect]) { value += nextValue() }
}

extension View {
    /// Mark a button on the chat screen (or something there that scrolls sideways) that a slide must not
    /// pull the drawer out from under.
    func drawerSwipeGuard() -> some View {
        modifier(DrawerSwipeGuard())
    }
}

/// The guarded view's frame on the chat card (SideDrawerMotion.card), where it holds still while the chat slides:
/// measured on the screen, every guard changed on every frame of a slide, and reporting them cost the frame.
private struct DrawerSwipeGuard: ViewModifier {
    @State private var frame = CGRect.zero

    func body(content: Content) -> some View {
        content
            .onGeometryChange(for: CGRect.self) { $0.frame(in: .named(SideDrawerMotion.card)) } action: { frame = $0 }
            .preference(key: DrawerSwipeGuardKey.self, value: [frame])
    }
}

/// ChatGPT-app style sidebar: the drawer waits, still, under the screen, which slides right to
/// reveal it, turning into a card with the display's rounded corners, a soft shadow and a light wash
/// (no grey dim). Swipe right anywhere on the screen to open, swipe left or tap the card to close.
struct SideDrawer<Drawer: View, Content: View>: View {
    @Binding var isOpen: Bool
    /// True from the moment the chat starts to slide until it comes to rest, home or out, for the chat's own handling
    /// of touches meanwhile: a tap the finger lifts on mid-slide was the swipe, and a transcript drag that became a
    /// pull lets the transcript go. Nothing the chat lays out or measures may depend on it: the frames it keeps are on
    /// the card (SideDrawerMotion.card), where a slide changes nothing, so the slide redraws none of it.
    @Binding var away: Bool
    /// Off while something on screen owns horizontal drags (the model slider), so a slide there
    /// does not also pull the drawer out.
    var swipeEnabled = true
    @ViewBuilder var drawer: Drawer
    @ViewBuilder var content: Content
    @Environment(\.theme) private var theme

    @State private var drag: CGFloat = 0
    @State private var dragAxis: Axis?
    /// Travel already spent before the gesture was recognised; subtracted so the panel starts moving
    /// from under the finger instead of snapping forward by the recogniser's threshold.
    @State private var dragSlack: CGFloat = 0
    /// Frames of the chat screen's buttons, on the card: a slide that starts on one never opens the drawer. Only checked
    /// with the drawer closed, when the card's coordinates are the screen's.
    @State private var contentGuards: [CGRect] = []
    /// Set on a gesture's first change when it started on a button; the rest of that gesture is ignored.
    @State private var dragChecked = false
    @State private var dragBlocked = false
    /// Bumped on every move, so a settle timer left over from an earlier one does nothing.
    @State private var moves = 0
    /// The home indicator's inset, kept for while the keyboard is up (see `body`).
    @State private var homeBar: CGFloat = 0
    /// A finger is on each gesture. These reset however a gesture finishes, including when the system cancels it
    /// (a scroll view or another recogniser taking the touch), which skips `onEnded` and left a half-done drag behind:
    /// the next slide blocked by a stale check, or the chat stuck part way out.
    @GestureState private var contentTouched = false
    @GestureState private var panelTouched = false

    /// The wash over the chat once the drawer is fully out. Dark mode's is measured off the ChatGPT app:
    /// black comes up to #131313 and white text goes down to about 68%.
    private static var systemWash: UIColor {
        UIColor { traits in
            traits.userInterfaceStyle == .dark
                ? UIColor(white: 0.19, alpha: 0.39)
                : UIColor.systemBackground.withAlphaComponent(0.45)
        }
    }

    /// A theme with its own background washes in that; the system theme keeps the measured one. Panes barely
    /// washes: the chat floats on the glow as a card and stays readable.
    private var wash: Color {
        if theme.backdrop { return theme.backgroundColor.opacity(0.18) }
        return switch theme.scheme {
        case .dark: Color(white: 0.19).opacity(0.39)
        case .light: theme.backgroundColor.opacity(0.45)
        default: Color(Self.systemWash)
        }
    }

    var body: some View {
        GeometryReader { geo in
            let width = min(geo.size.width * 0.72, 312)
            let offset = max(0, min(width, (isOpen ? width : 0) + drag))
            let progress = offset / width
            // The display's own curve from the first point of the swipe, so the border hugs it; at rest the
            // mask matches the glass exactly and changes nothing.
            let radius = DisplayCorner.radius
            let shrink = theme.backdrop ? progress : 0
            // The screen's bars. A bottom inset past 100 pt is the keyboard, not the home indicator: then the last
            // home indicator's.
            let keyboardUp = geo.safeAreaInsets.bottom > 100
            let bars = EdgeInsets(top: geo.safeAreaInsets.top, leading: geo.safeAreaInsets.leading,
                                  bottom: keyboardUp ? homeBar : geo.safeAreaInsets.bottom, trailing: geo.safeAreaInsets.trailing)
            // The card is the whole screen at rest and draws in to the space between the bars as Panes shrinks the
            // chat out, shrinking about that space's middle: out it is that space made 8% smaller.
            let pull = EdgeInsets(top: bars.top * shrink, leading: 0, bottom: bars.bottom * shrink, trailing: 0)
            let tall = geo.size.height + geo.safeAreaInsets.top + geo.safeAreaInsets.bottom
            let middle = tall > 0 ? (bars.top + (tall - bars.top - bars.bottom) / 2) / tall : 0.5
            ZStack(alignment: .leading) {
                drawer
                    .frame(width: width)
                    // It stays put under the chat and fades up as the chat slides off it, as the ChatGPT app's does.
                    .opacity(progress)
                    // Keep the drawer's own gestures out of the way until it is actually showing.
                    .allowsHitTesting(progress > 0.5)
                    // Nor is it there for VoiceOver (or a UI test) while it's away under the chat.
                    .accessibilityHidden(progress < 0.5)
                    // Swipe left on the panel itself to put it away, not just on the sliver of chat beside it.
                    .simultaneousGesture(dragGesture(width: width, closeOnly: true, touched: $panelTouched))
                content
                    .coordinateSpace(.named(SideDrawerMotion.card))
                    // The chat lays out over the whole screen with no safe area, keeping room for the bars as plain
                    // padding (drawerBars), the same out as home: the shrink only scales it and the card crops it. Under
                    // a shrink SwiftUI works the safe area out afresh from where the chat sits on screen, so with it the
                    // chat laid out inside the bars, couldn't draw under the clock or the home indicator, and its
                    // blurred messages there (with its buttons and the card's edges) snapped in as it landed. With the
                    // keyboard up the chat ends at the keyboard, so it keeps no room for the home indicator.
                    .environment(\.drawerBars, EdgeInsets(top: bars.top, leading: bars.leading,
                                                          bottom: keyboardUp ? 0 : bars.bottom, trailing: bars.trailing))
                    // No scrolling the transcript once the drawer is out or a sideways pull has started:
                    // a diagonal swipe used to drag the chat up while the drawer came in.
                    .scrollDisabled(progress > 0 || dragAxis == .horizontal)
                    // Pushed past the right edge, a scroll view picks up off-screen safe-area insets and draws
                    // its indicator in the middle of the screen; keep indicators off until it is back in place.
                    .scrollIndicators(progress > 0 ? .hidden : .automatic)
                    .overlay {
                        // A wash of the background colour, so the chat fades back rather than greying out; in dark
                        // mode it lifts the black to a dark grey card, as the ChatGPT app's does.
                        wash.opacity(progress)
                            .padding(pull)
                            .onTapGesture { close() }
                            .allowsHitTesting(progress > 0)
                    }
                    .mask { RoundedRectangle(cornerRadius: radius, style: .continuous).padding(pull) }
                    .overlay {
                        RoundedRectangle(cornerRadius: radius, style: .continuous)
                            // At full strength within the first few points of the swipe, not faded in with it.
                            .strokeBorder(Color.primary.opacity(0.18 * min(1, progress * 20)), lineWidth: 1)
                            .padding(pull)
                            .allowsHitTesting(false)
                    }
                    // The shadow is cast by a plain card behind the chat, not by the chat itself: shadowing the
                    // whole transcript meant re-rendering all of it offscreen on every frame of the slide.
                    .background {
                        RoundedRectangle(cornerRadius: radius, style: .continuous)
                            .fill(theme.backgroundColor)
                            .shadow(color: .black.opacity((theme.backdrop ? 0.24 : 0.14) * progress), radius: theme.backdrop ? 26 : 18)
                            .padding(pull)
                            .allowsHitTesting(false)
                    }
                    // Panes: the chat shrinks a little as it slides, so it floats on the glow with room above and below.
                    .scaleEffect(1 - SideDrawerMotion.shrinkScale * shrink, anchor: UnitPoint(x: 0, y: middle))
                    // The safe area stops here, outside the shrink, where it holds still (see `content`).
                    .ignoresSafeArea(.container)
                    .offset(x: offset)
                    // When the drawer swipe is disabled (notably over a pushed project chat), don't leave
                    // its recogniser attached at all: it can compete with NavigationStack's edge-back swipe
                    // even though dragGesture ignores the updates.
                    .simultaneousGesture(dragGesture(width: width, touched: $contentTouched), including: swipeEnabled || isOpen ? .all : .none)
                    .onPreferenceChange(DrawerSwipeGuardKey.self) { contentGuards = $0 }
            }
            .coordinateSpace(.named(SideDrawerMotion.screen))
            .background {
                if theme.backdrop { PanesBackdrop() } else { theme.backgroundColor.ignoresSafeArea() }
            }
            #if DEBUG
            // The performance hook's scripted finger (PerfDrive).
            .onReceive(NotificationCenter.default.publisher(for: PerfDrive.drag)) { note in
                if let moved = note.object as? CGFloat {
                    if dragAxis == nil { leave() }
                    dragAxis = .horizontal
                    drag = moved
                } else {
                    dragAxis = nil
                    drag = 0
                    settle()
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: PerfDrive.snap)) { note in
                if let open = note.object as? Bool { withAnimation(SideDrawerMotion.animation) { isOpen = open } }
            }
            #endif
            .onChange(of: geo.safeAreaInsets.bottom, initial: true) { _, bottom in
                if bottom <= 100 { homeBar = bottom }
            }
            .onChange(of: contentTouched || panelTouched) { _, touched in
                if !touched { dragCancelled() }
            }
            // The tap lands as the snap starts, not when it settles. Off `isOpen` itself: a counter bumped for it made
            // a second pass over this body (and the drawer) per snap.
            .sensoryFeedback(.impact(weight: .medium), trigger: isOpen)
            .onChange(of: isOpen) { _, open in
                leave()
                settle()
            }
        }
    }

    /// The chat is on the move (a slide has begun, or the drawer is opening or closing).
    private func leave() {
        moves += 1
        if !away { away = true }
    }

    /// Once the snap has run its course, and only if nothing has moved it since, the chat is at rest again.
    private func settle() {
        moves += 1
        let move = moves
        DispatchQueue.main.asyncAfter(deadline: .now() + SideDrawerMotion.duration + 0.05) {
            if moves == move && drag == 0 && dragAxis == nil { away = false }
        }
    }

    /// `closeOnly` is the copy living on the panel: it only pulls the panel away leftwards.
    private func dragGesture(width: CGFloat, closeOnly: Bool = false, touched: GestureState<Bool>) -> some Gesture {
        DragGesture(minimumDistance: 12, coordinateSpace: .global)
            .updating(touched) { _, on, _ in on = true }
            .onChanged { value in
                guard swipeEnabled || dragAxis != nil else { return }
                if !closeOnly {
                    // A press on a button (SwiftUI ones mark themselves; UIKit ones, the top bar's, are controls)
                    // stays a press however the finger slides, and one on a reply's selection drags its handle.
                    if !dragChecked {
                        dragChecked = true
                        dragBlocked = !isOpen && (contentGuards.contains { $0.contains(value.startLocation) } || startsOnControl(value.startLocation)
                                                  || ReplySelection.holds(value.startLocation) || startsOnEditedText(value.startLocation))
                    }
                    if dragBlocked { return }
                }
                if dragAxis == nil {
                    let t = value.translation
                    let sideways = abs(t.width) > abs(t.height)
                    if closeOnly && !(sideways && t.width < 0) { return }
                    dragAxis = sideways ? .horizontal : .vertical
                    dragSlack = t.width
                    if dragAxis == .horizontal {
                        dismissKeyboard()
                        leave()
                    }
                }
                guard dragAxis == .horizontal else { return }
                let moved = value.translation.width - dragSlack
                drag = closeOnly ? min(0, moved) : moved
            }
            .onEnded { value in
                if !closeOnly {
                    dragChecked = false
                    dragBlocked = false
                }
                if dragAxis == .horizontal {
                    let end = (isOpen ? width : 0) + value.predictedEndTranslation.width - dragSlack
                    let open = end > width / 2
                    let current = max(0, min(width, (isOpen ? width : 0) + drag))
                    let remaining = (open ? width : 0) - current
                    let velocity = abs(remaining) > 1 ? value.velocity.width / remaining : 0
                    withAnimation(SideDrawerMotion.release(velocity: velocity)) {
                        isOpen = open
                        dragAxis = nil
                        drag = 0
                        dragSlack = 0
                    }
                    settle()
                    return
                }
                dragAxis = nil
                drag = 0
                dragSlack = 0
            }
    }

    /// A gesture gone without `onEnded` (cancelled): clear what it left and settle the chat where it was headed
    /// before. After a normal end `onEnded` has already cleared it all and this does nothing.
    private func dragCancelled() {
        dragChecked = false
        dragBlocked = false
        guard dragAxis != nil || drag != 0 || dragSlack != 0 else { return }
        let moved = dragAxis == .horizontal
        withAnimation(SideDrawerMotion.animation) {
            dragAxis = nil
            drag = 0
            dragSlack = 0
        }
        if moved { settle() }
    }

    private func close() {
        withAnimation(SideDrawerMotion.animation) { isOpen = false }
    }

    private func hitView(at point: CGPoint) -> UIView? {
        let window = UIApplication.shared.connectedScenes.compactMap { ($0 as? UIWindowScene)?.keyWindow }.first
        return window?.hitTest(point, with: nil)
    }

    private func startsOnControl(_ point: CGPoint) -> Bool {
        var view = hitView(at: point)
        while let v = view {
            if v is UIControl { return true }
            view = v.superview
        }
        return false
    }

    /// A press on text being typed (the composer, with the keyboard up), or on one of its selection handles, moves
    /// the cursor or the selection, so it is never a pull on the drawer. Asked of UIKit as the finger lands rather
    /// than of frames the chat reported earlier: those can lag behind the keyboard coming up. With text selected,
    /// the band round the field counts too, where the handles' knobs hang over its edge.
    private func startsOnEditedText(_ point: CGPoint) -> Bool {
        var view = hitView(at: point)
        while let v = view {
            // Editable only: a reply's selectable text stays first responder after a selection, and counting it
            // blocked every pull that started on a reply (ReplySelection.holds covers its live selection).
            if let text = v as? UITextView, text.isEditable, text.isFirstResponder { return true }
            view = v.superview
        }
        guard let window = UIApplication.shared.connectedScenes.compactMap({ ($0 as? UIWindowScene)?.keyWindow }).first,
              let editing = Self.firstResponder(in: window) as? UITextView, editing.isEditable,
              editing.selectedRange.length > 0 else { return false }
        return editing.convert(editing.bounds, to: nil).insetBy(dx: -8, dy: -44).contains(point)
    }

    private static func firstResponder(in view: UIView) -> UIView? {
        if view.isFirstResponder { return view }
        for sub in view.subviews {
            if let hit = firstResponder(in: sub) { return hit }
        }
        return nil
    }

    private func dismissKeyboard() {
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
    }
}
