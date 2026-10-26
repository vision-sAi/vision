import AppIntents
import SwiftUI
import WidgetKit

/// The Home Screen and Lock Screen widgets: Projects, Usage and Next up ask the computer (GET /widgets, through
/// the pairing the app shares) about every quarter of an hour, and again whenever the app has been used (it
/// reloads them). Couldn't reach it, they show the last answer with "as of". Message Vision is a button.
struct GlanceEntry: TimelineEntry {
    let date: Date
    let glance: Glance?
    let problem: GlanceProblem?
}

struct GlanceProvider: TimelineProvider {
    static let every: TimeInterval = 15 * 60
    private static let lastKey = "glance.last"   // the extension's own defaults: the last answer, for when it can't ask

    func placeholder(in context: Context) -> GlanceEntry {
        GlanceEntry(date: .now, glance: Self.sample, problem: nil)
    }

    func getSnapshot(in context: Context, completion: @escaping (GlanceEntry) -> Void) {
        if context.isPreview {
            completion(GlanceEntry(date: .now, glance: Self.sample, problem: nil))
            return
        }
        Task { completion(await Self.entry()) }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<GlanceEntry>) -> Void) {
        Task {
            let entry = await Self.entry()
            completion(Timeline(entries: [entry], policy: .after(.now.addingTimeInterval(Self.every))))
        }
    }

    static func entry() async -> GlanceEntry {
        let last = UserDefaults.standard.data(forKey: lastKey).flatMap { try? Glance.decode($0) }
        do {
            let glance = try await GlanceFetch.fetch(SharedPairing.load())
            UserDefaults.standard.set(glance.encoded(), forKey: lastKey)
            return GlanceEntry(date: .now, glance: glance, problem: nil)
        } catch GlanceError.notPaired {
            return GlanceEntry(date: .now, glance: nil, problem: .notPaired)
        } catch GlanceError.outdated {
            return GlanceEntry(date: .now, glance: nil, problem: .outdated)
        } catch {
            return GlanceEntry(date: .now, glance: last, problem: .offline)
        }
    }

    /// The widget gallery's preview: made-up threads and usage, so it looks like something.
    static let sample = Glance(
        name: "computer", at: Date().timeIntervalSince1970,
        threads: .init(working: 2, needsYou: 1, ready: 0, rows: [
            .init(title: "Pick a database for sync", state: "waiting_on_you", project: "", projectTitle: "Sync"),
            .init(title: "Fix the flaky server test", state: "working", project: "", projectTitle: "Vision"),
            .init(title: "Write the setup guide", state: "working", project: "", projectTitle: "Vision"),
        ], projects: 2),
        usage: [.init(provider: "claude", label: "Claude", windows: [
            .init(label: "Current session", usedPercent: 24, resetsAt: Date().addingTimeInterval(3 * 3600).timeIntervalSince1970),
            .init(label: "Current week", usedPercent: 41, resetsAt: Date().addingTimeInterval(4 * 86400).timeIntervalSince1970),
        ])],
        next: .init(id: "", title: "Call the dentist", kind: "remind", at: Date().addingTimeInterval(2 * 3600).timeIntervalSince1970),
        upcoming: [.init(id: "1", title: "Call the dentist", kind: "remind", at: Date().addingTimeInterval(2 * 3600).timeIntervalSince1970),
                   .init(id: "2", title: "Check the nightly build", kind: "task", at: Date().addingTimeInterval(14 * 3600).timeIntervalSince1970)],
        chats: [.init(chat: "", title: "Loop bug", busy: false, last: "The return was inside the loop. Moved it out.", updated: 0),
                .init(chat: "", title: "Trip plan", busy: true, last: "", updated: 0)],
        computer: .init(name: "computer", cpu: 12, memory: 48, disk: 63, uptime: 3 * 86400, voiceOn: true, update: nil),
        scouts: [.init(id: "", name: "Atlas", hue: 205, trait: "antenna", mood: "needs_you", needs: 2, line: "2 need you"),
                 .init(id: "", name: "Juniper", hue: 32, trait: "sprout", mood: "focused", needs: 0, line: "working"),
                 .init(id: "", name: "Wren", hue: 268, trait: "ears", mood: "asleep", needs: 0, line: "resting"),
                 .init(id: "", name: "Moss", hue: 140, trait: "curl", mood: "proud", needs: 0, line: "resting")])
}

struct ProjectsWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "VisionProjects", provider: GlanceProvider()) { entry in
            ProjectsWidgetView(entry: entry)
                .containerBackground(.fill.tertiary, for: .widget)
        }
        .configurationDisplayName("Scouts")
        .description("Your scouts: what they're doing, and what needs you.")
        .supportedFamilies([.systemSmall, .systemMedium, .accessoryRectangular, .accessoryInline])
    }
}

