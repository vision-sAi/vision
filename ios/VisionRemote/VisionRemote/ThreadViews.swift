import SwiftUI

/// A project's chat carries its title between the chat's top buttons: the project's name with a status mark,
/// and under it how many threads are open and whether any needs you. A tap opens the project: its threads by
/// state, its Library and memory, its settings; a row opens one thread, and its own field messages it.
struct ThreadStrip: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.theme) private var theme
    /// The project whose screen is up (and the thread to open on it, from a tapped notification). The sheet is
    /// driven by it, not a flag beside it, which the sheet's closure would read stale.
    @State private var listed: Listed?

    private struct Listed: Identifiable {
        let project: String
        let thread: String?
        var id: String { project + "|" + (thread ?? "") }
    }

    var body: some View {
        let project = client.activeProject
        let counts = project.map(client.threadCounts) ?? [:]
        let open = counts.filter { $0.key != "resolved" }.values.reduce(0, +)
        ZStack(alignment: .top) {
            // The sheet hangs off this, not the pill.
            Color.clear.frame(height: 0).allowsHitTesting(false)
            if let project {
                pill(project, counts: counts, open: open) { listed = Listed(project: project.id, thread: nil) }
                .transition(.opacity)
            }
        }
        .animation(.snappy(duration: 0.25), value: project?.id)
        .sheet(item: $listed) { listed in
            ProjectSheet(projectID: listed.project, openThread: listed.thread)
                .presentationDetents([.large])
                .presentationDragIndicator(.hidden)
        }
        // A tapped notification about one of this project's threads: open it as soon as the chat is the project's.
        .task(id: "\(client.shownThread?.project ?? "")|\(client.shownThread?.thread ?? "")|\(project?.id ?? "")") {
            if let shown = client.shownThread, shown.project == project?.id {
                client.shownThread = nil
                listed = Listed(project: shown.project, thread: shown.thread.isEmpty ? nil : shown.thread)
            }
        }
    }

    /// The project's name with a status mark, as the inline subagents draw theirs (a pulsing sparkle while
    /// threads work, the accent when one needs you, a green tick when work waits for review), and under it,
    /// smaller, how many are open and what stands out. No capsule: it reads as the chat's title.
    private func pill(_ project: ProjectInfo, counts: [String: Int], open: Int, action: @escaping () -> Void) -> some View {
        let needs = (counts["waiting_on_you"] ?? 0) + project.approvals.count
        let working = counts["working"] ?? 0
        let review = counts["ready_for_review"] ?? 0
        var detail = ""
        if needs > 0 {
            detail = "\(needs) need\(needs == 1 ? "s" : "") you"
        } else if project.isPaused {
            detail = "paused"
        } else if working > 0 {
            detail = "\(working) working"
        } else if review > 0 {
            detail = "\(review) to review"
        }
        let label = open > 0 ? "\(open) thread\(open == 1 ? "" : "s")" + (detail.isEmpty ? "" : " · " + detail)
                             : (project.isPaused ? "Paused" : "No open threads")
        let busy = working > 0 && needs == 0 && !project.isPaused
        return Button(action: action) {
            VStack(spacing: 1) {
                HStack(spacing: 5) {
                    if project.scout.name.isEmpty {
                        mark(needs: needs, busy: busy, review: review, paused: project.isPaused)
                            .font(.system(size: 12, weight: .semibold))
                    } else {
                        ScoutBuddy(project: project, size: 22)
                    }
                    Text(project.name)
                        .themeFont(.subheadline, weight: .semibold)
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                    Image(systemName: "chevron.down")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundStyle(.tertiary)
                }
                Group {
                    if busy {
                        ScanText(text: label, scanning: true, live: false).id(label)
                    } else {
                        Text(label)
                    }
                }
                .themeFont(.caption)
                .foregroundStyle(needs > 0 ? theme.accent ?? Color.accentColor : Color.secondary)
                .lineLimit(1)
            }
            .frame(maxWidth: 230)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Threads")
        .accessibilityValue(label.replacingOccurrences(of: " · ", with: ", "))
        .accessibilityHint("Opens \(project.name)")
    }

    @ViewBuilder private func mark(needs: Int, busy: Bool, review: Int, paused: Bool) -> some View {
        if needs > 0 {
            Image(systemName: "questionmark.circle.fill").foregroundStyle(theme.accent ?? Color.accentColor)
        } else if paused {
            Image(systemName: "pause.circle.fill").foregroundStyle(.secondary)
        } else if busy {
            Image(systemName: "sparkle")
                .foregroundStyle(Color.cyan)
                .symbolEffect(.pulse, options: .repeating)
        } else if review > 0 {
            Image(systemName: "checkmark.circle.fill").foregroundStyle(Color.green)
        }
    }
}

