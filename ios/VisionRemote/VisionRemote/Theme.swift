import SwiftUI
import UIKit

/// The app's look, chosen in Settings › Appearance and kept in UserDefaults under `theme`. Every screen
/// reads the tokens here instead of naming a colour or a material itself, so a theme is a value, not a
/// branch: the same UX, worn differently. `system` is the look the app shipped with, so a phone that has
/// never opened Appearance changes nothing.
enum ThemeID: String, CaseIterable, Identifiable {
    case panes, system, hearth, paper, hairline, graphite
    var id: String { rawValue }
    static let key = "theme"

    /// What a phone wears before Appearance is opened. A fresh install gets Panes, the look the desktop app has;
    /// a phone that already paired with a computer (an update from before Panes) keeps System, the look it had.
    static func settle() {
        let defaults = UserDefaults.standard
        guard defaults.string(forKey: key) == nil else { return }
        let updated = !(defaults.string(forKey: "serverURL") ?? "").isEmpty
        defaults.set((updated ? ThemeID.system : .panes).rawValue, forKey: key)
    }
}

/// A typeface for a run of text: the system face, its serif (New York) or monospaced designs, or Hearth (`Hearth.swift`).
enum ThemeFace {
    case system, serif, mono, hearth
}

/// What the composer card, the attach menu and the jump arrow sit on.
enum ThemeSurface {
    /// Liquid Glass (iOS 26), the regular material before it: today's look.
    case glass
    /// A flat card with a hairline round it.
    case card(fill: Color, stroke: Color?)
    /// Panes: a card edged and glowing in the accent.
    case glow(fill: Color, accent: Color)
}

/// The live-voice button, and anything else that wears the accent as a round button.
enum ThemeAccentButton {
    /// The accent at 15% behind an accent glyph, as today.
    case tinted
    /// A solid accent disc with a white glyph.
    case solid
    /// A hairline ring in the text colour.
    case outlined
}

struct Theme {
    let id: ThemeID
    let name: String
    /// What it is, not how it was made: VoiceOver's hint on the swatch (no longer shown under the picker).
    let blurb: String
    /// Forced light or dark; nil follows the phone.
    let scheme: ColorScheme?

    let prose: ThemeFace
    /// Subheadline and below: the fold lines, clocks, pills and footers. Hairline sets these in mono.
    let label: ThemeFace

    /// nil: the system background, which follows light and dark.
    let background: Color?
    /// Cards inside the chat (the diff under an edit): nil is the system's secondary background.
    let surface: Color?
    /// nil: the app's accent colour from the asset catalogue (system blue).
    let accent: Color?

    let bubbleFill: Color?          // nil: secondarySystemFill
    let bubbleStroke: Color?
    let bubbleRadius: CGFloat

    let composer: ThemeSurface
    /// nil keeps the radius each caller asks for (30 on the composer, 18 on the jump arrow).
    let composerRadius: CGFloat?


    /// The chats and terminal buttons over the transcript: today they take the tint; the skins draw
    /// them in the text colour. (The toolbar's own glass disc stays either way.)
    let chromeMonochrome: Bool

    let voiceButton: ThemeAccentButton

    /// Panes: the drawer and the chat's top sit on a soft violet glow (`PanesBackdrop`), and the chat floats on
    /// it as a card while the drawer is out.
    var backdrop = false
    /// The user's own bubble as a gradient (nil: `bubbleColor`), with `userInk` for its text.
    var userBubble: [Color]? = nil
    var userInk: Color? = nil

    // MARK: the six

    /// Panes, the desktop app's look: a violet glow behind, panes on it, violet where you act; light and dark.
    static let panes = Theme(
        id: .panes, name: "Panes", blurb: "Panes on a soft violet glow, as on the desktop. Follows light and dark.",
        scheme: nil, prose: .system, label: .system,
        background: .adaptive(light: "#FFFFFF", dark: "#17151F"), surface: .adaptive(light: "#FAF9FE", dark: "#252232"),
        accent: .adaptive(light: "#7C5CFF", dark: "#8B6CFF"),
        bubbleFill: nil, bubbleStroke: nil, bubbleRadius: 20,
        composer: .glow(fill: .adaptive(light: "#FAF9FE", dark: "#252232"), accent: .adaptive(light: "#7C5CFF", dark: "#8B6CFF")),
        composerRadius: 26,
        chromeMonochrome: false,
        voiceButton: .solid,
        backdrop: true,
        userBubble: [.adaptive(light: "#7C5CFF", dark: "#3B3070"), .adaptive(light: "#5B8CFF", dark: "#2A3A6C")],
        userInk: .white)

