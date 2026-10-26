import ActivityKit
import Foundation

/// The app's side of the call's Live Activity (drawn by the VisionActivity extension): started with
/// the call, told each change of state, ended with it.  Needs "Live Activities" allowed for Vision in
/// Settings; without it the call simply runs without one.
@MainActor
final class CallLiveActivity {
    private var activity: Activity<VisionCallAttributes>?
    private var shown: VisionCallAttributes.ContentState?

    /// `call`: false for a spoken reply outside a call.  iOS only lets an activity start while the app
    /// is in front, so a reply that begins with the phone locked plays without one.
    func start(_ state: VisionCallAttributes.ContentState, call: Bool = true) {
        guard activity == nil, ActivityAuthorizationInfo().areActivitiesEnabled else { return }
        Self.endLeftovers()
        do {
            activity = try Activity.request(
                attributes: VisionCallAttributes(startedAt: .now, call: call),
                content: ActivityContent(state: state, staleDate: nil)
            )
            shown = state
        } catch {
            print("live activity: \(error)")
        }
    }

    func update(_ state: VisionCallAttributes.ContentState) {
        guard let activity, state != shown else { return }
        shown = state
        Task { await activity.update(ActivityContent(state: state, staleDate: nil)) }
    }

    func end() {
        guard let activity else { return }
        self.activity = nil
        shown = nil
        Task { await activity.end(nil, dismissalPolicy: .immediate) }
    }

    /// A call cut off by a force quit or crash leaves its activity behind; clear it on the way back in.
    static func endLeftovers() {
        for activity in Activity<VisionCallAttributes>.activities {
            Task { await activity.end(nil, dismissalPolicy: .immediate) }
        }
    }
}

/// The app's side of the project threads' card (VisionThreadsAttributes, drawn by the VisionActivity
/// extension). From launch on it hands the machine the tokens Apple needs to start and change the card
/// with the app shut; while the app is open it starts, changes and ends the card itself as the machine says.
@MainActor
final class ThreadsLiveActivity {
    static let shared = ThreadsLiveActivity()
    private var watching = false
    private var followed: Set<String> = []
    /// After the last thread stops, the "all done" card stays this long (the machine's LINGER_S).
    static let linger: TimeInterval = 15 * 60

    /// `register(kind, token)`: "start" for the token that lets the machine start a card, "card" for each
    /// card's own, which changes and ends it.
    func watch(register: @escaping (String, String) -> Void) {
        guard !watching else { return }
        watching = true
        Task {
            for await token in Activity<VisionThreadsAttributes>.pushToStartTokenUpdates {
                register("start", Self.hex(token))
            }
        }
        Task {
            for await activity in Activity<VisionThreadsAttributes>.activityUpdates {
                follow(activity, register)
            }
        }
        for activity in Activity<VisionThreadsAttributes>.activities {
            follow(activity, register)
        }
    }

    /// The tokens Apple has already given, handed over again: Apple only sends a token anew when it changes, so
    /// on every connect, a machine that missed one (or restarted, or wants this phone's key with it) has them.
    func reregister(_ register: (String, String) -> Void) {
        if let token = Activity<VisionThreadsAttributes>.pushToStartToken { register("start", Self.hex(token)) }
        // Only a card still up: an ended one lingers in the list, and its token would pass for a live card.
        for activity in Activity<VisionThreadsAttributes>.activities where [.active, .stale].contains(activity.activityState) {
            if let token = activity.pushToken { register("card", Self.hex(token)) }
        }
    }

    private func follow(_ activity: Activity<VisionThreadsAttributes>, _ register: @escaping (String, String) -> Void) {
        guard followed.insert(activity.id).inserted else { return }
        Task {
            for await token in activity.pushTokenUpdates {
                register("card", Self.hex(token))
            }
        }
    }

    /// What the machine says the card shows (hello, or a "threads_live" frame).
    func apply(_ state: VisionThreadsAttributes.ContentState) {
        let current = Activity<VisionThreadsAttributes>.activities.first { $0.activityState == .active }
        let content = ActivityContent(state: state, staleDate: nil)
        if state.active {
            if let current {
                Task { await current.update(content) }
            } else if ActivityAuthorizationInfo().areActivitiesEnabled {
                do {
                    _ = try Activity.request(attributes: VisionThreadsAttributes(), content: content, pushType: .token)
                } catch {
                    // No push here (a simulator without it, say): the card still runs while the app is open.
                    _ = try? Activity.request(attributes: VisionThreadsAttributes(), content: content, pushType: nil)
                }
            }
        } else if let current {
            Task { await current.end(content, dismissalPolicy: .after(.now.addingTimeInterval(Self.linger))) }
        }
    }

    private static func hex(_ data: Data) -> String {
        data.map { String(format: "%02x", $0) }.joined()
    }
}
