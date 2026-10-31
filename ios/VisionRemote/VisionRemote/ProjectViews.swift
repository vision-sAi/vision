import SwiftUI

/// The Scouts screen (each project is a scout), as the Claude app's Projects: the drawer's Scouts
/// row puts it where the chat was, and a tap on a scout pushes its chat on top (back comes here). Each row is its
/// buddy, its name and where it stands; long-press retires one. Search and New scout float at the bottom.
struct ProjectsHome: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.theme) private var theme
    @Environment(\.drawerBars) private var drawerBars
    /// A project picked: its chat is opened (on the machine too, if it isn't open) and pushed.
    let onOpen: (ProjectInfo) -> Void
    @State private var creating = false
    @State private var inbox = false
    @State private var searching = false
    @State private var query = ""
    @FocusState private var searchFocused: Bool

    private var shown: [ProjectInfo] {
        let q = query.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return client.projects }
        return client.projects.filter { $0.name.localizedCaseInsensitiveContains(q) || $0.title.localizedCaseInsensitiveContains(q)
            || $0.goal.localizedCaseInsensitiveContains(q) }
    }

    var body: some View {
        Group {
            if client.projects.isEmpty {
                VStack(spacing: 12) {
                    BuddyView(hue: 205, trait: "sprout", mood: "asleep", size: 96)
                    Text("Send your first scout").themeFont(.headline)
                    Text("Give it a goal; it works on it in the background and checks in.")
                        .themeFont(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                }
                .padding(32)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if shown.isEmpty {
                ProjectEmptyState(icon: "magnifyingglass", text: "No scouts match")
            } else {
                ScrollView {
                    LazyVStack(spacing: 2) {
                        if waiting > 0 && query.isEmpty {
                            NeedsBanner(count: waiting) { inbox = true }
                                .padding(.bottom, 8)
                        }
                        ForEach(shown) { project in
                            Button { onOpen(project) } label: { ProjectRow(project: project) }
                                .buttonStyle(ProjectRowStyle())
                                .contextMenu {
                                    Button { archive(project) } label: { Label("Retire", systemImage: "archivebox") }
                                } preview: {
                                    ProjectPreview(project: project)
                                }
                        }
                    }
                    .padding(.horizontal, 8)
                    .padding(.top, 4)
                    .padding(.bottom, 24)
                }
                .scrollDismissesKeyboard(.immediately)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(theme.backgroundColor.ignoresSafeArea())
        // Room for the home indicator as padding: in the drawer the screen lays out with no safe area (see SideDrawer).
        .safeAreaInset(edge: .bottom, spacing: 0) { bottomBar.padding(.bottom, drawerBars?.bottom ?? 0) }
        .navigationTitle("Scouts")
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(.hidden, for: .navigationBar)
        .sheet(isPresented: $inbox) {
            NeedsInboxView { project in
                inbox = false
                onOpen(project)
            }
            .presentationDetents([.large])
        }
        .sheet(isPresented: $creating) {
            // Made, its chat opens on the machine and the chat screen pushes it once it arrives.
            NewScoutSheet { creating = false }
                .presentationDetents([.large])
                .presentationDragIndicator(.hidden)
        }
    }

    /// Everything waiting on you, across the scouts: what the banner counts and Needs you lists.
    private var waiting: Int {
        client.projects.reduce(0) { $0 + ScoutWords.needs($1, counts: client.threadCounts($1)) }
    }

    /// Search on the left, the New scout pill on the right, as the drawer's own bottom bar; searching, a
    /// field takes the whole width.
    private var bottomBar: some View {
        HStack(spacing: 12) {
            if searching {
                HStack(spacing: 8) {
                    Image(systemName: "magnifyingglass")
                        .foregroundStyle(.secondary)
                        .accessibilityHidden(true)
                    TextField("Search scouts", text: $query)
                        .themeFont(.body)
                        .focused($searchFocused)
                        .submitLabel(.search)
                        .autocorrectionDisabled()
                        .onAppear { searchFocused = true }
                    Button {
                        query = ""
                        searchFocused = false
                        searching = false
                    } label: {
                        Image(systemName: "xmark.circle.fill").foregroundStyle(.tertiary)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Stop searching")
                }
                .padding(.horizontal, 16)
                .frame(height: 48)
                .background(Capsule().fill(.regularMaterial))
                .transition(.opacity)
            } else {
                Button { searching = true } label: {
                    Image(systemName: "magnifyingglass")
                        .font(.system(size: 18, weight: .medium))
                        .foregroundStyle(.primary)
                        .frame(width: 34, height: 34)
                        .padding(glassPadding)
                        .contentShape(Circle())
                }
                .drawerGlassButton()
                .accessibilityLabel("Search scouts")
                .disabled(client.projects.isEmpty)
                Spacer()
                Button { creating = true } label: {
                    HStack(spacing: 8) {
                        Image(systemName: "plus").font(.system(size: 17, weight: .semibold))
                        Text("New scout").themeFont(size: 17, weight: .semibold)
                    }
                    .foregroundStyle(Color(.systemBackground))
                    .padding(.horizontal, 20)
                    .frame(height: 48)
                    .background(Capsule().fill(Color.primary))
                    .shadow(color: .black.opacity(0.18), radius: 10, y: 4)
                }
                .buttonStyle(PressScaleStyle())
                .disabled(client.connection != .online)
                .opacity(client.connection == .online ? 1 : 0.5)
                .accessibilityLabel("New scout")
            }
        }
        .padding(.horizontal, 16)
        .padding(.bottom, 6)
        .animation(.snappy(duration: 0.25), value: searching)
    }

    /// As the drawer's gear: before iOS 26 the label carries the glass button's padding itself.
    private var glassPadding: CGFloat {
        if #available(iOS 26.0, *) { 0 } else { 7 }
    }

    private func archive(_ project: ProjectInfo) {
        Task {
            do {
                try await client.archiveProject(project.id)
            } catch {
                client.toast = "Couldn't retire \(project.name): \(SettingsView.explain(error))"
            }
        }
    }
}

/// The phone navigates immediately while the machine prepares a project's chat. Keeping this page in the
/// destination avoids making navigation wait on the socket, then swaps to the chat in place.
struct ProjectOpeningView: View {
    @Environment(\.theme) private var theme
    let title: String

    var body: some View {
        VStack(spacing: 12) {
            ProgressView()
            Text("Opening project…")
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(theme.backgroundColor.ignoresSafeArea())
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(.hidden, for: .navigationBar)
        .accessibilityElement(children: .combine)
    }
}

/// One scout on the Scouts screen: its buddy, its name, and under it where it stands ("2 need you", "Working",
/// "Next check-in 8:00 tomorrow"); how many things need you sits on the right.
private struct ProjectRow: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.theme) private var theme
    let project: ProjectInfo

    var body: some View {
        let chat = client.chats.first { $0.id == project.chat }
        let counts = client.threadCounts(project)
        let waiting = ScoutWords.needs(project, counts: counts)
        HStack(spacing: 14) {
            ScoutBuddy(project: project, size: 40)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(project.name)
                    .themeFont(size: 17, weight: .semibold)
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                Text(status(chat: chat, counts: counts))
                    .themeFont(.subheadline)
                    .foregroundStyle(waiting > 0 || chat?.waiting == true ? AnyShapeStyle(theme.accent ?? Color.accentColor)
                                                                          : AnyShapeStyle(HierarchicalShapeStyle.secondary))
                    .lineLimit(1)
            }
            Spacer(minLength: 8)
            // Threads waiting on an answer from you: their count, small, in the theme's accent.
            if waiting > 0 {
                Text("\(waiting)")
                    .themeFont(size: 12, weight: .semibold)
                    .monospacedDigit()
                    .foregroundStyle(.white)
                    .padding(.horizontal, 6)
                    .frame(minWidth: 18, minHeight: 18)
                    .background(theme.accent ?? Color.accentColor, in: Capsule())
                    .accessibilityLabel("\(waiting) thing\(waiting == 1 ? "" : "s") need\(waiting == 1 ? "s" : "") you")
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityHint("Opens the scout's chat")
    }

    private func status(chat: ChatInfo?, counts: [String: Int]) -> String {
        if let chat, chat.waiting { return "Needs your answer" }
        let line = ScoutWords.line(project, counts: counts)
        let full = (project.title.isEmpty ? project.goal : project.title).components(separatedBy: "\n").first ?? ""
        // Short enough that what it's doing still shows after it.
        let goal = full.count <= 24 ? full : String(full.prefix(24)).replacingOccurrences(of: #"\s+\S*$"#, with: "", options: .regularExpression) + "…"
        if !project.scout.name.isEmpty {
            // What needs you leads; otherwise what it's for, then what it's doing.
            return goal.isEmpty ? line : ScoutWords.needs(project, counts: counts) > 0 ? "\(line) · \(goal)" : "\(goal) · \(line)"
        }
        if let chat, chat.busy { return "Working…" }
        let last = max(project.updated, chat?.updated ?? 0)
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .full
        let when = last > 0 ? f.localizedString(for: Date(timeIntervalSince1970: last), relativeTo: Date()) : "New"
        return project.isPaused ? "Paused · " + when : when
    }
}

/// A project row: a soft rounded highlight while pressed, as the drawer's rows.
private struct ProjectRowStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .fill(Color(.label).opacity(configuration.isPressed ? 0.1 : 0))
            )
            .animation(configuration.isPressed ? nil : .easeOut(duration: 0.3), value: configuration.isPressed)
    }
}

/// An empty page, as the Claude app draws one: a small glyph in a soft circle and one quiet line, centred,
/// with an action under it if there's one to take.
struct ProjectEmptyState<Action: View>: View {
    let icon: String
    let text: String
    @ViewBuilder var action: Action

    init(icon: String, text: String, @ViewBuilder action: () -> Action) {
        self.icon = icon
        self.text = text
        self.action = action()
    }

    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: icon)
                .font(.system(size: 19, weight: .regular))
                .foregroundStyle(.secondary)
                .frame(width: 52, height: 52)
                .background(Circle().fill(Color(.secondarySystemFill)))
                .accessibilityHidden(true)
            Text(text)
                .themeFont(.body)
                .foregroundStyle(.secondary)
            action
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

extension ProjectEmptyState where Action == EmptyView {
    init(icon: String, text: String) {
        self.init(icon: icon, text: text) { EmptyView() }
    }
}

// MARK: the project forms' pieces: a quiet label over a well-rounded card, as Claude's project settings

/// A label over its card.
struct ProjectFormSection<Content: View>: View {
    let name: String
    @ViewBuilder var content: Content

    init(_ name: String, @ViewBuilder content: () -> Content) {
        self.name = name
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if !name.isEmpty {
                Text(name)
                    .themeFont(.subheadline)
                    .foregroundStyle(.secondary)
                    .padding(.leading, 18)
                    .accessibilityAddTraits(.isHeader)
            }
            content
        }
    }
}

/// Rows on one rounded card, parted by hairlines that start under the text.
struct ProjectFormCard<Content: View>: View {
    @Environment(\.theme) private var theme
    @ViewBuilder var content: Content

    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }

    var body: some View {
        VStack(spacing: 0) {
            Group(subviews: content) { rows in
                ForEach(Array(rows.enumerated()), id: \.offset) { i, row in
                    if i > 0 { Divider().padding(.leading, 52).padding(.trailing, 18) }
                    row
                }
            }
        }
        .background(theme.surfaceColor, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
    }
}

/// One row of a card: a glyph, what it says, and something on the right.
struct ProjectFormRow<Content: View, Trailing: View>: View {
    let icon: String
    var tint: Color = .primary
    @ViewBuilder var content: Content
    @ViewBuilder var trailing: Trailing

    init(icon: String, tint: Color = .primary, @ViewBuilder content: () -> Content,
         @ViewBuilder trailing: () -> Trailing = { EmptyView() }) {
        self.icon = icon
        self.tint = tint
        self.content = content()
        self.trailing = trailing()
    }

    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: icon)
                .font(.system(size: 17, weight: .regular))
                .foregroundStyle(tint == .primary ? AnyShapeStyle(HierarchicalShapeStyle.secondary) : AnyShapeStyle(tint))
                .frame(width: 22)
                .accessibilityHidden(true)
            content
                .themeFont(.body)
                .foregroundStyle(tint)
            Spacer(minLength: 8)
            trailing
                .themeFont(.body)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 18)
        .frame(minHeight: 54)
        .contentShape(Rectangle())
    }
}

