import SwiftUI

/// A scout's avatar: a small squishy creature in the scout's colour, with a trait of its own and a mood that says
/// what its project is doing. The same drawing as the desktop app's buddy.ts,
/// on a 100 × 100 canvas: keyframes, eyes, mouths, traits and props.
/// Shared by the app and the widgets; a widget, or Reduce Motion, holds one pose.
struct BuddyView: View {
    var hue: Double
    var trait: String
    var mood: String
    var size: CGFloat
    /// False: one still pose (the widgets, quiet rows). Reduce Motion holds it still too.
    var animated = true
    /// A tap squishes it and it giggles.
    var pokeable = false

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var pokedAt: Date?

    var body: some View {
        let moving = animated && !reduceMotion
        Group {
            if moving {
                TimelineView(.animation) { timeline in
                    canvas(at: timeline.date.timeIntervalSinceReferenceDate, now: timeline.date)
                }
            } else {
                canvas(at: nil, now: Date())
            }
        }
        .frame(width: size, height: size)
        .scaleEffect(squish)
        .contentShape(Rectangle())
        .onTapGesture {
            guard pokeable, !reduceMotion else { return }
            withAnimation(.spring(response: 0.25, dampingFraction: 0.35)) { pokedAt = Date() }
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.4) {
                withAnimation(.easeOut(duration: 0.2)) { pokedAt = nil }
            }
        }
        .allowsHitTesting(pokeable)
        .accessibilityLabel(BuddyMood(mood).label)
    }

    private var squish: CGSize {
        guard let pokedAt, Date().timeIntervalSince(pokedAt) < 0.3 else { return CGSize(width: 1, height: 1) }
        return CGSize(width: 1.12, height: 0.88)
    }

    private func canvas(at time: Double?, now: Date) -> some View {
        let shown = pokedAt != nil && now.timeIntervalSince(pokedAt!) < 1.4 ? BuddyMood("proud") : BuddyMood(mood)
        return Canvas { ctx, canvasSize in
            let scale = canvasSize.width / 100
            ctx.scaleBy(x: scale, y: scale)
            BuddyPainter(hue: hue, trait: trait, mood: shown, props: size >= 48,
                         time: time ?? shown.still).draw(in: &ctx)
        }
    }
}

// MARK: - moods

/// One keyframe: when (0–1), scale x and y, lift, gaze x and y, tilt in degrees.
struct BuddyKey {
    var t, sx, sy, lift, gx, gy, tilt: Double
    init(_ t: Double, _ sx: Double, _ sy: Double, _ lift: Double, _ gx: Double, _ gy: Double, _ tilt: Double) {
        (self.t, self.sx, self.sy, self.lift, self.gx, self.gy, self.tilt) = (t, sx, sy, lift, gx, gy, tilt)
    }
}

struct BuddyMood {
    var id: String
    var label: String
    var dur: Double
    var still: Double
    var keys: [BuddyKey]
    var eyes: String
    var mouth: String
    var brows: [(Double, Double)]? = nil
    /// Each arm's angle: one value held, or one per keyframe.
    var left: [Double] = [25]
    var right: [Double] = [-25]
    var wave = false
    var blush = false
    var dim = false
    var droop = false
    var extras: [String] = []