    static let system = Theme(
        id: .system, name: "System", blurb: "The app as it ships. Follows light and dark.",
        scheme: nil, prose: .system, label: .system,
        background: nil, surface: nil, accent: nil,
        bubbleFill: nil, bubbleStroke: nil, bubbleRadius: 20,
        composer: .glass, composerRadius: nil,
        chromeMonochrome: false,
        voiceButton: .tinted)

    /// System's shapes, face and glass, in warm ink with a copper accent: only the colours change.
    static let hearth = Theme(
        id: .hearth, name: "Hearth", blurb: "The app as it ships, in warm ink and copper. Dark.",
        scheme: .dark, prose: .system, label: .system,
        background: Color(hex: "#16130F"), surface: Color(hex: "#1F1A16"), accent: Color(hex: "#D97857"),
        bubbleFill: Color(hex: "#2B2521"), bubbleStroke: nil, bubbleRadius: 20,
        composer: .glass, composerRadius: nil,
        chromeMonochrome: false,
        voiceButton: .tinted)

    /// A book page: New York for what you read, the system face for the labels round it, one oxblood mark.
    static let paper = Theme(
        id: .paper, name: "Paper", blurb: "Serif text on warm stock, one oxblood accent. Light.",
        scheme: .light, prose: .serif, label: .system,
        background: Color(hex: "#F3EFE6"), surface: Color(hex: "#FBF9F4"), accent: Color(hex: "#9B2C1F"),
        bubbleFill: Color(hex: "#E6E0D3"), bubbleStroke: nil, bubbleRadius: 8,
        composer: .card(fill: Color(hex: "#FBF9F4")!, stroke: Color(hex: "#DCD5C6")), composerRadius: 14,
        chromeMonochrome: true,
        voiceButton: .solid)

    static let hairline = Theme(
        id: .hairline, name: "Hairline", blurb: "Ink rules on off-white, labels in mono. Light.",
        scheme: .light, prose: .system, label: .mono,
        background: Color(hex: "#FAFAF8"), surface: .white, accent: Color(hex: "#141414"),
        bubbleFill: .clear, bubbleStroke: Color(hex: "#141414"), bubbleRadius: 6,
        composer: .card(fill: .white, stroke: Color(hex: "#141414")), composerRadius: 10,
        chromeMonochrome: true,
        voiceButton: .outlined)

    /// An instrument panel: flat graphite planes, no glow, mono labels, and signal orange only where you act.
    static let graphite = Theme(
        id: .graphite, name: "Graphite", blurb: "Flat graphite, mono labels, signal orange. Dark.",
        scheme: .dark, prose: .system, label: .mono,
        background: Color(hex: "#111213"), surface: Color(hex: "#1A1B1D"), accent: Color(hex: "#FF6A2B"),
        bubbleFill: Color(hex: "#232427"), bubbleStroke: nil, bubbleRadius: 10,
        composer: .card(fill: Color(hex: "#18191B")!, stroke: Color(hex: "#2C2D30")), composerRadius: 16,
        chromeMonochrome: true,
        voiceButton: .solid)

    static let all: [Theme] = [.panes, .system, .hearth, .paper, .hairline, .graphite]

    static func named(_ raw: String) -> Theme {
        all.first { $0.id.rawValue == raw } ?? .system
    }

    /// Light or dark goes on the windows, not through preferredColorScheme: that reached the app and an open
    /// sheet on separate passes, so on a pick in Settings the sheet and the chat behind it turned a beat
    /// apart. On the window both take it in the same frame, and nil (unspecified) hands back to the phone.
    func applyScheme() {
        let style: UIUserInterfaceStyle = switch scheme {
        case .dark: .dark
        case .light: .light
        default: .unspecified
        }
        for case let scene as UIWindowScene in UIApplication.shared.connectedScenes {
            for window in scene.windows { window.overrideUserInterfaceStyle = style }
        }
    }

    // MARK: derived