/// A row's chevron, for one that opens a page or a sheet.
struct ProjectFormChevron: View {
    var body: some View {
        Image(systemName: "chevron.right")
            .font(.system(size: 13, weight: .semibold))
            .foregroundStyle(.tertiary)
            .accessibilityHidden(true)
    }
}

/// The project's name with its glyph in a small tile beside it, as the first card of a project form.
struct ProjectNameField: View {
    @Binding var title: String

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "shippingbox")
                .font(.system(size: 16, weight: .regular))
                .foregroundStyle(.secondary)
                .frame(width: 36, height: 36)
                .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color(.tertiarySystemFill)))
                .accessibilityHidden(true)
            TextField("Name", text: $title, prompt: Text("Project name").foregroundStyle(.secondary))
                .themeFont(.body)
                .accessibilityLabel("Name")
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 10)
    }
}

/// How a project runs, in a word, for the row that opens its settings.
func projectSettingsSummary(_ settings: ProjectSettings) -> String {
    settings == ProjectSettings() ? "Defaults" : "Up to \(settings.maxThreads) at once"
}

/// How a project runs, on a page of its own: pushed from a project form's row.
struct ProjectRunSettingsPage: View {
    @Binding var settings: ProjectSettings

    var body: some View {
        Form { ProjectSettingsSections(settings: $settings) }
            .themeFont(.body)
            .navigationTitle("How it runs")
            .navigationBarTitleDisplayMode(.inline)
    }
}

