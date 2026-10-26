import ActivityKit
import AppIntents
import SwiftUI
import WidgetKit

@main
struct VisionActivityBundle: WidgetBundle {
    var body: some Widget {
        VisionCallLiveActivity()
        VisionThreadsLiveActivity()
        ProjectsWidget()
        UsageWidget()
        NextUpWidget()
        RecentChatsWidget()
        ComputerWidget()
        MessageWidget()
        MessageControl()
    }
}

/// The project threads at a glance: on the Lock Screen, the counts and the first few by name with where each
/// stands; in the Dynamic Island, a mark and a count (orange when something needs you), the names when held.
struct VisionThreadsLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: VisionThreadsAttributes.self) { context in
            LockScreenThreads(state: context.state)
                .activityBackgroundTint(PipPalette.face.opacity(0.92))
                .activitySystemActionForegroundColor(PipPalette.eye)
        } dynamicIsland: { context in
            let state = context.state
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Label("Threads", systemImage: "square.stack.3d.up.fill")
                        .font(.headline)
                        .foregroundStyle(PipPalette.eye)
                        .padding(.leading, 4)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Text(state.shortHeadline)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .padding(.trailing, 4)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    ThreadRows(rows: Array(state.shownRows.prefix(3)))
                        .padding(.horizontal, 4)
                }
            } compactLeading: {
                Image(systemName: "square.stack.3d.up.fill")
                    .foregroundStyle(state.needsYou > 0 ? Color.orange : PipPalette.eye)
            } compactTrailing: {
                Text(state.needsYou > 0 ? "\(state.needsYou)!" : "\(state.working)")
                    .font(.caption.weight(.bold))
                    .monospacedDigit()
                    .foregroundStyle(state.needsYou > 0 ? Color.orange : PipPalette.eye)
            } minimal: {
                Image(systemName: "square.stack.3d.up.fill")
                    .foregroundStyle(state.needsYou > 0 ? Color.orange : PipPalette.eye)
            }
            .keylineTint(PipPalette.eye)
        }
    }
}

private struct LockScreenThreads: View {
    var state: VisionThreadsAttributes.ContentState

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label("Threads", systemImage: "square.stack.3d.up.fill")
                    .font(.headline)
                    .foregroundStyle(.white)
                Spacer()
                ViewThatFits(in: .horizontal) {
                    Text(state.headline)
                    Text(state.shortHeadline)
                }
                .font(.subheadline.weight(.medium))
                .foregroundStyle(state.needsYou > 0 ? Color.orange : PipPalette.eye)
                .lineLimit(1)
            }
            ThreadRows(rows: state.shownRows)
        }
        .padding(16)
    }
}

/// One line per thread: a dot in its state's colour, its name, and where it stands.
private struct ThreadRows: View {
    var rows: [VisionThreadsAttributes.Row]

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                HStack(spacing: 8) {
                    Circle().fill(Self.color(row.state)).frame(width: 7, height: 7)
                    Text(row.title)
                        .foregroundStyle(.white)
                        .lineLimit(1)
                    Spacer(minLength: 8)
                    Text(Self.label(row.state))
                        .foregroundStyle(Self.color(row.state))
                }
                .font(.subheadline)
            }
        }
    }

    static func label(_ state: String) -> String {
        switch state {
        case "waiting_on_you": return "Needs you"
        case "working": return "Working"
        case "landing": return "Landing"
        case "ready_for_review": return "To review"
        default: return state
        }
    }

    static func color(_ state: String) -> Color {
        switch state {
        case "waiting_on_you": return .orange
        case "working", "landing": return PipPalette.eye
        case "ready_for_review": return .green
        default: return .secondary
        }
    }
}

/// Vision in the Dynamic Island and on the Lock Screen.  A call shows Pip, a mark for who is talking
/// (a waveform while it hears you, a speaker while Vision speaks), a call timer, and mute + hang-up;
/// a spoken reply shows the speaker and a stop button.  Both work without opening the app.
struct VisionCallLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: VisionCallAttributes.self) { context in
            LockScreenCall(state: context.state, attributes: context.attributes)
                .activityBackgroundTint(PipPalette.face.opacity(0.92))
                .activitySystemActionForegroundColor(PipPalette.eye)
        } dynamicIsland: { context in
            let state = context.state
            let attributes = context.attributes
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    HStack(spacing: 8) {
                        CallPip(mood: state.mood, cell: 3)
                        Text("Vision").font(.headline)
                    }
                    .padding(.leading, 4)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    if attributes.call {
                        CallTimer(startedAt: attributes.startedAt)
                            .font(.headline)
                            .foregroundStyle(.secondary)
                            .padding(.trailing, 4)
                    }
                }
                DynamicIslandExpandedRegion(.bottom) {
                    HStack(spacing: 8) {
                        StateMark(mood: state.mood)
                            .font(.subheadline.weight(.semibold))
                        Text(state.caption)
                            .font(.subheadline.weight(.medium))
                            .foregroundStyle(moodColor(state.mood))
                            .lineLimit(1)
                        Spacer()
                        CallButtons(call: attributes.call, muted: state.muted)
                    }
                    .padding(.horizontal, 4)
                }
            } compactLeading: {
                CallPip(mood: state.mood, cell: 2.2)
            } compactTrailing: {
                // Who is talking, at a glance; between turns a call shows its timer.
                if state.mood.shown || !attributes.call {
                    StateMark(mood: state.mood)
                        .font(.caption.weight(.bold))
                } else {
                    CallTimer(startedAt: attributes.startedAt)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(moodColor(state.mood))
                        .frame(width: 40)
                }
            } minimal: {
                StateMark(mood: state.mood)
                    .font(.caption.weight(.bold))
            }
            .keylineTint(PipPalette.eye)
        }
    }
}