    static let moods: [String: BuddyMood] = {
        let rest: [BuddyKey] = [.init(0, 1, 1, 0, 0, 0, 0), .init(0.5, 1.05, 0.94, 0, 0, 0, 0), .init(1, 1, 1, 0, 0, 0, 0)]
        let hop: [BuddyKey] = [.init(0, 1, 1, 0, 0, 0, 0), .init(0.14, 1.12, 0.86, 0, 0, 1, 0), .init(0.32, 0.92, 1.1, 10, 0, -1, 0),
                               .init(0.48, 0.96, 1.05, 3, 0, 0, 0), .init(0.58, 1.1, 0.9, 0, 0, 1, 0), .init(0.72, 1, 1, 0, 0, 0, 0),
                               .init(1, 1, 1, 0, 0, 0, 0)]
        let list: [BuddyMood] = [
            .init(id: "asleep", label: "Asleep", dur: 5, still: 2.6, keys: rest, eyes: "closed", mouth: "tiny", dim: true,
                  extras: ["zz", "bubble"]),
            .init(id: "waking", label: "Waking up", dur: 3.2, still: 1.3,
                  keys: [.init(0, 1, 1, 0, 0, 0, 0), .init(0.3, 0.9, 1.12, 1, 0, -1, 0), .init(0.6, 0.9, 1.12, 1, 0, -1, 0),
                         .init(0.78, 1.07, 0.93, 0, 0, 0, 0), .init(1, 1, 1, 0, 0, 0, 0)],
                  eyes: "squeeze", mouth: "yawn", left: [25, 150, 150, 25, 25], right: [-25, -150, -150, -25, -25]),
            .init(id: "curious", label: "Curious", dur: 3.6, still: 0.9,
                  keys: [.init(0, 1, 1, 0, -4, -1, -7), .init(0.42, 1, 1, 0, -4, -1, -7), .init(0.5, 1, 1, 0, 4, -1, 7),
                         .init(0.92, 1, 1, 0, 4, -1, 7), .init(1, 1, 1, 0, -4, -1, -7)],
                  eyes: "open", mouth: "o", brows: [(0, 0), (-5, -3)], extras: ["question"]),
            .init(id: "focused", label: "Focused", dur: 0.8, still: 0.2,
                  keys: [.init(0, 1, 1, 0, 0, 3, 0), .init(0.5, 1.03, 0.97, 0, 0, 3, 0), .init(1, 1, 1, 0, 0, 3, 0)],
                  eyes: "narrow", mouth: "tongue", brows: [(-1.5, 2.5), (2.5, -1.5)], left: [40, 60, 40], right: [-60, -40, -60],
                  extras: ["sparks"]),
            .init(id: "found_it", label: "Found it", dur: 1.5, still: 0.5,
                  keys: [.init(0, 1, 1, 0, 0, 0, 0), .init(0.15, 1.12, 0.85, 0, 0, 1, 0), .init(0.35, 0.9, 1.12, 14, 0, -2, 0),
                         .init(0.55, 1, 1, 6, 0, 0, 0), .init(0.7, 1.1, 0.88, 0, 0, 1, 0), .init(0.82, 1, 1, 0, 0, 0, 0),
                         .init(1, 1, 1, 0, 0, 0, 0)],
                  eyes: "star", mouth: "open", left: [25, 30, 150, 140, 40, 25, 25], right: [-25, -30, -150, -140, -40, -25, -25],
                  blush: true, extras: ["stars"]),
            .init(id: "needs_you", label: "Needs you", dur: 1.8, still: 0.6, keys: hop, eyes: "big", mouth: "smile",
                  brows: [(-3.5, -2.5), (-2.5, -3.5)], wave: true, extras: ["bang", "ring"]),
            .init(id: "still_waiting", label: "Still waiting", dur: 4.6, still: 2.3,
                  keys: [.init(0, 1, 1, 0, 0, 3, 0), .init(0.5, 1.07, 0.91, 0, 0, 4, -3), .init(1, 1, 1, 0, 0, 3, 0)],
                  eyes: "glossy", mouth: "frown", brows: [(1, -3.5), (-3.5, 1)], left: [12], right: [-12], droop: true,
                  extras: ["dots"]),
            .init(id: "proud", label: "Proud", dur: 1.4, still: 0.35,
                  keys: [.init(0, 1, 1, 0, 0, 0, -6), .init(0.25, 1.06, 0.94, 0, 0, 0, 0), .init(0.5, 1, 1, 3, 0, 0, 6),
                         .init(0.75, 1.06, 0.94, 0, 0, 0, 0), .init(1, 1, 1, 0, 0, 0, -6)],
                  eyes: "happy", mouth: "open", left: [140, 165, 140, 165, 140], right: [-165, -140, -165, -140, -165],
                  blush: true, extras: ["confetti"]),
            .init(id: "worried", label: "Worried", dur: 0.35, still: 0.1,
                  keys: [.init(0, 1, 1, 0, -1, 0, -2), .init(0.5, 1, 1, 0, 1, 0, 2), .init(1, 1, 1, 0, -1, 0, -2)],
                  eyes: "small", mouth: "wobbly", brows: [(1.5, -3), (-3, 1.5)], left: [55], right: [-55], droop: true,
                  extras: ["sweat"]),
            .init(id: "out_cold", label: "Out cold", dur: 5, still: 2.6, keys: rest, eyes: "closed", mouth: "o-small", dim: true,
                  extras: ["zz", "nightcap", "moon"]),
        ]
        return Dictionary(uniqueKeysWithValues: list.map { ($0.id, $0) })
    }()

    init(_ id: String) {
        self = Self.moods[id] ?? Self.moods["asleep"]!
    }

