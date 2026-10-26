import AppIntents
import Foundation
import Security
import SwiftUI
import UserNotifications

/// What the Home Screen widgets show, as the computer's `GET /widgets` answers it: the project threads, the
/// subscriptions' usage, and what is next on the schedule. Shared by the app (it hands the widgets its pairing,
/// and draws these views on its `--open widgets` screen) and VisionActivity, where the widgets live.
struct Glance: Codable, Equatable {
    struct Threads: Codable, Equatable {
        var working: Int
        var needsYou: Int
        var ready: Int
        var rows: [Row]
        var projects: Int
    }

    struct Row: Codable, Equatable, Hashable {
        var title: String
        var state: String
        var project: String
        var projectTitle: String
    }

    struct Provider: Codable, Equatable {
        var provider: String
        var label: String
        var windows: [Window]
    }

    struct Window: Codable, Equatable {
        var label: String
        var usedPercent: Double?
        var resetsAt: Double?
    }

    struct Next: Codable, Equatable, Hashable {
        var id: String
        var title: String
        var kind: String
        var at: Double
    }

    struct Chat: Codable, Equatable, Hashable {
        var chat: String
        var title: String
        var busy: Bool
        var last: String
        var updated: Double
    }

    /// A scout as the Scouts widget draws it: its buddy, how many things need you, one line on where it stands.
    struct Scout: Codable, Equatable, Hashable {
        var id: String
        var name: String
        var hue: Double
        var trait: String
        var mood: String
        var needs: Int
        var line: String
        var nextCheckin: Double?
    }

    struct Computer: Codable, Equatable {
        var name: String
        var cpu: Int?
        var memory: Int?
        var disk: Int?
        var uptime: Double?
        var voiceOn: Bool
        var update: String?
    }

    var name: String
    var at: Double
    var threads: Threads
    var usage: [Provider]
    var next: Next?
    // Newer than the first widgets: optional, so an answer without them still reads.
    var upcoming: [Next]?
    var chats: [Chat]?
    var computer: Computer?
    var scouts: [Scout]?

    static func decode(_ data: Data) throws -> Glance {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(Glance.self, from: data)
    }

    func encoded() -> Data? {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        return try? encoder.encode(self)
    }
}

/// Where a widget's tap lands in the app (ChatView.open(link:)).
enum GlanceLink {
    static let compose = URL(string: "vision://compose")!
    static let usage = URL(string: "vision://usage")!
    static let scheduled = URL(string: "vision://scheduled")!
    static let projects = URL(string: "vision://projects")!
    static let chats = URL(string: "vision://chats")!
    static func project(_ id: String) -> URL { URL(string: "vision://project/\(id)") ?? projects }
    static func chat(_ id: String) -> URL { URL(string: "vision://chat/\(id)") ?? chats }
}

/// The app's pairing (the computer's addresses, main one first, and the key), copied where the widgets can read
/// it: the keychain group the app shares with its extensions (VisionKeychainGroup in each Info.plist, as PushKey).
/// Readable after the first unlock, so a widget refreshing on a locked phone still can.
enum SharedPairing {
    struct Pairing: Codable, Equatable {
        var addresses: [String]
        var token: String
    }

    private static let service = "vision.pairing"
    private static var group: String? { Bundle.main.object(forInfoDictionaryKey: "VisionKeychainGroup") as? String }

    private static var query: [String: Any] {
        var q: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                kSecAttrService as String: service,
                                kSecAttrAccount as String: "current"]
        if let group, !group.isEmpty { q[kSecAttrAccessGroup as String] = group }
        return q
    }

    static func load() -> Pairing? {
        var q = query
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &item) == errSecSuccess, let data = item as? Data else { return nil }
        return try? JSONDecoder().decode(Pairing.self, from: data)
    }

    /// Kept in step with the app's pairing; nil (unpaired) takes it away.
    static func save(_ pairing: Pairing?) {
        guard pairing != load() else { return }
        SecItemDelete(query as CFDictionary)
        guard let pairing, !pairing.token.isEmpty, !pairing.addresses.isEmpty,
              let data = try? JSONEncoder().encode(pairing) else { return }
        var q = query
        q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        q[kSecValueData as String] = data
        SecItemAdd(q as CFDictionary, nil)
    }
}

enum GlanceError: Error {
    case notPaired, outdated, unreachable
}