/// A project on the phone, kept as plain as the Claude app's: three tabs in one segmented pill. Threads groups
/// them by state (Needs you, Working, Queued, Ready for review, Landing, Stopped, Closed), Library is what was
/// sent in it and what its threads made, Memory what it keeps in mind. The chat itself is under the sheet.
/// + sets a thread going; the other button opens its settings (goal, folder, usage, Pause, Archive).
struct ProjectSheet: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.theme) private var theme
    @Environment(\.dismiss) private var dismiss
    let projectID: String
    @State private var path: [String]
    @State private var tab: Tab = .threads
    @State private var starting = false
    @State private var editing = false
    @Namespace private var tabPill
    /// The project's Library, for the pictures shown on rows that wait for review.
    @State private var library: [LibraryItem] = []

    enum Tab: String, CaseIterable { case threads = "Threads", library = "Library", memory = "Memory" }

    /// Changes whenever a thread arrives at review or reruns, so the rows' pictures are fetched again.
    private var reviewKey: String {
        all.filter { $0.state == "ready_for_review" || $0.state == "landing" }
            .map { "\($0.id)|\($0.runID ?? "")" }.sorted().joined(separator: ",")
    }

    /// `openThread`: a thread to open straight away (a tapped notification).
    init(projectID: String, openThread: String? = nil) {
        self.projectID = projectID
        _path = State(initialValue: openThread.map { [$0] } ?? [])
    }

    private var project: ProjectInfo? { client.projects.first { $0.id == projectID } }
    private var all: [ProjectThread] { client.threads[projectID] ?? [] }

    /// The threads in each state, in the order the list shows them: what needs you first.
    private var groups: [(String, [ProjectThread])] {
        let order: [(String, (ProjectThread) -> Bool)] = [
            ("Needs you", { $0.state == "waiting_on_you" }),
            ("Working", { $0.isWorking && !$0.queued }),
            ("Queued", { $0.isWorking && $0.queued }),
            ("Ready for review", { $0.state == "ready_for_review" }),
            ("Landing", { $0.state == "landing" }),
            ("Stopped", { $0.state == "idle" }),
            ("Closed", { $0.state == "resolved" }),
        ]
        return order.compactMap { name, fits in
            let list = all.filter(fits).sorted { $0.updated > $1.updated }
            return list.isEmpty ? nil : (name, list)
        }
    }

    var body: some View {
        NavigationStack(path: $path) {
            VStack(spacing: 0) {
                if let project { scoutHeader(project) }
                tabs
                if project?.isPaused == true {
                    Text("Paused: nothing new starts")
                        .themeFont(.footnote)
                        .foregroundStyle(.secondary)
                        .padding(.bottom, 6)
                }
                switch tab {
                case .threads: threadList
                case .library: LibraryView(project: projectID)
                case .memory: ProjectMemoryView(projectID: projectID)
                }
            }
            .background {
                ZStack(alignment: .top) {
                    theme.backgroundColor
                    // Panes: the scout's header sits in a soft violet wash.
                    if theme.backdrop {
                        LinearGradient(colors: [(theme.accent ?? .accentColor).opacity(0.16), .clear], startPoint: .top, endPoint: .bottom)
                            .frame(height: 240)
                    }
                }
                .ignoresSafeArea()
            }
            .navigationTitle(project?.name ?? "Scout")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { dismiss() } label: { Image(systemName: "xmark") }
                        .accessibilityLabel("Close")
                }
                ToolbarItemGroup(placement: .topBarTrailing) {
                    Button { starting = true } label: { Image(systemName: "plus") }
                        .accessibilityLabel("New thread")
                        .disabled(client.connection != .online)
                    if project != nil {
                        Button { editing = true } label: { Image(systemName: "slider.horizontal.3") }
                            .accessibilityLabel("Project settings")
                    }
                }
            }
            .navigationDestination(for: String.self) { id in
                ThreadDetailView(projectID: projectID, threadID: id) { next in
                    // A message to a closed thread starts a new one that carries it on: show that instead.
                    if let i = path.lastIndex(of: id) { path[i] = next }
                }
            }
        }
        .task { await client.refreshThreads(project: projectID) }
        .task(id: reviewKey) {
            guard !reviewKey.isEmpty, let items = try? await client.fetchLibrary(project: projectID) else { return }
            library = items
        }
        .sheet(isPresented: $starting) {
            NewThreadView(projectID: projectID) { thread in
                starting = false
                tab = .threads
                path = [thread.id]
            }
            .presentationDetents([.medium, .large])
            .presentationDragIndicator(.visible)
        }
        .sheet(isPresented: $editing) {
            if let project {
                ProjectSettingsView(project: project) {
                    // Archived: the project's gone from the list, so its screen goes too.
                    editing = false
                    dismiss()
                }
                .presentationDetents([.large])
                .presentationDragIndicator(.hidden)
            }
        }
    }

    /// Threads · Library · Memory as one segmented pill, centred, as the Claude app's: a soft track with a
    /// lighter capsule sliding under the one showing.
    private var tabs: some View {
        HStack(spacing: 0) {
            ForEach(Tab.allCases, id: \.self) { t in
                Button {
                    withAnimation(.snappy(duration: 0.25)) { tab = t }
                } label: {
                    Text(t.rawValue)
                        .themeFont(.body, weight: .medium)
                        .foregroundStyle(tab == t ? Color.primary : Color.secondary)
                        .padding(.horizontal, 18)
                        .frame(height: 36)
                        .background {
                            if tab == t {
                                Capsule()
                                    .fill(Color(.tertiarySystemFill))
                                    .matchedGeometryEffect(id: "tab", in: tabPill)
                            }
                        }
                        .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("project-tab-\(t.rawValue)")
                .accessibilityAddTraits(tab == t ? [.isSelected] : [])
            }
        }
        .padding(3)
        .background(Capsule().fill(Color(.secondarySystemFill)))
        .padding(.top, 4)
        .padding(.bottom, 10)
    }

    /// The scout: its buddy (a tap pokes it), name and goal, its leash and next check-in, and Check in now.
    private func scoutHeader(_ project: ProjectInfo) -> some View {
        HStack(alignment: .center, spacing: 14) {
            ScoutBuddy(project: project, size: 60, moving: true, pokeable: true)
            VStack(alignment: .leading, spacing: 3) {
                Text(project.goal).themeFont(.subheadline).foregroundStyle(.secondary).lineLimit(2)
                HStack(spacing: 6) {
                    if let checkin = project.checkin, checkin.schedule?.enabled == true {
                        Text(checkin.words).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
            }
            Spacer(minLength: 0)
            Button(project.checkin?.running != nil ? "Reporting…" : "Status report") {
                Task { try? await client.checkInNow(projectID) }
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .disabled(project.checkin?.running != nil || project.isPaused || client.connection != .online)
        }
        .padding(.horizontal, 18)
        .padding(.bottom, 10)
    }

    @ViewBuilder private var threadList: some View {
        if let project, !project.approvals.isEmpty {
            // What it asked leave to run, above everything else: it waits on you.
            VStack(spacing: 8) {
                ForEach(project.approvals) { ApprovalCardView(project: project, approval: $0) }
            }
            .padding(.horizontal, 16)
            .padding(.bottom, 8)
        }
        if all.isEmpty {
            ProjectEmptyState(icon: "bubble.left.and.bubble.right", text: "No threads yet") {
                Button("New thread") { starting = true }
                    .buttonStyle(.bordered)
                    .buttonBorderShape(.capsule)
                    .disabled(client.connection != .online)
            }
        } else {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 22) {
                    ForEach(groups, id: \.0) { name, threads in
                        VStack(alignment: .leading, spacing: 8) {
                            HStack(spacing: 6) {
                                Text(name)
                                    .themeFont(.subheadline, weight: .semibold)
                                    .foregroundStyle(name == "Needs you" ? theme.accent ?? Color.accentColor : Color.primary)
                                Text("\(threads.count)")
                                    .themeFont(.subheadline)
                                    .monospacedDigit()
                                    .foregroundStyle(.secondary)
                            }
                            .padding(.leading, 4)
                            .accessibilityAddTraits(.isHeader)
                            // One card per group, the rows parted by hairlines, as the app's other sheets draw theirs.
                            VStack(spacing: 0) {
                                ForEach(Array(threads.enumerated()), id: \.element.id) { i, thread in
                                    if i > 0 {
                                        Divider().padding(.leading, 34)
                                    }
                                    NavigationLink(value: thread.id) {
                                        ThreadRow(thread: thread, pictures: thread.state == "ready_for_review" || thread.state == "landing"
                                                  ? ThreadPictures.made(by: thread.id, in: library) : [])
                                    }
                                    .buttonStyle(.plain)
                                }
                            }
                            .background(theme.surfaceColor, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                        }
                    }
                }
                .padding(.horizontal, 16)
                .padding(.top, 10)
                .padding(.bottom, 28)
            }
            .refreshable { await client.refreshThreads(project: projectID) }
        }
    }

}

/// One thread in the list: its mark, its title and one line of where it is. When it last moved sits on the right.
private struct ThreadRow: View {
    @Environment(\.theme) private var theme
    let thread: ProjectThread
    /// What it made, shown small under its status while it waits for review (the row opens the thread).
    var pictures: [LibraryItem] = []

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            ThreadMark(thread: thread)
                .padding(.top, 7)
            VStack(alignment: .leading, spacing: 3) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(thread.title.isEmpty ? thread.objective : thread.title)
                        .themeFont(.body, weight: .medium)
                        .foregroundStyle(thread.isOpen ? .primary : .secondary)
                        .lineLimit(1)
                    Spacer(minLength: 4)
                    if thread.updated > 0 {
                        Text(ThreadWords.ago(thread.updated))
                            .themeFont(.caption)
                            .foregroundStyle(.secondary)
                            .layoutPriority(1)
                    }
                }
                ThreadStatusLine(thread: thread)
                    .themeFont(.subheadline)
                    .lineLimit(2)
                if !pictures.isEmpty {
                    HStack(spacing: 6) {
                        ForEach(pictures.prefix(4)) { ThreadPictureTile(item: $0, side: 48) }
                        if pictures.count > 4 {
                            Text("+\(pictures.count - 4)")
                                .themeFont(.footnote, weight: .medium)
                                .foregroundStyle(.secondary)
                                .monospacedDigit()
                        }
                    }
                    .padding(.top, 6)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel("\(pictures.count) picture\(pictures.count == 1 ? "" : "s")")
                }
            }
            Image(systemName: "chevron.right")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(.tertiary)
                .padding(.top, 5)
                .accessibilityHidden(true)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 13)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }
}

/// The pictures and clips a thread made, in a row that scrolls sideways, in the order it made them; a tap
/// opens one full screen.
struct ThreadPictures: View {
    let items: [LibraryItem]
    var side: CGFloat = 112
    /// Taller than wide for screenshots; square when nil.
    var height: CGFloat? = nil
    let open: (LibraryItem) -> Void

    /// What the project's Library lists as made by this thread, files left out (they stay under Made).
    static func made(by thread: String, in library: [LibraryItem]) -> [LibraryItem] {
        library.filter { $0.thread == thread && $0.source == "thread" && $0.kind != "file" }
            .sorted { $0.time < $1.time }
    }

    /// What a picture shows: its caption, else its file name read as words ("login-google-dark.png" → "Login google dark").
    static func label(_ item: LibraryItem) -> String {
        if !item.caption.isEmpty { return item.caption }
        let words = (item.name as NSString).deletingPathExtension
            .replacingOccurrences(of: "[-_]+", with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespaces)
        return words.prefix(1).uppercased() + words.dropFirst()
    }

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(alignment: .top, spacing: 10) {
                ForEach(items) { item in
                    Button { open(item) } label: {
                        VStack(alignment: .leading, spacing: 6) {
                            ThreadPictureTile(item: item, side: side, height: height)
                            Text(Self.label(item))
                                .themeFont(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(2)
                                .multilineTextAlignment(.leading)
                                .frame(width: side, alignment: .leading)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .buttonStyle(.plain)
                    .contextMenu {
                        Button { UIPasteboard.general.string = item.path } label: {
                            Label("Copy path", systemImage: "doc.on.doc")
                        }
                    }
                    .accessibilityLabel(Self.label(item))
                }
            }
            .padding(.horizontal, 20)
        }
    }
}

/// One square thumbnail of a thread's picture (a clip's first frame, with a play badge).
struct ThreadPictureTile: View {
    @EnvironmentObject private var client: VisionClient
    let item: LibraryItem
    let side: CGFloat
    var height: CGFloat? = nil
    @State private var image: UIImage?

