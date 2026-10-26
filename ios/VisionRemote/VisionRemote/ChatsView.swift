import SwiftUI

/// The side drawer, laid out like the ChatGPT app's: the title, a column of plain icon rows (Projects, Library,
/// Scheduled, Earlier conversations, Usage), then every other chat open on the machine. Tap a chat to switch to
/// it, long-press it for a preview and Close; the floating Chat pill starts a new one and the gear opens Settings. A chat
/// open in a terminal on the machine is listed too (terminal icon); it closes from its own window. A project's chat
/// isn't listed: it opens from the Projects page.
struct ChatsView: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.theme) private var theme
    @Environment(\.dismiss) private var dismiss
    @State private var sheet: Sheet?
    /// The row just tapped, lit briefly: the List swallows the press state of a quick tap.
    @State private var flashed: String?
    /// A chat picked to close: blanked and dropped from the list (its row folding away) while its preview
    /// fades, before the machine confirms; it comes back if the machine hasn't closed it in 10 s.
    @State private var fading: String?
    @State private var closed: Set<String> = []
    /// The main screen is the Projects screen rather than a chat: its row is lit.
    var projectsShown = false
    /// Put the Projects screen in the chat's place (true), or the chat back (false: a chat or a new one picked here).
    var showProjects: (Bool) -> Void = { _ in }
    /// Set when shown in the side drawer; a sheet falls back to `dismiss`.
    var onClose: (() -> Void)? = nil

    enum Sheet: String, Identifiable {
        case library, scheduled, sessions, usage, settings, folder
        var id: String { rawValue }
    }

    private func close() {
        if let onClose { onClose() } else { dismiss() }
    }

    private var rows: [ChatInfo] {
        // A project's chat lives on the Projects page; an archived project's chat (no longer listed) comes back here.
        let projectChats = Set(client.projects.map(\.chat))
        return client.chats.filter { !closed.contains($0.id) && !projectChats.contains($0.id) }
            .sorted { ($0.busy ? 1 : 0, $0.updated) > ($1.busy ? 1 : 0, $1.updated) }
    }

    var body: some View {
        List {
            Group {
                header
                    .padding(.top, 8)
                    .padding(.bottom, 10)
                projectsRow
                navRow("Library", icon: "books.vertical", sheet: .library)
                navRow("Scheduled", icon: "clock", sheet: .scheduled)
                navRow("Earlier conversations", icon: "bubble.left.and.text.bubble.right", sheet: .sessions)
                navRow("Usage", icon: "gauge.with.dots.needle.67percent", sheet: .usage)
            }
            .listRowInsets(EdgeInsets(top: 0, leading: 12, bottom: 0, trailing: 12))
            .listRowBackground(Color.clear)
            .listRowSeparator(.hidden)

            Group {
                sectionTitle("Chats")
                if rows.isEmpty {
                    Text("No chats open")
                        .themeFont(.body)
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 10)
                }
            }
            .listRowInsets(EdgeInsets(top: 0, leading: 12, bottom: 0, trailing: 12))
            .listRowBackground(Color.clear)
            .listRowSeparator(.hidden)

            ForEach(rows) { chat in
                Button {
                    flash("chat:" + chat.id) { open(chat) }
                } label: {
                    row(chat)
                }
                .buttonStyle(DrawerRowStyle(selected: !projectsShown && chat.id == client.active?.id, flashed: flashed == "chat:" + chat.id))
                // Long-press lifts a preview of the chat with its actions, as in the ChatGPT app; a tap on the
                // preview opens the chat. Laid over the row, it takes the row's plain taps too.
                .overlay(ChatRowMenu(client: client, chat: chat) {
                    flash("chat:" + chat.id) { open(chat) }
                } open: { open(chat) } closing: { fading = chat.id } close: { closeChat(chat.id) })
                .opacity(fading == chat.id ? 0 : 1)
                .listRowInsets(EdgeInsets(top: 1, leading: 12, bottom: 1, trailing: 12))
                .listRowBackground(Color.clear)
                .listRowSeparator(.hidden)
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .environment(\.defaultMinListRowHeight, 0)
        .background(theme.backgroundColor)
        // The Chat pill and the gear float over the bottom of the list, which scrolls on under them.
        .safeAreaInset(edge: .bottom, spacing: 0) { bottomBar }
        .onChange(of: client.chats.map(\.id)) { _, ids in
            closed.formIntersection(ids)
            if let f = fading, !ids.contains(f) { fading = nil }
        }
        #if DEBUG
        .task {
            // Development: `--drawer-sheet library|scheduled|…` opens one of the drawer's pages (simulator screenshots).
            if let i = CommandLine.arguments.firstIndex(of: "--drawer-sheet"), i + 1 < CommandLine.arguments.count,
               let target = Sheet(rawValue: CommandLine.arguments[i + 1]) {
                try? await Task.sleep(for: .seconds(1))
                sheet = target
            }
        }
        #endif
        .sheet(item: $sheet) { which in
            Group {
                switch which {
                case .library: LibraryView()
                case .scheduled: ScheduledView()
                case .sessions: SessionsView()
                case .usage: UsageView()
                case .settings: SettingsView()
                case .folder:
                    FolderPickerView { path in
                        sheet = nil
                        showProjects(false)
                        client.newConversation(in: path)
                        close()
                    }
                }
            }
            // Settings sits at full height and doesn't resize.
            .presentationDetents(which == .settings ? [.large] : [.fraction(0.88), .large])
            .presentationDragIndicator(which == .settings ? .hidden : .visible)
        }
    }

    private var header: some View {
        Text("Vision")
            .themeFont(size: 26, weight: .bold)
            .padding(.horizontal, 12)
            .accessibilityAddTraits(.isHeader)
    }

    private func navRow(_ title: String, icon: String, sheet target: Sheet) -> some View {
        pageRow(title, icon: icon, id: target.rawValue) { sheet = target }
    }

    /// Projects isn't a sheet: it takes the chat's place on the main screen, and its row stays lit while it's there.
    private var projectsRow: some View {
        pageRow("Scouts", icon: "sparkles", id: "projects", selected: projectsShown, badge: projectsWaiting) {
            showProjects(true)
            close()
        }
    }

    /// `badge`: a count to show small on the right, in the theme's accent (projects' threads that need you).
    private func pageRow(_ title: String, icon: String, id: String, selected: Bool = false, badge: Int = 0,
                         action: @escaping () -> Void) -> some View {
        Button { flash(id, then: action) } label: {
            HStack(spacing: 16) {
                Image(systemName: icon)
                    .font(.system(size: 19, weight: .medium))
                    .frame(width: 26)
                Text(title)
                    .themeFont(size: 17, weight: .medium)
                Spacer(minLength: 0)
                if badge > 0 {
                    Text("\(badge)")
                        .themeFont(size: 12, weight: .semibold)
                        .monospacedDigit()
                        .foregroundStyle(.white)
                        .padding(.horizontal, 6)
                        .frame(minWidth: 18, minHeight: 18)
                        .background(theme.accent ?? Color.accentColor, in: Capsule())
                        .accessibilityLabel("\(badge) need\(badge == 1 ? "s" : "") you")
                }
            }
            .foregroundStyle(.primary)
            .frame(height: 46)
        }
        .buttonStyle(DrawerRowStyle(selected: selected, flashed: flashed == id))
        .accessibilityAddTraits(selected ? [.isSelected] : [])
    }

    /// Across every scout: threads waiting on an answer or a look from you, and requests to run something.
    private var projectsWaiting: Int {
        client.projects.reduce(0) { $0 + ScoutWords.needs($1, counts: client.threadCounts($1)) }
    }

    private func sectionTitle(_ title: String) -> some View {
        Text(title)
            .themeFont(size: 17, weight: .semibold)
            .padding(.top, 22)
            .padding(.bottom, 4)
            .padding(.horizontal, 12)
            .accessibilityAddTraits(.isHeader)
    }

    private func open(_ chat: ChatInfo) {
        // The new transcript lands whole, in a still transaction of its own, a tick before the drawer
        // moves: inside the drawer's animated one every bubble faded and slid in with it.
        var still = Transaction()
        still.disablesAnimations = true
        withTransaction(still) {
            showProjects(false)
            client.select(chat: chat.id)
        }
        DispatchQueue.main.async { close() }
    }

    private func closeChat(_ id: String) {
        withAnimation(.smooth(duration: 0.4)) { _ = closed.insert(id) }
        client.close(chat: id)
        Task { @MainActor in
            try? await Task.sleep(for: .seconds(10))
            guard closed.contains(id), client.chats.contains(where: { $0.id == id }) else { return }
            withAnimation(.snappy(duration: 0.35)) {
                closed.remove(id)
                if fading == id { fading = nil }
            }
        }
    }

    /// Lights the row, lets that frame draw, runs the action, then fades the light out.
    private func flash(_ id: String, then action: @escaping () -> Void) {
        flashed = id
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(80))
            action()
            try? await Task.sleep(for: .milliseconds(150))
            withAnimation(.easeOut(duration: 0.3)) {
                if flashed == id { flashed = nil }
            }
        }
    }

    private var bottomBar: some View {
        HStack {
            Button {
                showProjects(false)
                client.newConversation()
                close()
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "square.and.pencil").font(.system(size: 17, weight: .semibold))
                    Text("Chat").themeFont(size: 17, weight: .semibold)
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
            .accessibilityLabel("New chat")
            // The same, somewhere else: pick the machine folder the new chat works in.
            Button { sheet = .folder } label: {
                Image(systemName: "folder.badge.plus")
                    .font(.system(size: 18, weight: .medium))
                    .foregroundStyle(.primary)
                    .frame(width: 34, height: 34)
                    .padding(gearPadding)
                    .contentShape(Circle())
            }
            .drawerGlassButton()
            .disabled(client.connection != .online)
            .opacity(client.connection == .online ? 1 : 0.5)
            .accessibilityLabel("New chat in a folder")
            Spacer()
            Button { sheet = .settings } label: {
                Image(systemName: "gearshape")
                    .font(.system(size: 19, weight: .medium))
                    .foregroundStyle(.primary)
                    // The glass button pads this out to the Chat pill's 48 pt (before iOS 26 the label does).
                    .frame(width: 34, height: 34)
                    .padding(gearPadding)
                    // Glass isn't hit-testable, so without this only the glyph itself takes the tap.
                    .contentShape(Circle())
            }
            .drawerGlassButton()
            .accessibilityLabel("Settings")
        }
        .padding(.horizontal, 16)
        .padding(.bottom, 6)
    }

    /// Pre-iOS 26 has no glass button to pad the gear's glyph out: its label carries the 7 pt itself, so the
    /// material circle and the tap stay 48 pt.
    private var gearPadding: CGFloat {
        if #available(iOS 26.0, *) { 0 } else { 7 }
    }

    private func row(_ chat: ChatInfo) -> some View {
        HStack(spacing: 10) {
            if chat.busy || chat.waiting {
                Circle()
                    .fill(chat.waiting ? Color.purple : Color.orange)
                    .frame(width: 8, height: 8)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(chat.title.isEmpty ? "New chat" : chat.title)
                    .themeFont(size: 17)
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                    .truncationMode(.tail)
                HStack(spacing: 5) {
                    if chat.isTerminal {
                        Image(systemName: "terminal").imageScale(.small)
                    }
                    if !chat.model.isEmpty {
                        Text(chat.model + (chat.effort.isEmpty ? "" : " \(chat.effort)"))
                        Text("·")
                    }
                    Text(status(chat))
                    // Where it works on the machine, home written as ~; a long path gives way in the middle.
                    if !chat.folder.isEmpty {
                        Text("·")
                        Text(chat.folder)
                            .truncationMode(.middle)
                            .layoutPriority(-1)
                    }
                }
                .themeFont(.footnote)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            }
            Spacer(minLength: 0)
        }
        .frame(height: 52)
        .contentShape(Rectangle())
    }

    private func status(_ chat: ChatInfo) -> String {
        if chat.waiting { return "needs your answer" }
        if chat.busy { return "working…" }
        guard chat.updated > 0 else { return "idle" }
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .short
        return f.localizedString(for: Date(timeIntervalSince1970: chat.updated), relativeTo: Date())
    }
}