/// Send a scout: what it should work on, its name and look, where on the machine, its leash and when it checks in,
/// and anything it should always keep in mind. Send makes it on the machine and opens its chat.
struct NewProjectView: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.theme) private var theme
    @Environment(\.dismiss) private var dismiss
    /// Called once the project's chat is on its way, so the drawer can get out of the way too.
    let onCreated: () -> Void
    /// What the one-sentence sheet had, carried over by its More options.
    var prefill: ScoutDraft? = nil
    var prefillFolder: String? = nil
    @State private var goal = ""
    @State private var title = ""
    @State private var instructions = ""
    @State private var folder: String?
    @State private var pickingFolder = false
    @State private var settings = ProjectSettings()
    @State private var look = ScoutLook(name: "", hue: 205, trait: "antenna")
    @State private var checkins: [String: Any] = ["repeat": "daily", "time": "08:00"]
    @State private var more = false   // name and look, leash, instructions, how it runs
    @State private var saving = false
    @State private var error: String?
    @FocusState private var focus: Field?

    private enum Field { case goal, instructions }

    private var trimmedGoal: String { goal.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    ProjectFormSection("Goal") {
                        ProjectFormCard {
                            TextField("Goal", text: $goal, prompt: Text("What should it work on?").foregroundStyle(.secondary), axis: .vertical)
                                .themeFont(.body)
                                .lineLimit(3...8)
                                .focused($focus, equals: .goal)
                                .padding(18)
                                .accessibilityLabel("Goal")
                        }
                    }
                    ProjectFormSection("When it runs") {
                        ProjectFormCard {
                            ScoutScheduleEditor(schedule: prefill == nil ? .init(repeatKind: "daily", time: "08:00", everyMinutes: 120, enabled: true)
                                                : prefill?.reports) {
                                checkins = $0
                            }
                            .themeFont(.body)
                            .padding(.horizontal, 18)
                            .frame(minHeight: 54)
                        }
                    }
                    // The folder: optional. Without one it works in the machine's default folder (a news brief
                    // needs none); with one, every run starts there.
                    ProjectFormSection("Folder (optional)") {
                        ProjectFormCard {
                            Button { pickingFolder = true } label: {
                                ProjectFormRow(icon: "folder") {
                                    Text(folder.map(homeAsTilde) ?? "None: for code or files, pick one")
                                        .foregroundStyle(folder == nil ? .secondary : .primary)
                                        .lineLimit(1)
                                        .truncationMode(.head)
                                } trailing: {
                                    if folder != nil {
                                        Button { folder = nil; settings.leash = "checkins" } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary) }
                                            .buttonStyle(.plain)
                                            .accessibilityLabel("No folder")
                                    } else {
                                        ProjectFormChevron()
                                    }
                                }
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel("Folder")
                            .accessibilityValue(folder.map(homeAsTilde) ?? "none chosen")
                        }
                    }
                    ProjectFormCard {
                        Button { withAnimation(.snappy(duration: 0.25)) { more.toggle() } } label: {
                            ProjectFormRow(icon: "ellipsis.circle") {
                                Text(more ? "Fewer options" : "Name, look and more")
                            } trailing: {
                                Image(systemName: "chevron.down")
                                    .font(.footnote.weight(.semibold))
                                    .foregroundStyle(.secondary)
                                    .rotationEffect(.degrees(more ? 180 : 0))
                            }
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("More options")
                    }
                    if more {
                    ProjectFormSection("Scout") {
                        ProjectFormCard { ScoutLookPicker(look: $look) }
                    }
                    // Only for a scout that works on files: a news brief never needs the choice.
                    if folder != nil {
                    ProjectFormCard {
                        Toggle(ScoutWords.askLabel, isOn: Binding(get: { ScoutWords.asks(settings.leash) },
                                                                  set: { settings.leash = ScoutWords.leash(asking: $0) }))
                            .padding(16)
                    }
                    }
                    ProjectFormSection("Instructions (optional)") {
                        ProjectFormCard {
                            TextField("Instructions", text: $instructions, prompt: Text("How to work, what to avoid").foregroundStyle(.secondary),
                                      axis: .vertical)
                                .themeFont(.body)
                                .lineLimit(2...8)
                                .focused($focus, equals: .instructions)
                                .padding(18)
                                .accessibilityLabel("Instructions")
                        }
                    }
                    ProjectFormCard {
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
                        .accessibilityLabel("Settings")
                    }
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
            // A tap anywhere off a field puts the keyboard away.
            .contentShape(Rectangle())
            .onTapGesture { focus = nil }
            .background(theme.backgroundColor.ignoresSafeArea())
            .safeAreaInset(edge: .bottom, spacing: 0) { createButton }
            .navigationTitle("New scout")
            .onAppear(perform: suggest)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button { dismiss() } label: { Image(systemName: "xmark") }
                        .accessibilityLabel("Cancel")
                }
            }
            .sheet(isPresented: $pickingFolder) {
                FolderPickerView(title: "Project folder", pickLabel: "Use this folder") { path in
                    folder = path
                    pickingFolder = false
                }
                .presentationDetents([.fraction(0.88), .large])
                .presentationDragIndicator(.visible)
            }
        }
    }

    private var ready: Bool { !trimmedGoal.isEmpty && client.connection == .online }

    /// A name, colour and look none of the other scouts has, as the machine would pick (projects.suggest_scout).
    private func suggest() {
        if let prefill, goal.isEmpty {
            goal = prefill.goal
            checkins = prefill.reportsJSON
            if !prefill.look.name.isEmpty { look = prefill.look }
            folder = folder ?? prefillFolder
        }
        guard look.name.isEmpty else { return }
        let others = client.projects.map(\.scout)
        let taken = Set(others.map { $0.name.lowercased() })
        look.name = ["Atlas", "Juniper", "Wren", "Moss", "Pip", "Fern", "Orion", "Clover", "Basil", "Rowan", "Indigo", "Maple"]
            .first { !taken.contains($0.lowercased()) } ?? "Scout"
        func gap(_ h: Double) -> Double { others.map { min(abs(h - $0.hue), 360 - abs(h - $0.hue)) }.min() ?? 360 }
        look.hue = ScoutLook.hues.max { gap($0) < gap($1) } ?? 205
        look.trait = ScoutLook.traits.min { a, b in others.filter { $0.trait == a }.count < others.filter { $0.trait == b }.count } ?? "antenna"
    }

    /// One button, the accent, the width of the page: it waits for a goal and the machine.
    private var createButton: some View {
        Button { create() } label: {
            Group {
                if saving {
                    ProgressView().tint(.white)
                } else {
                    Text("Send scout")
                        .font(.body.weight(.semibold))
                        .foregroundStyle(.white)
                }
            }
            .frame(maxWidth: .infinity)
            .frame(height: 48)
            .background(theme.accent ?? Color.accentColor, in: Capsule())
        }
        .buttonStyle(.plain)
        .disabled(saving || !ready)
        .opacity(ready ? 1 : 0.55)
        .animation(.snappy(duration: 0.2), value: ready)
        .accessibilityLabel("Create")
        .padding(.horizontal, 16)
        .padding(.top, 8)
        .padding(.bottom, 10)
        .background(theme.backgroundColor.ignoresSafeArea())
    }

    private func create() {
        saving = true
        error = nil
        Task {
            do {
                try await client.createProject(goal: trimmedGoal, title: title.trimmingCharacters(in: .whitespacesAndNewlines),
                                               instructions: instructions.trimmingCharacters(in: .whitespacesAndNewlines), folder: folder,
                                               settings: settings.json, scout: look, checkins: checkins)
                onCreated()
            } catch {
                self.error = SettingsView.explain(error)
                saving = false
            }
        }
    }
}

