import AVKit
import Combine
import SwiftUI
import UIKit

/// A photo (or video) picked for full-screen viewing; `Identifiable` so it can drive `fullScreenCover(item:)`.
/// `images` are the photos to swipe through, opening on `start`; with `video` set the cover plays it instead.
struct PreviewImage: Identifiable {
    let id = UUID()
    let images: [UIImage]
    var start = 0
    var video: AVAsset? = nil
    /// Where each photo sits on the machine, and how to fetch it: `images` may be small thumbnails, so each
    /// page swaps in the full copy once it arrives.
    var paths: [String] = []
    var fetch: ((String) async -> UIImage?)? = nil
    /// A swipe-through page that's a clip: its still is in `images`, the clip itself here.
    var clips: [AVAsset?] = []
    /// Where each page sits in the chat (`matchedTransitionSource` ids in `zoom`): the viewer grows out of
    /// the tapped thumbnail and a pull down shrinks it back into whichever one is showing.
    var sources: [String] = []
    var zoom: Namespace.ID? = nil

    init(image: UIImage, video: AVAsset? = nil) {
        images = [image]
        self.video = video
    }

    init(images: [UIImage], start: Int, paths: [String] = [], clips: [AVAsset?] = [],
         fetch: ((String) async -> UIImage?)? = nil) {
        self.images = images
        self.start = min(max(start, 0), max(images.count - 1, 0))
        self.paths = paths
        self.clips = clips
        self.fetch = fetch
    }
}

/// Full-screen photo viewer: swipe left and right through the photos and clips, pinch to zoom, double-tap to toggle,
/// drag down to dismiss, tap to hide the buttons.
struct ImagePreview: View {
    let images: [UIImage]
    var paths: [String] = []
    var fetch: ((String) async -> UIImage?)? = nil
    var clips: [AVAsset?] = []
    var sources: [String] = []
    var zoom: Namespace.ID? = nil
    private let start: Int
    @Environment(\.dismiss) private var dismiss
    @State private var current: Int?
    @State private var zoomed = false
    @State private var scrubbing = false
    @State private var playTap = 0   // a tap on the play button while the pager was still settling
    @State private var chromeHidden = false
    @State private var dragToDismiss: CGFloat = 0
    @State private var shown = false   // fades in on open and out on close, Photos style, instead of a sheet sliding
    @State private var insets = EdgeInsets()   // the screen's safe area; the pager ignores it, a clip's controls mustn't

    init(images: [UIImage], start: Int, paths: [String] = [], clips: [AVAsset?] = [],
         fetch: ((String) async -> UIImage?)? = nil, sources: [String] = [], zoom: Namespace.ID? = nil) {
        self.images = images
        self.paths = paths
        self.clips = clips
        self.fetch = fetch
        self.sources = sources
        self.zoom = zoom
        self.start = start
        _current = State(initialValue: start)
        _shown = State(initialValue: zoom != nil)   // the zoom brings it in; no fade on top
    }

    /// The thumbnail the showing page came from, for the zoom back on close.
    private var source: String? {
        let i = current ?? start
        return sources.indices.contains(i) ? sources[i] : nil
    }