    private init(id: String, label: String, dur: Double, still: Double, keys: [BuddyKey], eyes: String, mouth: String,
                 brows: [(Double, Double)]? = nil, left: [Double] = [25], right: [Double] = [-25], wave: Bool = false,
                 blush: Bool = false, dim: Bool = false, droop: Bool = false, extras: [String] = []) {
        (self.id, self.label, self.dur, self.still, self.keys, self.eyes, self.mouth) = (id, label, dur, still, keys, eyes, mouth)
        (self.brows, self.left, self.right, self.wave, self.blush, self.dim, self.droop, self.extras) =
            (brows, left, right, wave, blush, dim, droop, extras)
    }
}

// MARK: - drawing

/// HSL as the desktop's CSS writes it (hue in degrees, saturation and lightness in percent).
func buddyHSL(_ h: Double, _ s: Double, _ l: Double, _ a: Double = 1) -> Color {
    let hue = (h.truncatingRemainder(dividingBy: 360) + 360).truncatingRemainder(dividingBy: 360) / 360
    let sat = s / 100, light = l / 100
    let q = light < 0.5 ? light * (1 + sat) : light + sat - light * sat
    let p = 2 * light - q
    func channel(_ t0: Double) -> Double {
        var t = t0
        if t < 0 { t += 1 }
        if t > 1 { t -= 1 }
        if t < 1.0 / 6 { return p + (q - p) * 6 * t }
        if t < 0.5 { return q }
        if t < 2.0 / 3 { return p + (q - p) * (2.0 / 3 - t) * 6 }
        return p
    }
    return Color(red: channel(hue + 1.0 / 3), green: channel(hue), blue: channel(hue - 1.0 / 3), opacity: a)
}

private let amber = Color(red: 0.96, green: 0.65, blue: 0.14)
private let green = Color(red: 0.25, green: 0.81, blue: 0.43)
private let ink = Color(red: 0.086, green: 0.086, blue: 0.114)
private let pink = Color(red: 1, green: 0.48, blue: 0.6)
private let tearBlue = Color(red: 0.62, green: 0.85, blue: 1)

private let radius = 31.0, bottom = 82.0
private func centreY(_ sy: Double, _ lift: Double) -> Double { bottom - lift - 0.85 * radius * sy }
private let c0 = centreY(1, 0), top0 = c0 - radius, eyeY = c0 - 3, mouthY = eyeY + 10.5, browY = eyeY - 10.5
private let eyeXs = [39.0, 61.0]

/// The SVG's `.42 0 .58 1` spline between keyframes, near enough.
private func ease(_ x: Double) -> Double { x * x * (3 - 2 * x) }
/// A value swinging between two over `period` seconds, eased at each end.
private func swing(_ time: Double, _ period: Double, _ a: Double, _ b: Double, offset: Double = 0) -> Double {
    let p = ((time + offset) / period).truncatingRemainder(dividingBy: 1)
    return a + (b - a) * (0.5 - 0.5 * cos(p * 2 * .pi))
}
/// 0–1 through a cycle of `period` seconds, started `begin` seconds in.
private func cycle(_ time: Double, _ period: Double, begin: Double = 0) -> Double {
    let p = ((time - begin) / period).truncatingRemainder(dividingBy: 1)
    return p < 0 ? p + 1 : p
}
/// A piecewise-linear value at `p` through (key, value) points.
private func through(_ p: Double, _ points: [(Double, Double)]) -> Double {
    for i in 1..<points.count where p <= points[i].0 {
        let (t0, v0) = points[i - 1], (t1, v1) = points[i]
        return t1 > t0 ? v0 + (v1 - v0) * (p - t0) / (t1 - t0) : v1
    }
    return points.last!.1
}

private func pt(_ x: Double, _ y: Double) -> CGPoint { CGPoint(x: x, y: y) }

private func ellipse(_ cx: Double, _ cy: Double, _ rx: Double, _ ry: Double) -> Path {
    Path(ellipseIn: CGRect(x: cx - rx, y: cy - ry, width: rx * 2, height: ry * 2))
}

/// The four-point sparkle the props and star eyes use, `r` from the middle to a point.
private func sparkle(_ x: Double, _ y: Double, _ r: Double) -> Path {
    var p = Path()
    p.move(to: pt(x, y - r))
    p.addQuadCurve(to: pt(x + r, y), control: pt(x, y))
    p.addQuadCurve(to: pt(x, y + r), control: pt(x, y))
    p.addQuadCurve(to: pt(x - r, y), control: pt(x, y))
    p.addQuadCurve(to: pt(x, y - r), control: pt(x, y))
    p.closeSubpath()
    return p
}