    var body: some View {
        Color(.secondarySystemFill)
            .frame(width: side, height: height ?? side)
            .overlay(alignment: .top) {
                if let image {
                    Image(uiImage: image).resizable().scaledToFill()
                        .frame(width: side, height: height ?? side, alignment: .top)
                        .transition(.opacity)
                }
            }
            .overlay(alignment: .bottomLeading) {
                if item.kind == "video" {
                    Image(systemName: "play.fill")
                        .font(.system(size: side < 64 ? 8 : 11, weight: .bold))
                        .foregroundStyle(.white)
                        .shadow(color: .black.opacity(0.5), radius: 3)
                        .padding(side < 64 ? 4 : 6)
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: side < 64 ? 8 : 12, style: .continuous))
            .task(id: item.path) {
                guard image == nil, let thumb = await client.thumbnail(path: item.path, size: height == nil ? 360 : 720) else { return }
                withAnimation(.easeOut(duration: 0.2)) { image = thumb }
            }
    }
}

/// Where a thread is, in one line: what it is doing (shimmering) while it works, Queued while it waits for a
/// slot, its question when it needs you, and what it came back with once it stopped.
private struct ThreadStatusLine: View {
    let thread: ProjectThread

    var body: some View {
        if thread.isWorking && thread.paused {
            Text("Paused with the project").foregroundStyle(.secondary)
        } else if (thread.isWorking && !thread.queued) || thread.state == "landing" {
            let doing = thread.state == "landing" ? "Merging into the project's folder" : ThreadWords.activity(thread)
            ScanText(text: doing, scanning: true, live: false)
                .foregroundStyle(.secondary)
                .id(doing)
        } else if thread.isWorking {
            Text("Queued").foregroundStyle(.secondary)
        } else if thread.isWaiting {
            Text(ThreadWords.question(thread) ?? "Needs you").foregroundStyle(.primary.opacity(0.8))
        } else if let error = thread.error, !error.isEmpty, thread.lastResult?.summary.isEmpty ?? true {
            Text(error).foregroundStyle(.red)
        } else {
            Text(ThreadWords.outcome(thread)).foregroundStyle(.secondary)
        }
    }
}

/// A thread's state as a small mark: the accent when it needs you, solid when it has something to review,
/// half-tone while it works, a ring while it waits for a slot or sits idle, faint once closed.
private struct ThreadMark: View {
    @Environment(\.theme) private var theme
    let thread: ProjectThread

    var body: some View {
        if theme.backdrop, let tone = PanesDot.tone(thread.state, queued: thread.queued) {
            PanesDot(color: tone).accessibilityHidden(true)
        } else {
            plain
        }
    }

    private var plain: some View {
        Group {
            switch thread.state {
            case "waiting_on_you": Circle().fill(theme.accent ?? Color.accentColor)
            case "working" where !thread.queued: Circle().fill(Color.primary.opacity(0.45))
            case "landing": Circle().fill((theme.accent ?? Color.accentColor).opacity(0.5))
            case "ready_for_review": Circle().fill(Color.primary.opacity(0.85))
            case "resolved": Circle().strokeBorder(Color.primary.opacity(0.2), lineWidth: 1.2)
            default: Circle().strokeBorder(Color.primary.opacity(0.45), lineWidth: 1.2)
            }
        }
        .frame(width: 8, height: 8)
        .accessibilityHidden(true)
    }
}

/// The words the thread views share.
private enum ThreadWords {
    /// Needs you first, then working, queued, to review, idle, closed.
    static func rank(_ t: ProjectThread) -> Int {
        switch t.state {
        case "waiting_on_you": 0
        case "working": t.queued ? 2 : 1
        case "ready_for_review": 3
        case "landing": 3
        case "idle": 4
        default: 5
        }
    }

    static func state(_ t: ProjectThread) -> String {
        switch t.state {
        case "working": t.paused ? "Paused" : t.queued ? "Queued" : "Working"
        case "waiting_on_you": "Needs you"
        case "ready_for_review": "Ready for review"
        case "landing": "Landing"
        case "idle": "Stopped"
        case "resolved": "Closed"
        default: t.state.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    /// The machine's plain words for what it is doing, sentence-cased ("Running tests").
    static func activity(_ t: ProjectThread) -> String {
        let words = t.activity.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let first = words.first else { return "Working" }
        return first.uppercased() + words.dropFirst()
    }

    static func question(_ t: ProjectThread) -> String? {
        [t.question, t.lastResult?.question].compactMap { $0 }.first { !$0.isEmpty }
    }

    static func outcome(_ t: ProjectThread) -> String {
        [t.lastResult?.summary, t.note, t.error].compactMap { $0 }.first { !$0.isEmpty } ?? state(t)
    }

    static func kind(_ kind: String) -> String {
        switch kind {
        case "start": "Started"
        case "continue": "Continued"
        case "answer": "Answered"
        case "resume": "Resumed"
        default: kind.capitalized
        }
    }

    /// A run's outcome as a word ("needs_input" → "asked you").
    static func runStatus(_ status: String) -> String {
        switch status {
        case "completed": "finished"
        case "needs_input", "waiting_for_user": "asked you"
        case "failed": "failed"
        case "timed_out": "timed out"
        case "stopped_at_limit": "hit its limit"
        default: status.replacingOccurrences(of: "_", with: " ")
        }
    }

    static func ago(_ at: Double) -> String {
        guard at > 0 else { return "" }
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .short
        return f.localizedString(for: Date(timeIntervalSince1970: at), relativeTo: Date())
    }

    static func duration(_ seconds: Double?) -> String? {
        guard let seconds, seconds > 0 else { return nil }
        let s = Int(seconds.rounded())
        if s < 60 { return "\(s)s" }
        if s < 3600 { return "\(s / 60)m \(s % 60)s" }
        return "\(s / 3600)h \(s % 3600 / 60)m"
    }

    /// A 409's own sentence (the machine says why it can't), else the app's usual explanation.
    static func explain(_ error: Error) -> String {
        if case ClientError.http(409, let detail) = error, !detail.isEmpty { return detail }
        return SettingsView.explain(error)
    }
}

/// One thread: what it came back with first (its lists folded), the pictures it made, what it was asked
/// (four lines until tapped), its earlier runs, and where and with what it works; its conversation opens in a
/// sheet. The field at the bottom answers it when it is waiting, and otherwise steers it or gives it more to do.
struct ThreadDetailView: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.theme) private var theme
    let projectID: String
    let threadID: String
    /// A message to a closed thread came back as a new thread carrying it on (its id): the screen moves to it.
    var carriedOn: (String) -> Void = { _ in }
    @State private var draft = ""
    @State private var sending = false
    @State private var error: String?
    @State private var confirmClose = false
    @State private var confirmLand = false
    @State private var landing = false
    @State private var showingConversation = false
    /// The pictures and clips it made, from the project's Library (empty until loaded, or when it made none).
    @State private var pictures: [LibraryItem] = []
    @State private var viewing: LibraryItem?
    /// What is folded open: the objective, the result's lists by title, runs by when they started.
    @State private var objectiveOpen = false
    @State private var openLists: Set<String> = []
    @State private var openRuns: Set<Double> = []
    @State private var allRuns = false
    /// The name moves into the bar once the page's own title has scrolled under it.
    @State private var titleInBar = false
    /// What landing it would bring in (nil until loaded, or when it has no branch).
    @State private var diff: ThreadDiff?
    @State private var allFiles = false
    @State private var openFile: ThreadDiff.File?
    /// Reviewing: the bottom bar is Land and Ask for changes until the latter swaps in the field.
    @State private var asking = false
    @FocusState private var fieldFocused: Bool

    private var thread: ProjectThread? { client.threads[projectID]?.first { $0.id == threadID } }