    var body: some View {
        ZStack {
            Color.black.opacity(Double(1 - min(0.6, abs(dragToDismiss) / 400))).ignoresSafeArea()
                .onGeometryChange(for: EdgeInsets.self) { $0.safeAreaInsets } action: { insets = $0 }
            ScrollView(.horizontal, showsIndicators: false) {
                LazyHStack(spacing: 12) {
                    ForEach(images.indices, id: \.self) { i in
                        Group {
                            if clips.indices.contains(i), let clip = clips[i] {
                                // Every clip waits for its play button, the one tapped open too: nothing plays unasked.
                                PreviewVideoPage(still: images[i], asset: clip, insets: insets,
                                                 controlsHidden: chromeHidden, scrubbing: $scrubbing, playTap: playTap,
                                                 isCurrent: i == current, onTap: toggleChrome)
                            } else {
                                PreviewPhotoPage(image: images[i], path: paths.indices.contains(i) ? paths[i] : nil, fetch: fetch,
                                                 isCurrent: i == current, zoomed: $zoomed, onTap: toggleChrome)
                            }
                        }
                        .containerRelativeFrame([.horizontal, .vertical])
                    }
                }
                .scrollTargetLayout()
                .background(PagerTouches { playTap += 1 })
            }
            .scrollTargetBehavior(.viewAligned(limitBehavior: .alwaysByOne))
            .scrollPosition(id: $current)
            // Held still during a pull down, or a slightly diagonal pull drags the next page into view.
            .scrollDisabled(zoomed || scrubbing || images.count < 2 || dragToDismiss != 0)
            .ignoresSafeArea()
            .scaleEffect(1 - min(0.25, abs(dragToDismiss) / 1200))
            .offset(y: dragToDismiss)
            // With the zoom transition the system's own pull down shrinks the page back into its thumbnail.
            .gesture(DismissPan(enabled: !zoomed && !scrubbing && zoom == nil) { y in
                dragToDismiss = y
            } ended: { y, velocity in
                if abs(y) > 120 || abs(velocity) > 800 {
                    close(towards: y + velocity * 0.1)
                } else {
                    withAnimation(.spring(duration: 0.3)) { dragToDismiss = 0 }
                }
            })
        }
        .overlay(alignment: .top) { ViewerScrim(edge: .top).opacity(chromeHidden || dragToDismiss != 0 ? 0 : 1) }
        .overlay(alignment: .top) { chrome }
        .opacity(shown ? 1 : 0)
        .statusBarHidden()   // shown with the buttons, it blurred in and out on every tap
        .onAppear { withAnimation(.easeOut(duration: 0.2)) { shown = true } }
        .modifier(ZoomFrom(source: source, zoom: zoom))
    }

    /// Fade out (carrying on the way it was pulled), then drop the cover; the system's own slide-down
    /// then moves nothing visible, where before it dragged the dimmed backdrop slowly off the screen.
    private func close(towards direction: CGFloat = 0) {
        if zoom != nil { dismiss(); return }   // zooms back into the thumbnail
        withAnimation(.easeOut(duration: 0.2)) {
            shown = false
            if direction != 0 { dragToDismiss += direction < 0 ? -160 : 160 }
        }
        Task {
            try? await Task.sleep(for: .milliseconds(200))
            dismiss()
        }
    }

    private var chrome: some View {
        ZStack {
            if images.count > 1 {
                Text("\((current ?? 0) + 1) of \(images.count)")
                    .themeFont(.subheadline, weight: .semibold)
                    .foregroundStyle(.white)
                    .monospacedDigit()
                    .padding(.horizontal, 14)
                    .frame(height: 36)
                    .viewerBacking(in: Capsule())
            }
            HStack {
                PreviewGlassButton(systemImage: "xmark", label: "Close") { close() }
                Spacer()
            }
        }
        .padding(.horizontal, 16)
        .padding(.top, 8)
        .opacity(chromeHidden || dragToDismiss != 0 ? 0 : 1)
        .allowsHitTesting(!chromeHidden)
    }

    /// The tap doesn't wait to see if it's a double-tap, so the buttons go at once.
    private func toggleChrome() {
        withAnimation(.easeOut(duration: 0.15)) { chromeHidden.toggle() }
    }
}

/// The zoom transition from a chat thumbnail, when there is one to zoom from.
private struct ZoomFrom: ViewModifier {
    let source: String?
    let zoom: Namespace.ID?

    func body(content: Content) -> some View {
        if let source, let zoom {
            content.navigationTransition(.zoom(sourceID: source, in: zoom))
        } else {
            content
        }
    }
}

/// One photo in the viewer. Pinch or double-tap to zoom; while zoomed a drag pans it and the pager stops paging.
private struct PreviewPhotoPage: View {
    let image: UIImage
    let path: String?
    let fetch: ((String) async -> UIImage?)?
    let isCurrent: Bool
    @Binding var zoomed: Bool
    let onTap: () -> Void
    @State private var scale: CGFloat = 1
    @State private var lastScale: CGFloat = 1
    @State private var offset: CGSize = .zero
    @State private var lastOffset: CGSize = .zero
    @State private var full: UIImage?

    var body: some View {
        Image(uiImage: full ?? image)
            .resizable()
            .scaledToFit()
            .scaleEffect(scale)
            .offset(offset)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .contentShape(Rectangle())  // pull down from the black bars too, not just the photo
            .gesture(magnify)
            .simultaneousGesture(pan, including: scale > 1 ? .all : .subviews)
            .onTapGesture(count: 2) { toggleZoom() }
            .simultaneousGesture(TapGesture().onEnded { onTap() })
            .onChange(of: scale) { if isCurrent { zoomed = scale > 1 } }
            .onChange(of: isCurrent) { if isCurrent { zoomed = scale > 1 } else { reset() } }
            .task(id: path) {
                guard let path, let fetch, full == nil else { return }
                full = await fetch(path)
            }
            .accessibilityLabel("Photo")
    }

