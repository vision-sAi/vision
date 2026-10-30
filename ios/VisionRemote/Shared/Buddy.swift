import SwiftUI

/// A scout's avatar: a soft little capsule in the scout's colour (the shape from the app icon), lit from above with a
/// gentle gradient, a rim of light and a shadow on the ground; never glossy. A trait of its own on top and a mood that
/// says what its project is doing. The same drawing as the desktop app's buddy.ts, on a 100 × 100 canvas.
/// Shared by the app and the widgets; a widget, or Reduce Motion, holds one pose.
struct BuddyView: View {
    var hue: Double
    var trait: String
    var mood: String
    var size: CGFloat
    /// False: one still pose (the widgets, quiet rows). Reduce Motion holds it still too.
    var animated = true
    /// A tap makes it hop and look proud.
    var pokeable = false
    /// The badges and bubbles around it; nil leaves them to the size (dropped under 48 points).
    var props: Bool? = nil

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
        .offset(y: hopped ? -size * 0.1 : 0)
        .contentShape(Rectangle())
        .onTapGesture {
            guard pokeable, !reduceMotion else { return }
            pokedAt = Date()
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.4) {
                pokedAt = nil
            }
        }
        .allowsHitTesting(pokeable)
        .accessibilityLabel(BuddyMood(mood).label)
    }

    private var hopped: Bool {
        guard let pokedAt else { return false }
        return Date().timeIntervalSince(pokedAt) < 0.2
    }

    private func canvas(at time: Double?, now: Date) -> some View {
        let shown = pokedAt != nil && now.timeIntervalSince(pokedAt!) < 1.4 ? BuddyMood("proud") : BuddyMood(mood)
        return Canvas { ctx, canvasSize in
            let scale = canvasSize.width / 100
            ctx.scaleBy(x: scale, y: scale)
            BuddyPainter(hue: hue, trait: trait, mood: shown, props: props ?? (size >= 48),
                         time: time ?? shown.still).draw(in: &ctx)
        }
    }
}

// MARK: - Vision's own

/// Vision itself, wherever it shows its face: a violet scout with a sprout, the one on the app icon.
enum VisionFaceMood: Equatable {
    case idle, thinking, listening, speaking, error, asleep

    fileprivate var buddy: String {
        switch self {
        case .idle, .listening: return "curious"
        case .thinking: return "focused"
        case .speaking: return "proud"
        case .error: return "worried"
        case .asleep: return "asleep"
        }
    }
}

/// The scout drawn at `width` points across its body; the frame hugs the scout rather than the square canvas.
struct VisionFace: View {
    static let hue = 248.0

    var mood: VisionFaceMood
    var width: CGFloat
    var animated = true

    var body: some View {
        let canvas = width / 0.7
        BuddyView(hue: Self.hue, trait: "sprout", mood: mood.buddy, size: canvas, animated: animated, props: mood == .asleep)
            .frame(width: width, height: canvas * 0.66)
    }
}

/// Vision's colours outside the scout: the icon's dark backdrop, the antenna's glow, and a warm red for trouble.
enum VisionPalette {
    static let backdrop = buddyHSL(VisionFace.hue, 35, 12)
    static let glow = buddyHSL(VisionFace.hue, 60, 86)
    static let trouble = Color(red: 0.85, green: 0.63, blue: 0.57)
}

// MARK: - moods

/// A step: at `t` (0–1 through the cycle), `x` and `y` in steps of 4 units; the body eases from one to the next.
struct BuddyStep {
    var t, x, y: Double
    init(_ t: Double, _ x: Double, _ y: Double) { (self.t, self.x, self.y) = (t, x, y) }
}

struct BuddyMood {
    var id: String
    var label: String
    var dur: Double
    var still: Double
    var eyes: String
    var hop: [BuddyStep] = []
    var look: [BuddyStep] = []
    var blink = false
    var dim = false
    var sad = false
    var extras: [String] = []