/// UIKit's context menu, laid over a drawer row: SwiftUI's own has no way to act on a tap on the preview.
/// A SwiftUI button under it never sees the touch, so this view takes the row's plain tap as well.
/// A terminal chat closes by quitting that terminal (the server relays /quit). Close keeps the menu up for a
/// beat so the card is told to fade where it is rather than fly back into a row that's about to go; the row
/// folds away as it fades.
private struct ChatRowMenu: UIViewRepresentable {
    let client: VisionClient
    let chat: ChatInfo
    let tap: () -> Void
    let open: () -> Void
    let closing: () -> Void
    let close: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> UIView {
        let view = UIView()
        let interaction = UIContextMenuInteraction(delegate: context.coordinator)
        context.coordinator.interaction = interaction
        view.addInteraction(interaction)
        view.addGestureRecognizer(UITapGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.tapped)))
        return view
    }

    func updateUIView(_ view: UIView, context: Context) {
        context.coordinator.row = self
    }

    final class Coordinator: NSObject, UIContextMenuInteractionDelegate {
        var row: ChatRowMenu?
        weak var interaction: UIContextMenuInteraction?
        /// Set by Close: the card fades out in place while the row folds away.
        private var closing = false

        @objc func tapped() { row?.tap() }

        private func startClose() {
            guard !closing else { return }
            closing = true
            row?.closing()
            interaction?.dismissMenu()
        }

        func contextMenuInteraction(_ interaction: UIContextMenuInteraction,
                                    willEndFor configuration: UIContextMenuConfiguration,
                                    animator: UIContextMenuInteractionAnimating?) {
            guard closing else { return }
            // The row folds while the card fades, not after it: waiting left a gap that then snapped shut.
            // A tick's wait lets UIKit take the (nil) dismissal target before the row starts to go.
            DispatchQueue.main.async { [weak self] in
                guard let self, self.closing else { return }
                self.closing = false
                self.row?.close()
            }
        }

        func contextMenuInteraction(_ interaction: UIContextMenuInteraction,
                                    configurationForMenuAtLocation location: CGPoint) -> UIContextMenuConfiguration? {
            guard let row else { return nil }
            return UIContextMenuConfiguration(identifier: nil) {
                let host = UIHostingController(rootView: ChatPreview(client: row.client, chat: row.chat))
                host.preferredContentSize = ChatPreview.size
                return host
            } actionProvider: { _ in
                let quit = row.chat.isTerminal
                return UIMenu(children: [
                    UIAction(title: quit ? "Quit" : "Close", image: UIImage(systemName: quit ? "power" : "xmark"),
                             attributes: [.destructive, .keepsMenuPresented]) { [weak self] _ in
                        self?.startClose()
                    },
                ])
            }
        }

        // What lifts while the finger is held: the row itself (its list cell, cut to the row's rounded shape) on a
        // soft grey, not this clear view on UIKit's default white.
        func contextMenuInteraction(_ interaction: UIContextMenuInteraction,
                                    configuration: UIContextMenuConfiguration,
                                    highlightPreviewForItemWithIdentifier identifier: NSCopying) -> UITargetedPreview? {
            lifted(interaction)
        }

        func contextMenuInteraction(_ interaction: UIContextMenuInteraction,
                                    configuration: UIContextMenuConfiguration,
                                    dismissalPreviewForItemWithIdentifier identifier: NSCopying) -> UITargetedPreview? {
            // nil: the card fades where it stands instead of morphing back into the row.
            closing ? nil : lifted(interaction)
        }

        private func lifted(_ interaction: UIContextMenuInteraction) -> UITargetedPreview? {
            guard let view = interaction.view, view.window != nil else { return nil }
            var cell: UIView? = view.superview
            while let c = cell, !(c is UICollectionViewCell) { cell = c.superview }
            let target = cell ?? view
            let parameters = UIPreviewParameters()
            parameters.backgroundColor = .secondarySystemBackground
            parameters.visiblePath = UIBezierPath(roundedRect: view.convert(view.bounds, to: target), cornerRadius: 14)
            return UITargetedPreview(view: target, parameters: parameters)
        }

        func contextMenuInteraction(_ interaction: UIContextMenuInteraction,
                                    willPerformPreviewActionForMenuWith configuration: UIContextMenuConfiguration,
                                    animator: UIContextMenuInteractionCommitAnimating) {
            guard let row else { return }
            animator.preferredCommitStyle = .dismiss
            animator.addCompletion { row.open() }
        }
    }
}