    private var magnify: some Gesture {
        MagnifyGesture()
            .onChanged { value in
                scale = max(1, min(5, lastScale * value.magnification))
            }
            .onEnded { _ in
                lastScale = scale
                if scale <= 1.02 { withAnimation(.spring(duration: 0.3)) { reset() } }
            }
    }

    private var pan: some Gesture {
        DragGesture(minimumDistance: 10)
            .onChanged { value in
                offset = CGSize(width: lastOffset.width + value.translation.width,
                                height: lastOffset.height + value.translation.height)
            }
            .onEnded { _ in lastOffset = offset }
    }

    private func toggleZoom() {
        withAnimation(.spring(duration: 0.3)) {
            if scale > 1 { reset() } else { scale = 2.5; lastScale = 2.5 }
        }
    }

    private func reset() {
        scale = 1; lastScale = 1
        offset = .zero; lastOffset = .zero
    }
}

/// One clip in the viewer: its first frame with a play button until it's played; it pauses when swiped away.
private struct PreviewVideoPage: View {
    let still: UIImage
    let asset: AVAsset
    let insets: EdgeInsets
    let controlsHidden: Bool
    @Binding var scrubbing: Bool
    let playTap: Int
    let isCurrent: Bool
    let onTap: () -> Void

    var body: some View {
        ClipPage(still: still, asset: { asset }, insets: insets, controlsHidden: controlsHidden,
                 scrubbing: $scrubbing, playTap: playTap, isCurrent: isCurrent, onTap: onTap)
    }
}

/// A clip page for the viewers, with its own controls rather than the system player's (whose scrubber
/// buzzes at each end and can't be told not to): the big play/pause button in the middle, and a scrubber
/// with the times on dark glass along the bottom, hidden with the rest of the viewer's buttons. The still stays on top
/// until the first frame is on screen, so the player's black first moments never show; swiped away and
/// back, the big play button returns to resume it.
struct ClipPage: View {
    let still: UIImage?
    let asset: () -> AVAsset?
    let insets: EdgeInsets
    var controlsHidden = false
    var extraBottom: CGFloat = 0   // room for anything the viewer lays along the bottom (the Library's caption)
    @Binding var scrubbing: Bool   // the pager holds still while it's true
    var playTap = 0                // bumped by the pager for a play tap it caught mid-settle
    let isCurrent: Bool
    let onTap: () -> Void
    @State private var player: AVPlayer?
    @State private var ready = false
    @State private var playing = false
    @State private var parked = false
    @State private var ended = false
    @State private var time: Double = 0
    @State private var duration: Double = 0
    @State private var resumeAfterScrub = false
    @State private var playTapAt: Date?
    @State private var failure: String?