    static let moods: [String: BuddyMood] = {
        let list: [BuddyMood] = [
            .init(id: "asleep", label: "Asleep", dur: 4, still: 1, eyes: "closed", hop: [.init(0, 0, 0), .init(0.5, 0, 0.25)], dim: true, extras: ["zz"]),
            .init(id: "waking", label: "Waking up", dur: 3, still: 1.2, eyes: "squeeze", hop: [.init(0, 0, 0), .init(0.4, 0, -1), .init(0.7, 0, 0)]),
            .init(id: "curious", label: "Curious", dur: 3.2, still: 0.8, eyes: "open", blink: true,
                  extras: ["question"]),
            .init(id: "focused", label: "Focused", dur: 1.6, still: 0.2, eyes: "focused", look: [.init(0, 0, 0.5)], blink: true),
            .init(id: "found_it", label: "Found it", dur: 1.4, still: 0.3, eyes: "star",
                  hop: [.init(0, 0, 0), .init(0.2, 0, -2), .init(0.45, 0, -1), .init(0.6, 0, 0)], extras: ["sparks"]),
            .init(id: "needs_you", label: "Needs you", dur: 1.6, still: 0.5, eyes: "big",
                  hop: [.init(0, 0, 0), .init(0.15, 0, -1), .init(0.3, 0, 0), .init(0.45, 0, -1), .init(0.6, 0, 0)], blink: true,
                  extras: ["bang"]),
            .init(id: "still_waiting", label: "Still waiting", dur: 4, still: 2, eyes: "sad", blink: true, sad: true, extras: ["dots"]),
            .init(id: "proud", label: "Proud", dur: 1, still: 0.3, eyes: "happy", hop: [.init(0, 0, 0), .init(0.5, 0, -1)]),
            .init(id: "worried", label: "Worried", dur: 0.5, still: 0.1, eyes: "small", hop: [.init(0, -0.4, 0), .init(0.5, 0.4, 0)], sad: true),
            .init(id: "out_cold", label: "Out cold", dur: 4, still: 1, eyes: "x", dim: true, extras: ["zz"]),
        ]
        return Dictionary(uniqueKeysWithValues: list.map { ($0.id, $0) })
    }()

    init(_ id: String) {
        self = Self.moods[id] ?? Self.moods["asleep"]!
    }