    var body: some View {
        Group {
            if let thread {
                ScrollView {
                    page(thread)
                        .padding(.horizontal, 20)
                        .padding(.top, 8)
                        .padding(.bottom, 20)
                }
                .scrollDismissesKeyboard(.interactively)
                .onScrollGeometryChange(for: Bool.self) { $0.contentOffset.y + $0.contentInsets.top > 44 } action: { _, past in
                    withAnimation(.easeOut(duration: 0.15)) { titleInBar = past }
                }
                .softScrollEdges()
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    if thread.canLand && !asking && draft.isEmpty { reviewBar(thread) } else { field(thread) }
                }
                .animation(.snappy(duration: 0.22), value: asking)
            } else {
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background(theme.backgroundColor.ignoresSafeArea())
        // The page carries the name and state; the bar takes the name only once that has scrolled away.
        .navigationTitle(titleInBar ? (thread.map { $0.title.isEmpty ? $0.objective : $0.title } ?? "") : "")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if let thread, thread.isOpen {
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        if thread.isWorking {
                            Button { act { try await client.stopThread(project: projectID, id: threadID) } } label: {
                                Label("Stop", systemImage: "stop")
                            }
                        }
                        if thread.canLand {
                            Button { confirmLand = true } label: {
                                Label("Land", systemImage: "arrow.triangle.merge")
                            }
                        }
                        Button(role: .destructive) { confirmClose = true } label: {
                            Label("Close thread", systemImage: "checkmark.circle")
                        }
                    } label: {
                        Image(systemName: "ellipsis")
                    }
                    .accessibilityLabel("More")
                    .disabled(client.connection != .online)
                }
            }
        }
        .confirmationDialog("Close this thread?", isPresented: $confirmClose, titleVisibility: .visible) {
            Button("Close thread", role: .destructive) {
                act { try await client.resolveThread(project: projectID, id: threadID) }
            }
        } message: {
            Text("It stops, if it is working, and leaves the project's open threads.")
        }
        .confirmationDialog("Land this thread?", isPresented: $confirmLand, titleVisibility: .visible) {
            Button("Land") { land() }
        } message: {
            Text("Its branch is merged into the project's folder and the thread closes. A conflict leaves everything as it was.")
        }
        // The full view carries the runs, which frames leave out: fetched on opening, and again as each run ends.
        .task(id: "\(thread?.state ?? "")|\(thread?.runID ?? "")") {
            _ = try? await client.fetchThread(project: projectID, id: threadID)
            if let items = try? await client.fetchLibrary(project: projectID) {
                pictures = ThreadPictures.made(by: threadID, in: items)
            }
            if let t = thread, !(t.branch ?? "").isEmpty, !t.isWorking {
                diff = try? await client.threadDiff(project: projectID, id: threadID)
            }
        }
        .sheet(item: $openFile) { file in
            ThreadDiffSheet(projectID: projectID, threadID: threadID, file: file)
        }
        .fullScreenCover(item: $viewing) { item in
            LibraryViewer(items: pictures, start: item) { gone in
                try await client.deleteLibraryItem(path: gone.path, project: projectID)
                pictures.removeAll { $0.path == gone.path }
            }
        }
        .sheet(isPresented: $showingConversation) {
            ThreadConversationSheet(projectID: projectID, threadID: threadID)
        }
    }

    @ViewBuilder private func page(_ t: ProjectThread) -> some View {
        VStack(alignment: .leading, spacing: 22) {
            // The name large, and under it one quiet line: its state, and when it last ran.
            VStack(alignment: .leading, spacing: 6) {
                Text(t.title.isEmpty ? t.objective : t.title)
                    .themeFont(.title2, weight: .bold)
                    .lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 7) {
                    ThreadMark(thread: t)
                    Text(ThreadWords.state(t))
                        .themeFont(.subheadline, weight: .medium)
                        .foregroundStyle(t.isWaiting ? theme.accent ?? Color.accentColor : Color.primary)
                    if let last = t.runs?.last, last.at > 0 {
                        Text("· \(ThreadWords.ago(last.at))")
                            .themeFont(.subheadline)
                            .foregroundStyle(.secondary)
                    }
                }
                .accessibilityElement(children: .combine)
            }

            if t.isWorking && !t.queued && !t.paused {
                // What it is doing this minute, live, and since when (its tool count is under Details).
                card {
                    VStack(alignment: .leading, spacing: 6) {
                        let doing = ThreadWords.activity(t)
                        ScanText(text: doing, scanning: true, live: false)
                            .themeFont(.body, weight: .medium)
                            .id(doing)
                        if t.created > 0 {
                            Text("Started \(ThreadWords.ago(t.created))")
                                .themeFont(.footnote)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }

            if t.isWaiting, let question = ThreadWords.question(t) {
                card {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("It asks")
                            .themeFont(.footnote, weight: .semibold)
                            .foregroundStyle(theme.accent ?? Color.accentColor)
                        Text(question)
                            .themeFont(.body)
                            .textSelection(.enabled)
                    }
                }
                .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .strokeBorder((theme.accent ?? Color.accentColor).opacity(0.35), lineWidth: 1))
            }

            if let error = t.error, !error.isEmpty {
                Text(error)
                    .themeFont(.callout)
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
            }

            // What there is to review, in the order you'd check it: what it says it did, what it looks like,
            // what it changed. Land and Ask for changes sit at the bottom.
            if let summary = t.lastResult?.summary, !summary.isEmpty {
                Text(summary)
                    .themeFont(.body)
                    .foregroundStyle(.primary.opacity(0.9))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }

            if !pictures.isEmpty {
                ThreadPictures(items: pictures, side: 132, height: 236) { viewing = $0 }
                    .padding(.horizontal, -20)   // runs to the screen's edges as it scrolls
            }

            if let diff, !diff.files.isEmpty {
                section("What changed", note: diffNote(diff)) { changedFiles(diff) }
            }

            if let result = t.lastResult {
                let lists = [("Changes", diff?.files.isEmpty == false ? [] : result.changes), ("Checks", result.checks),
                             ("Findings", result.findings), ("Decided", result.decisions),
                             ("Made", result.artifacts.filter { a in !pictures.contains { a.hasPrefix($0.path) } }
                                .map { ($0.components(separatedBy: " — ")[0] as NSString).lastPathComponent })]
                    .filter { !$0.1.isEmpty }
                if !lists.isEmpty {
                    VStack(spacing: 0) {
                        ForEach(Array(lists.enumerated()), id: \.element.0) { i, list in
                            if i > 0 { Divider().padding(.leading, 14) }
                            foldingList(list.0, list.1)
                        }
                    }
                    .background(theme.surfaceColor, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                }
            }

            if let note = t.note, !note.isEmpty, note != t.lastResult?.summary {
                section("Outcome") {
                    card {
                        Text(note)
                            .themeFont(.body)
                            .textSelection(.enabled)
                    }
                }
            }

            section("Objective") { objective(t.objective) }

            if let told = t.told, !told.isEmpty {
                section("You told it") {
                    card {
                        Text(told)
                            .themeFont(.body)
                            .textSelection(.enabled)
                    }
                }
            }

            // Earlier runs, newest first. The newest is already the Result above, so it isn't repeated here.
            let earlier = earlierRuns(t)
            if !earlier.isEmpty {
                section("Earlier runs") {
                    let shown = allRuns ? earlier : Array(earlier.prefix(3))
                    VStack(spacing: 0) {
                        ForEach(Array(shown.enumerated()), id: \.offset) { i, run in
                            if i > 0 { Divider().padding(.leading, 14) }
                            runRow(run)
                        }
                        if earlier.count > shown.count || allRuns && earlier.count > 3 {
                            Divider().padding(.leading, 14)
                            Button {
                                withAnimation(.snappy(duration: 0.25)) { allRuns.toggle() }
                            } label: {
                                Text(allRuns ? "Show fewer" : "Show all \(earlier.count)")
                                    .themeFont(.subheadline, weight: .medium)
                                    .foregroundStyle(theme.accent ?? Color.accentColor)
                                    .frame(maxWidth: .infinity, minHeight: 44)
                                    .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .background(theme.surfaceColor, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                }
            }

            section("Details") {
                VStack(spacing: 0) {
                    let rows = detailRows(t)
                    ForEach(Array(rows.enumerated()), id: \.offset) { i, row in
                        if i > 0 { Divider().padding(.leading, 46) }
                        detail(row.0, row.1, head: row.0 == "folder")
                    }
                    if t.sessionID?.isEmpty == false {
                        if !rows.isEmpty { Divider().padding(.leading, 46) }
                        Button { showingConversation = true } label: {
                            HStack(spacing: 12) {
                                Image(systemName: "text.bubble")
                                    .font(.system(size: 14, weight: .medium))
                                    .foregroundStyle(.secondary)
                                    .frame(width: 20)
                                Text("Conversation")
                                    .themeFont(.subheadline)
                                    .foregroundStyle(.primary)
                                Spacer(minLength: 0)
                                Image(systemName: "chevron.right")
                                    .font(.system(size: 12, weight: .semibold))
                                    .foregroundStyle(.tertiary)
                            }
                            .padding(.horizontal, 14)
                            .frame(minHeight: 44)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                }
                .background(theme.surfaceColor, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// "2 commits · +48 −12"
    private func diffNote(_ d: ThreadDiff) -> String {
        let added = d.files.reduce(0) { $0 + max($1.added, 0) }, removed = d.files.reduce(0) { $0 + max($1.removed, 0) }
        return ["\(d.commits) commit\(d.commits == 1 ? "" : "s")", "+\(added) −\(removed)"].joined(separator: " · ")
    }

    /// The files landing it would change, biggest first: name, folder under it, lines added and removed. Six
    /// until asked for all; a tap opens one's patch.
    private func changedFiles(_ d: ThreadDiff) -> some View {
        let shown = allFiles ? d.files : Array(d.files.prefix(6))
        let hidden = d.files.count - shown.count + d.more
        return VStack(spacing: 0) {
            ForEach(Array(shown.enumerated()), id: \.element.id) { i, file in
                if i > 0 { Divider().padding(.leading, 14) }
                Button { openFile = file } label: { fileRow(file) }
                    .buttonStyle(.plain)
            }
            if hidden > 0 || allFiles && d.files.count > 6 {
                Divider().padding(.leading, 14)
                Button {
                    withAnimation(.snappy(duration: 0.25)) { allFiles.toggle() }
                } label: {
                    Text(allFiles ? "Show fewer" : "\(hidden) more file\(hidden == 1 ? "" : "s")")
                        .themeFont(.subheadline, weight: .medium)
                        .foregroundStyle(theme.accent ?? Color.accentColor)
                        .frame(maxWidth: .infinity, minHeight: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .background(theme.surfaceColor, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }

    private func fileRow(_ file: ThreadDiff.File) -> some View {
        let name = (file.path as NSString).lastPathComponent
        let folder = (file.path as NSString).deletingLastPathComponent
        return HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 1) {
                Text(name)
                    .themeFont(.subheadline, weight: .medium)
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                if !folder.isEmpty {
                    Text(folder)
                        .themeFont(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.head)
                }
            }
            Spacer(minLength: 8)
            if file.added < 0 {
                Text("binary").themeFont(.caption).foregroundStyle(.secondary)
            } else {
                HStack(spacing: 6) {
                    if file.added > 0 { Text("+\(file.added)").foregroundStyle(Color.green) }
                    if file.removed > 0 { Text("−\(file.removed)").foregroundStyle(Color.red.opacity(0.9)) }
                }
                .themeFont(.footnote, weight: .medium, digits: true)
            }
            Image(systemName: "chevron.right")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(.tertiary)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
        .frame(minHeight: 48)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }

    /// Its runs before the newest, newest first; the newest too when the Result above didn't come from it.
    private func earlierRuns(_ t: ProjectThread) -> [ThreadRun] {
        guard var runs = t.runs, let last = runs.last else { return [] }
        // The run log keeps a summary with its whitespace squashed and cut short, so match it that way.
        if let summary = t.lastResult?.summary, !summary.isEmpty {
            let squashed = summary.split(whereSeparator: \.isWhitespace).joined(separator: " ")
            let logged = last.summary.hasSuffix("…") ? String(last.summary.dropLast()) : last.summary
            if squashed == last.summary || (last.summary.hasSuffix("…") && squashed.hasPrefix(logged)) { runs.removeLast() }
        }
        return runs.reversed()
    }

    /// "Continued · 9 hr. ago · 2m 29s": how a run came about, when, and for how long (and how it ended,
    /// when that wasn't simply finishing).
    private func runMeta(_ run: ThreadRun) -> String {
        let status = run.status == "completed" ? "" : ThreadWords.runStatus(run.status)
        return [ThreadWords.kind(run.kind), ThreadWords.ago(run.at), status, ThreadWords.duration(run.durationS) ?? ""]
            .filter { !$0.isEmpty }.joined(separator: " · ")
    }

    /// The objective, which can run to pages: four lines, and a tap shows the rest.
    private func objective(_ text: String) -> some View {
        let long = text.count > 280 || text.filter { $0 == "\n" }.count > 3
        return VStack(alignment: .leading, spacing: 8) {
            Text(text)
                .themeFont(.callout)
                .foregroundStyle(.primary.opacity(0.85))
                .lineLimit(objectiveOpen || !long ? nil : 4)
                .textSelection(.enabled)
            if long {
                Text(objectiveOpen ? "Show less" : "Show more")
                    .themeFont(.subheadline, weight: .medium)
                    .foregroundStyle(theme.accent ?? Color.accentColor)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(theme.surfaceColor, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .contentShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
        .onTapGesture {
            guard long else { return }
            withAnimation(.snappy(duration: 0.25)) { objectiveOpen.toggle() }
        }
    }

    /// One of the result's lists as a row ("Checks  3  ›"); a tap opens it in place.
    private func foldingList(_ title: String, _ items: [String]) -> some View {
        let open = openLists.contains(title)
        return VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(.snappy(duration: 0.25)) {
                    if open { openLists.remove(title) } else { openLists.insert(title) }
                }
            } label: {
                HStack(spacing: 8) {
                    Text(title)
                        .themeFont(.subheadline, weight: .medium)
                        .foregroundStyle(.primary)
                    Spacer(minLength: 0)
                    Text("\(items.count)")
                        .themeFont(.subheadline, digits: true)
                        .foregroundStyle(.secondary)
                    Image(systemName: "chevron.right")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(.tertiary)
                        .rotationEffect(.degrees(open ? 90 : 0))
                }
                .padding(.horizontal, 14)
                .frame(minHeight: 44)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(open ? "Open" : "Folded")
            if open {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text("•").foregroundStyle(.secondary)
                            Text(item).textSelection(.enabled)
                        }
                        .themeFont(.callout)
                    }
                }
                .padding(.horizontal, 14)
                .padding(.bottom, 14)
                .transition(.opacity)
            }
        }
    }

    /// Where and with what it works, one (icon, text) per line.
    private func detailRows(_ t: ProjectThread) -> [(String, String)] {
        var rows: [(String, String)] = []
        if let folder = t.folder ?? t.workdir, !folder.isEmpty { rows.append(("folder", folder)) }
        if let branch = t.branch, !branch.isEmpty { rows.append(("arrow.triangle.branch", branch)) }
        let brain = [t.model, t.effort].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
        if !brain.isEmpty { rows.append(("cpu", brain)) }
        if !t.edits { rows.append(("eye", "Read only")) }
        let usage = [t.tools > 0 ? "\(t.tools) tool\(t.tools == 1 ? "" : "s")" : "",
                     t.tokensOut > 0 ? "\(tokenText(t.tokensOut)) tokens" : ""].filter { !$0.isEmpty }
        if !usage.isEmpty { rows.append(("wrench.and.screwdriver", usage.joined(separator: " · "))) }
        return rows
    }

    private func card<Content: View>(@ViewBuilder content: () -> Content) -> some View {
        content()
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(14)
            .background(theme.surfaceColor, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }

    /// A section's small grey heading, with a note on the right when it has one ("Continued · 9 hr. ago").
    private func section<Content: View>(_ title: String, note: String? = nil,
                                        @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(title)
                    .themeFont(.footnote, weight: .semibold)
                    .foregroundStyle(.secondary)
                    .accessibilityAddTraits(.isHeader)
                Spacer(minLength: 0)
                if let note, !note.isEmpty {
                    Text(note)
                        .themeFont(.footnote)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
            }
            .padding(.horizontal, 4)
            content()
        }
    }

    private func detail(_ icon: String, _ text: String, head: Bool = false) -> some View {
        HStack(spacing: 12) {
            Image(systemName: icon)
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(.secondary)
                .frame(width: 20)
            Text(text)
                .themeFont(.subheadline)
                .foregroundStyle(.primary)
                .lineLimit(1)
                .truncationMode(head ? .head : .tail)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 14)
        .frame(minHeight: 44)
    }

    /// One earlier run: when and how, and two lines of what it said; a tap shows all of it.
    private func runRow(_ run: ThreadRun) -> some View {
        let open = openRuns.contains(run.at)
        return VStack(alignment: .leading, spacing: 4) {
            Text(runMeta(run))
                .themeFont(.footnote)
                .foregroundStyle(.secondary)
            if !run.summary.isEmpty {
                Text(run.summary)
                    .themeFont(.callout)
                    .foregroundStyle(.primary.opacity(0.85))
                    .lineLimit(open ? nil : 2)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
        .onTapGesture {
            withAnimation(.snappy(duration: 0.25)) {
                if open { openRuns.remove(run.at) } else { openRuns.insert(run.at) }
            }
        }
        .contextMenu {
            Button { UIPasteboard.general.string = run.summary } label: { Label("Copy", systemImage: "doc.on.doc") }
        }
    }

    /// Reviewing finished work: the two answers to it, side by side. Ask for changes swaps in the field.
    private func reviewBar(_ t: ProjectThread) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            if let error {
                Text(error)
                    .themeFont(.footnote)
                    .foregroundStyle(.red)
                    .padding(.horizontal, 6)
            }
            HStack(spacing: 10) {
                Button {
                    asking = true
                    fieldFocused = true
                } label: {
                    Text("Ask for changes")
                        .themeFont(.body, weight: .semibold)
                        .foregroundStyle(.primary)
                        .frame(maxWidth: .infinity, minHeight: 48)
                        .background(theme.surfaceColor, in: Capsule())
                        .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                Button { confirmLand = true } label: {
                    HStack(spacing: 7) {
                        if landing {
                            ProgressView().tint(.white)
                        } else {
                            Image(systemName: "arrow.triangle.merge")
                                .font(.system(size: 15, weight: .semibold))
                            Text("Land")
                                .themeFont(.body, weight: .semibold)
                        }
                    }
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity, minHeight: 48)
                    .background(theme.accent ?? Color.accentColor, in: Capsule())
                    .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .disabled(landing)
                .accessibilityLabel("Land")
            }
            .disabled(client.connection != .online)
            .opacity(client.connection == .online ? 1 : 0.55)
        }
        .padding(.horizontal, 16)
        .padding(.top, 8)
        .padding(.bottom, 8)
        .background(theme.backgroundColor.ignoresSafeArea())
        .transition(.opacity)
    }

    /// The small field at the bottom: an answer while it waits on you, a message otherwise.
    private func field(_ t: ProjectThread) -> some View {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        return VStack(alignment: .leading, spacing: 6) {
            if let error {
                Text(error)
                    .themeFont(.footnote)
                    .foregroundStyle(.red)
                    .padding(.horizontal, 6)
            }
            HStack(alignment: .bottom, spacing: 8) {
                if t.canLand {
                    // Back to Land and Ask for changes.
                    Button {
                        draft = ""
                        fieldFocused = false
                        asking = false
                    } label: {
                        Image(systemName: "xmark")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundStyle(.secondary)
                            .frame(width: 30, height: 30)
                    }
                    .buttonStyle(.plain)
                    .padding(.bottom, 3)
                    .accessibilityLabel("Cancel")
                }
                let prompt = t.isWaiting ? "Answer" : t.canLand ? "What should change?"
                    : t.isOpen ? "Message this thread" : "Message to carry it on"
                TextField(prompt, text: $draft, axis: .vertical)
                    .themeFont(.body)
                    .lineLimit(1...5)
                    .focused($fieldFocused)
                    .accessibilityLabel(prompt)
                    .padding(.vertical, 8)
                    .padding(.leading, 6)
                if !text.isEmpty || sending {
                    Button { send(text) } label: {
                        Group {
                            if sending {
                                ProgressView().controlSize(.small)
                            } else {
                                Image(systemName: "arrow.up")
                                    .font(.system(size: 14, weight: .bold))
                                    .foregroundStyle(.white)
                            }
                        }
                        .frame(width: 30, height: 30)
                        .background(theme.accent ?? Color.accentColor, in: Circle())
                    }
                    .buttonStyle(.plain)
                    .disabled(sending || client.connection != .online)
                    .padding(.bottom, 3)
                    .accessibilityLabel("Send")
                }
            }
            .padding(.horizontal, 8)
            .themeSurface(theme.composer, radius: 20)
        }
        .padding(.horizontal, 12)
        .padding(.top, 6)
        .padding(.bottom, 8)
        .background(theme.backgroundColor.ignoresSafeArea())
        .animation(.snappy(duration: 0.2), value: text.isEmpty)
    }

    private func send(_ text: String) {
        guard !text.isEmpty, !sending else { return }
        sending = true
        error = nil
        Task {
            do {
                let told = try await client.tellThread(project: projectID, id: threadID, text: text)
                draft = ""
                fieldFocused = false
                asking = false
                if told.id != threadID { carriedOn(told.id) }
            } catch {
                self.error = ThreadWords.explain(error)
            }
            sending = false
        }
    }

    private func act(_ work: @escaping () async throws -> ProjectThread) {
        error = nil
        Task {
            do { _ = try await work() } catch { self.error = ThreadWords.explain(error) }
        }
    }

    private func land() {
        landing = true
        error = nil
        Task {
            do {
                _ = try await client.landThread(project: projectID, id: threadID)
                UINotificationFeedbackGenerator().notificationOccurred(.success)
            } catch {
                self.error = ThreadWords.explain(error)
            }
            landing = false
        }
    }
}

/// One file's change, as landing the thread would bring it in: each hunk under a quiet "Line N", added lines
/// on green, removed on red, wrapped to the screen. Fetched when the sheet opens.
private struct ThreadDiffSheet: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.theme) private var theme
    @Environment(\.dismiss) private var dismiss
    let projectID: String
    let threadID: String
    let file: ThreadDiff.File
    @State private var lines: [Line]?
    @State private var cut = false
    @State private var failed: String?

    struct Line: Identifiable {
        enum Kind { case added, removed, context, hunk }
        let id: Int
        let kind: Kind
        let text: String
    }

    /// A unified diff's lines, its headers left out; a hunk's header becomes "Line N".
    static func parse(_ patch: String) -> [Line] {
        var out: [Line] = []
        var inHunk = false
        for raw in patch.components(separatedBy: "\n") {
            if raw.hasPrefix("diff --git") {
                inHunk = false
            } else if raw.hasPrefix("@@") {
                inHunk = true
                let start = raw.split(separator: "+").dropFirst().first?.split(whereSeparator: { $0 == "," || $0 == " " }).first
                out.append(Line(id: out.count, kind: .hunk, text: start.map { "Line \($0)" } ?? ""))
            } else if !inHunk || raw.hasPrefix("\\") {
                continue  // index, ---, +++, "\ No newline at end of file"
            } else if let first = raw.first {
                let kind: Line.Kind = first == "+" ? .added : first == "-" ? .removed : .context
                out.append(Line(id: out.count, kind: kind, text: String(raw.dropFirst())))
            }
        }
        return out
    }

    var body: some View {
        NavigationStack {
            Group {
                if let failed {
                    Text(failed)
                        .themeFont(.subheadline)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .padding(32)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if let lines, lines.isEmpty {
                    Text(file.added < 0 ? "A binary file: nothing to show line by line." : "No line changes to show.")
                        .themeFont(.subheadline)
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if let lines {
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 0) {
                            ForEach(lines) { line in row(line) }
                            if cut {
                                Text("The rest is too long to show here.")
                                    .themeFont(.footnote)
                                    .foregroundStyle(.secondary)
                                    .padding(16)
                            }
                        }
                        .padding(.vertical, 8)
                    }
                    .softScrollEdges()
                } else {
                    ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .background(theme.backgroundColor.ignoresSafeArea())
            .navigationTitle((file.path as NSString).lastPathComponent)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { dismiss() } label: { Image(systemName: "xmark") }
                        .accessibilityLabel("Done")
                }
            }
        }
        .task {
            guard lines == nil else { return }
            do {
                let diff = try await client.threadDiff(project: projectID, id: threadID, path: file.path)
                lines = Self.parse(diff.patch ?? "")
                cut = diff.cut ?? false
            } catch {
                failed = ThreadWords.explain(error)
            }
        }
    }

    @ViewBuilder private func row(_ line: Line) -> some View {
        switch line.kind {
        case .hunk:
            Text(line.text)
                .themeFont(.caption, weight: .semibold)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 16)
                .padding(.top, line.id == 0 ? 4 : 18)
                .padding(.bottom, 6)
        default:
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(line.kind == .added ? "+" : line.kind == .removed ? "−" : " ")
                    .foregroundStyle(line.kind == .added ? Color.green : line.kind == .removed ? Color.red : Color.clear)
                    .frame(width: 10)
                Text(line.text.isEmpty ? " " : line.text)
                    .foregroundStyle(line.kind == .context ? Color.primary.opacity(0.6) : Color.primary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .font(.system(size: 12, design: .monospaced))
            .padding(.horizontal, 12)
            .padding(.vertical, 1.5)
            .background(line.kind == .added ? Color.green.opacity(0.12) : line.kind == .removed ? Color.red.opacity(0.12) : Color.clear)
        }
    }
}

/// A thread's conversation as its provider keeps it, read only (it can be long): what Vision gave it, and what
/// it returned. Fetched when the sheet opens.
private struct ThreadConversationSheet: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.theme) private var theme
    @Environment(\.dismiss) private var dismiss
    let projectID: String
    let threadID: String
    /// nil until it has loaded.
    @State private var messages: [ThreadMessage]?

    var body: some View {
        NavigationStack {
            Group {
                if let messages, messages.isEmpty {
                    Text("Its provider's record of it can't be read; the runs tell its story.")
                        .themeFont(.subheadline)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .padding(32)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if let messages {
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 18) {
                            ForEach(Array(messages.enumerated()), id: \.offset) { _, m in
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(m.role == "user" ? "Vision" : "Thread")
                                        .themeFont(.footnote, weight: .semibold)
                                        .foregroundStyle(.secondary)
                                    Text(m.text)
                                        .themeFont(.callout)
                                        .foregroundStyle(Color.primary.opacity(m.role == "user" ? 0.7 : 1))
                                        .textSelection(.enabled)
                                }
                            }
                        }
                        .padding(20)
                    }
                    .softScrollEdges()
                } else {
                    ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .background(theme.backgroundColor.ignoresSafeArea())
            .navigationTitle("Conversation")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { dismiss() } label: { Image(systemName: "xmark") }
                        .accessibilityLabel("Done")
                }
            }
        }
        .task {
            guard messages == nil else { return }
            messages = (try? await client.threadHistory(project: projectID, id: threadID)) ?? []
        }
    }
}

/// A project's settings, laid out like the Claude app's: its name, goal and instructions on cards, its folder
/// and how it runs (who coordinates and who does the threads, how many at once, how freely they start, how much
/// the chat reports, what reaches the phone), then one card with its usage, Pause and Archive. ✓ saves the
/// fields; Pause and Archive act at once.
struct ProjectSettingsView: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.theme) private var theme
    @Environment(\.dismiss) private var dismiss
    let project: ProjectInfo
    /// Called once it's archived, so the project's own screen closes too.
    var onArchived: () -> Void = {}
    @State private var title: String
    @State private var goal: String
    @State private var instructions: String
    @State private var settings: ProjectSettings
    @State private var look: ScoutLook
    @State private var saving = false
    @State private var confirmingArchive = false
    @State private var error: String?

    init(project: ProjectInfo, onArchived: @escaping () -> Void = {}) {
        self.project = project
        self.onArchived = onArchived
        _title = State(initialValue: project.title)
        _goal = State(initialValue: project.goal)
        _instructions = State(initialValue: project.instructions)
        _settings = State(initialValue: project.settings)
        _look = State(initialValue: project.scout)
    }

    /// As the list has it now: Pause lands while the sheet is up.
    private var live: ProjectInfo { client.projects.first { $0.id == project.id } ?? project }
    private var online: Bool { client.connection == .online }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    ProjectFormSection("Scout") {
                        ProjectFormCard { ScoutLookPicker(look: $look, mood: live.mood) }
                    }
                    ProjectFormSection("Goal") {
                        ProjectFormCard {
                            TextField("Goal", text: $goal, prompt: Text("What's the end result?").foregroundStyle(.secondary), axis: .vertical)
                                .themeFont(.body)
                                .lineLimit(3...8)
                                .padding(18)
                                .accessibilityLabel("Goal")
                        }
                    }
                    ProjectFormSection("Project name") {
                        ProjectFormCard { ProjectNameField(title: $title) }
                    }
                    ProjectFormSection("Instructions (optional)") {
                        ProjectFormCard {
                            TextField("Instructions", text: $instructions, prompt: Text("How to work, what to avoid").foregroundStyle(.secondary),
                                      axis: .vertical)
                                .themeFont(.body)
                                .lineLimit(2...10)
                                .padding(18)
                                .accessibilityLabel("Instructions")
                        }
                    }
                    ProjectFormCard {
                        ProjectFormRow(icon: "folder") {
                            Text(live.folder.isEmpty ? "Default folder" : live.folder)
                                .lineLimit(1)
                                .truncationMode(.head)
                        }
                        .accessibilityElement(children: .combine)
                        .accessibilityLabel("Folder, \(live.folder.isEmpty ? "default" : live.folder)")
                        NavigationLink {
                            ScoutLeashPage(projectID: project.id)
                        } label: {
                            ProjectFormRow(icon: "leaf") {
                                Text("Schedule")
                            } trailing: {
                                ProjectFormChevron()
                            }
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Schedule")
                        NavigationLink {
                            ProjectRunSettingsPage(settings: $settings)
                        } label: {
                            ProjectFormRow(icon: "slider.horizontal.3") {
                                Text("How it runs")
                            } trailing: {
                                HStack(spacing: 8) {
                                    Text(projectSettingsSummary(settings))
                                    ProjectFormChevron()
                                }
                            }
                        }
                        .buttonStyle(.plain)
                    }
                    ProjectFormCard {
                        ProjectFormRow(icon: "chart.xyaxis.line") {
                            Text("Usage")
                        } trailing: {
                            Text(live.tokensOut > 0 ? "\(tokenText(live.tokensOut)) tokens" : "None yet")
                                .monospacedDigit()
                        }
                        .accessibilityElement(children: .combine)
                        Button { pause(!live.isPaused) } label: {
                            ProjectFormRow(icon: live.isPaused ? "play" : "pause") {
                                Text(live.isPaused ? "Resume" : "Pause")
                            }
                        }
                        .buttonStyle(.plain)
                        .disabled(!online)
                        Button { confirmingArchive = true } label: {
                            ProjectFormRow(icon: "archivebox", tint: .red) {
                                Text("Retire scout")
                            }
                        }
                        .buttonStyle(.plain)
                        .disabled(!online)
                    }
                    if let error {
                        Text(error)
                            .themeFont(.footnote)
                            .foregroundStyle(.red)
                            .padding(.leading, 18)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.top, 12)
                .padding(.bottom, 24)
            }
            .scrollDismissesKeyboard(.interactively)
            .background(theme.backgroundColor.ignoresSafeArea())
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button { dismiss() } label: { Image(systemName: "xmark") }
                        .accessibilityLabel("Cancel")
                }
                ToolbarItem(placement: .confirmationAction) {
                    if saving {
                        ProgressView()
                    } else {
                        Button { save() } label: { Image(systemName: "checkmark") }
                            .accessibilityLabel("Save")
                            .disabled(goal.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !online)
                    }
                }
            }
            .confirmationDialog("Retire \(live.name)?", isPresented: $confirmingArchive, titleVisibility: .visible) {
                Button("Retire", role: .destructive) { archive() }
            } message: {
                Text("It leaves Scouts and its chat carries on as an ordinary one. Nothing is deleted.")
            }
        }
    }

    private func pause(_ paused: Bool) {
        Task {
            do { try await client.setProjectPaused(project.id, paused) } catch { self.error = ThreadWords.explain(error) }
        }
    }

    private func archive() {
        Task {
            do {
                try await client.archiveProject(project.id)
                onArchived()
            } catch {
                self.error = ThreadWords.explain(error)
            }
        }
    }

    private func save() {
        saving = true
        error = nil
        Task {
            do {
                try await client.saveProject(project.id, goal: goal.trimmingCharacters(in: .whitespacesAndNewlines),
                                             title: title.trimmingCharacters(in: .whitespacesAndNewlines),
                                             instructions: instructions.trimmingCharacters(in: .whitespacesAndNewlines),
                                             settings: settings.json)
                if look != project.scout { try await client.saveScout(project.id, look) }
                dismiss()
            } catch {
                self.error = ThreadWords.explain(error)
                saving = false
            }
        }
    }
}

/// How a project runs, as Form sections: shared by its settings sheet and the new-project sheet.
struct ProjectSettingsSections: View {
    @Binding var settings: ProjectSettings

    var body: some View {
        Section {
            Toggle(ScoutWords.askLabel, isOn: Binding(get: { ScoutWords.asks(settings.leash) },
                                                      set: { settings.leash = ScoutWords.leash(asking: $0) }))
            Picker("Reports in the chat", selection: $settings.updates) {
                Text("Everything").tag("all")
                Text("Milestones").tag("milestones")
                Text("Only when needed").tag("needs_you")
            }
            Picker("Notifications", selection: $settings.notify) {
                Text("Every finished thread").tag("all")
                Text("When a thread needs me").tag("needs_you")
                Text("None").tag("none")
            }
        } header: {
            Text("Coordinating")
        } footer: {
            Text("A thread that asks or fails always notifies you, unless notifications are off.")
        }
        Section {
            Stepper("Up to \(settings.maxThreads) at once", value: $settings.maxThreads, in: 1...8)
            Picker("Each go runs", selection: $settings.length) {
                Text("Up to 25 min").tag("short")
                Text("Up to 45 min").tag("normal")
                Text("Up to 2 hours").tag("long")
            }
            Picker("Each editing thread", selection: $settings.isolation) {
                Text("Gets its own branch").tag("worktree")
                Text("Shares the folder").tag("shared")
            }
        } header: {
            Text("Threads")
        } footer: {
            Text("A thread is asked to wrap up near its limit and stopped at it, so one can't eat your plan overnight. "
                 + "Its own branch means a git worktree each, landed when you say so.")
        }
        Section {
            ScoutModelRows(role: "Coordinator", model: $settings.coordinatorModel, effort: $settings.coordinatorEffort)
            ScoutModelRows(role: "Threads", model: $settings.threadModel, effort: $settings.threadEffort)
        } header: {
            Text("Models")
        } footer: {
            Text("A cheaper thread model makes several threads go further.")
        }
    }
}

/// One role's model and effort as menus, from the machine's catalogue; the effort levels are the picked model's.
/// Text fields while the catalogue can't be had (offline), so a name can still be typed.
private struct ScoutModelRows: View {
    @EnvironmentObject private var client: VisionClient
    let role: String
    @Binding var model: String
    @Binding var effort: String
    @State private var catalogue: ModelCatalogue?

    var body: some View {
        Group {
            if let catalogue {
                Picker("\(role) model", selection: $model) {
                    Text("The chat's own").tag("")
                    if !model.isEmpty, !catalogue.tabs.contains(where: { $0.models.contains { $0.id == model } }) {
                        Text(model).tag(model)  // typed by name elsewhere, or a model no longer set up
                    }
                    ForEach(catalogue.tabs.filter { !$0.models.isEmpty }) { provider in
                        Section(provider.tab) {
                            ForEach(provider.models) { Text($0.label).tag($0.id) }
                        }
                    }
                }
                .pickerStyle(.menu)
                let levels = catalogue.efforts.filter { !$0.id.isEmpty }
                if !levels.isEmpty {
                    Picker("\(role) effort", selection: $effort) {
                        Text("The model's own").tag("")
                        if !effort.isEmpty, !levels.contains(where: { $0.id == effort }) {
                            Text(ModelPickerState.effortTitle(effort)).tag(effort)
                        }
                        ForEach(levels) { Text(ModelPickerState.effortTitle($0.label)).tag($0.id) }
                    }
                    .pickerStyle(.menu)
                }
            } else {
                TextField("\(role) model (blank: the chat's own)", text: $model)
                TextField("\(role) effort", text: $effort)
            }
        }
        .textInputAutocapitalization(.never)
        .autocorrectionDisabled()
        .task(id: model) {
            guard let fresh = try? await client.fetchCatalogue(model: model) else { return }
            let switched = catalogue != nil
            catalogue = fresh
            // A level the newly picked model doesn't have goes back to the model's own.
            if switched, !effort.isEmpty, !fresh.efforts.contains(where: { $0.id == effort }) { effort = "" }
        }
    }
}

/// What the project keeps in mind: what its threads decided and what you asked it to remember, oldest first,
/// one card. Hold one to forget it; the small field at the bottom adds one.
struct ProjectMemoryView: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.theme) private var theme
    let projectID: String
    @State private var facts: [String]?
    @State private var draft = ""
    @State private var adding = false
    @State private var error: String?
    @FocusState private var fieldFocused: Bool

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                if let facts {
                    if facts.isEmpty {
                        Text("Nothing yet. Threads add what they decide; you can add a line below.")
                            .themeFont(.footnote)
                            .foregroundStyle(.secondary)
                            .padding(.leading, 4)
                    } else {
                        VStack(spacing: 0) {
                            ForEach(Array(facts.enumerated()), id: \.offset) { i, line in
                                if i > 0 { Divider().padding(.leading, 14) }
                                fact(line)
                                    .padding(.horizontal, 14)
                                    .padding(.vertical, 12)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .contentShape(Rectangle())
                                    .contextMenu {
                                        Button(role: .destructive) { forget(i) } label: { Label("Forget", systemImage: "trash") }
                                    }
                            }
                        }
                        .background(theme.surfaceColor, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                        Text("Its coordinator and every thread see these. Hold one to forget it.")
                            .themeFont(.footnote)
                            .foregroundStyle(.secondary)
                            .padding(.leading, 4)
                    }
                } else if let error {
                    Text(error).themeFont(.footnote).foregroundStyle(.red)
                } else {
                    ProgressView().frame(maxWidth: .infinity).padding(.top, 40)
                }
            }
            .padding(.horizontal, 16)
            .padding(.top, 10)
            .padding(.bottom, 20)
        }
        .scrollDismissesKeyboard(.interactively)
        .safeAreaInset(edge: .bottom, spacing: 0) { field }
        .task { await load() }
        .refreshable { await load() }
    }

    /// "- 2026-10-11: Use UTC. (thread Dates)" as the fact, with its date and who said it under it.
    private func fact(_ line: String) -> some View {
        var text = line.hasPrefix("- ") ? String(line.dropFirst(2)) : line
        var meta: [String] = []
        if let r = text.range(of: #"^\d{4}-\d{2}-\d{2}: "#, options: .regularExpression) {
            meta.append(String(text[r].dropLast(2)))
            text.removeSubrange(r)
        }
        if let r = text.range(of: #" \((thread|from) [^)]*\)$"#, options: .regularExpression) {
            meta.append(String(text[r]).trimmingCharacters(in: CharacterSet(charactersIn: " ()")))
            text.removeSubrange(r)
        }
        return VStack(alignment: .leading, spacing: 3) {
            Text(text)
                .themeFont(.callout)
                .textSelection(.enabled)
            if !meta.isEmpty {
                Text(meta.joined(separator: " · "))
                    .themeFont(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    /// The small field at the bottom, as the thread page's: a line for the project to keep in mind.
    private var field: some View {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        return VStack(alignment: .leading, spacing: 6) {
            if let error, facts != nil {
                Text(error).themeFont(.footnote).foregroundStyle(.red).padding(.horizontal, 6)
            }
            HStack(alignment: .bottom, spacing: 8) {
                TextField("Something to keep in mind", text: $draft, axis: .vertical)
                    .themeFont(.body)
                    .lineLimit(1...4)
                    .focused($fieldFocused)
                    .accessibilityLabel("New memory")
                    .padding(.vertical, 8)
                    .padding(.leading, 6)
                if !text.isEmpty || adding {
                    Button { add(text) } label: {
                        Group {
                            if adding {
                                ProgressView().controlSize(.small)
                            } else {
                                Image(systemName: "arrow.up")
                                    .font(.system(size: 14, weight: .bold))
                                    .foregroundStyle(.white)
                            }
                        }
                        .frame(width: 30, height: 30)
                        .background(theme.accent ?? Color.accentColor, in: Circle())
                    }
                    .buttonStyle(.plain)
                    .disabled(adding || client.connection != .online)
                    .padding(.bottom, 3)
                    .accessibilityLabel("Add")
                }
            }
            .padding(.horizontal, 8)
            .themeSurface(theme.composer, radius: 20)
        }
        .padding(.horizontal, 12)
        .padding(.top, 6)
        .padding(.bottom, 8)
        .background(theme.backgroundColor.ignoresSafeArea())
        .animation(.snappy(duration: 0.2), value: text.isEmpty)
    }

    private func load() async {
        do {
            facts = try await client.projectMemory(projectID)
            error = nil
        } catch {
            self.error = ThreadWords.explain(error)
        }
    }

    private func add(_ fact: String) {
        guard !fact.isEmpty, !adding else { return }
        adding = true
        Task {
            do {
                facts = try await client.rememberInProject(projectID, fact: fact)
                draft = ""
                fieldFocused = false
            } catch {
                self.error = ThreadWords.explain(error)
            }
            adding = false
        }
    }

    private func forget(_ index: Int) {
        Task {
            do { facts = try await client.forgetInProject(projectID, index: index) } catch { self.error = ThreadWords.explain(error) }
        }
    }
}

/// Set a thread going on the project: what it should do, and whether it may change files.
struct NewThreadView: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.dismiss) private var dismiss
    let projectID: String
    /// Called with the thread once the machine has it, so the list can open it.
    let onStarted: (ProjectThread) -> Void
    @State private var objective = ""
    @State private var title = ""
    @State private var readOnly = false
    @State private var saving = false
    @State private var error: String?
    @FocusState private var objectiveFocused: Bool

    private var trimmed: String { objective.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("What should this thread do?", text: $objective, axis: .vertical)
                        .lineLimit(3...8)
                        .focused($objectiveFocused)
                        .accessibilityLabel("Objective")
                    TextField("Name (optional)", text: $title)
                }
                Section {
                    Toggle("Read only", isOn: $readOnly)
                } footer: {
                    Text(readOnly ? "It looks and reports back, but changes no files."
                                  : "It may change files, on a branch of its own unless the project's threads share its folder.")
                }
                if let error {
                    Section {
                        Text(error)
                            .foregroundStyle(.red)
                    }
                }
            }
            .themeFont(.body)
            .navigationTitle("New thread")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    if saving {
                        ProgressView()
                    } else {
                        Button("Start") { start() }
                            .fontWeight(.semibold)
                            .disabled(trimmed.isEmpty || client.connection != .online)
                    }
                }
            }
            .onAppear { objectiveFocused = true }
        }
    }

    private func start() {
        saving = true
        error = nil
        Task {
            do {
                let thread = try await client.startThread(project: projectID, objective: trimmed,
                                                          title: title.trimmingCharacters(in: .whitespacesAndNewlines),
                                                          edits: !readOnly)
                onStarted(thread)
            } catch {
                self.error = ThreadWords.explain(error)
                saving = false
            }
        }
    }
}