struct BuddyPainter {
    let hue: Double
    let trait: String
    let mood: BuddyMood
    let props: Bool
    let time: Double

    private var sad: Bool { mood.droop }
    private var sleepy: Bool { mood.id == "asleep" || mood.id == "out_cold" }

    /// Where the mood's keyframes have it now: (interpolated key, segment index, fraction through it).
    private var pose: (key: BuddyKey, index: Int, frac: Double) {
        let p = cycle(time, mood.dur)
        let keys = mood.keys
        for i in 1..<keys.count where p <= keys[i].t {
            let a = keys[i - 1], b = keys[i]
            let f = b.t > a.t ? ease((p - a.t) / (b.t - a.t)) : 1
            func mix(_ x: Double, _ y: Double) -> Double { x + (y - x) * f }
            let k = BuddyKey(p, mix(a.sx, b.sx), mix(a.sy, b.sy), mix(a.lift, b.lift), mix(a.gx, b.gx), mix(a.gy, b.gy), mix(a.tilt, b.tilt))
            return (k, i - 1, f)
        }
        return (keys.last!, keys.count - 2, 1)
    }

    private func armAngle(_ values: [Double], _ pose: (key: BuddyKey, index: Int, frac: Double)) -> Double {
        guard values.count == mood.keys.count else { return values.first ?? 0 }
        let a = values[pose.index], b = values[min(pose.index + 1, values.count - 1)]
        return a + (b - a) * pose.frac
    }

    func draw(in ctx: inout GraphicsContext) {
        let pose = self.pose
        let k = pose.key
        let cy = centreY(k.sy, k.lift)
        let topShift = cy - radius * k.sy - top0
        let gradient = bodyGradient(cy)

        // The shadow on the ground, smaller as it jumps.
        let shadow = max(10, 23 * k.sx - k.lift * 1.1)
        ctx.fill(ellipse(50, 88, shadow, 3.2), with: .color(.black.opacity(0.3)))

        var body = ctx
        body.opacity = mood.dim ? 0.88 : 1
        body.translateBy(x: 50, y: bottom)
        body.rotate(by: .degrees(k.tilt))
        body.translateBy(x: -50, y: -bottom)

        // Arms behind the body, then the trait's back (ears), the body, the trait's front, the face.
        var arms = body
        arms.translateBy(x: 0, y: cy - c0)
        drawArm(&arms, side: -1, angle: armAngle(mood.left, pose))
        drawArm(&arms, side: 1, angle: mood.wave ? swing(time, 0.55, -150, -105) : armAngle(mood.right, pose))

        var top = body
        top.translateBy(x: 0, y: topShift)
        if trait == "ears" { drawEars(&top, gradient: gradient) }
        body.fill(bodyPath(k), with: gradient)
        drawTrait(&top)
        if mood.extras.contains("nightcap") { drawNightcap(&top) }

        var face = body
        face.translateBy(x: k.gx, y: cy - c0 + k.gy)
        drawFace(&face)

        if props { drawProps(&ctx) }
    }

    private func bodyGradient(_ cy: Double) -> GraphicsContext.Shading {
        .radialGradient(Gradient(stops: [.init(color: buddyHSL(hue, 95, 80), location: 0),
                                         .init(color: buddyHSL(hue, 85, 61), location: 0.45),
                                         .init(color: buddyHSL(hue + 8, 70, 42), location: 1)]),
                        center: pt(50 - radius * 0.24, cy - radius * 0.4), startRadius: 0, endRadius: radius * 1.6)
    }

    private func bodyPath(_ k: BuddyKey) -> Path {
        let c = centreY(k.sy, k.lift)
        let points: [CGPoint] = (0..<12).map { i in
            let a = Double(i) / 12 * 2 * .pi - .pi / 2, s = sin(a)
            return pt(50 + radius * k.sx * cos(a), c + radius * k.sy * s * (s > 0 ? 0.85 : 1))
        }
        var path = Path()
        path.move(to: points[0])
        let n = points.count
        for i in 0..<n {
            let p0 = points[(i - 1 + n) % n], p1 = points[i], p2 = points[(i + 1) % n], p3 = points[(i + 2) % n]
            path.addCurve(to: p2, control1: pt(p1.x + (p2.x - p0.x) / 6, p1.y + (p2.y - p0.y) / 6),
                          control2: pt(p2.x - (p3.x - p1.x) / 6, p2.y - (p3.y - p1.y) / 6))
        }
        path.closeSubpath()
        return path
    }