/// A machine path the way the rest of the app writes one: the home folder as ~ (the picker hands back the full path).
func homeAsTilde(_ path: String) -> String {
    path.replacingOccurrences(of: #"^/(home|Users)/[^/]+(?=/|$)"#, with: "~", options: .regularExpression)
}

/// What a long-press on a project lifts: its name, goal and folder, so the right one is archived.
struct ProjectPreview: View {
    @Environment(\.theme) private var theme
    let project: ProjectInfo

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(project.title)
                .themeFont(.headline)
            Text(project.goal)
                .themeFont(.body)
                .foregroundStyle(.secondary)
                .lineLimit(6)
            Label(project.folder.isEmpty ? "default folder" : project.folder, systemImage: "folder")
                .themeFont(.footnote)
                .foregroundStyle(.tertiary)
                .lineLimit(1)
                .truncationMode(.head)
        }
        .padding(20)
        .frame(width: 300, alignment: .leading)
        .background(theme.surfaceColor)
    }
}

// MARK: - scouts: every project is a scout, with a buddy, a leash, check-ins and requests

/// What a scout's row and title say under its name: what needs you, else what it's doing or when it's next.
enum ScoutWords {
    static func needs(_ project: ProjectInfo, counts: [String: Int]) -> Int {
        (counts["waiting_on_you"] ?? 0) + (counts["ready_for_review"] ?? 0) + project.approvals.count
    }

    static func line(_ project: ProjectInfo, counts: [String: Int]) -> String {
        let n = needs(project, counts: counts)
        if n > 0 { return "\(n) need\(n == 1 ? "s" : "") you" }
        if project.isPaused { return "Paused" }
        if project.checkin?.running != nil { return "Reporting…" }
        switch project.mood {
        case "worried": return "Something failed"
        case "focused": return "Working"
        case "curious": return "Reading up"
        case "proud": return "Landed something"
        default: break
        }
        if project.leash == "on_call" { return "Resting" }
        if let next = project.checkin?.schedule?.nextRun { return "Next status report " + when(next) }
        return "Resting"
    }

    /// "in 40 min", "14:00", "8:00 tomorrow", "Mon 9:00".
    static func when(_ ts: Double) -> String {
        let at = Date(timeIntervalSince1970: ts)
        let mins = Int(at.timeIntervalSinceNow / 60)
        if mins < 60 { return "in \(max(1, mins)) min" }
        let time = at.formatted(date: .omitted, time: .shortened)
        if Calendar.current.isDateInToday(at) { return "at " + time }
        if Calendar.current.isDateInTomorrow(at) { return time + " tomorrow" }
        return at.formatted(.dateTime.weekday(.abbreviated)) + " " + time
    }

    /// Moods worth moving for in a list; the rest hold still so a list of scouts isn't an aquarium.
    static func lively(_ mood: String) -> Bool { ["needs_you", "still_waiting", "worried", "found_it", "waking"].contains(mood) }

    /// The one choice of how far a scout goes alone (its leash underneath): ask first, or don't.
    static let askLabel = "Ask me before big changes"
    static let askNote = "Off, it pushes, deletes and merges without asking."
    static func asks(_ leash: String) -> Bool { leash != "autonomous" }
    static func leash(asking: Bool) -> String { asking ? "checkins" : "autonomous" }
}

/// A scout's buddy as its project has it.
struct ScoutBuddy: View {
    let project: ProjectInfo
    var size: CGFloat
    var moving: Bool? = nil
    var pokeable = false

    var body: some View {
        BuddyView(hue: project.scout.hue, trait: project.scout.trait, mood: project.mood, size: size,
                  animated: moving ?? ScoutWords.lively(project.mood), pokeable: pokeable)
    }
}

/// Its name, colour and look, with a live buddy: in New scout and a scout's settings.
struct ScoutLookPicker: View {
    @Binding var look: ScoutLook
    var mood = "curious"

