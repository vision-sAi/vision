import AVKit
import SwiftUI
import UIKit

/// The Library's full-screen viewer: swipe left and right through the pictures and clips, pinch or
/// double-tap to zoom a picture, drag down to close, and the bin deletes what's on screen from the machine.
struct LibraryViewer: View {
    @Environment(\.dismiss) private var dismiss
    @State private var items: [LibraryItem]
    @State private var current: String?
    @State private var zoomed = false
    @State private var scrubbing = false
    @State private var playTap = 0
    @State private var chromeHidden = false
    @State private var dragToDismiss: CGFloat = 0
    @State private var insets = EdgeInsets()
    @State private var deleting = false
    @State private var failure: String?
    let delete: (LibraryItem) async throws -> Void

    init(items: [LibraryItem], start: LibraryItem, delete: @escaping (LibraryItem) async throws -> Void) {
        _items = State(initialValue: items)
        _current = State(initialValue: start.path)
        self.delete = delete
    }

    private var shown: LibraryItem? { items.first { $0.path == current } }

    var body: some View {
        ZStack {
            Color.black.opacity(Double(1 - min(0.6, abs(dragToDismiss) / 400))).ignoresSafeArea()
                .onGeometryChange(for: EdgeInsets.self) { $0.safeAreaInsets } action: { insets = $0 }
            ScrollView(.horizontal, showsIndicators: false) {
                LazyHStack(spacing: 12) {
                    ForEach(items) { item in
                        Group {
                            if item.kind == "video" {
                                LibraryVideoPage(item: item, insets: insets, controlsHidden: chromeHidden, scrubbing: $scrubbing, playTap: playTap,
                                                 isCurrent: item.path == current, onTap: toggleChrome)
                            } else {
                                LibraryPhotoPage(item: item, isCurrent: item.path == current, zoomed: $zoomed,
                                                 onTap: toggleChrome)
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
            .scrollDisabled(zoomed || scrubbing)
            .ignoresSafeArea()
            .offset(y: dragToDismiss)
            .gesture(DismissPan(enabled: !zoomed && !scrubbing) { y in
                dragToDismiss = y
            } ended: { y, velocity in
                if abs(y) > 120 || abs(velocity) > 800 {
                    dismiss()
                } else {
                    withAnimation(.spring(duration: 0.3)) { dragToDismiss = 0 }
                }
            })
        }
        .overlay(alignment: .top) { ViewerScrim(edge: .top).opacity(chromeHidden || dragToDismiss != 0 ? 0 : 1) }
        .overlay(alignment: .top) { chrome }
        .overlay(alignment: .bottom) {
            if let caption = shown?.caption, !caption.isEmpty {
                Text(caption)
                    .themeFont(.subheadline, weight: .medium)
                    .foregroundStyle(.white)
                    .multilineTextAlignment(.center)
                    .lineLimit(3)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .viewerBacking(in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                    .padding(.horizontal, 16)
                    .padding(.bottom, 8)
                    .opacity(chromeHidden || dragToDismiss != 0 ? 0 : 1)
            }
        }
        .statusBarHidden()   // shown with the buttons, it blurred in and out on every tap
        .alert("Couldn't delete it", isPresented: Binding(get: { failure != nil }, set: { if !$0 { failure = nil } })) {
        } message: {
            Text(failure ?? "")
        }
    }

    private var chrome: some View {
        HStack {
            PreviewCloseButton()
            Spacer(minLength: 8)
            if let shown {
                Text(shown.date.formatted(date: .abbreviated, time: .shortened))
                    .themeFont(.subheadline, weight: .semibold)
                    .foregroundStyle(.white)
                    .lineLimit(1)
                    .padding(.horizontal, 14)
                    .frame(height: 36)
                    .viewerBacking(in: Capsule())
            }
            Spacer(minLength: 8)
            PreviewGlassButton(systemImage: "trash", label: "Delete") { Task { await deleteShown() } }
                .disabled(deleting || shown == nil)
        }
        .padding(.horizontal, 16)
        .padding(.top, 8)
        .opacity(chromeHidden || dragToDismiss != 0 ? 0 : 1)
        .allowsHitTesting(!chromeHidden)
    }

    /// Show or hide the buttons and labels at once: the tap doesn't wait to see if it's a double-tap.
    private func toggleChrome() {
        withAnimation(.easeOut(duration: 0.15)) { chromeHidden.toggle() }
    }

    /// Delete the item on screen, then land on the one after it (or before, at the end); close when none are left.
    private func deleteShown() async {
        guard let item = shown, let index = items.firstIndex(of: item) else { return }
        deleting = true
        defer { deleting = false }
        do {
            try await delete(item)
        } catch {
            failure = error.localizedDescription
            return
        }
        let next = items.indices.contains(index + 1) ? items[index + 1] : index > 0 ? items[index - 1] : nil
        guard let next else { dismiss(); return }
        withAnimation(.snappy(duration: 0.25)) {
            items.remove(at: index)
            current = next.path
        }
    }
}

/// One picture: shown from its large thumbnail at once, then at full size. Pinch or double-tap to zoom;
/// while zoomed a drag pans it and the pager stops paging.
private struct LibraryPhotoPage: View {
    @EnvironmentObject private var client: VisionClient
    let item: LibraryItem
    let isCurrent: Bool
    @Binding var zoomed: Bool
    let onTap: () -> Void
    @State private var image: UIImage?
    @State private var scale: CGFloat = 1
    @State private var lastScale: CGFloat = 1
    @State private var offset: CGSize = .zero
    @State private var lastOffset: CGSize = .zero

    var body: some View {
        ZStack {
            if let image {
                Image(uiImage: image)
                    .resizable()
                    .scaledToFit()
                    .scaleEffect(scale)
                    .offset(offset)
            } else {
                ProgressView().tint(.white)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .contentShape(Rectangle())
        .gesture(magnify)
        .simultaneousGesture(pan, including: scale > 1 ? .all : .subviews)
        // Simultaneous, so a single tap acts straight away instead of waiting out the double-tap window
        // (a double-tap toggles twice, leaving the chrome as it was).
        .onTapGesture(count: 2) { toggleZoom() }
        .simultaneousGesture(TapGesture().onEnded { onTap() })
        .onChange(of: scale) { if isCurrent { zoomed = scale > 1 } }
        .onChange(of: isCurrent) { if isCurrent { zoomed = scale > 1 } else { reset() } }
        .task(id: item.path) {
            if image == nil { image = await client.thumbnail(path: item.path, size: 1024) }
            if let full = await client.replyImage(path: item.path) { image = full }
        }
        .accessibilityLabel(item.caption.isEmpty ? "Picture" : item.caption)
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

/// One clip: its first frame with a play button; the system player takes over once it's tapped, and
/// pauses when the clip is swiped away.
private struct LibraryVideoPage: View {
    @EnvironmentObject private var client: VisionClient
    let item: LibraryItem
    let insets: EdgeInsets
    let controlsHidden: Bool
    @Binding var scrubbing: Bool
    let playTap: Int
    let isCurrent: Bool
    let onTap: () -> Void
    @State private var still: UIImage?

    var body: some View {
        ClipPage(still: still, asset: { client.videoAsset(path: item.path) }, insets: insets,
                 controlsHidden: controlsHidden, extraBottom: item.caption.isEmpty ? 0 : 64,
                 scrubbing: $scrubbing, playTap: playTap, isCurrent: isCurrent, onTap: onTap)
            .task(id: item.path) { still = await client.thumbnail(path: item.path, size: 1024) }
    }
}

extension View {
    /// Dark glass behind text and buttons laid over a picture, so they read on anything from snow to night.
    @ViewBuilder func viewerBacking<S: Shape>(in shape: S) -> some View {
        if #available(iOS 26, *) {
            glassEffect(.regular.tint(.black.opacity(0.35)), in: shape).environment(\.colorScheme, .dark)
        } else {
            background(.black.opacity(0.35), in: shape)
                .background(.ultraThinMaterial, in: shape)
                .environment(\.colorScheme, .dark)
        }
    }
}

/// A vertical pull on the viewer, to close it. A UIKit pan so it only starts on a mostly vertical drag and
/// runs alongside the pager's own scrolling instead of swallowing sideways swipes as a SwiftUI drag would.
struct DismissPan: UIGestureRecognizerRepresentable {
    let enabled: Bool
    let changed: (CGFloat) -> Void
    let ended: (_ translation: CGFloat, _ velocity: CGFloat) -> Void

    func makeCoordinator(converter: CoordinateSpaceConverter) -> Coordinator { Coordinator() }

    func makeUIGestureRecognizer(context: Context) -> UIPanGestureRecognizer {
        let pan = UIPanGestureRecognizer()
        pan.delegate = context.coordinator
        return pan
    }

    func updateUIGestureRecognizer(_ recognizer: UIPanGestureRecognizer, context: Context) {
        recognizer.isEnabled = enabled
    }

    func handleUIGestureRecognizerAction(_ recognizer: UIPanGestureRecognizer, context: Context) {
        let y = recognizer.translation(in: recognizer.view).y
        switch recognizer.state {
        case .changed: changed(y)
        case .ended, .cancelled, .failed: ended(y, recognizer.velocity(in: recognizer.view).y)
        default: break
        }
    }

    final class Coordinator: NSObject, UIGestureRecognizerDelegate {
        func gestureRecognizerShouldBegin(_ recognizer: UIGestureRecognizer) -> Bool {
            guard let pan = recognizer as? UIPanGestureRecognizer else { return false }
            let v = pan.velocity(in: pan.view)
            return abs(v.y) > abs(v.x) * 1.5
        }

        func gestureRecognizer(_ recognizer: UIGestureRecognizer,
                               shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool { true }
    }
}
