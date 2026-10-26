import SwiftUI
import UIKit

/// Pip's face.  The same six the CLI draws (`vision/buddy.py`); `chore` and `tool` stay on the machine,
/// where there is a rim to fill and a command line to scan.
enum PipMood: Equatable {
    case idle, thinking, listening, speaking, error, asleep
}

/// Pip, drawn the way the terminal draws him: a 16 × 4 grid of character cells, graphite shell,
/// recessed face, jade eyes — the geometry, palette and rhythms of `vision/buddy.py`, in shapes
/// rather than glyphs so he stays crisp at any size.  One character across both screens.
struct PipView: View {
    var mood: PipMood
    /// 0…1 microphone level; the listening face turns it into a waveform, exactly as the CLI does.
    var level: Double = 0
    /// One terminal cell's width in points.  A cell is twice as tall as it is wide, and the capsule
    /// occupies ten cells across and three down, so the view measures 10 × 6 of these.
    var cell: CGFloat = 7
    var animated = true

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var moving: Bool { animated && !reduceMotion }

    var body: some View {
        Group {
            if moving {
                // 20 fps: the sprite changes on cell boundaries, so nothing is gained by drawing faster.
                TimelineView(.periodic(from: .now, by: 1.0 / 20.0)) { tick in
                    face(at: tick.date.timeIntervalSinceReferenceDate)
                }
            } else {
                face(at: 0)
            }
        }
        .frame(width: cell * 11, height: cell * 6)
        .overlay(alignment: .topTrailing) {
            // The sleep mark sits outside the shell, up and to the right, as it does in the gutter.
            if mood == .asleep {
                Text("z")
                    .font(.system(size: cell * 1.6, weight: .medium, design: .monospaced))
                    .foregroundStyle(PipPalette.asleepAccent)
                    .offset(x: cell * 1.2, y: -cell * 0.9)
            }
        }
        .accessibilityElement()
        .accessibilityLabel(label)
    }

    private func face(at seconds: TimeInterval) -> some View {
        Canvas(opaque: false, rendersAsynchronously: false) { context, _ in
            for ink in PipSprite.ink(mood: mood, seconds: moving ? seconds : 0, level: level, cell: cell) {
                switch ink.shape {
                case .rect(let r):
                    context.fill(Path(r), with: .color(ink.color))
                case .ellipse(let r):
                    context.fill(Path(ellipseIn: r), with: .color(ink.color))
                case .line(let a, let b, let width):
                    var path = Path()
                    path.move(to: a)
                    path.addLine(to: b)
                    context.stroke(path, with: .color(ink.color), style: StrokeStyle(lineWidth: width, lineCap: .round))
                }
            }
        }
    }

    private var label: String {
        switch mood {
        case .idle: return "Pip, waiting"
        case .thinking: return "Pip, thinking"
        case .listening: return "Pip, listening"
        case .speaking: return "Pip, speaking"
        case .error: return "Pip, something went wrong"
        case .asleep: return "Pip, asleep"
        }
    }
}

// MARK: - the sprite

enum PipPalette {
    static let shell = Color(pip: 0x899a90)
    static let lower = Color(pip: 0x4c6054)
    static let face = Color(pip: 0x202d26)
    static let eye = Color(pip: 0xc1ebd1)
    static let asleepShell = Color(pip: 0x4f6255)
    static let asleepAccent = Color(pip: 0x738378)
    static let errorAccent = Color(pip: 0xd8a191)
}

/// One piece of Pip, already placed in the view's own coordinates.
struct PipInk {
    enum Shape {
        case rect(CGRect)
        case ellipse(CGRect)
        case line(CGPoint, CGPoint, CGFloat)
    }

    let shape: Shape
    let color: Color
}

/// The cell layout, straight from `vision/buddy.py`: the face well runs from column 3 to 11, the
/// rails stand half a cell outside it at 2 and 12, the eyes sit at 5 and 9, the mouth at 7.
enum PipSprite {
    private static let faceX0 = 3.0, faceSpan = 9.0
    /// The recessed well runs a half cell wider than the shell bars at each side, and the rails
    /// stand inside it — the same cells `vision/buddy.py` paints with `FACE_BG`.
    private static let wellX0 = 2.0, wellSpan = 11.0
    private static let leftEye = 5.0, rightEye = 9.0, mouth = 7.0
    private static let railLeft = 2.5, railRight = 12.0

    private static let blinkPeriod = 4.0, blinkDuration = 0.14
    private static let glancePeriod = 7.0
    private static let animationStep = 0.48