    var body: some View {
        HStack(alignment: .center, spacing: 16) {
            VStack(spacing: 6) {
                BuddyView(hue: look.hue, trait: look.trait, mood: mood, size: 88, pokeable: true)
                Text(look.name.isEmpty ? "Scout" : look.name).font(.subheadline.weight(.semibold)).lineLimit(1)
            }
            .frame(width: 110)
            VStack(alignment: .leading, spacing: 12) {
                TextField("Name", text: $look.name)
                    .textFieldStyle(.roundedBorder)
                    .textInputAutocapitalization(.words)
                    .accessibilityLabel("Scout's name")
                HStack(spacing: 8) {
                    ForEach(ScoutLook.hues, id: \.self) { hue in
                        Button { look.hue = hue } label: {
                            Circle().fill(buddyHSL(hue, 85, 60)).frame(width: 22, height: 22)
                                .overlay(Circle().strokeBorder(Color.primary, lineWidth: look.hue == hue ? 2 : 0).padding(-3))
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Colour \(Int(hue))")
                        .accessibilityAddTraits(look.hue == hue ? [.isSelected] : [])
                    }
                }
                HStack(spacing: 6) {
                    ForEach(ScoutLook.traits, id: \.self) { trait in
                        Button { look.trait = trait } label: {
                            VStack(spacing: 0) {
                                BuddyView(hue: look.hue, trait: trait, mood: "asleep", size: 34, animated: false)
                                Text(trait).font(.caption2).foregroundStyle(look.trait == trait ? .primary : .secondary)
                            }
                            .padding(.vertical, 4)
                            .frame(maxWidth: .infinity)
                            .background(RoundedRectangle(cornerRadius: 9, style: .continuous)
                                .strokeBorder(look.trait == trait ? Color.accentColor : Color.secondary.opacity(0.25)))
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel(trait)
                        .accessibilityAddTraits(look.trait == trait ? [.isSelected] : [])
                    }
                }
            }
        }
        .padding(16)
    }
}

/// A scout asking leave to run something: what, who asks and why, with Approve, Edit (a shell command) and Deny.
struct ApprovalCardView: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.theme) private var theme
    let project: ProjectInfo
    let approval: ScoutApproval
    @State private var editing = false
    @State private var edited = ""
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 7) {
                if theme.backdrop { PanesDot(color: theme.accent ?? .accentColor) }
                Text("\(approval.title.isEmpty ? project.name : approval.title) asks to run")
                    .themeFont(.subheadline, weight: .semibold)
            }
            Text(approval.command)
                .font(.system(.footnote, design: .monospaced))
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color(.tertiarySystemFill), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                .textSelection(.enabled)
            if let rule = project.rules.first(where: { $0.action == approval.action }) {
                Text("\(rule.label): asks first").themeFont(.caption).foregroundStyle(.secondary)
            }
            HStack(spacing: 8) {
                Button("Approve") { decide(true) }.buttonStyle(.borderedProminent)
                if approval.tool == "Bash" {
                    Button("Edit") { edited = approval.command; editing = true }.buttonStyle(.bordered)
                }
                Button("Deny") { decide(false) }.buttonStyle(.bordered)
                if busy { ProgressView().padding(.leading, 4) }
            }
            .controlSize(.small)
            .disabled(busy || client.connection != .online)
            if let error { Text(error).themeFont(.caption).foregroundStyle(.red) }
        }
        .padding(14)
        // Panes: a neutral card like the rest, its kind in the dot; the other themes keep the orange.
        .background(theme.backdrop ? theme.surfaceColor : Color.orange.opacity(0.1), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous)
            .strokeBorder(theme.backdrop ? Color(.separator).opacity(0.5) : Color.orange.opacity(0.45)))
        .alert("Edit the command", isPresented: $editing) {
            TextField("Command", text: $edited).font(.system(.body, design: .monospaced))
            Button("Approve edited") { decide(true, command: edited) }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("It runs exactly what you approve, once.")
        }
        .accessibilityIdentifier("approval-\(approval.id)")
    }

    private func decide(_ approve: Bool, command: String? = nil) {
        busy = true
        error = nil
        Task {
            do {
                try await client.decideApproval(project: project.id, id: approval.id, approve: approve, command: command)
            } catch {
                self.error = SettingsView.explain(error)
            }
            busy = false
        }
    }
}

/// A check-in in the chat: the scout's buddy, when it ran, how many things need you, and its report.
struct CheckinCardView: View {
    @Environment(\.theme) private var theme
    let card: CheckinCard
    let text: String
    let project: ProjectInfo?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                if let project {
                    BuddyView(hue: project.scout.hue, trait: project.scout.trait,
                              mood: text.isEmpty ? "waking" : card.needsYou > 0 ? "needs_you" : "proud", size: 24, animated: false)
                }
                Text(card.label).themeFont(.subheadline, weight: .semibold)
                if card.needsYou > 0 {
                    Text("\(card.needsYou) need\(card.needsYou == 1 ? "s" : "") you")
                        .font(.caption2.weight(.semibold))
                        .padding(.horizontal, 7).padding(.vertical, 2)
                        .background(Color.orange.opacity(0.2), in: Capsule())
                        .foregroundStyle(Color.orange)
                }
                Spacer(minLength: 4)
                Text(span).themeFont(.caption).foregroundStyle(.secondary)
            }
            Divider()
            if text.isEmpty {
                HStack(spacing: 8) { ProgressView(); Text("Reporting…").foregroundStyle(.secondary) }
            } else {
                ReplyMarkdown(text: text, selectable: true)
            }
        }
        .padding(14)
        .background(theme.surfaceColor, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .accessibilityElement(children: .combine)
    }

    private var span: String {
        guard let started = card.started else { return "" }
        let from = Date(timeIntervalSince1970: started).formatted(date: .omitted, time: .shortened)
        guard let ended = card.ended else { return from }
        let mins = max(1, Int((ended - started) / 60))
        return "\(from) · \(mins) min"
    }
}

/// A thread report (the note a report turn shows) as a card: one line per thread, the names in bold.
struct ReportCardView: View {
    @Environment(\.theme) private var theme
    let text: String
    let project: ProjectInfo

    /// The threads a report names, or nil when the note is something else.
    static func lines(_ text: String) -> [String]? {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if t.hasPrefix("Thread “") { return [String(t.dropFirst("Thread ".count))] }
        guard let r = t.range(of: #"^\d+ threads stopped: "#, options: .regularExpression) else { return nil }
        return t[r.upperBound...].components(separatedBy: " · ")
    }

    var body: some View {
        let lines = Self.lines(text) ?? []
        let needs = lines.filter { $0.contains(" needs you: ") }.count
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                BuddyView(hue: project.scout.hue, trait: project.scout.trait, mood: needs > 0 ? "needs_you" : "proud", size: 22, animated: false)
                Text(lines.count == 1 ? "Report" : "\(lines.count) threads reported").themeFont(.subheadline, weight: .semibold)
                if needs > 0 {
                    Text("\(needs) need\(needs == 1 ? "s" : "") you").font(.caption2.weight(.semibold))
                        .padding(.horizontal, 7).padding(.vertical, 2)
                        .background(Color.orange.opacity(0.2), in: Capsule()).foregroundStyle(Color.orange)
                }
            }
            ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text("•").foregroundStyle(.secondary)
                    Text(Self.styled(line)).themeFont(.subheadline)
                }
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(theme.surfaceColor, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
    }