    /// The chat's background as a view: the theme's colour, or the system's.
    var backgroundColor: Color { background ?? Color(.systemBackground) }
    var surfaceColor: Color { surface ?? Color(.secondarySystemBackground) }
    var bubbleColor: Color { bubbleFill ?? Color(.secondarySystemFill) }

    /// Styles at subheadline and under are labels; the rest is prose.
    private func face(for style: Font.TextStyle) -> ThemeFace {
        switch style {
        case .subheadline, .footnote, .caption, .caption2: label
        default: prose
        }
    }

    private func face(forSize size: CGFloat) -> ThemeFace { size <= 15 ? label : prose }

    /// A text style in the theme's face, scaling with Dynamic Type. Headlines default to semibold like SF.
    func font(_ style: Font.TextStyle, weight: Font.Weight? = nil) -> Font {
        let weight = weight ?? (style == .headline ? .semibold : .regular)
        switch face(for: style) {
        case .system: return .system(style, weight: weight)
        case .serif: return .system(style, design: .serif, weight: weight)
        case .mono: return .system(style, design: .monospaced, weight: weight)
        case .hearth: return .custom(Hearth.name(weight), size: Hearth.size(style), relativeTo: style)
        }
    }

    /// A fixed size in the theme's face.
    func font(size: CGFloat, weight: Font.Weight = .regular) -> Font {
        switch face(forSize: size) {
        case .system: return .system(size: size, weight: weight)
        case .serif: return .system(size: size, weight: weight, design: .serif)
        case .mono: return .system(size: size, weight: weight, design: .monospaced)
        case .hearth: return .custom(Hearth.name(weight), fixedSize: size)
        }
    }

    /// For UIKit text (the composer's UITextView), scaled like `.body`.
    func uiFont(size: CGFloat, weight: UIFont.Weight = .regular) -> UIFont {
        let base: UIFont
        switch face(forSize: size) {
        case .system: base = .systemFont(ofSize: size, weight: weight)
        case .serif: base = .systemFont(ofSize: size, weight: weight).withDesign(.serif)
        case .mono: base = .monospacedSystemFont(ofSize: size, weight: weight)
        case .hearth: base = UIFont(name: Hearth.name(weight), size: size) ?? .systemFont(ofSize: size, weight: weight)
        }
        return UIFontMetrics(forTextStyle: .body).scaledFont(for: base)
    }
}

extension Theme {
    /// A text style in the theme's face for UIKit text (a reply's selectable prose): `font(_:weight:)`'s
    /// twin, at the size Dynamic Type gives that style now.
    func uiFont(_ style: UIFont.TextStyle, weight: UIFont.Weight? = nil) -> UIFont {
        let weight = weight ?? (style == .headline ? .semibold : .regular)
        let size = UIFont.preferredFont(forTextStyle: style).pointSize
        let labels: [UIFont.TextStyle] = [.subheadline, .footnote, .caption1, .caption2]
        switch labels.contains(style) ? label : prose {
        case .system: return .systemFont(ofSize: size, weight: weight)
        case .serif: return UIFont.systemFont(ofSize: size, weight: weight).withDesign(.serif)
        case .mono: return .monospacedSystemFont(ofSize: size, weight: weight)
        case .hearth:
            let unscaled = UIFont.preferredFont(forTextStyle: style, compatibleWith: UITraitCollection(preferredContentSizeCategory: .large)).pointSize
            guard let face = UIFont(name: Hearth.name(weight), size: unscaled) else { return .systemFont(ofSize: size, weight: weight) }
            return UIFontMetrics(forTextStyle: style).scaledFont(for: face)
        }
    }
}

private extension UIFont {
    func withDesign(_ design: UIFontDescriptor.SystemDesign) -> UIFont {
        guard let descriptor = fontDescriptor.withDesign(design) else { return self }
        return UIFont(descriptor: descriptor, size: pointSize)
    }
}

// MARK: environment

private struct ThemeKey: EnvironmentKey {
    static let defaultValue = Theme.system
}

extension EnvironmentValues {
    var theme: Theme {
        get { self[ThemeKey.self] }
        set { self[ThemeKey.self] = newValue }
    }
}

/// `.themeFont(.body)`: the stand-in for `.font(...)` everywhere in the app, so a theme change restyles
/// every label in place (a `Font` can't read the environment; this modifier can).
private struct ThemeFont: ViewModifier {
    var style: Font.TextStyle?
    var size: CGFloat?
    var weight: Font.Weight?
    var digits = false
    @Environment(\.theme) private var theme