/// GET /widgets from the computer: its main address first, its Wi-Fi one when the main one can't be reached.
enum GlanceFetch {
    static func fetch(_ pairing: SharedPairing.Pairing?) async throws -> Glance {
        guard let pairing, !pairing.token.isEmpty else { throw GlanceError.notPaired }
        for address in pairing.addresses {
            guard let base = URL(string: address) else { continue }
            var req = URLRequest(url: base.appendingPathComponent("widgets"))
            req.timeoutInterval = 15
            req.setValue("Bearer \(pairing.token)", forHTTPHeaderField: "Authorization")
            guard let (data, response) = try? await URLSession.shared.data(for: req),
                  let http = response as? HTTPURLResponse else { continue }
            if http.statusCode == 404 { throw GlanceError.outdated }   // a Vision from before the widgets
            if http.statusCode == 401 { throw GlanceError.notPaired }
            if (200..<300).contains(http.statusCode) { return try Glance.decode(data) }
        }
        throw GlanceError.unreachable
    }
}

/// A scout's request decided, or a finished thread landed, from a notification's buttons: straight
/// to the computer with the shared pairing, so it works without the app on screen.
enum ScoutActions {
    static let approval = "approval"
    static let land = "land"
    static let approve = "approve"
    static let deny = "deny"
    static let landIt = "land_it"

    /// The categories the app registers: Approve / Deny on a request, Land on work ready for review.
    static var categories: Set<UNNotificationCategory> {
        let approve = UNNotificationAction(identifier: Self.approve, title: "Approve", options: [.authenticationRequired])
        let deny = UNNotificationAction(identifier: Self.deny, title: "Deny", options: [.destructive, .authenticationRequired])
        let land = UNNotificationAction(identifier: Self.landIt, title: "Land", options: [.authenticationRequired])
        return [UNNotificationCategory(identifier: Self.approval, actions: [approve, deny], intentIdentifiers: []),
                UNNotificationCategory(identifier: Self.land, actions: [land], intentIdentifiers: [])]
    }

    /// Carry out a button's action; nil when done, else what went wrong in a few words.
    static func perform(_ action: String, info: [AnyHashable: Any]) async -> String? {
        let project = info["project"] as? String ?? ""
        let path: String
        var body: [String: Any] = [:]
        switch action {
        case Self.approve, Self.deny:
            guard let id = info["approval"] as? String, !project.isEmpty else { return "nothing to decide" }
            path = "projects/\(project)/approvals/\(id)"
            body["decision"] = action == Self.approve ? "approve" : "deny"
        case Self.landIt:
            guard let thread = info["thread"] as? String, !project.isEmpty else { return "nothing to land" }
            path = "projects/\(project)/threads/\(thread)/land"
        default:
            return nil
        }
        return await post(path, json: body)
    }

    /// POST to the computer, its main address first; nil on success, else why not.
    static func post(_ path: String, json: [String: Any]) async -> String? {
        guard let pairing = SharedPairing.load(), !pairing.token.isEmpty else { return "this phone isn't paired" }
        var why = "the computer can't be reached"
        for address in pairing.addresses {
            guard let base = URL(string: address) else { continue }
            var req = URLRequest(url: base.appendingPathComponent(path))
            req.httpMethod = "POST"
            req.timeoutInterval = 20
            req.setValue("Bearer \(pairing.token)", forHTTPHeaderField: "Authorization")
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: json)
            guard let (data, response) = try? await URLSession.shared.data(for: req), let http = response as? HTTPURLResponse else { continue }
            if (200..<300).contains(http.statusCode) { return nil }
            why = ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any])?["detail"] as? String ?? "it answered \(http.statusCode)"
            break
        }
        return why
    }
}

/// "Message Vision" from the Lock Screen or Control Center: the app comes forward with the keyboard out. Runs in
/// the app; on a cold launch the chat screen isn't up yet, so it leaves `pending` set as well as posting.
struct MessageVisionIntent: AppIntent {
    static let title: LocalizedStringResource = "Message Vision"
    static let description = IntentDescription("Open Vision ready to type a message.")
    static let openAppWhenRun = true

    @MainActor
    func perform() async throws -> some IntentResult {
        ComposeRequest.pending = true
        NotificationCenter.default.post(name: ComposeRequest.now, object: nil)
        return .result()
    }
}

enum ComposeRequest {
    @MainActor static var pending = false
    static let now = Notification.Name("VisionComposeNow")
}

// MARK: - the widgets' faces

