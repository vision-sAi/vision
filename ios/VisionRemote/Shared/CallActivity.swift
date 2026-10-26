import ActivityKit
import AppIntents
import Foundation

/// Vision's Live Activity: the Dynamic Island and Lock Screen show a live voice call (whether it is
/// hearing you, thinking or speaking, with mute and hang-up) or a spoken reply (with stop), so the
/// app can sit in the background.  Compiled into the app and the widget extension; the widget draws
/// it, the app starts, updates and ends it.
struct VisionCallAttributes: ActivityAttributes {
    enum Mood: String, Codable, Hashable {
        case starting, listening, hearing, thinking, speaking, muted, error
    }

    struct ContentState: Codable, Hashable {
        var mood: Mood
        /// A word or two: "Listening", "Thinking", or what went wrong.
        var caption: String
        var muted: Bool
    }

    var startedAt: Date
    /// A live voice call; false for a spoken reply outside one (no timer, no mute, stop instead of hang-up).
    var call = true
}

/// Where the island's buttons land.  The intents run in the app's process (that is what a
/// `LiveActivityIntent` is), and the app sets these.  Unset in the widget.
@MainActor
enum CallRemote {
    static var toggleMute: (() -> Void)?
    static var end: (() -> Void)?
    static var stopSpeaking: (() -> Void)?
}

struct StopSpeakingIntent: LiveActivityIntent {
    static let title: LocalizedStringResource = "Stop Vision speaking"
    static let isDiscoverable = false

    init() {}

    @MainActor
    func perform() async throws -> some IntentResult {
        CallRemote.stopSpeaking?()
        return .result()
    }
}

struct ToggleCallMuteIntent: LiveActivityIntent {
    static let title: LocalizedStringResource = "Mute or unmute Vision"
    static let isDiscoverable = false

    init() {}

    @MainActor
    func perform() async throws -> some IntentResult {
        CallRemote.toggleMute?()
        return .result()
    }
}

struct EndCallIntent: LiveActivityIntent {
    static let title: LocalizedStringResource = "End voice chat with Vision"
    static let isDiscoverable = false

    init() {}

    @MainActor
    func perform() async throws -> some IntentResult {
        CallRemote.end?()
        return .result()
    }
}

/// The project threads' card on the Lock Screen and in the Dynamic Island: how many are working and how
/// many need you, with the first few by name. The machine works out what it shows (vision/liveactivity.py)
/// and starts, changes and ends it through Apple, so it follows the threads with the app shut; while the
/// app is open it keeps the card in step itself (ThreadsLiveActivity). Through Apple the names come sealed
/// (`enc`, opened with PushKey as the card is drawn), so only the counts pass readable.
struct VisionThreadsAttributes: ActivityAttributes {
    struct Row: Codable, Hashable {
        var title: String
        /// "waiting_on_you", "working", "landing" or "ready_for_review".
        var state: String
    }

    struct ContentState: Codable, Hashable {
        /// The rows as they are: from the open app, which hears them over the paired connection.
        var rows: [Row]
        var working: Int
        var needsYou: Int
        var ready: Int
        /// The rows sealed for this phone: from a push, which carries them through Apple.
        var enc: String?

        /// What the card lists: the rows, or the sealed ones opened; none when they can't be (a phone that
        /// has since re-paired, say), and the counts carry the card.
        var shownRows: [Row] {
            guard rows.isEmpty, let enc, let plain = PushKey.unseal(enc) else { return rows }
            return (try? JSONDecoder().decode([Row].self, from: plain)) ?? []
        }

        /// Something is working or waiting on you, so a card is wanted.
        var active: Bool { working > 0 || needsYou > 0 }

        /// "2 working · 1 needs you", "All done · 1 to review".
        var headline: String {
            ready > 0 ? "\(shortHeadline) · \(ready) to review" : shortHeadline
        }

        /// The headline without what is ready to review, for where it doesn't fit (the rows say it anyway).
        var shortHeadline: String {
            var parts: [String] = []
            if working > 0 { parts.append("\(working) working") }
            if needsYou > 0 { parts.append("\(needsYou) need\(needsYou == 1 ? "s" : "") you") }
            return parts.isEmpty ? "All done" : parts.joined(separator: " · ")
        }
    }
}