    func body(content: Content) -> some View {
        let font = style.map { theme.font($0, weight: weight) } ?? theme.font(size: size ?? 17, weight: weight ?? .regular)
        content.font(digits ? font.monospacedDigit() : font)
    }
}

extension View {
    func themeFont(_ style: Font.TextStyle, weight: Font.Weight? = nil, digits: Bool = false) -> some View {
        modifier(ThemeFont(style: style, weight: weight, digits: digits))
    }

    func themeFont(size: CGFloat, weight: Font.Weight = .regular) -> some View {
        modifier(ThemeFont(size: size, weight: weight))
    }

    /// The composer's surface, the attach menu's and the jump arrow's: glass today, a flat card under the
    /// other themes.
    func themeSurface(_ surface: ThemeSurface, radius: CGFloat, interactive: Bool = false) -> some View {
        modifier(ThemeSurfaceModifier(surface: surface, radius: radius, interactive: interactive))
    }
}

private struct ThemeSurfaceModifier: ViewModifier {
    let surface: ThemeSurface
    let radius: CGFloat
    let interactive: Bool

    func body(content: Content) -> some View {
        let shape = RoundedRectangle(cornerRadius: radius, style: .continuous)
        switch surface {
        case .glass:
            if #available(iOS 26, *) {
                content.glassEffect(.regular.interactive(interactive), in: shape)
            } else {
                content
                    .background(.regularMaterial, in: shape)
                    .overlay(shape.strokeBorder(Color(.separator).opacity(0.5), lineWidth: 0.5))
            }
        case .card(let fill, let stroke):
            content
                .background(fill, in: shape)
                .overlay { if let stroke { shape.strokeBorder(stroke, lineWidth: 1) } }
        case .glow(let fill, let accent):
            content
                .background {
                    shape.fill(fill)
                        .shadow(color: accent.opacity(0.22), radius: 14)
                        .shadow(color: .black.opacity(0.10), radius: 18, y: 8)
                }
                .overlay { shape.strokeBorder(accent.opacity(0.4), lineWidth: 1) }
        }
    }
}


// MARK: Panes

extension Color {
    /// A colour that follows light and dark (Panes is one theme for both).
    static func adaptive(light: String, dark: String) -> Color {
        let l = UIColor(Color(hex: light) ?? .white), d = UIColor(Color(hex: dark) ?? .black)
        return Color(UIColor { $0.userInterfaceStyle == .dark ? d : l })
    }
}

/// What Panes sits on: lavender to blue in light, deep violet to navy in dark (the desktop's --backdrop).
struct PanesBackdrop: View {
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        let dark = scheme == .dark
        GeometryReader { geo in
            let w = geo.size.width, h = geo.size.height
            ZStack {
                (dark ? Color(hex: "#0E0C15")! : Color(hex: "#EEECF6")!)
                RadialGradient(colors: [dark ? Color(hex: "#3B2A75")! : Color(hex: "#D9CFFF")!, .clear],
                               center: UnitPoint(x: -0.1, y: -0.05), startRadius: 0, endRadius: max(w, h) * 0.75)
                RadialGradient(colors: [dark ? Color(hex: "#1B3A73")! : Color(hex: "#CFE0FF")!, .clear],
                               center: UnitPoint(x: 1.1, y: 1.05), startRadius: 0, endRadius: max(w, h) * 0.7)
            }
        }
        .ignoresSafeArea()
    }
}

/// Panes' status dot: a small disc in its colour, glowing.
struct PanesDot: View {
    let color: Color
    var size: CGFloat = 8

    var body: some View {
        Circle().fill(color).frame(width: size, height: size).shadow(color: color.opacity(0.9), radius: 4)
    }

    /// A thread's colour under Panes: amber when it needs you, the accent while it works, green when it's
    /// ready to look at; nil (the plain mark) for the rest.
    static func tone(_ state: String, queued: Bool) -> Color? {
        switch state {
        case "waiting_on_you": .adaptive(light: "#C88400", dark: "#FFC46B")
        case "ready_for_review": .adaptive(light: "#1FA86A", dark: "#5BE3A0")
        case "working" where !queued, "landing": .adaptive(light: "#7C5CFF", dark: "#8B6CFF")
        default: nil
        }
    }
}