    static func ink(mood: PipMood, seconds: TimeInterval, level: Double, cell: CGFloat) -> [PipInk] {
        let w = Double(cell), h = Double(cell) * 2
        // Cropped to Pip: the well's left edge and the bottom of row 0 become the origin.
        let ox = -wellX0 * w, oy = -0.5 * h

        func box(_ x: Double, _ y: Double, _ width: Double, _ height: Double) -> CGRect {
            CGRect(x: ox + x * w, y: oy + y * h, width: width * w, height: height * h)
        }

        let asleep = mood == .asleep
        let shell = asleep ? PipPalette.asleepShell : PipPalette.shell
        let lower = asleep ? PipPalette.asleepShell : PipPalette.lower
        let accent: Color = mood == .error ? PipPalette.errorAccent : asleep ? PipPalette.asleepAccent : PipPalette.eye

        var ink: [PipInk] = [
            // The recessed well first, then the shell around it.
            PipInk(shape: .rect(box(wellX0, 1, wellSpan, 2)), color: PipPalette.face),
            PipInk(shape: .rect(box(faceX0, 0.5, faceSpan, 0.5)), color: shell),
            PipInk(shape: .rect(box(faceX0, 3, faceSpan, 0.5)), color: lower),
        ]

        // The rails: two cells tall each, and only the upper cell answers the microphone.
        var railTint = shell
        if mood == .listening {
            let breath = 0.2 + 0.3 * (1 - cos(seconds / 3 * .pi * 2)) / 2
            railTint = mix(shell, PipPalette.eye, max(breath, level))
        }
        for x in [railLeft, railRight] {
            ink.append(PipInk(shape: .rect(box(x, 1, 0.5, 1)), color: railTint))
            ink.append(PipInk(shape: .rect(box(x, 2, 0.5, 1)), color: shell))
        }

        // The eyes.  Idle is the only face that blinks or lets them drift.
        var look = 0.0
        var blinking = false
        if mood == .idle {
            look = glance(seconds)
            blinking = seconds.truncatingRemainder(dividingBy: blinkPeriod) < blinkDuration
        }
        for x in [leftEye, rightEye] {
            switch mood {
            case .error:
                let falling = x == leftEye
                let a = CGPoint(x: ox + (x + (falling ? 0.15 : 0.85)) * w, y: oy + 1.72 * h)
                let b = CGPoint(x: ox + (x + (falling ? 0.85 : 0.15)) * w, y: oy + 1.28 * h)
                ink.append(PipInk(shape: .line(a, b, CGFloat(w * 0.22)), color: accent))
            case .asleep:
                ink.append(PipInk(shape: .rect(box(x, 1.44, 1, 0.12)), color: accent))
            default:
                if blinking {
                    ink.append(PipInk(shape: .rect(box(x, 1.44, 1, 0.12)), color: accent))
                } else {
                    ink.append(PipInk(shape: .rect(box(x + look + 0.08, 1.38, 0.84, 0.24)), color: accent))
                }
            }
        }

        // The mouth, which is where every state says what it is doing.
        switch mood {
        case .idle:
            ink.append(PipInk(shape: .rect(box(mouth, 2.4, 1, 0.18)), color: accent))
        case .asleep:
            ink.append(PipInk(shape: .rect(box(mouth + 0.36, 2.44, 0.28, 0.14)), color: accent))
        case .error:
            ink.append(PipInk(shape: .rect(box(mouth, 2.42, 1, 0.16)), color: accent))
        case .speaking:
            // Open and half-open on the CLI's beat.
            let open = Int(seconds / animationStep) % 2 == 0
            let height = open ? 0.5 : 0.24
            ink.append(PipInk(shape: .ellipse(box(mouth + 0.18, 2.5 - height / 2, 0.64, height)), color: accent))
        case .thinking:
            // Three dots trading a soft highlight, one pass every 2.4 seconds.
            let phase = seconds.truncatingRemainder(dividingBy: 2.4) / 0.8
            for (index, x) in [mouth - 2, mouth, mouth + 2].enumerated() {
                let gap = abs(phase - Double(index))
                let distance = min(gap, 3 - gap)
                let colour = mix(Color(pip: 0x5b6558), Color(pip: 0xdbcaa5), max(0, 1 - distance))
                ink.append(PipInk(shape: .rect(box(x + 0.35, 2.42, 0.3, 0.16)), color: colour))
            }
        case .listening:
            if level > 0.05 {
                // Five bars under the eyes, the loudest in the middle, growing out of the cell floor.
                for (index, weight) in [0.35, 0.7, 1.0, 0.7, 0.35].enumerated() {
                    let step = min(5.0, (level * weight * 5).rounded(.down))
                    let fill = (step + 1) / 8
                    ink.append(PipInk(shape: .rect(box(mouth - 2 + Double(index), 3 - fill, 1, fill)), color: accent))
                }
            } else {
                ink.append(PipInk(shape: .rect(box(mouth, 2.4, 1, 0.18)), color: accent))
            }
        }
        return ink
    }

    /// Idle only: the eyes drift one cell right, later one cell left, and settle again.
    private static func glance(_ seconds: TimeInterval) -> Double {
        let phase = seconds.truncatingRemainder(dividingBy: glancePeriod)
        if phase > 2.2 && phase < 2.9 { return 1 }
        if phase > 4.8 && phase < 5.4 { return -1 }
        return 0
    }

    /// The CLI's sixteen flat steps, so a tinted rail matches the terminal's.
    private static func mix(_ a: Color, _ b: Color, _ amount: Double) -> Color {
        let step = (max(0, min(1, amount)) * 16 + 0.5).rounded(.down) / 16
        let (ar, ag, ab) = a.pipComponents, (br, bg, bb) = b.pipComponents
        return Color(red: ar + (br - ar) * step, green: ag + (bg - ag) * step, blue: ab + (bb - ab) * step)
    }
}

extension Color {
    /// The CLI's palette is written as hex in `vision/buddy.py`; keep it in that form here too.
    init(pip hex: UInt32) {
        self.init(red: Double((hex >> 16) & 0xff) / 255,
                  green: Double((hex >> 8) & 0xff) / 255,
                  blue: Double(hex & 0xff) / 255)
    }

    fileprivate var pipComponents: (Double, Double, Double) {
        let parts = UIColor(self).cgColor.components ?? [0, 0, 0, 1]
        guard parts.count >= 3 else { return (Double(parts.first ?? 0), Double(parts.first ?? 0), Double(parts.first ?? 0)) }
        return (Double(parts[0]), Double(parts[1]), Double(parts[2]))
    }
}