/// The sizes the faces come in: the Home Screen's small and medium, and the Lock Screen's (round, a strip of
/// a few lines, and the line above the clock).
enum GlanceSize {
    case small, medium, lockScreen, round, inline
}

/// What a widget has to say instead of numbers, or under old ones.
enum GlanceProblem: Equatable {
    case notPaired, outdated, offline

    var text: String {
        switch self {
        case .notPaired: return "Open Vision and pair it with your computer."
        case .outdated: return "Update Vision on your computer: run vision upgrade."
        case .offline: return "Can't reach your computer."
        }
    }
}

private extension Glance.Row {
    var colour: Color {
        switch state {
        case "waiting_on_you": return .orange
        case "ready_for_review": return .green
        default: return .blue
        }
    }
}

/// "as of 2:40", under numbers the widget couldn't refresh.
private struct AsOf: View {
    let glance: Glance
    let problem: GlanceProblem?

    var body: some View {
        if problem != nil {
            HStack(spacing: 3) {
                Image(systemName: "wifi.slash")
                Text("as of \(Date(timeIntervalSince1970: glance.at), style: .time)")
            }
            .font(.caption2)
            .foregroundStyle(.secondary)
        }
    }
}

private struct ProblemView: View {
    let title: String
    let symbol: String
    let problem: GlanceProblem

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label(title, systemImage: symbol).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            Spacer(minLength: 0)
            Text(problem.text).font(.footnote).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

/// Project threads: how many are working, need you or are ready, and the first few by name.
struct ProjectsGlanceView: View {
    let glance: Glance?
    let problem: GlanceProblem?
    let size: GlanceSize