    var body: some View {
        ZStack {
            if let player {
                PlayerSurface(player: player) { withAnimation(.easeOut(duration: 0.15)) { ready = true } }
            }
            if !ready, let still {
                Image(uiImage: still).resizable().scaledToFit().transition(.opacity)
            }
            Color.clear
                .contentShape(Rectangle())
                .onTapGesture { onTap() }
            if let failure {
                // The still with a play button that does nothing would say nothing; say what went wrong.
                VStack(spacing: 10) {
                    Image(systemName: "exclamationmark.triangle").themeFont(.largeTitle)
                    Text("Couldn't play this video").themeFont(.headline)
                    Text(failure).themeFont(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                }
                .foregroundStyle(.white)
                .padding(24)
                .viewerBacking(in: RoundedRectangle(cornerRadius: 20))
                .padding(32)
                .transition(.opacity)
            } else if !scrubbing && (player == nil || !controlsHidden) {
                // The one play/pause control: comes and goes with the scrubber (before the first play, when
                // there's no scrubber yet, it stays).
                PlayButton(systemImage: playing ? "pause.fill" : "play.fill") { playing ? pause() : start() }
                    .transition(.opacity)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .overlay(alignment: .bottom) {
            if player != nil && failure == nil {
                ZStack(alignment: .bottom) {
                    ViewerScrim(edge: .bottom, height: insets.bottom + extraBottom + 120)
                    controls
                        .padding(.horizontal, 16)
                        .padding(.bottom, insets.bottom + 8 + extraBottom)
                }
                .opacity(controlsHidden ? 0 : 1)
                .allowsHitTesting(!controlsHidden)
            }
        }
        .onChange(of: isCurrent) {
            if !isCurrent, let player, !ended { player.pause(); parked = true }
            // The glide the tap stopped may have landed here a moment after it.
            if isCurrent, let at = playTapAt, Date().timeIntervalSince(at) < 0.6 { playTapAt = nil; startIfIdle() }
        }
        .onChange(of: playTap) {
            if isCurrent { startIfIdle() } else { playTapAt = Date() }
        }
        .onDisappear { player?.pause() }
        .task(id: player.map(ObjectIdentifier.init)) { await follow() }
        .accessibilityLabel("Video")
    }

    private var controls: some View {
        HStack(spacing: 10) {
            Text(Self.clock(time))
            scrubber
            Text(Self.clock(duration))
        }
        .font(.system(size: 12, weight: .semibold))
        .monospacedDigit()
        .foregroundStyle(.white)
        .padding(.horizontal, 14)
        .frame(height: 40)
        .viewerBacking(in: Capsule())
    }

    /// A thin track that thickens under the finger; touch anywhere on it to jump, drag to scrub.
    private var scrubber: some View {
        GeometryReader { geo in
            let fraction = duration > 0 ? min(1, max(0, time / duration)) : 0
            ZStack(alignment: .leading) {
                Capsule().fill(.white.opacity(0.3))
                Capsule().fill(.white).frame(width: geo.size.width * fraction)
            }
            .frame(height: scrubbing ? 8 : 4)
            .frame(maxHeight: .infinity)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { value in
                        if !scrubbing { beginScrub() }
                        scrub(to: min(1, max(0, value.location.x / max(geo.size.width, 1))) * duration)
                    }
                    .onEnded { _ in endScrub() }
            )
            .animation(.snappy(duration: 0.15), value: scrubbing)
        }
        .frame(height: 40)
        .accessibilityElement()
        .accessibilityLabel("Position")
        .accessibilityValue("\(Self.clock(time)) of \(Self.clock(duration))")
        .accessibilityAdjustableAction { direction in
            scrub(to: min(duration, max(0, time + (direction == .increment ? 5 : -5))))
        }
    }

    private func start() {
        if let player {
            if ended { player.seek(to: .zero) }
            player.play()
        } else {
            guard let asset = asset() else { return }
            // Sound with the silent switch on, as in Photos, when nothing has set the session up yet; a
            // call's playAndRecord and spoken replies' own playback setting are left as they are.
            let session = AVAudioSession.sharedInstance()
            if session.category == .soloAmbient || session.category == .ambient {
                try? session.setCategory(.playback, mode: .moviePlayback, options: [.mixWithOthers])
            }
            let player = AVPlayer(playerItem: AVPlayerItem(asset: asset))
            self.player = player
            player.play()
        }
        parked = false
        ended = false
        playing = true
    }

    private func pause() {
        player?.pause()
        playing = false
    }

    private func startIfIdle() {
        if player == nil || parked || ended { start() }
    }

    private func beginScrub() {
        scrubbing = true
        resumeAfterScrub = playing
        player?.pause()
    }

    private func scrub(to seconds: Double) {
        guard duration > 0 else { return }
        time = seconds
        player?.seek(to: CMTime(seconds: seconds, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero)
    }

    private func endScrub() {
        scrubbing = false
        ended = false
        parked = false
        if resumeAfterScrub { player?.play() }
    }

    /// Keeps the time, the play/pause glyph and the end-of-clip state in step with the player, and says
    /// so when the clip won't load at all.
    private func follow() async {
        guard let player, let item = player.currentItem else { return }
        if let length = try? await item.asset.load(.duration), length.seconds.isFinite {
            duration = length.seconds
        }
        while !Task.isCancelled {
            if item.status == .failed {
                let reason = item.error?.localizedDescription ?? "Unknown error."
                withAnimation(.easeOut(duration: 0.15)) {
                    failure = reason.contains("not found")
                        ? "Vision can't stream it. If `vision serve` was started before video playback was added, restart it."
                        : reason
                }
                playing = false
                return
            }
            let now = player.timeControlStatus != .paused
            if now != playing { playing = now }
            if !scrubbing {
                time = player.currentTime().seconds
                if !now && !parked && duration > 0 && time >= duration - 0.1 { ended = true }
            }
            try? await Task.sleep(for: .milliseconds(100))
        }
    }

    static func clock(_ seconds: Double) -> String {
        guard seconds.isFinite else { return "0:00" }
        let s = Int(seconds.rounded(.down))
        return String(format: "%d:%02d", s / 60, s % 60)
    }
}

/// The picture of a clip, with nothing drawn on top: an AVPlayerLayer that says when its first frame is up.
private struct PlayerSurface: UIViewRepresentable {
    let player: AVPlayer
    let onReady: () -> Void

    final class Surface: UIView {
        override class var layerClass: AnyClass { AVPlayerLayer.self }
        var playerLayer: AVPlayerLayer { layer as! AVPlayerLayer }
        var watch: NSKeyValueObservation?
    }

    func makeUIView(context: Context) -> Surface {
        let view = Surface()
        view.backgroundColor = .clear
        view.isUserInteractionEnabled = false
        view.playerLayer.videoGravity = .resizeAspect
        view.playerLayer.player = player
        let ready = onReady
        view.watch = view.playerLayer.observe(\.isReadyForDisplay, options: [.initial, .new]) { layer, _ in
            guard layer.isReadyForDisplay else { return }
            DispatchQueue.main.async { ready() }
        }
        return view
    }

    func updateUIView(_ view: Surface, context: Context) {
        if view.playerLayer.player !== player { view.playerLayer.player = player }
    }
}

/// The system close glyph on a round glass button, as Photos and the share sheet draw it.
struct PreviewCloseButton: View {
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        PreviewGlassButton(systemImage: "xmark", label: "Close") { dismiss() }
    }
}

/// A round glass button for the full-screen viewers' corners.
struct PreviewGlassButton: View {
    let systemImage: String
    let label: String
    let action: () -> Void

    var body: some View {
        // Plain button on drawn glass: the .glass button style pads its label out past the size asked for.
        Button(action: action) {
            Image(systemName: systemImage)
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(.white)
                .frame(width: 36, height: 36)
                .viewerBacking(in: Circle())
                .contentShape(Circle().inset(by: -6))  // still an easy target
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }
}

/// Full-screen video player: the system controls, playing as soon as it opens (the app's playAndRecord
/// session keeps the sound on with the silent switch on). The close button sits where the photo viewer's does.
struct VideoPreview: View {
    @Environment(\.dismiss) private var dismiss
    @State private var player: AVPlayer
    @State private var failure: String?
    @State private var dragToDismiss: CGFloat = 0

    init(asset: AVAsset) {
        _player = State(initialValue: AVPlayer(playerItem: AVPlayerItem(asset: asset)))
    }

    var body: some View {
        ZStack {
            Color.black.opacity(Double(1 - min(0.6, abs(dragToDismiss) / 400))).ignoresSafeArea()
            Group {
                if let failure {
                    // A black player with a crossed-out play glyph says nothing; say what went wrong.
                    VStack(spacing: 10) {
                        Image(systemName: "exclamationmark.triangle").themeFont(.largeTitle)
                        Text("Couldn't play this video").themeFont(.headline)
                        Text(failure).themeFont(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    }
                    .foregroundStyle(.white)
                    .padding(32)
                } else {
                    VideoPlayer(player: player)
                }
            }
            .offset(y: dragToDismiss)
        }
        .contentShape(Rectangle())
        // A UIKit pan so it only starts on a mostly vertical drag and leaves the scrubber's sideways drags alone.
        .gesture(DismissPan(enabled: true) { y in
            dragToDismiss = y
        } ended: { y, velocity in
            if abs(y) > 120 || abs(velocity) > 800 {
                dismiss()
            } else {
                withAnimation(.spring(duration: 0.3)) { dragToDismiss = 0 }
            }
        })
        .overlay(alignment: .topLeading) {
            PreviewCloseButton()
                .padding(.horizontal, 16)
                .padding(.top, 8)
                .opacity(dragToDismiss != 0 ? 0 : 1)
        }
        .onAppear { player.play() }
        .onDisappear { player.pause() }
        .task {
            guard let item = player.currentItem else { return }
            for await status in item.publisher(for: \.status).values where status == .failed {
                let reason = item.error?.localizedDescription ?? "Unknown error."
                failure = reason.contains("not found")
                    ? "Vision can't stream it. If `vision serve` was started before video playback was added, restart it."
                    : reason
                return
            }
        }
    }
}

/// A soft dark fade behind the viewer's buttons along one edge, so they read over anything and the glass
/// doesn't lens a full-screen screenshot's own buttons into a jumble underneath them.
struct ViewerScrim: View {
    let edge: VerticalEdge
    var height: CGFloat = 150

    var body: some View {
        LinearGradient(colors: [.black.opacity(0.6), .black.opacity(0)],
                       startPoint: edge == .top ? .top : .bottom, endPoint: edge == .top ? .bottom : .top)
            .frame(height: height)
            .frame(maxWidth: .infinity)
            .ignoresSafeArea()
            .allowsHitTesting(false)
    }
}

/// The big round play (or pause) button in the middle of a clip.
struct PlayButton: View {
    var systemImage = "play.fill"
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: systemImage)
                .contentTransition(.symbolEffect(.replace))
                .font(.system(size: 28, weight: .semibold))
                .foregroundStyle(.white)
                .frame(width: 76, height: 76)
                .viewerBacking(in: Circle())
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(systemImage == "pause.fill" ? "Pause" : "Play")
    }
}

/// Lets a clip's scrubber win a sideways drag inside a pager, lands each swipe fast, and catches a tap
/// that lands while a swipe is still settling. By default the scroll view holds each touch back to see
/// if it's a swipe and then takes it, so dragging the scrubber paged instead; with touches delivered
/// straight away the scrubber keeps its drag while a swipe anywhere else still pages. And while the
/// pager is gliding onto a page, iOS spends any tap on stopping the glide, so the play button on a clip
/// just swiped to did nothing for a second: `onSettlingTap` hears that tap instead (when it's near the
/// middle of the screen, where the play button sits).
struct PagerTouches: UIViewRepresentable {
    var onSettlingTap: (() -> Void)? = nil

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> UIView {
        let view = UIView()
        view.isUserInteractionEnabled = false
        return view
    }

    func updateUIView(_ view: UIView, context: Context) {
        let coordinator = context.coordinator
        coordinator.onSettlingTap = onSettlingTap
        DispatchQueue.main.async {
            var next = view.superview
            while let candidate = next, !(candidate is UIScrollView) { next = candidate.superview }
            guard let pager = next as? UIScrollView else { return }
            pager.delaysContentTouches = false
            pager.decelerationRate = .fast
            coordinator.attach(to: pager)
        }
    }

    final class Coordinator: NSObject, UIGestureRecognizerDelegate {
        var onSettlingTap: (() -> Void)?
        private weak var pager: UIScrollView?
        private var tap: UITapGestureRecognizer?
        private var settling = false

        func attach(to pager: UIScrollView) {
            guard self.pager !== pager else { return }
            if let tap { tap.view?.removeGestureRecognizer(tap) }
            let tap = UITapGestureRecognizer(target: self, action: #selector(tapped(_:)))
            tap.cancelsTouchesInView = false
            tap.delegate = self
            pager.addGestureRecognizer(tap)
            self.tap = tap
            self.pager = pager
        }

        /// Whether the pager is still moving when the finger comes down: dragging, gliding, or between pages.
        func gestureRecognizer(_ recognizer: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
            guard let pager else { return false }
            let page = pager.bounds.width + 12   // a page plus the gap the viewers leave between pages
            let offset = pager.contentOffset.x.truncatingRemainder(dividingBy: max(page, 1))
            let between = offset > 2 && offset < page - 2
            settling = pager.isDragging || pager.isDecelerating || between
            return settling
        }

        func gestureRecognizer(_ recognizer: UIGestureRecognizer,
                               shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool { true }

        @objc private func tapped(_ tap: UITapGestureRecognizer) {
            guard settling, let window = tap.view?.window else { return }
            let point = tap.location(in: window)
            let middle = CGPoint(x: window.bounds.midX, y: window.bounds.midY)
            guard hypot(point.x - middle.x, point.y - middle.y) < 70 else { return }
            onSettlingTap?()
        }
    }
}

extension View {
    /// Presents `ImagePreview` (or `VideoPreview`) full screen whenever `item` is set; tapping any thumbnail sets it.
    func imagePreview(_ item: Binding<PreviewImage?>) -> some View {
        fullScreenCover(item: item) { item in
            Group {
                if let video = item.video {
                    VideoPreview(asset: video)
                } else {
                    ImagePreview(images: item.images, start: item.start, paths: item.paths, clips: item.clips, fetch: item.fetch,
                                 sources: item.sources, zoom: item.zoom)
                }
            }
            .presentationBackground(.clear)  // the chat shows through as the black fades on a pull down
        }
    }
}