    /// “Name” rest → the name in bold, then the rest.
    static func styled(_ line: String) -> AttributedString {
        guard line.hasPrefix("“"), let close = line.range(of: "” ") else { return AttributedString(line) }
        var name = AttributedString(String(line[line.index(after: line.startIndex)..<close.lowerBound]))
        name.font = .subheadline.weight(.semibold)
        return name + AttributedString(" " + line[close.upperBound...])
    }
}

/// When a scout checks in: Off, every day or weekdays at a time, or every few hours. `onChange` gets what
/// POST …/checkins takes.
struct ScoutScheduleEditor: View {
    @State private var kind: String
    @State private var time: Date
    @State private var hours: Int
    @State private var weekday: Int
    let onChange: ([String: Any]) -> Void

    init(schedule: ScoutCheckins.Schedule?, onChange: @escaping ([String: Any]) -> Void) {
        let on = schedule?.enabled ?? false
        let kinds = ["every", "weekdays", "weekly"]
        _kind = State(initialValue: on ? (kinds.first { $0 == schedule?.repeatKind } ?? "daily") : "off")
        _weekday = State(initialValue: schedule?.weekday ?? 0)
        let parts = (schedule?.time ?? "08:00").split(separator: ":").compactMap { Int($0) }
        _time = State(initialValue: Calendar.current.date(bySettingHour: parts.first ?? 8, minute: parts.count > 1 ? parts[1] : 0,
                                                          second: 0, of: Date()) ?? Date())
        _hours = State(initialValue: max(1, (schedule?.everyMinutes ?? 120) / 60))
        self.onChange = onChange
    }

    // Each row lays out its own label, so it reads the same in a Form and on a project form's card.
    var body: some View {
        row("Runs") {
            Picker("Runs", selection: $kind) {
                Text("Off").tag("off")
                Text("Every day").tag("daily")
                Text("Weekdays").tag("weekdays")
                Text("Every week").tag("weekly")
                Text("Every few hours").tag("every")
            }
            .pickerStyle(.menu)
        }
        .onChange(of: kind) { send() }
        if kind == "weekly" {
            row("On") {
                Picker("On", selection: $weekday) {
                    ForEach(0..<7, id: \.self) { i in Text(Calendar.current.standaloneWeekdaySymbols[(i + 1) % 7]).tag(i) }
                }
                .pickerStyle(.menu)
            }
            .onChange(of: weekday) { send() }
        }
        if kind == "daily" || kind == "weekdays" || kind == "weekly" {
            row("At") { DatePicker("At", selection: $time, displayedComponents: .hourAndMinute) }
                .onChange(of: time) { send() }
        } else if kind == "every" {
            row("Every \(hours) hour\(hours == 1 ? "" : "s")") { Stepper("Hours", value: $hours, in: 1...12) }
                .onChange(of: hours) { send() }
        }
    }

    private func row<Control: View>(_ label: String, @ViewBuilder control: () -> Control) -> some View {
        HStack {
            Text(label)
            Spacer(minLength: 8)
            control().labelsHidden().fixedSize()
        }
    }

    var payload: [String: Any] {
        switch kind {
        case "off": return ["repeat": "off"]
        case "every": return ["repeat": "every", "every_minutes": hours * 60]
        default:
            let c = Calendar.current.dateComponents([.hour, .minute], from: time)
            var body: [String: Any] = ["repeat": kind, "time": String(format: "%02d:%02d", c.hour ?? 8, c.minute ?? 0)]
            if kind == "weekly" { body["weekday"] = weekday }
            return body
        }
    }

    private func send() { onChange(payload) }
}

/// A scout's leash, its rules and its check-ins, each saved as it's changed.
struct ScoutLeashPage: View {
    @EnvironmentObject private var client: VisionClient
    let projectID: String
    @State private var error: String?
    @State private var checkingIn = false

    private var project: ProjectInfo? { client.projects.first { $0.id == projectID } }

    var body: some View {
        Form {
            if let project {
                Section {
                    Toggle(ScoutWords.askLabel, isOn: Binding(get: { ScoutWords.asks(project.leash) },
                                                              set: { v in save { try await client.setLeash(projectID, ScoutWords.leash(asking: v)) } }))
                } footer: {
                    Text(ScoutWords.askNote)
                }
                Section {
                    ForEach(project.rules) { rule in
                        VStack(alignment: .leading, spacing: 6) {
                            HStack {
                                Text(rule.label).font(.subheadline.weight(rule.set ? .semibold : .regular))
                                Spacer()
                                if rule.set {
                                    Button("Default") { save { try await client.setRule(projectID, action: rule.action, rule: "default") } }
                                        .font(.caption).buttonStyle(.borderless)
                                }
                            }
                            Picker(rule.label, selection: Binding(get: { rule.rule },
                                                                  set: { v in save { try await client.setRule(projectID, action: rule.action, rule: v) } })) {
                                Text("Does it").tag("does")
                                Text("Asks").tag("asks")
                                Text("Never").tag("never")
                            }
                            .pickerStyle(.segmented)
                            .labelsHidden()
                        }
                        .padding(.vertical, 2)
                    }
                    LabeledContent("Go past its usage budget", value: "Never")
                } header: {
                    Text("What it may do alone")
                } footer: {
                    Text("Editing files on its own branch, running tests and committing there are always fine. A call that asks "
                         + "first waits here as a request; never is refused.")
                }
                Section {
                    ScoutScheduleEditor(schedule: project.checkin?.schedule) { body in
                        save { try await client.setCheckins(projectID, body) }
                    }
                    if let last = project.checkin?.lastStarted {
                        LabeledContent("Last", value: Date(timeIntervalSince1970: last).formatted(date: .abbreviated, time: .shortened)
                                       + (project.checkin?.lastNeeds ?? 0 > 0 ? " · \(project.checkin!.lastNeeds) needed you" : ""))
                    }
                    Button(checkingIn || project.checkin?.running != nil ? "Reporting…" : "Status report now") {
                        checkingIn = true
                        save { try await client.checkInNow(projectID) }
                    }
                    .disabled(checkingIn || project.checkin?.running != nil || project.isPaused || client.connection != .online)
                } header: {
                    Text("Status reports")
                } footer: {
                    Text("It looks at what changed, works on its goal and reports in its chat. One notification if something needs you.")
                }
                if let error {
                    Section { Text(error).foregroundStyle(.red) }
                }
            }
        }
        .navigationTitle("Schedule")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func save(_ work: @escaping () async throws -> Void) {
        error = nil
        Task {
            do { try await work() } catch { self.error = SettingsView.explain(error) }
        }
    }
}

// MARK: - a new scout in one sentence

/// New scout: the one-sentence sheet, or (its More options) the full form with what was typed carried over.
struct NewScoutSheet: View {
    let onCreated: () -> Void
    @State private var full: (ScoutDraft, String?)?