private struct LockScreenCall: View {
    var state: VisionCallAttributes.ContentState
    var attributes: VisionCallAttributes

    var body: some View {
        HStack(spacing: 12) {
            CallPip(mood: state.mood, cell: 4)
            VStack(alignment: .leading, spacing: 2) {
                Text("Vision").font(.headline).foregroundStyle(.white)
                HStack(spacing: 6) {
                    StateMark(mood: state.mood)
                    Text(state.caption).foregroundStyle(moodColor(state.mood))
                    if attributes.call {
                        Text("·").foregroundStyle(.white.opacity(0.5))
                        CallTimer(startedAt: attributes.startedAt).foregroundStyle(.white.opacity(0.7))
                    }
                }
                .font(.subheadline)
                .lineLimit(1)
            }
            Spacer()
            CallButtons(call: attributes.call, muted: state.muted)
        }
        .padding(16)
    }
}

private struct CallButtons: View {
    var call: Bool
    var muted: Bool

    var body: some View {
        HStack(spacing: 10) {
            if call {
                Button(intent: ToggleCallMuteIntent()) {
                    Image(systemName: muted ? "mic.slash.fill" : "mic.fill")
                        .font(.system(size: 16, weight: .semibold))
                        .foregroundStyle(muted ? Color.red : Color.white)
                        .frame(width: 40, height: 40)
                        .background(Color.white.opacity(0.16), in: Circle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(muted ? "Unmute" : "Mute")
                Button(intent: EndCallIntent()) {
                    Image(systemName: "phone.down.fill")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(.white)
                        .frame(width: 40, height: 40)
                        .background(Color.red, in: Circle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("End voice chat")
            } else {
                Button(intent: StopSpeakingIntent()) {
                    Image(systemName: "stop.fill")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(.white)
                        .frame(width: 40, height: 40)
                        .background(Color.white.opacity(0.16), in: Circle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Stop speaking")
            }
        }
    }
}

/// Pip, still: widgets draw one frame, so he holds the face for the state rather than animating it.
/// Hearing you gives him a tall waveform, plain listening a flat one.
private struct CallPip: View {
    var mood: VisionCallAttributes.Mood
    var cell: CGFloat

    var body: some View {
        PipView(mood: pipMood, level: mood == .hearing ? 0.9 : mood == .listening ? 0.12 : 0, cell: cell, animated: false)
    }

    private var pipMood: PipMood {
        switch mood {
        case .starting, .muted: return .idle
        case .listening, .hearing: return .listening
        case .thinking: return .thinking
        case .speaking: return .speaking
        case .error: return .error
        }
    }
}

/// One symbol for the state: whose voice it is right now, or why neither.
private struct StateMark: View {
    var mood: VisionCallAttributes.Mood

    var body: some View {
        Image(systemName: symbol)
            .foregroundStyle(moodColor(mood))
            .accessibilityHidden(true)
    }

    private var symbol: String {
        switch mood {
        case .starting: return "circle.dotted"
        case .listening: return "mic.fill"
        case .hearing: return "waveform"
        case .thinking: return "ellipsis"
        case .speaking: return "speaker.wave.2.fill"
        case .muted: return "mic.slash.fill"
        case .error: return "exclamationmark.triangle.fill"
        }
    }
}

/// Counts up from the start of the call, the way the phone app's own call timer does.
private struct CallTimer: View {
    var startedAt: Date

    var body: some View {
        Text(timerInterval: startedAt...Date.distantFuture, countsDown: false)
            .monospacedDigit()
            .multilineTextAlignment(.trailing)
    }
}

private extension VisionCallAttributes.Mood {
    /// States worth the compact island's corner over the timer.
    var shown: Bool {
        switch self {
        case .hearing, .thinking, .speaking, .muted, .error: return true
        case .starting, .listening: return false
        }
    }
}

private func moodColor(_ mood: VisionCallAttributes.Mood) -> Color {
    switch mood {
    case .muted, .error: return PipPalette.errorAccent
    case .starting, .listening, .thinking: return .secondary
    case .hearing, .speaking: return PipPalette.eye
    }
}