/// The card a long-pressed chat lifts into: the time it last moved, then its last two exchanges from
/// the top of a turn, cut off at the bottom. A chat whose transcript isn't on the phone yet is fetched.
private struct ChatPreview: View {
    static let size = CGSize(width: 340, height: 420)

    @ObservedObject var client: VisionClient
    let chat: ChatInfo
    @Environment(\.theme) private var theme

    private var turns: [Message] {
        let all = (client.transcripts[chat.id] ?? []).filter { $0.role != .note && !$0.trimmedText.isEmpty }
        let users = all.indices.filter { all[$0].role == .user }
        return Array(all[(users.suffix(2).first ?? all.startIndex)...])
    }

    var body: some View {
        VStack(spacing: 14) {
            if chat.updated > 0 {
                Text(Date(timeIntervalSince1970: chat.updated).formatted(.dateTime.weekday(.wide).hour().minute()))
                    .themeFont(.caption, weight: .medium)
                    .foregroundStyle(.secondary)
            }
            if turns.isEmpty {
                Text("No messages yet")
                    .themeFont(.body)
                    .foregroundStyle(.secondary)
                    .padding(.top, 40)
            }
            ForEach(turns) { turn in
                if turn.role == .user {
                    Text(turn.trimmedText)
                        .themeFont(size: 16)
                        .lineLimit(6)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 9)
                        .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(Color(.secondarySystemBackground)))
                        .frame(maxWidth: 260, alignment: .trailing)
                        .frame(maxWidth: .infinity, alignment: .trailing)
                } else {
                    Text(inline(turn.trimmedText))
                        .themeFont(size: 16)
                        .lineLimit(14)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
        .padding(16)
        .frame(width: Self.size.width, height: Self.size.height, alignment: .top)
        .clipped()
        .background(theme.backgroundColor)
        .task {
            if client.transcripts[chat.id] == nil { await client.fetchHistory(chat: chat.id) }
        }
    }

    private func inline(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
    }
}

/// Drawer rows: plain text on the panel, a soft rounded highlight for the chat on screen and while pressed.
private struct DrawerRowStyle: ButtonStyle {
    let selected: Bool
    var flashed = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .padding(.horizontal, 12)
            .background(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .fill(Color(.label).opacity(configuration.isPressed || flashed ? 0.1 : selected ? 0.06 : 0))
            )
            // The whole row takes the tap, not just the icon and text (a clear fill isn't hit-tested).
            .contentShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
            // Light up at once on touch, fade out slowly so even a quick tap shows.
            .animation(configuration.isPressed ? nil : .easeOut(duration: 0.3), value: configuration.isPressed)
    }
}