    var body: some View {
        if let full {
            NewProjectView(onCreated: onCreated, prefill: full.0, prefillFolder: full.1)
        } else {
            QuickScoutView(onCreated: onCreated) { draft, folder in full = (draft, folder) }
        }
    }
}

/// Say what it should do, and when in the same words; the machine reads out the schedule and picks a name,
/// shown in one line beneath as you type (POST /scouts/draft). Send scout makes it in one call (POST /scouts).
struct QuickScoutView: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.theme) private var theme
    @Environment(\.dismiss) private var dismiss
    let onCreated: () -> Void
    let onMore: (ScoutDraft, String?) -> Void
    @State private var text = ""
    @State private var draft: ScoutDraft?
    @State private var folder: String?
    @State private var pickingFolder = false
    @State private var saving = false
    @State private var error: String?
    @FocusState private var focused: Bool

    static let examples = [
        "Keep the server tests green, check every morning",
        "Every weekday at 7, give me the three tech stories worth reading",
        "Watch my homelab and tell me when something looks off, every 2 hours",
    ]
    @State private var example = QuickScoutView.examples.randomElement()!

    private var trimmed: String { text.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var ready: Bool { !trimmed.isEmpty && client.connection == .online }

    /// "Pip · reports weekdays at 7:00", or how to say when while the box is empty.
    static func line(_ draft: ScoutDraft?) -> String {
        guard let draft, !draft.goal.isEmpty else { return "Say when in the same words: every morning, weekdays at 9, every 2 hours, only when I ask." }
        return "\(draft.look.name) · \(draft.words.prefix(1).lowercased() + draft.words.dropFirst())"
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    ProjectFormCard {
                        TextField("What should it do?", text: $text, prompt: Text("What should it do? e.g. \(example)").foregroundStyle(.secondary), axis: .vertical)
                            .themeFont(.body)
                            .lineLimit(3...8)
                            .focused($focused)
                            .padding(18)
                            .accessibilityIdentifier("quick-scout-text")
                    }
                    Text(Self.line(trimmed.isEmpty ? nil : draft))
                        .themeFont(.footnote)
                        .foregroundStyle(.secondary)
                        .padding(.leading, 4)
                        .accessibilityIdentifier("quick-scout-when")
                        .animation(.snappy(duration: 0.2), value: draft)
                    ProjectFormCard {
                        Button { pickingFolder = true } label: {
                            ProjectFormRow(icon: "folder") {
                                Text(folder.map(homeAsTilde) ?? "No folder: pick one for code or files")
                                    .foregroundStyle(folder == nil ? .secondary : .primary)
                                    .lineLimit(1)
                                    .truncationMode(.head)
                            } trailing: {
                                if folder != nil {
                                    Button { folder = nil } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary) }
                                        .buttonStyle(.plain)
                                        .accessibilityLabel("No folder")
                                } else {
                                    ProjectFormChevron()
                                }
                            }
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Folder")
                    }
                    .padding(.top, 8)
                    if let error {
                        Text(error).themeFont(.footnote).foregroundStyle(.red).padding(.leading, 4)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.top, 12)
                .padding(.bottom, 24)
            }
            .scrollDismissesKeyboard(.interactively)
            .background(theme.backgroundColor.ignoresSafeArea())
            .safeAreaInset(edge: .bottom, spacing: 0) { sendButton }
            .navigationTitle("New scout")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button { dismiss() } label: { Image(systemName: "xmark") }
                        .accessibilityLabel("Cancel")
                }
                ToolbarItem(placement: .primaryAction) {
                    Button("More options") { onMore(draft ?? ScoutDraft(["goal": trimmed]), folder) }
                }
            }
            .onAppear { focused = true }
            // A pause in the typing asks the machine what it would make of it.
            .task(id: trimmed) {
                guard !trimmed.isEmpty else { draft = nil; return }
                try? await Task.sleep(for: .milliseconds(250))
                guard !Task.isCancelled, let d = try? await client.draftScout(trimmed), !Task.isCancelled else { return }
                draft = d
            }
            .sheet(isPresented: $pickingFolder) {
                FolderPickerView(title: "Scout folder", pickLabel: "Use this folder") { path in
                    folder = path
                    pickingFolder = false
                }
                .presentationDetents([.fraction(0.88), .large])
                .presentationDragIndicator(.visible)
            }
        }
    }

    private var sendButton: some View {
        Button { create() } label: {
            Group {
                if saving {
                    ProgressView().tint(.white)
                } else {
                    Text("Send scout").font(.body.weight(.semibold)).foregroundStyle(.white)
                }
            }
            .frame(maxWidth: .infinity)
            .frame(height: 48)
            .background(theme.accent ?? Color.accentColor, in: Capsule())
        }
        .buttonStyle(.plain)
        .disabled(saving || !ready)
        .opacity(ready ? 1 : 0.55)
        .accessibilityLabel("Create")
        .padding(.horizontal, 16)
        .padding(.top, 8)
        .padding(.bottom, 10)
        .background(theme.backgroundColor.ignoresSafeArea())
    }

    private func create() {
        saving = true
        error = nil
        Task {
            do {
                try await client.createScout(text: trimmed, folder: folder)
                onCreated()
            } catch {
                self.error = SettingsView.explain(error)
                saving = false
            }
        }
    }
}

// MARK: - Needs you: every scout's asks in one place

/// The row atop the Scouts list when anything waits on you, in the list's own style; a tap opens Needs you.
struct NeedsBanner: View {
    let count: Int
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 14) {
                Image(systemName: "tray")
                    .font(.system(size: 18))
                    .foregroundStyle(.secondary)
                    .frame(width: 40)
                Text("\(count) waiting on you")
                    .themeFont(size: 17, weight: .semibold)
                Spacer()
                Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
            .contentShape(Rectangle())
        }
        .buttonStyle(ProjectRowStyle())
        .accessibilityIdentifier("needs-banner")
    }
}