    var body: some View {
        if size == .lockScreen || size == .inline {
            let line = glance.map { Self.line($0.threads) } ?? "Vision"
            if size == .inline {
                Label(line, systemImage: "sparkles")
            } else {
                VStack(alignment: .leading, spacing: 1) {
                    Label("Scouts", systemImage: "sparkles").font(.headline)
                    Text(line).font(.caption).lineLimit(2)
                    if let first = glance?.threads.rows.first, size == .lockScreen {
                        Text(first.title).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        } else if let glance, let scouts = glance.scouts, !scouts.isEmpty {
            ScoutsGlance(scouts: scouts, size: size).overlay(alignment: .bottomTrailing) { AsOf(glance: glance, problem: problem) }
        } else if let glance {
            content(glance.threads).overlay(alignment: .bottomTrailing) { AsOf(glance: glance, problem: problem) }
        } else {
            ProblemView(title: "Scouts", symbol: "sparkles", problem: problem ?? .offline)
        }
    }

    /// "1 needs you · 2 working", or "All quiet".
    static func line(_ t: Glance.Threads) -> String {
        let parts = [(t.needsYou, t.needsYou == 1 ? "needs you" : "need you"), (t.working, "working"), (t.ready, "ready")]
            .filter { $0.0 > 0 }.map { "\($0.0) \($0.1)" }
        return parts.isEmpty ? "All quiet" : parts.joined(separator: " · ")
    }

    @ViewBuilder
    private func content(_ t: Glance.Threads) -> some View {
        let counts = VStack(alignment: .leading, spacing: 4) {
            Label("Projects", systemImage: "square.stack.3d.up").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            Spacer(minLength: 0)
            if t.working + t.needsYou + t.ready == 0 {
                Text(t.projects == 0 ? "No projects" : "All quiet").font(.headline)
                Text(t.projects == 0 ? "Start one in the app" : "Nothing running").font(.caption).foregroundStyle(.secondary)
            } else {
                count(t.needsYou, "need you", .orange)
                count(t.working, "working", .blue)
                count(t.ready, "ready", .green)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        if size == .medium && !t.rows.isEmpty {
            HStack(alignment: .top, spacing: 14) {
                counts.frame(width: 104)
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(t.rows.prefix(3), id: \.self) { row in
                        Link(destination: GlanceLink.project(row.project)) {
                            HStack(alignment: .firstTextBaseline, spacing: 6) {
                                Circle().fill(row.colour).frame(width: 7, height: 7)
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(row.title).font(.footnote.weight(.medium)).foregroundStyle(.primary).lineLimit(1)
                                    Text(row.projectTitle).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                                }
                            }
                        }
                        .tint(.primary)   // a link's label would take the accent colour
                    }
                    Spacer(minLength: 0)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        } else {
            counts
        }
    }

    @ViewBuilder
    private func count(_ n: Int, _ label: String, _ colour: Color) -> some View {
        if n > 0 {
            HStack(alignment: .firstTextBaseline, spacing: 5) {
                Text("\(n)").font(.title2.weight(.semibold)).foregroundStyle(colour).monospacedDigit()
                Text(label).font(.subheadline).foregroundStyle(.secondary)
            }
        }
    }

    /// Where a tap on the whole widget goes: the scout (or project) most in need of a look, or the list.
    static func link(_ glance: Glance?) -> URL {
        if let scout = glance?.scouts?.first { return GlanceLink.project(scout.id) }
        return glance?.threads.rows.first.map { GlanceLink.project($0.project) } ?? GlanceLink.projects
    }
}

/// The Scouts widget from a Vision with scouts: small, the one most in need of you, big; medium, up to four side by
/// side, each a link to its scout. Their buddies hold still (a widget doesn't animate).
struct ScoutsGlance: View {
    let scouts: [Glance.Scout]
    let size: GlanceSize

    var body: some View {
        let needs = scouts.reduce(0) { $0 + $1.needs }
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Label("Scouts", systemImage: "sparkles").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                Spacer(minLength: 0)
                if needs > 0 {
                    Text("\(needs) need\(needs == 1 ? "s" : "") you").font(.caption2.weight(.semibold)).foregroundStyle(.orange)
                }
            }
            if size == .small, let first = scouts.first {
                Spacer(minLength: 0)
                HStack(spacing: 8) {
                    BuddyView(hue: first.hue, trait: first.trait, mood: first.mood, size: 56, animated: false)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(first.name).font(.headline).lineLimit(1)
                        Text(first.line).font(.caption).foregroundStyle(first.needs > 0 ? .orange : .secondary).lineLimit(2)
                    }
                }
                Spacer(minLength: 0)
            } else {
                HStack(alignment: .top, spacing: 6) {
                    ForEach(scouts.prefix(4), id: \.self) { scout in
                        Link(destination: GlanceLink.project(scout.id)) {
                            VStack(spacing: 2) {
                                BuddyView(hue: scout.hue, trait: scout.trait, mood: scout.mood, size: 52, animated: false)
                                Text(scout.name).font(.caption.weight(.semibold)).foregroundStyle(.primary).lineLimit(1)
                                Text(scout.line).font(.caption2).foregroundStyle(scout.needs > 0 ? .orange : .secondary).lineLimit(1)
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .tint(.primary)
                    }
                }
                Spacer(minLength: 0)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

/// The subscriptions' usage: each provider's windows ("Session 9%, resets 5:19 PM"), as the Usage screen has them.
struct UsageGlanceView: View {
    let glance: Glance?
    let problem: GlanceProblem?
    let size: GlanceSize

    var body: some View {
        if size == .round {
            let window = glance?.usage.first?.windows.first
            Gauge(value: min(100, max(0, window?.usedPercent ?? 0)), in: 0...100) {
                Image(systemName: "gauge.with.dots.needle.50percent")
            } currentValueLabel: {
                Text(window?.usedPercent.map { "\(Int($0.rounded()))" } ?? "–")
            }
            .gaugeStyle(.accessoryCircular)
            .accessibilityLabel("\(glance?.usage.first?.label ?? "Usage") \(Self.short(window?.label ?? "session")) used")
        } else if let glance, !glance.usage.isEmpty {
            VStack(alignment: .leading, spacing: size == .small ? 8 : 6) {
                Label("Usage", systemImage: "gauge.with.dots.needle.50percent").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                Spacer(minLength: 0)
                if size == .small, let first = glance.usage.first {
                    provider(first, windows: 2)
                } else {
                    HStack(alignment: .top, spacing: 14) {
                        ForEach(glance.usage.prefix(3), id: \.provider) { provider($0, windows: 2) }
                    }
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .overlay(alignment: .topTrailing) { AsOf(glance: glance, problem: problem) }
        } else if glance != nil {
            ProblemView(title: "Usage", symbol: "gauge.with.dots.needle.50percent", problem: problem ?? .offline)
                .overlay { if problem == nil { Text("No usage to show").font(.footnote).foregroundStyle(.secondary) } }
        } else {
            ProblemView(title: "Usage", symbol: "gauge.with.dots.needle.50percent", problem: problem ?? .offline)
        }
    }

    private func provider(_ p: Glance.Provider, windows: Int) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(p.label).font(.footnote.weight(.semibold))
            ForEach(Array(p.windows.prefix(windows).enumerated()), id: \.offset) { _, w in
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 4) {
                        Text(Self.short(w.label)).font(.caption2).foregroundStyle(.secondary)
                        Spacer(minLength: 2)
                        Text(w.usedPercent.map { "\(Int($0.rounded()))%" } ?? "?").font(.caption2.weight(.semibold)).monospacedDigit()
                    }
                    ProgressView(value: min(100, max(0, w.usedPercent ?? 0)), total: 100)
                        .tint(Self.colour(w.usedPercent ?? 0))
                    if let resets = w.resetsAt {
                        Text("resets \(Self.when(resets))").font(.system(size: 9)).foregroundStyle(.tertiary).lineLimit(1)
                    }
                }
            }
        }
    }

    /// "Current session" → "Session", as there's no room for the rest.
    static func short(_ label: String) -> String {
        label.hasPrefix("Current ") ? String(label.dropFirst("Current ".count)).capitalized : label
    }

    static func colour(_ pct: Double) -> Color { pct < 60 ? .green : pct < 85 ? .yellow : .red }

    /// A reset today by its time, a later one by its day too.
    static func when(_ ts: Double) -> String {
        let date = Date(timeIntervalSince1970: ts)
        let f = DateFormatter()
        f.dateFormat = Calendar.current.isDateInToday(date) ? "h:mm a" : "EEE h:mm a"
        return f.string(from: date)
    }
}

/// The next reminder or scheduled task, and when it goes off.
struct NextUpGlanceView: View {
    let glance: Glance?
    let problem: GlanceProblem?
    let size: GlanceSize

    var body: some View {
        if let glance {
            if size == .medium, let upcoming = glance.upcoming, upcoming.count > 1 {
                VStack(alignment: .leading, spacing: 7) {
                    Label("Next up", systemImage: "clock").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    Spacer(minLength: 0)
                    ForEach(upcoming.prefix(3), id: \.self) { item in
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Image(systemName: item.kind == "remind" ? "bell" : "clock").font(.caption2).foregroundStyle(.secondary)
                            Text(item.title).font(.footnote.weight(.medium)).lineLimit(1)
                            Spacer(minLength: 4)
                            Text(Self.when(Date(timeIntervalSince1970: item.at))).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                        }
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                .overlay(alignment: .topTrailing) { AsOf(glance: glance, problem: problem) }
            } else if let next = glance.next {
                let date = Date(timeIntervalSince1970: next.at)
                let symbol = next.kind == "remind" ? "bell" : "clock"
                if size == .lockScreen {
                    VStack(alignment: .leading, spacing: 1) {
                        Label { Text(date, style: .time) } icon: { Image(systemName: symbol) }.font(.headline)
                        Text(next.title).font(.caption).lineLimit(2)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                } else {
                    VStack(alignment: .leading, spacing: 4) {
                        Label("Next up", systemImage: symbol).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                        Spacer(minLength: 0)
                        Text(next.title).font(.subheadline.weight(.semibold)).lineLimit(3)
                        Text(Self.when(date)).font(.caption).foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                    .overlay(alignment: .topTrailing) { AsOf(glance: glance, problem: problem) }
                }
            } else if size == .lockScreen {
                Label("Nothing scheduled", systemImage: "clock").font(.caption)
            } else {
                VStack(alignment: .leading, spacing: 4) {
                    Label("Next up", systemImage: "clock").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    Spacer(minLength: 0)
                    Text("Nothing scheduled").font(.subheadline.weight(.semibold))
                    Text("Ask Vision to remind you").font(.caption).foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            }
        } else if size == .lockScreen {
            Label("Vision", systemImage: "clock").font(.caption)
        } else {
            ProblemView(title: "Next up", symbol: "clock", problem: problem ?? .offline)
        }
    }

    /// "Today 6:00 PM", "Tomorrow 9:00 AM", "Mon 9:00 AM".
    static func when(_ date: Date) -> String {
        let f = DateFormatter()
        f.dateFormat = "h:mm a"
        let time = f.string(from: date)
        if Calendar.current.isDateInToday(date) { return "Today \(time)" }
        if Calendar.current.isDateInTomorrow(date) { return "Tomorrow \(time)" }
        f.dateFormat = "EEE h:mm a"
        return f.string(from: date)
    }
}

/// The chats last spoken in, each with the opening of its latest reply, and a dot on one still replying.
struct RecentChatsGlanceView: View {
    let glance: Glance?
    let problem: GlanceProblem?
    let size: GlanceSize

    var body: some View {
        if let glance, let chats = glance.chats {
            VStack(alignment: .leading, spacing: size == .small ? 4 : 7) {
                Label("Chats", systemImage: "bubble.left.and.bubble.right").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                Spacer(minLength: 0)
                if chats.isEmpty {
                    Text("No chats open").font(.subheadline.weight(.semibold))
                } else if size == .small, let chat = chats.first {
                    row(chat, lines: 3)
                } else {
                    ForEach(chats.prefix(3), id: \.self) { chat in
                        Link(destination: GlanceLink.chat(chat.chat)) { row(chat, lines: 1) }
                            .tint(.primary)   // a link's label would take the accent colour
                    }
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .overlay(alignment: .topTrailing) { AsOf(glance: glance, problem: problem) }
        } else if glance != nil {
            ProblemView(title: "Chats", symbol: "bubble.left.and.bubble.right", problem: .outdated)
        } else {
            ProblemView(title: "Chats", symbol: "bubble.left.and.bubble.right", problem: problem ?? .offline)
        }
    }

    private func row(_ chat: Glance.Chat, lines: Int) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            HStack(spacing: 5) {
                if chat.busy { Circle().fill(.blue).frame(width: 6, height: 6) }
                Text(chat.title).font(.footnote.weight(.semibold)).foregroundStyle(.primary).lineLimit(1)
            }
            Text(chat.busy ? "Replying…" : chat.last.isEmpty ? " " : chat.last)
                .font(.caption).foregroundStyle(.secondary).lineLimit(lines)
        }
    }

    /// Where a tap on the small one goes: its chat.
    static func link(_ glance: Glance?) -> URL {
        glance?.chats?.first.map { GlanceLink.chat($0.chat) } ?? GlanceLink.chats
    }
}

/// The computer Vision runs on: reachable or not, how busy, the voice switch, and an update when one is out.
struct ComputerGlanceView: View {
    let glance: Glance?
    let problem: GlanceProblem?
    let size: GlanceSize

    var body: some View {
        if let glance, let c = glance.computer {
            VStack(alignment: .leading, spacing: 5) {
                HStack(spacing: 5) {
                    Circle().fill(problem == nil ? Color.green : Color.red).frame(width: 7, height: 7)
                    Text(c.name).font(.caption.weight(.semibold)).lineLimit(1)
                    Spacer(minLength: 0)
                    if let up = c.uptime, problem == nil { Text("up \(Self.uptime(up))").font(.caption2).foregroundStyle(.secondary) }
                }
                Spacer(minLength: 0)
                meter("CPU", c.cpu)
                meter("Memory", c.memory)
                meter("Disk", c.disk)
                Spacer(minLength: 0)
                if problem != nil {
                    AsOf(glance: glance, problem: problem)
                } else if c.update != nil {
                    Label("Update available", systemImage: "arrow.down.circle").font(.caption2).foregroundStyle(.orange)
                } else {
                    Label(c.voiceOn ? "Voice on" : "Voice off", systemImage: c.voiceOn ? "waveform" : "waveform.slash")
                        .font(.caption2).foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        } else if glance != nil {
            ProblemView(title: "Computer", symbol: "desktopcomputer", problem: .outdated)
        } else {
            ProblemView(title: "Computer", symbol: "desktopcomputer", problem: problem ?? .offline)
        }
    }

    @ViewBuilder
    private func meter(_ label: String, _ pct: Int?) -> some View {
        if let pct {
            VStack(alignment: .leading, spacing: 2) {
                HStack {
                    Text(label).font(.caption2).foregroundStyle(.secondary)
                    Spacer(minLength: 2)
                    Text("\(pct)%").font(.caption2.weight(.semibold)).monospacedDigit()
                }
                ProgressView(value: Double(min(100, max(0, pct))), total: 100).tint(UsageGlanceView.colour(Double(pct)))
            }
        }
    }

    /// "3 d", "5 h", "12 min".
    static func uptime(_ seconds: Double) -> String {
        if seconds >= 86400 { return "\(Int(seconds / 86400)) d" }
        if seconds >= 3600 { return "\(Int(seconds / 3600)) h" }
        return "\(max(1, Int(seconds / 60))) min"
    }
}