    private func drawArm(_ ctx: inout GraphicsContext, side: Double, angle: Double) {
        var arm = ctx
        arm.translateBy(x: 50 + side * 25, y: c0 + 5)
        arm.rotate(by: .degrees(angle))
        arm.fill(ellipse(0, 10.5, 5, 9), with: .color(buddyHSL(hue, 75, 50)))
    }

    // MARK: traits

    private func drawTrait(_ ctx: inout GraphicsContext) {
        let t = top0
        func swung(_ a: Double, _ b: Double, _ period: Double) -> GraphicsContext {
            var c = ctx
            c.translateBy(x: 50, y: t + 1)
            c.rotate(by: .degrees(swing(time, period, a, b)))
            c.translateBy(x: -50, y: -(t + 1))
            return c
        }
        let stroke = StrokeStyle(lineWidth: 2.2, lineCap: .round)
        switch trait {
        case "sprout":
            var c = sad ? swung(38, 42, 3) : swung(-7, 7, 2.2)
            var stem = Path()
            stem.move(to: pt(50, t + 2))
            stem.addQuadCurve(to: pt(51, t - 9), control: pt(48, t - 4))
            c.stroke(stem, with: .color(Color(red: 0.25, green: 0.6, blue: 0.32)), style: stroke)
            let leaf = sleepy ? Color(red: 0.29, green: 0.6, blue: 0.35) : Color(red: 0.37, green: 0.82, blue: 0.48)
            for (x, y, rx, ry, rot) in [(45.0, t - 10, 6.0, 3.2, -28.0), (56.5, t - 12, 6.5, 3.4, 32.0)] {
                var l = c
                l.translateBy(x: x, y: y)
                l.rotate(by: .degrees(rot))
                l.fill(ellipse(0, 0, rx, ry), with: .color(leaf))
            }
        case "curl":
            var curl = Path()
            curl.move(to: pt(48, t + 2))
            curl.addCurve(to: pt(57, t - 4), control1: pt(44, t - 7), control2: pt(57, t - 11))
            curl.addCurve(to: pt(52, t - 4), control1: pt(57, t), control2: pt(51, t))
            ctx.stroke(curl, with: .color(buddyHSL(hue, 70, sad ? 34 : 38)), style: StrokeStyle(lineWidth: 2.6, lineCap: .round))
        case "ears":
            break   // drawn behind the body (drawEars)
        default:   // antenna
            var c = sad ? swung(24, 28, 3) : swung(-9, 9, mood.id == "focused" ? 0.5 : 1.4)
            var stem = Path()
            stem.move(to: pt(50, t + 2))
            stem.addQuadCurve(to: pt(50, t - 10), control: pt(49, t - 5))
            c.stroke(stem, with: .color(buddyHSL(hue, 50, 35)), style: stroke)
            let bulb = mood.id == "needs_you" ? amber : ["proud", "found_it"].contains(mood.id) ? green
                : sad || sleepy ? buddyHSL(hue, 20, 58) : buddyHSL(hue + 30, 95, 78)
            if !(sad || sleepy) { c.fill(ellipse(50, t - 13, 7, 7), with: .color(bulb.opacity(0.3))) }
            c.fill(ellipse(50, t - 13, 3.6, 3.6), with: .color(bulb))
        }
    }

    private func drawEars(_ ctx: inout GraphicsContext, gradient: GraphicsContext.Shading) {
        let t = top0
        for side in [-1.0, 1.0] {
            let x = 50 + side * 17
            var ear = ctx
            ear.translateBy(x: x, y: t + 8)
            var angle = sad || sleepy ? side * 38 : 0
            if side > 0 && !(sad || sleepy) { angle += through(cycle(time, 3), [(0, 0), (0.82, 0), (0.88, 16), (1, 0)]) }
            ear.rotate(by: .degrees(angle))
            ear.translateBy(x: -x, y: -(t + 8))
            if side > 0 {   // the right ear is the left one mirrored about its own middle
                ear.translateBy(x: 2 * x, y: 0)
                ear.scaleBy(x: -1, y: 1)
            }
            var outer = Path()
            outer.move(to: pt(x - 9, t + 10))
            outer.addQuadCurve(to: pt(x + 9, t + 6), control: pt(x - 4, t - 14))
            outer.closeSubpath()
            ear.fill(outer, with: gradient)
            var inner = Path()
            inner.move(to: pt(x - 5, t + 7))
            inner.addQuadCurve(to: pt(x + 5, t + 4), control: pt(x - 2, t - 7))
            inner.closeSubpath()
            ear.fill(inner, with: .color(pink.opacity(0.55)))
        }
    }