/// Needs you: what every scout is waiting on you for (GET /needs), newest first, a row each with its one next
/// step: Approve a request, answer a question, Apply finished work. A tap opens the scout; swipe for the rest.
struct NeedsInboxView: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.theme) private var theme
    @Environment(\.dismiss) private var dismiss
    let onOpen: (ProjectInfo) -> Void
    @State private var items: [NeedItem]?
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Group {
                if let items, items.isEmpty {
                    VStack(spacing: 10) {
                        BuddyView(hue: 140, trait: "sprout", mood: "proud", size: 80, animated: false)
                        Text("Nothing needs you").themeFont(.headline)
                        Text("Your scouts are getting on with it. Anything that needs a yes lands here.")
                            .themeFont(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    }
                    .padding(32)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if let items {
                    List {
                        ForEach(items) { item in
                            NeedRow(item: item, onDone: { Task { await load() } }) {
                                if let p = client.projects.first(where: { $0.id == item.project }) { onOpen(p) }
                            }
                            .listRowBackground(Color.clear)
                            .listRowSeparator(.hidden, edges: .top)
                        }
                    }
                    .listStyle(.plain)
                    .scrollContentBackground(.hidden)
                    .refreshable { await load() }
                } else if let error {
                    Text(error).themeFont(.footnote).foregroundStyle(.red)
                } else {
                    ProgressView()
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(theme.backgroundColor.ignoresSafeArea())
            .navigationTitle("Needs you")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button { dismiss() } label: { Image(systemName: "xmark") }.accessibilityLabel("Close")
                }
            }
            .task { await load() }
            // A scout's frames (a request decided elsewhere, a thread moving on) can change the list.
            .onChange(of: client.projects) { Task { await load() } }
        }
    }

    private func load() async {
        do {
            let list = try await client.fetchNeeds()
            withAnimation(.snappy(duration: 0.25)) { items = list }
            error = nil
        } catch {
            if items == nil { self.error = SettingsView.explain(error) }
        }
    }
}

/// One thing a scout waits on you for: who wants what in bold, the thing itself under it, and its one step as
/// a plain button (Approve, Send, Apply); Deny and Edit by swipe or a long press.
struct NeedRow: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.theme) private var theme
    let item: NeedItem
    let onDone: () -> Void
    let onOpen: () -> Void
    @State private var answer = ""
    @State private var editing = false
    @State private var edited = ""
    @State private var busy = false
    @State private var error: String?

    private var lead: String {
        switch item.kind {
        case .approval: "\(item.look.name) wants to run"
        case .question: "\(item.look.name) asked"
        case .review: "\(item.look.name) finished “\(item.title)”"
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            BuddyView(hue: item.look.hue, trait: item.look.trait, mood: item.kind == .review ? "proud" : "needs_you", size: 32, animated: false)
                .padding(.top, 2)
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline) {
                    Text(lead).themeFont(.subheadline, weight: .semibold).lineLimit(2)
                    Spacer(minLength: 8)
                    Text(Self.ago(item.at)).themeFont(.caption).foregroundStyle(.secondary)
                }
                if !item.text.isEmpty, item.kind == .approval {
                    Text(item.text).font(.system(.footnote, design: .monospaced)).foregroundStyle(.secondary).lineLimit(2)
                } else if !item.text.isEmpty {
                    Text(item.text).themeFont(.footnote).foregroundStyle(.secondary).lineLimit(4)
                }
                actions
                    .disabled(busy || client.connection != .online)
                if let error { Text(error).themeFont(.caption).foregroundStyle(.red) }
            }
        }
        .padding(.vertical, 6)
        .opacity(busy ? 0.5 : 1)
        .contentShape(Rectangle())
        .onTapGesture(perform: onOpen)
        .swipeActions(edge: .trailing, allowsFullSwipe: false) {
            switch item.kind {
            case .approval:
                Button(role: .destructive) { decide(false) } label: { Label("Deny", systemImage: "xmark") }
                Button { decide(true) } label: { Label("Approve", systemImage: "checkmark") }.tint(.green)
            case .review where item.canApply:
                Button { apply() } label: { Label("Apply", systemImage: "arrow.down.to.line") }.tint(.green)
            default:
                EmptyView()
            }
        }
        .contextMenu {
            if item.kind == .approval {
                if item.tool == "Bash" { Button { edited = item.text; editing = true } label: { Label("Edit the command", systemImage: "pencil") } }
                Button(role: .destructive) { decide(false) } label: { Label("Deny", systemImage: "xmark") }
            }
            Button(action: onOpen) { Label("Open \(item.look.name)", systemImage: "arrow.up.right") }
        }
        .alert("Edit the command", isPresented: $editing) {
            TextField("Command", text: $edited).font(.system(.body, design: .monospaced))
            Button("Approve edited") { decide(true, command: edited) }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("It runs exactly what you approve, once.")
        }
        .accessibilityIdentifier("need-\(item.id)")
    }

    /// "15m", "2h", "3d".
    static func ago(_ ts: Double) -> String {
        let s = max(0, Date().timeIntervalSince1970 - ts)
        if s < 3600 { return "\(max(1, Int((s / 60).rounded())))m" }
        if s < 86400 { return "\(Int((s / 3600).rounded()))h" }
        return "\(Int((s / 86400).rounded()))d"
    }

    @ViewBuilder private var actions: some View {
        switch item.kind {
        case .approval:
            HStack(spacing: 16) {
                Button("Approve") { decide(true) }
                Button("Deny") { decide(false) }.foregroundStyle(.secondary)
            }
            .themeFont(.subheadline, weight: .semibold)
            .buttonStyle(.borderless)
        case .question:
            HStack(spacing: 12) {
                TextField("Answer…", text: $answer, axis: .vertical)
                    .textFieldStyle(.plain)
                    .themeFont(.subheadline)
                    .lineLimit(1...4)
                    .onSubmit(send)
                    .overlay(alignment: .bottom) { Divider().offset(y: 4) }
                Button("Send", action: send)
                    .themeFont(.subheadline, weight: .semibold)
                    .buttonStyle(.borderless)
                    .disabled(answer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            .padding(.top, 2)
        case .review:
            if item.canApply {
                Button("Apply") { apply() }
                    .themeFont(.subheadline, weight: .semibold)
                    .buttonStyle(.borderless)
            }
        }
    }

    private func decide(_ approve: Bool, command: String? = nil) {
        run { try await client.decideApproval(project: item.project, id: item.id, approve: approve, command: command) }
    }

    private func apply() {
        run { try await client.landThread(project: item.project, id: item.id) }
    }

    private func send() {
        let text = answer.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        run { _ = try await client.tellThread(project: item.project, id: item.id, text: text) }
    }

    private func run(_ work: @escaping () async throws -> Void) {
        busy = true
        error = nil
        Task {
            do {
                try await work()
                onDone()
            } catch {
                self.error = SettingsView.explain(error)
                busy = false
            }
        }
    }
}
