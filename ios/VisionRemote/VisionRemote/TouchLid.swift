import SwiftUI
import UIKit

/// An invisible sheet over the whole screen while `active`, with one `hole` (global coordinates) that stays live.
/// Everything else, the nav bar, the composer, the keyboard, looks untouched and does not even highlight; a tap or
/// swipe there only runs `onTouch`. It is its own transparent UIWindow above the keyboard's, because UIKit hands a
/// touch to exactly one window: a view in the app window still let the glass bar light up and the keys type.
struct TouchLid: UIViewRepresentable {
    var active: Bool
    var hole: CGRect
    var onTouch: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> UIView {
        let v = UIView()
        v.isUserInteractionEnabled = false
        v.isHidden = true
        return v
    }

    func updateUIView(_ view: UIView, context: Context) {
        let c = context.coordinator
        c.onTouch = onTouch
        DispatchQueue.main.async {
            guard let window = view.window, let scene = window.windowScene else { return }
            guard active else { c.hide(); return }
            c.show(in: scene, frame: window.frame, hole: hole)
            #if DEBUG
            if CommandLine.arguments.contains("--lid-check") {
                // Development: prove the lid window is up and where its hole is (simulator log).
                let centre = CGPoint(x: hole.midX, y: hole.midY)
                let lidHit = c.lidWindow?.hitTest(centre, with: nil)
                let appHit = window.hitTest(centre, with: nil)
                NSLog("LIDCHECK lid=\(c.lidWindow?.frame ?? .zero) hole=\(hole) lidHit=\(lidHit.map { String(describing: type(of: $0)) } ?? "nil") appHit=\(appHit.map { String(describing: type(of: $0)) } ?? "nil")")
            }
            #endif
        }
    }

    static func dismantleUIView(_ view: UIView, coordinator: Coordinator) {
        coordinator.hide()
    }

    /// A window that steps aside when nothing in it wants the touch, so the app window underneath gets it.
    final class LidWindow: UIWindow {
        override func hitTest(_ point: CGPoint, with event: UIEvent?) -> UIView? {
            let v = super.hitTest(point, with: event)
            return v === self ? nil : v
        }
    }

    /// The lid's content: passes touches through inside the hole, catches them everywhere else.
    final class LidView: UIView {
        var hole: CGRect = .zero
        override func hitTest(_ point: CGPoint, with event: UIEvent?) -> UIView? {
            hole.contains(convert(point, to: nil)) ? nil : super.hitTest(point, with: event)
        }
    }

    final class Coordinator: NSObject {
        var lidWindow: UIWindow?
        var onTouch: () -> Void = {}

        func show(in scene: UIWindowScene, frame: CGRect, hole: CGRect) {
            if lidWindow == nil {
                let w = LidWindow(windowScene: scene)
                // Above the keyboard's window (10_000_001), so the keys are covered too.
                w.windowLevel = UIWindow.Level(rawValue: 10_000_002)
                w.backgroundColor = .clear
                let vc = UIViewController()
                let lid = LidView()
                lid.backgroundColor = .clear
                lid.addGestureRecognizer(UITapGestureRecognizer(target: self, action: #selector(hit)))
                lid.addGestureRecognizer(UIPanGestureRecognizer(target: self, action: #selector(hit)))
                vc.view = lid
                w.rootViewController = vc
                lidWindow = w
            }
            (lidWindow?.rootViewController?.view as? LidView)?.hole = hole
            lidWindow?.frame = frame
            lidWindow?.isHidden = false
        }

        func hide() {
            lidWindow?.isHidden = true
            lidWindow = nil
        }

        @objc func hit(_ g: UIGestureRecognizer) {
            if let pan = g as? UIPanGestureRecognizer, pan.state != .began { return }
            onTouch()
        }
    }
}