    private func drawNightcap(_ ctx: inout GraphicsContext) {
        let t = top0
        var cap = Path()
        cap.move(to: pt(27, t + 13))
        cap.addQuadCurve(to: pt(58, t - 9), control: pt(30, t - 10))
        cap.addQuadCurve(to: pt(86, t + 17), control: pt(78, t - 8))
        cap.addQuadCurve(to: pt(72, t + 9), control: pt(75, t + 3))
        cap.closeSubpath()
        ctx.fill(cap, with: .color(buddyHSL(hue + 200, 45, 42)))
        var trim = Path()
        trim.move(to: pt(26, t + 14))
        trim.addQuadCurve(to: pt(74, t + 10), control: pt(50, t - 1))
        let cream = Color(red: 0.945, green: 0.933, blue: 0.9)
        ctx.stroke(trim, with: .color(cream), style: StrokeStyle(lineWidth: 5, lineCap: .round))
        ctx.fill(ellipse(86.5, t + 19, 4.2, 4.2), with: .color(cream))
    }

    // MARK: face

    private var blinking: Bool {
        let p = cycle(time, 3.7)
        return p > 0.93
    }

    private func drawFace(_ ctx: inout GraphicsContext) {
        // The highlight on the body, then cheeks, brows, eyes and mouth.
        var shine = ctx
        shine.translateBy(x: 34, y: c0 - 17)
        shine.rotate(by: .degrees(-25))
        shine.fill(ellipse(0, 0, 7, 4.2), with: .color(.white.opacity(0.42)))
        if mood.blush {
            for x in eyeXs { ctx.fill(ellipse(x + (x < 50 ? -9 : 9), eyeY + 8, 5, 3), with: .color(pink.opacity(0.5))) }
        }
        if let brows = mood.brows {
            for (i, x) in eyeXs.enumerated() {
                var b = Path()
                b.move(to: pt(x - 5, browY + brows[i].0))
                b.addLine(to: pt(x + 5, browY + brows[i].1))
                ctx.stroke(b, with: .color(ink), style: StrokeStyle(lineWidth: 2.4, lineCap: .round))
            }
        }
        for (i, x) in eyeXs.enumerated() { drawEye(&ctx, x: x, right: i == 1) }
        drawMouth(&ctx)
        if props && mood.extras.contains("bubble") {
            let r = through(cycle(time, 5), [(0, 0), (0.75, 5.5), (0.8, 6), (0.81, 0), (1, 0)])
            if r > 0.1 {
                ctx.fill(ellipse(57, mouthY + 1, r, r), with: .color(.white.opacity(0.25)))
                ctx.stroke(ellipse(57, mouthY + 1, r, r), with: .color(.white.opacity(0.7)), lineWidth: 0.8)
            }
        }
        if mood.extras.contains("sweat") {
            let p = cycle(time, 1.4)
            var drop = Path()
            let x = 74 + p, y = eyeY - 10 + 9 * p
            drop.move(to: pt(x, y))
            drop.addCurve(to: pt(x, y + 7), control1: pt(x + 3, y + 4), control2: pt(x + 3, y + 7))
            drop.addCurve(to: pt(x, y), control1: pt(x - 3, y + 7), control2: pt(x - 3, y + 4))
            ctx.fill(drop, with: .color(tearBlue.opacity(p < 0.6 ? 1 : (1 - p) / 0.4)))
        }
    }