    private init(id: String, label: String, dur: Double, still: Double, eyes: String, hop: [BuddyStep] = [], look: [BuddyStep] = [],
                 blink: Bool = false, dim: Bool = false, sad: Bool = false, extras: [String] = []) {
        (self.id, self.label, self.dur, self.still, self.eyes) = (id, label, dur, still, eyes)
        (self.hop, self.look, self.blink, self.dim, self.sad, self.extras) = (hop, look, blink, dim, sad, extras)
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
private let spark = Color(red: 1, green: 0.84, blue: 0.42)

/// SVG path data as buddy.ts writes it: M L H V Q C Z, absolute or relative, nothing else.
private func svgPath(_ d: String) -> Path {
    var path = Path()
    var tokens: [String] = []
    let scanner = Scanner(string: d)
    scanner.charactersToBeSkipped = CharacterSet(charactersIn: " ,\n")
    while !scanner.isAtEnd {
        if let cmd = scanner.scanCharacter(), "MmLlHhVvQqCcZz".contains(cmd) {
            tokens.append(String(cmd))
        } else {
            scanner.currentIndex = scanner.string.index(before: scanner.currentIndex)
            guard let n = scanner.scanDouble() else { break }
            tokens.append(String(n))
        }
    }
    var i = 0, cmd = "M", cur = CGPoint.zero, start = CGPoint.zero
    func num() -> Double { defer { i += 1 }; return Double(tokens[i]) ?? 0 }
    func pt(_ rel: Bool) -> CGPoint { let x = num(), y = num(); return rel ? CGPoint(x: cur.x + x, y: cur.y + y) : CGPoint(x: x, y: y) }
    while i < tokens.count {
        if Double(tokens[i]) == nil { cmd = tokens[i]; i += 1 }
        let rel = cmd == cmd.lowercased()
        switch cmd.uppercased() {
        case "M":
            cur = pt(rel); start = cur; path.move(to: cur)
            cmd = rel ? "l" : "L" // further pairs are lines
        case "L": cur = pt(rel); path.addLine(to: cur)
        case "H": let x = num(); cur.x = rel ? cur.x + x : x; path.addLine(to: cur)
        case "V": let y = num(); cur.y = rel ? cur.y + y : y; path.addLine(to: cur)
        case "Q": let c = pt(rel), e = pt(rel); path.addQuadCurve(to: e, control: c); cur = e
        case "C": let c1 = pt(rel), c2 = pt(rel), e = pt(rel); path.addCurve(to: e, control1: c1, control2: c2); cur = e
        case "Z": path.closeSubpath(); cur = start
        default: i += 1
        }
    }
    return path
}

private func circle(_ x: Double, _ y: Double, _ r: Double) -> Path { Path(ellipseIn: CGRect(x: x - r, y: y - r, width: r * 2, height: r * 2)) }

private func sparkle(_ r: Double) -> Path {
    let k = r * 0.18
    return svgPath("M0 \(-r)Q\(k) \(-k) \(r) 0Q\(k) \(k) 0 \(r)Q\(-k) \(k) \(-r) 0Q\(-k) \(-k) 0 \(-r)Z")
}

/// The shapes, as buddy.ts has them. The body spans 15–85 across and 38–78 down; the eyes sit at 37.5 and 62.5.
private enum Shape {
    static let step = 4.0, eyeY = 57.0, eyeX = 12.5, eyeScale = 1.35
    static let body = svgPath("M30 38H70C78.28 38 85 44.72 85 53V63C85 71.28 78.28 78 70 78H30C21.72 78 15 71.28 15 63V53C15 44.72 21.72 38 30 38Z")
    static let rim = svgPath("M21.5 46Q24.5 41 31 41H69Q75.5 41 78.5 46")
    static let underside = svgPath("M24 74.5Q50 79 76 74.5")
    static let feet = svgPath("M25 74h11v5q0 3.5-3.5 3.5h-4q-3.5 0-3.5-3.5ZM64 74h11v5q0 3.5-3.5 3.5h-4q-3.5 0-3.5-3.5Z")

    struct Eye { var path: Path; var line = false; var shine: [(Double, Double)] = [] }
    /// Each eye drawn as the left one about its centre (+x is toward the nose).
    static let eyes: [String: Eye] = [
        "open": .init(path: svgPath("M-3.5 -1.5C-3.5 -6.17 3.5 -6.17 3.5 -1.5V1.5C3.5 6.17 -3.5 6.17 -3.5 1.5Z"), shine: [(1.2, -2.2)]),
        "big": .init(path: svgPath("M-4 -3C-4 -8.33 4 -8.33 4 -3V3C4 8.33 -4 8.33 -4 3Z"), shine: [(1.4, -3.6), (-1.6, 2.6)]),
        "closed": .init(path: svgPath("M-4 0.5Q0 3.5 4 0.5"), line: true),
        "focused": .init(path: svgPath("M-3.5 -0.5H3.5V1.5C3.5 6.17 -3.5 6.17 -3.5 1.5Z"), shine: [(1.4, 0.6)]),
        "sad": .init(path: svgPath("M-3.5 -1L3.5 -3.6V1C3.5 5.67 -3.5 5.67 -3.5 1Z"), shine: [(1.2, -1.6)]),
        "small": .init(path: svgPath("M-2 0C-2 -2.67 2 -2.67 2 0C2 2.67 -2 2.67 -2 0Z")),
        "squeeze": .init(path: svgPath("M-3 -3.5L2.5 0L-3 3.5"), line: true),
        "happy": .init(path: svgPath("M-4 2Q0 -4 4 2"), line: true),
        "star": .init(path: sparkle(5)),
        "x": .init(path: svgPath("M-3 -3L3 3M3 -3L-3 3"), line: true),
    ]

    static let leaves = svgPath("M50 31Q43 21 36 25Q41 32 50 31ZM50 30Q57 18 66 21Q61 30 50 30Z")
    static let leavesDown = svgPath("M50 31Q42 27 37 33Q44 36 50 31ZM50 31Q58 27 63 33Q56 36 50 31Z")
    static let ears = svgPath("M19 44Q17 30 22 26Q29 32 33 40ZM81 44Q83 30 78 26Q71 32 67 40Z")
    static let earsDown = svgPath("M19 44Q19 35 26 33Q31 37 33 40ZM81 44Q81 35 74 33Q69 37 67 40Z")
    static let earsInner = svgPath("M22 40Q21 33 23 30Q27 34 29 39ZM78 40Q79 33 77 30Q73 34 71 39Z")
    static let curl = svgPath("M49 39C45 31 52 25 57 28C61 31 57 36 53 33")
    static let zed = svgPath("M77 26h6l-6 7h6")
    static let question = svgPath("M82 19.5C82 15 89 15 89 19.5C89 21.5 87.5 22.3 86.5 22.8C85.5 23.3 85.5 24 85.5 25")
}

/// 0–1 through a cycle of `period` seconds, started `begin` seconds in.
private func cycle(_ time: Double, _ period: Double, begin: Double = 0) -> Double {
    let p = ((time - begin) / period).truncatingRemainder(dividingBy: 1)
    return p < 0 ? p + 1 : p
}

/// Which of `n` equal steps a cycle is on.
private func stepOf(_ time: Double, _ period: Double, _ n: Int, begin: Double = 0) -> Int {
    min(n - 1, Int(cycle(time, period, begin: begin) * Double(n)))
}

struct BuddyPainter {
    let hue: Double
    let trait: String
    let mood: BuddyMood
    let props: Bool
    let time: Double

    private struct Colours { var top, bottom, low, glow, inner, ink: Color }
    private var c: Colours {
        let h = hue
        return mood.dim
            ? Colours(top: buddyHSL(h, 24, 52), bottom: buddyHSL(h, 26, 40), low: buddyHSL(h, 24, 36), glow: buddyHSL(h, 30, 70),
                      inner: buddyHSL(h, 30, 62), ink: buddyHSL(h, 35, 12))
            : Colours(top: buddyHSL(h, 58, 70), bottom: buddyHSL(h, 54, 55), low: buddyHSL(h, 48, 44), glow: buddyHSL(h, 70, 88),
                      inner: buddyHSL(h, 70, 82), ink: buddyHSL(h, 35, 12))
    }

    /// Where a list of steps has the body now, eased between steps, in canvas units.
    private func eased(_ steps: [BuddyStep]) -> CGSize {
        guard let first = steps.first else { return .zero }
        let all = steps.last!.t < 1 ? steps + [BuddyStep(1, first.x, first.y)] : steps
        let p = cycle(time, mood.dur)
        let i = max(0, (all.lastIndex { $0.t <= p } ?? 0))
        let a = all[i], b = all[min(i + 1, all.count - 1)]
        let u = b.t > a.t ? (p - a.t) / (b.t - a.t) : 0
        let e = u * u * (3 - 2 * u)
        return CGSize(width: (a.x + (b.x - a.x) * e) * Shape.step, height: (a.y + (b.y - a.y) * e) * Shape.step)
    }

    private func round(_ width: Double) -> StrokeStyle { StrokeStyle(lineWidth: width, lineCap: .round, lineJoin: .round) }

    func draw(in ctx: inout GraphicsContext) {
        let c = c
        var shadow = ctx
        shadow.translateBy(x: 50, y: 83)
        shadow.scaleBy(x: 1, y: 4.0 / 31)
        shadow.fill(circle(0, 0, 31), with: .radialGradient(Gradient(colors: [.black.opacity(0.3), .black.opacity(0)]),
                                                            center: .zero, startRadius: 0, endRadius: 31))
        var me = ctx
        let hop = eased(mood.hop)
        me.translateBy(x: hop.width, y: hop.height)
        drawTrait(&me, c)
        me.fill(Shape.feet, with: .color(c.low))
        me.fill(Shape.body, with: .linearGradient(Gradient(colors: [c.top, c.bottom]), startPoint: CGPoint(x: 0, y: 38), endPoint: CGPoint(x: 0, y: 78)))
        me.stroke(Shape.rim, with: .color(.white.opacity(mood.dim ? 0.14 : 0.32)), style: round(1.6))
        me.stroke(Shape.underside, with: .color(c.low.opacity(0.55)), style: round(1.6))
        var face = me
        let look = eased(mood.look)
        face.translateBy(x: look.width, y: look.height)
        let blinking = mood.blink && cycle(time, 3.7) >= 0.94 && cycle(time, 3.7) < 0.97
        let eye = Shape.eyes[blinking ? "closed" : mood.eyes] ?? Shape.eyes["open"]!
        for side in [-1.0, 1.0] {
            var one = face
            one.translateBy(x: 50 + side * Shape.eyeX, y: Shape.eyeY)
            one.scaleBy(x: -side * Shape.eyeScale, y: Shape.eyeScale)
            if eye.line {
                one.stroke(eye.path, with: .color(c.ink), style: round(1.8))
            } else {
                one.fill(eye.path, with: .color(c.ink))
                for (n, (x, y)) in eye.shine.enumerated() {
                    one.fill(circle(x, y, n == 0 ? 1.15 : 0.7), with: .color(.white.opacity(n == 0 ? 0.85 : 0.45)))
                }
            }
        }
        drawProps(&ctx)
    }

    private func drawTrait(_ ctx: inout GraphicsContext, _ c: Colours) {
        let sleepy = mood.dim, droop = mood.sad || sleepy
        switch trait {
        case "sprout":
            ctx.stroke(svgPath("M50 39V30"), with: .color(Color(red: 0.23, green: 0.55, blue: 0.31)), style: round(2.2))
            let leaves = droop ? Shape.leavesDown : Shape.leaves
            let box = leaves.boundingRect
            let colours = sleepy ? [Color(red: 0.31, green: 0.6, blue: 0.38), Color(red: 0.18, green: 0.43, blue: 0.25)]
                                 : [Color(red: 0.49, green: 0.89, blue: 0.58), Color(red: 0.25, green: 0.68, blue: 0.35)]
            ctx.fill(leaves, with: .linearGradient(Gradient(colors: colours), startPoint: CGPoint(x: 0, y: box.minY), endPoint: CGPoint(x: 0, y: box.maxY)))
        case "ears":
            ctx.fill(droop ? Shape.earsDown : Shape.ears, with: .color(c.top))
            if !droop { ctx.fill(Shape.earsInner, with: .color(c.inner)) }
        case "curl":
            ctx.stroke(Shape.curl, with: .color(c.top), style: round(3.2))
        default: // antenna
            ctx.stroke(svgPath("M67.5 39V27"), with: .color(c.low), style: round(2.2))
            let bulb = mood.id == "needs_you" ? amber : mood.id == "proud" || mood.id == "found_it" ? green : droop ? c.low : c.glow
            var lamp = ctx
            if mood.id == "needs_you" && stepOf(time, 0.8, 2) == 0 { lamp.opacity = 0.35 }
            lamp.fill(circle(67.5, 24.5, 3.6), with: .radialGradient(
                Gradient(stops: [.init(color: .white.opacity(0.9), location: 0), .init(color: bulb, location: 0.35), .init(color: bulb, location: 1)]),
                center: CGPoint(x: 66.4, y: 23.1), startRadius: 0, endRadius: 5.8))
        }
    }

    private func drawProps(_ ctx: inout GraphicsContext) {
        let small: Set = ["zz", "question", "sparks", "bang", "dots"]
        for e in mood.extras where props || !small.contains(e) {
            var p = ctx
            switch e {
            case "zz":
                for begin in [0.0, 1.5] {
                    let u = cycle(time, 3, begin: begin)
                    var z = ctx
                    z.opacity = u < 0.2 ? u / 0.2 : u < 0.7 ? 1 : (1 - u) / 0.3
                    z.translateBy(x: 9 * u, y: -10 * u)
                    z.stroke(Shape.zed, with: .color(buddyHSL(hue, 40, 78)), style: round(1.8))
                }
            case "question":
                let colour = buddyHSL(hue, 60, 85)
                p.stroke(Shape.question, with: .color(colour), style: round(2.2))
                p.fill(circle(85.5, 30, 1.3), with: .color(colour))
            case "bang":
                p.opacity = stepOf(time, 0.8, 4) == 3 ? 0.35 : 1
                p.stroke(svgPath("M87 17v8"), with: .color(amber), style: round(3))
                p.fill(circle(87, 30.5, 1.7), with: .color(amber))
            case "dots":
                let lit = stepOf(time, 2.4, 4)
                for i in 0..<3 {
                    var d = ctx
                    d.opacity = lit == i ? 1 : 0.3
                    d.fill(circle(79 + Double(i) * 6, 28, 1.7), with: .color(buddyHSL(hue, 20, 72)))
                }
            case "sparks":
                for (x, y, d) in [(84.0, 22.0, 0.0), (13, 30, 0.35), (89, 44, 0.7)] where stepOf(time, 1.05, 3, begin: d) == 1 {
                    var s = ctx
                    s.translateBy(x: x, y: y)
                    s.fill(sparkle(4), with: .color(spark))
                }
            default: break
            }
        }
    }
}
