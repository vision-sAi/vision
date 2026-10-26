import SwiftUI
import UIKit

/// UIKit's keyboard window. The keys are drawn in-process, in a `UIRemoteKeyboardWindow` that belongs to the
/// keyboard's own scene (above every app window), so no public window list includes it.
enum KeyboardWindow {
    static func current() -> UIWindow? {
        let active = NSSelectorFromString("activeKeyboard")
        guard let cls: AnyObject = NSClassFromString("UIKeyboard"), cls.responds(to: active),
              let keyboard = cls.perform(active)?.takeUnretainedValue() as? UIView,
              let window = keyboard.window, !window.isHidden else { return nil }
        return window
    }
}

/// SwiftUI drawn inside the keyboard's window, so it lies over the live keys (ChatGPT's attach menu and cards do
/// this). The layer covers the whole window in screen coordinates; only `hole` takes touches, everything else
/// falls through to the keys or the app. Mounted while `active` and the keyboard is up.
struct KeyboardLayer<Content: View>: UIViewRepresentable {
    var active: Bool
    var hole: CGRect
    var content: Content

    func makeCoordinator() -> Coordinator { Coordinator(content) }

    func makeUIView(context: Context) -> UIView {
        let v = UIView()
        v.isUserInteractionEnabled = false
        v.isHidden = true
        return v
    }

    func updateUIView(_ view: UIView, context: Context) {
        let c = context.coordinator
        c.host.rootView = content
        c.container.hole = hole
        c.active = active
        DispatchQueue.main.async { c.place(); c.keepTicking() }
    }

    static func dismantleUIView(_ view: UIView, coordinator: Coordinator) {
        coordinator.container.removeFromSuperview()
    }

    /// Passes every touch outside the hole through to whatever is underneath.
    final class Container: UIView {
        var hole: CGRect = .zero
        override func hitTest(_ point: CGPoint, with event: UIEvent?) -> UIView? {
            hole.contains(convert(point, to: nil)) ? super.hitTest(point, with: event) : nil
        }
    }

    final class Coordinator: NSObject {
        let host: UIHostingController<Content>
        let container = Container()
        var active = false
        /// SwiftUI's own frame loop never runs for a view in the keyboard's scene: an update draws only when the
        /// host is laid out, and an animation freezes on its first frame. So while the layer is in use (and for a
        /// moment after, to finish closing) a display link lays it out every frame.
        private var link: CADisplayLink?
        private var busyUntil = Date.distantPast

        init(_ content: Content) {
            host = UIHostingController(rootView: content)
            super.init()
            host.view.backgroundColor = .clear
            host.safeAreaRegions = []
            container.backgroundColor = .clear
            container.tag = KeyboardLayerTag
            container.addSubview(host.view)
            // The keyboard window only exists once the keys are up; lay the layer out then, so the first open
            // already has something to animate from.
            NotificationCenter.default.addObserver(self, selector: #selector(keyboardChanged),
                                                   name: UIResponder.keyboardDidShowNotification, object: nil)
        }

        @objc private func keyboardChanged() { place() }

        func keepTicking() {
            busyUntil = Date().addingTimeInterval(1)
            guard link == nil, container.superview != nil else { return }
            let link = CADisplayLink(target: self, selector: #selector(tick))
            link.add(to: .main, forMode: .common)
            self.link = link
        }

        @objc private func tick() {
            guard container.superview != nil, !container.hole.isEmpty || Date() < busyUntil else {
                link?.invalidate()
                link = nil
                return
            }
            host.view.setNeedsLayout()
            host.view.layoutIfNeeded()
        }

        func place() {
            guard active, let window = KeyboardWindow.current() else {
                container.removeFromSuperview()
                return
            }
            if container.superview !== window { window.addSubview(container) }
            container.frame = window.bounds
            host.view.frame = container.bounds
            host.view.setNeedsLayout()
            host.view.layoutIfNeeded()
            window.bringSubviewToFront(container)
        }
    }
}

/// Marks the layer in the keyboard window, so `KeyboardLid` can tuck itself underneath.
let KeyboardLayerTag = 0x5649_5349