    private func drawEye(_ ctx: inout GraphicsContext, x: Double, right: Bool) {
        let line = StrokeStyle(lineWidth: 2.8, lineCap: .round, lineJoin: .round)
        let blink = blinking
        func open(_ rx: Double, _ ry: Double, _ dy: Double = 0, shine: (Double, Double, Double)? = nil, second: (Double, Double, Double)? = nil) {
            ctx.fill(ellipse(x, eyeY + dy, rx, blink ? 0.6 : ry), with: .color(ink))
            guard !blink else { return }
            if let s = shine { ctx.fill(ellipse(x + s.0, eyeY + s.1, s.2, s.2), with: .color(.white)) }
            if let s = second { ctx.fill(ellipse(x + s.0, eyeY + s.1, s.2, s.2), with: .color(.white)) }
        }
        switch mood.eyes {
        case "closed":
            var p = Path()
            p.move(to: pt(x - 5, eyeY))
            p.addQuadCurve(to: pt(x + 5, eyeY), control: pt(x, eyeY + 4.5))
            ctx.stroke(p, with: .color(ink), style: line)
        case "happy":
            var p = Path()
            p.move(to: pt(x - 5, eyeY + 2))
            p.addLine(to: pt(x, eyeY - 3.5))
            p.addLine(to: pt(x + 5, eyeY + 2))
            ctx.stroke(p, with: .color(ink), style: line)
        case "squeeze":
            var p = Path()
            let d = right ? -1.0 : 1.0
            p.move(to: pt(x - 4 * d, eyeY - 4))
            p.addLine(to: pt(x + 3 * d, eyeY))
            p.addLine(to: pt(x - 4 * d, eyeY + 4))
            ctx.stroke(p, with: .color(ink), style: line)
        case "star":
            let s = swing(time, 0.6, 1, 1.3)
            ctx.fill(sparkle(x, eyeY, 8 * s), with: .color(ink))
            ctx.fill(ellipse(x, eyeY, 1.6, 1.6), with: .color(.white))
        case "narrow": open(5, 4.6, 1, shine: (1.6, 0.4, 1.3))
        case "small":
            ctx.fill(ellipse(x, eyeY, 3.8, 5.6), with: .color(ink))
            ctx.fill(ellipse(x + 1.2, eyeY - 2, 1.2, 1.2), with: .color(.white))
        case "big": open(6.5, 9, shine: (2, -3.2, 2.4), second: (-2.2, 3.4, 1.1))
        case "glossy":
            ctx.fill(ellipse(x, eyeY + 1, 6.8, 8.6), with: .color(ink))
            ctx.fill(ellipse(x + 2, eyeY - 2.4, 2.8, 2.8), with: .color(.white))
            ctx.fill(ellipse(x - 2.4, eyeY + 3, 1.3, 1.3), with: .color(.white))
            var tear = Path()
            tear.move(to: pt(x - 5, eyeY + 6))
            tear.addQuadCurve(to: pt(x + 5, eyeY + 6), control: pt(x, eyeY + 9.5))
            ctx.stroke(tear, with: .color(tearBlue.opacity(swing(time, 1.8, 0.4, 1))), style: StrokeStyle(lineWidth: 1.6, lineCap: .round))
        default: open(5, 7.5, shine: (1.8, -2.8, 1.7))
        }
    }

    private func drawMouth(_ ctx: inout GraphicsContext) {
        let line = StrokeStyle(lineWidth: 2.4, lineCap: .round, lineJoin: .round)
        let y = mouthY
        func curve(_ x0: Double, _ y0: Double, _ cx: Double, _ cy: Double, _ x1: Double, _ y1: Double) {
            var p = Path()
            p.move(to: pt(x0, y0))
            p.addQuadCurve(to: pt(x1, y1), control: pt(cx, cy))
            ctx.stroke(p, with: .color(ink), style: line)
        }
        switch mood.mouth {
        case "tiny": curve(48, y, 49, y + 1.8, 52, y)
        case "smile": curve(45.5, y, 50, y + 4, 54.5, y)
        case "o": ctx.fill(ellipse(50, y + 1, 2.4, 3), with: .color(ink))
        case "o-small": ctx.fill(ellipse(50, y + 1, 1.6, 1.9), with: .color(ink))
        case "frown": curve(46.5, y + 2.5, 50, y - 0.7, 53.5, y + 2.5)
        case "wobbly":
            var p = Path()
            p.move(to: pt(44, y + 1))
            for i in 0..<4 {
                let x = 44 + Double(i) * 3
                p.addQuadCurve(to: pt(x + 3, y + 1), control: pt(x + 1.5, y + (i % 2 == 0 ? -1 : 3)))
            }
            ctx.stroke(p, with: .color(ink), style: StrokeStyle(lineWidth: 2, lineCap: .round, lineJoin: .round))
        case "tongue":
            ctx.fill(ellipse(54, y + 2.6, 2.2, 3), with: .color(pink))
            curve(45, y, 50, y + 3, 54.5, y - 0.5)
        case "open":
            var p = Path()
            p.move(to: pt(44, y - 1))
            p.addQuadCurve(to: pt(56, y - 1), control: pt(50, y + 8))
            p.closeSubpath()
            ctx.fill(p, with: .color(ink))
            ctx.fill(ellipse(50, y + 3.4, 3, 1.6), with: .color(pink))
        case "yawn":
            let wide = through(cycle(time, mood.dur), [(0, 0), (0.3, 1), (0.6, 1), (0.78, 0), (1, 0)])
            ctx.fill(ellipse(50, y + 2, 2.2 + 2 * wide, 1.6 + 4.9 * wide), with: .color(ink))
        default: break
        }
    }