struct PressScaleStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.94 : 1)
            .animation(.spring(duration: 0.2, bounce: 0.3), value: configuration.isPressed)
    }
}

extension View {
    /// The system's glass button: it swells under the finger like interactive glass and still fires on every
    /// tap. Interactive glass on a plain Button's label ran its own press recogniser, which often took the tap.
    @ViewBuilder func drawerGlassButton() -> some View {
        if #available(iOS 26.0, *) {
            buttonStyle(.glass).buttonBorderShape(.circle)
        } else {
            buttonStyle(PressScaleStyle())
                .background(Circle().fill(.regularMaterial))
                .shadow(color: .black.opacity(0.12), radius: 8, y: 3)
        }
    }
}

/// Where a new chat should work: the recent folders first, then the machine's folders to walk through.
/// A tap on a recent one starts the chat there; a tap on a folder opens it, and the button at the
/// bottom starts the chat in the folder being shown.
struct FolderPickerView: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.dismiss) private var dismiss
    var title = "New chat in a folder"
    var pickLabel = "Start chat here"
    let onPick: (String) -> Void
    @State private var listing: FolderListing?
    @State private var recent: [FolderListing.Place] = []
    @State private var error: String?

    var body: some View {
        NavigationStack {
            List {
                if !recent.isEmpty {
                    Section("Recent") {
                        ForEach(recent) { place in
                            Button { onPick(place.path) } label: {
                                Label(place.label, systemImage: "clock.arrow.circlepath")
                                    .lineLimit(1)
                                    .truncationMode(.head)
                            }
                            .foregroundStyle(.primary)
                        }
                    }
                }
                if let listing {
                    Section {
                        if !listing.parent.isEmpty {
                            Button { open(listing.parent) } label: { Label("Up one level", systemImage: "arrow.turn.left.up") }
                                .foregroundStyle(.secondary)
                        }
                        ForEach(listing.folders) { folder in
                            Button { open(folder.path) } label: {
                                HStack {
                                    Label(folder.name, systemImage: "folder").lineLimit(1)
                                    Spacer(minLength: 8)
                                    Image(systemName: "chevron.right")
                                        .font(.system(size: 13, weight: .semibold))
                                        .foregroundStyle(.tertiary)
                                        .accessibilityHidden(true)
                                }
                            }
                            .foregroundStyle(.primary)
                        }
                        if listing.folders.isEmpty {
                            Text("No folders in here").foregroundStyle(.secondary)
                        }
                        if let more = listing.more, more > 0 {
                            Text("and \(more) more").foregroundStyle(.secondary)
                        }
                    } header: {
                        Text(listing.label).textCase(nil)
                    }
                } else if let error {
                    Text(error).foregroundStyle(.secondary)
                } else {
                    HStack { Spacer(); ProgressView(); Spacer() }.listRowBackground(Color.clear)
                }
            }
            .themeFont(.body)
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) { Button("Cancel") { dismiss() } }
            }
            .safeAreaInset(edge: .bottom) {
                if let listing {
                    Button { onPick(listing.path) } label: {
                        Text(pickLabel)
                            .themeFont(.body, weight: .semibold)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 6)
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                    .padding(.horizontal, 20)
                    .padding(.bottom, 8)
                    .accessibilityHint("Picks \(listing.label)")
                }
            }
            .task { open("") }
        }
    }

    /// Show a folder ("" = where the picker starts). A folder that can't be read leaves the one shown.
    private func open(_ path: String) {
        Task {
            do {
                let next = try await client.fetchFolders(path: path)
                if let places = next.recent { recent = places }
                listing = next
                error = nil
            } catch {
                if listing == nil { self.error = SettingsView.explain(error) } else { client.toast = "Couldn't open that folder" }
            }
        }
    }
}