/// Small, a tap opens the project most in need of a look; medium lists threads by name, each a link to its project.
private struct ProjectsWidgetView: View {
    let entry: GlanceEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        ProjectsGlanceView(glance: entry.glance, problem: entry.problem, size: size)
            .widgetURL(family == .systemMedium ? GlanceLink.projects : ProjectsGlanceView.link(entry.glance))
    }

    private var size: GlanceSize {
        switch family {
        case .systemSmall: return .small
        case .accessoryRectangular: return .lockScreen
        case .accessoryInline: return .inline
        default: return .medium
        }
    }
}

struct UsageWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "VisionUsage", provider: GlanceProvider()) { entry in
            UsageWidgetView(entry: entry)
                .widgetURL(GlanceLink.usage)
                .containerBackground(.fill.tertiary, for: .widget)
        }
        .configurationDisplayName("Usage")
        .description("How much of your Claude, Codex and Grok limits you've used.")
        .supportedFamilies([.systemSmall, .systemMedium, .accessoryCircular])
    }
}

private struct UsageWidgetView: View {
    let entry: GlanceEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        UsageGlanceView(glance: entry.glance, problem: entry.problem,
                        size: family == .systemSmall ? .small : family == .accessoryCircular ? .round : .medium)
    }
}

struct NextUpWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "VisionNextUp", provider: GlanceProvider()) { entry in
            NextUpWidgetView(entry: entry)
                .widgetURL(GlanceLink.scheduled)
                .containerBackground(.fill.tertiary, for: .widget)
        }
        .configurationDisplayName("Next Up")
        .description("Your next reminders and scheduled tasks.")
        .supportedFamilies([.systemSmall, .systemMedium, .accessoryRectangular])
    }
}

private struct NextUpWidgetView: View {
    let entry: GlanceEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        NextUpGlanceView(glance: entry.glance, problem: entry.problem,
                         size: family == .accessoryRectangular ? .lockScreen : family == .systemMedium ? .medium : .small)
    }
}

struct RecentChatsWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "VisionChats", provider: GlanceProvider()) { entry in
            RecentChatsWidgetView(entry: entry)
                .containerBackground(.fill.tertiary, for: .widget)
        }
        .configurationDisplayName("Recent Chats")
        .description("Your latest chats and how each last replied.")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}

private struct RecentChatsWidgetView: View {
    let entry: GlanceEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        RecentChatsGlanceView(glance: entry.glance, problem: entry.problem, size: family == .systemSmall ? .small : .medium)
            .widgetURL(family == .systemSmall ? RecentChatsGlanceView.link(entry.glance) : GlanceLink.chats)
    }
}

struct ComputerWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "VisionComputer", provider: GlanceProvider()) { entry in
            ComputerGlanceView(glance: entry.glance, problem: entry.problem, size: .small)
                .containerBackground(.fill.tertiary, for: .widget)
        }
        .configurationDisplayName("Computer")
        .description("Whether Vision's computer is reachable, and how busy it is.")
        .supportedFamilies([.systemSmall])
    }
}

/// Message Vision on the Lock Screen: a round button that opens the app with the keyboard up.
struct MessageWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "VisionMessage", provider: MessageProvider()) { _ in
            ZStack {
                AccessoryWidgetBackground()
                Image(systemName: "square.and.pencil").font(.title3.weight(.semibold))
            }
            .widgetURL(GlanceLink.compose)
            .containerBackground(.clear, for: .widget)
            .accessibilityLabel("Message Vision")
        }
        .configurationDisplayName("Message Vision")
        .description("Open Vision ready to type.")
        .supportedFamilies([.accessoryCircular])
    }
}

/// The button has nothing to refresh.
struct MessageProvider: TimelineProvider {
    func placeholder(in context: Context) -> SimpleEntry { SimpleEntry(date: .now) }
    func getSnapshot(in context: Context, completion: @escaping (SimpleEntry) -> Void) { completion(SimpleEntry(date: .now)) }
    func getTimeline(in context: Context, completion: @escaping (Timeline<SimpleEntry>) -> Void) {
        completion(Timeline(entries: [SimpleEntry(date: .now)], policy: .never))
    }

    struct SimpleEntry: TimelineEntry { let date: Date }
}

/// Message Vision in Control Center, on the Lock Screen's buttons, or the Action button.
struct MessageControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: "VisionMessageControl") {
            ControlWidgetButton(action: MessageVisionIntent()) {
                Label("Message Vision", systemImage: "square.and.pencil")
            }
        }
        .displayName("Message Vision")
        .description("Open Vision ready to type.")
    }
}