    // MARK: props (48 pt and up)

    private func drawProps(_ ctx: inout GraphicsContext) {
        let extras = mood.extras
        if extras.contains("ring") {
            let p = cycle(time, 1.8)
            ctx.stroke(ellipse(50, c0, 33 + 15 * p, 33 + 15 * p), with: .color(amber.opacity(0.8 * (1 - p))), lineWidth: 2.5)
        }
        if extras.contains("zz") {
            for begin in [0.0, 1.6] {
                let p = cycle(time, 3.2, begin: begin)
                let text = Text("z").font(.system(size: 13, weight: .bold)).foregroundColor(buddyHSL(hue, 60, 82))
                var z = ctx
                z.opacity = through(p, [(0, 0), (0.5, 0.9), (1, 0)])
                z.draw(text, at: pt(79 + 8 * p, 20 - 13 * p))
            }
        }
        if extras.contains("question") {
            let text = Text("?").font(.system(size: 17, weight: .heavy)).foregroundColor(buddyHSL(hue, 70, 82))
            ctx.draw(text, at: pt(82, 19 + swing(time, 1.8, 0, -3)))
        }
        if extras.contains("sparks") {
            for (x, y, begin) in [(80.0, 26.0, 0.0), (86, 40, 0.25), (72, 16, 0.5)] {
                var s = ctx
                s.opacity = through(cycle(time, 0.75, begin: begin), [(0, 0), (0.5, 1), (1, 0)])
                s.fill(sparkle(x, y, 4), with: .color(buddyHSL(hue, 90, 82)))
            }
        }
        if extras.contains("stars") {
            for (x, y, begin) in [(16.0, 30.0, 0.0), (84, 24, 0.2), (12, 60, 0.45), (88, 58, 0.65), (50, 4, 0.3)] {
                let s = through(cycle(time, 0.9, begin: begin), [(0, 0), (0.5, 1.2), (1, 0)])
                ctx.fill(sparkle(x, y, 6 * s), with: .color(Color(red: 1, green: 0.84, blue: 0.42)))
            }
        }
        if extras.contains("bang") {
            var b = ctx
            let s = swing(time, 0.9, 1, 1.15)
            b.translateBy(x: 81, y: 21)
            b.scaleBy(x: s, y: s)
            b.fill(ellipse(0, 0, 8.5, 8.5), with: .color(amber))
            var mark = Path()
            mark.move(to: pt(0, -4.5))
            mark.addLine(to: pt(0, 1))
            b.stroke(mark, with: .color(.white), style: StrokeStyle(lineWidth: 2.6, lineCap: .round))
            b.fill(ellipse(0, 4.3, 1.5, 1.5), with: .color(.white))
        }
        if extras.contains("dots") {
            ctx.fill(Path(roundedRect: CGRect(x: 68, y: 15.5, width: 22, height: 13), cornerRadius: 6.5), with: .color(buddyHSL(hue, 15, 60, 0.25)))
            for i in 0..<3 {
                let o = through(cycle(time, 2.4, begin: Double(i) * 0.4), [(0, 0.3), (0.2, 1), (0.4, 0.3), (1, 0.3)])
                ctx.fill(ellipse(74 + Double(i) * 5, 22, 1.7, 1.7), with: .color(amber.opacity(o)))
            }
        }
        if extras.contains("confetti") {
            let colours: [Color] = [amber, green, pink, Color(red: 0.24, green: 0.68, blue: 0.91), .white, Color(red: 0.7, green: 0.55, blue: 1)]
            for i in 0..<9 {
                let p = cycle(time, 1.6, begin: (Double(i) * 0.37).truncatingRemainder(dividingBy: 1.6))
                let x = 8 + Double(i) * 10.5 + (i % 2 == 1 ? 4 : -4) * p, y = -2 + 94 * p
                var c = ctx
                c.opacity = p < 0.7 ? 1 : (1 - p) / 0.3
                c.fill(Path(roundedRect: CGRect(x: x - 1.6, y: y - 2.6, width: 3.2, height: 5.2), cornerRadius: 0.8),
                       with: .color(colours[i % colours.count]))
            }
        }
        if extras.contains("moon") {
            var m = ctx
            m.opacity = swing(time, 4, 0.6, 1)
            m.clip(to: ellipse(19, 15, 6, 6), options: .inverse)
            m.fill(ellipse(15, 18, 7, 7), with: .color(Color(red: 1, green: 0.88, blue: 0.54)))
        }
    }
}
