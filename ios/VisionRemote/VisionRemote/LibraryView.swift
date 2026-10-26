import SwiftUI
import UIKit

/// Bottom sheet: every picture Vision showed in a reply and everything sent from this phone, newest
/// first in a Photos-style grid. Tap a picture or clip to view it full screen and swipe through the rest;
/// long-press anything (or the viewer's bin) to delete it from the machine, or Select to delete several at once.
struct LibraryView: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.dismiss) private var dismiss
    /// A project's Library instead (a tab of its screen, inside that screen's navigation): what was sent in its
    /// chat, what its threads made, and its replies' pictures. A thread's file only ever leaves the list.
    var project: String? = nil
    @State private var items: [LibraryItem]?
    @State private var error: String?
    @State private var filter: Filter
    @State private var viewing: LibraryItem?
    /// Two or more items picked in Select mode, waiting on the delete alert (one goes without asking).
    @State private var confirming: [LibraryItem]?
    @State private var deleting = false
    @State private var deleteError: String?
    @State private var selecting = false
    @State private var selected: Set<String> = []

    init(project: String? = nil) {
        self.project = project
        _filter = State(initialValue: project == nil ? .vision : .threads)
    }

    enum Filter: String, CaseIterable {
        case threads = "From threads", vision = "From Vision", you = "From you", files = "Files"
    }

    private var filters: [Filter] { project == nil ? [.vision, .you, .files] : Filter.allCases }

    private var shown: [LibraryItem] {
        (items ?? []).filter {
            switch filter {
            case .threads: return $0.source == "thread"
            case .vision: return $0.source == "vision"
            case .you: return $0.source == "you" && $0.kind != "file"
            case .files: return $0.kind == "file"
            }
        }
    }

    var body: some View {
        if project != nil {
            core   // inside the project's own navigation: no stack, title or Close of its own
        } else {
            NavigationStack {
                core
                    .navigationTitle("Library")
                    .navigationBarTitleDisplayMode(.inline)
                    .toolbar { toolbar }
            }
        }
    }

    @ToolbarContentBuilder private var toolbar: some ToolbarContent {
        if selecting {
            ToolbarItem(placement: .topBarLeading) {
                Button(allSelected ? "Deselect All" : "Select All") {
                    selected = allSelected ? [] : Set(shown.map(\.path))
                }
            }
            ToolbarItem(placement: .topBarTrailing) {
                Button("Done") { endSelecting() }.fontWeight(.semibold).disabled(deleting)
            }
        } else {
            if !shown.isEmpty {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Select") { withAnimation(.snappy(duration: 0.2)) { selecting = true } }
                }
            }
            ToolbarItem(placement: .topBarTrailing) {
                Button { dismiss() } label: { Image(systemName: "xmark") }
                    .accessibilityLabel("Close")
            }
        }
    }

    private var core: some View {
        content
        .safeAreaInset(edge: .bottom) {
            if selecting { bottomBar.transition(.move(edge: .bottom).combined(with: .opacity)) }
        }
        .onChange(of: filter) { selected = [] }
        // The same centred alert as removing a saved address in Settings.
        .alert("Delete \(confirming?.count ?? 0) items?", isPresented: Binding(
            get: { confirming != nil }, set: { if !$0 { confirming = nil } }
        ), presenting: confirming) { doomed in
            Button("Cancel", role: .cancel) {}
            Button("Delete", role: .destructive) { Task { await deleteConfirmed(doomed) } }
        } message: { doomed in
            Text(project == nil ? "They're deleted from the computer too."
                                : "What you sent is deleted from the computer; files threads made stay there and only leave this list.")
        }
        .task { await load() }
        .fullScreenCover(item: $viewing) { item in
            LibraryViewer(items: shown, start: item) { try await delete($0) }
        }
        .alert("Couldn't delete it", isPresented: Binding(
            get: { deleteError != nil }, set: { if !$0 { deleteError = nil } }
        )) {
        } message: {
            Text(deleteError ?? "")
        }
    }

    @ViewBuilder private var content: some View {
        if items == nil, let error {
            ContentUnavailableView {
                Label("Couldn't load the library", systemImage: "wifi.exclamationmark")
            } description: {
                Text(error)
            } actions: {
                Button("Try again") { Task { await load() } }.buttonStyle(.bordered)
            }
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    chips
                    if items == nil {
                        ProgressView().frame(maxWidth: .infinity).padding(.top, 80)
                    } else if shown.isEmpty {
                        ContentUnavailableView(filter == .files ? "No files yet" : "Nothing here yet",
                                               systemImage: filter == .files ? "doc" : "photo.on.rectangle",
                                               description: Text(emptyText))
                            .padding(.top, 40)
                    } else if filter == .files {
                        files(shown)
                    } else {
                        // A thread's reports and documents sit under its pictures.
                        grid(shown.filter { $0.kind != "file" })
                        files(shown.filter { $0.kind == "file" })
                    }
                }
                .padding(.bottom, 24)
            }
            .refreshable { await load() }
        }
    }

    private var emptyText: String {
        switch filter {
        case .threads: "Pictures, reports and documents the project's threads make land here."
        case .files: project == nil ? "Files you send Vision from the + menu land here." : "Files sent in this project and those its threads make land here."
        default: project == nil ? "Pictures Vision shows you and photos you send it land here." : "Pictures shown and photos sent in this project land here."
        }
    }

    /// The filters: capsules on the Library sheet; inside a project (under its own tab chips) a quieter row
    /// of small words, the one showing in the text colour with a rule under it.
    private var chips: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: project == nil ? 8 : 18) {
                ForEach(filters, id: \.self) { f in
                    Button {
                        withAnimation(.snappy(duration: 0.2)) { filter = f }
                    } label: {
                        if project == nil {
                            Text(f.rawValue)
                                .themeFont(.subheadline, weight: .medium)
                                .padding(.horizontal, 14)
                                .padding(.vertical, 8)
                                .foregroundStyle(filter == f ? Color(.systemBackground) : Color(.label))
                                .background(Capsule().fill(filter == f ? Color(.label) : Color(.secondarySystemFill)))
                        } else {
                            Text(f.rawValue)
                                .themeFont(.footnote, weight: .medium)
                                .foregroundStyle(filter == f ? .primary : .secondary)
                                .padding(.vertical, 6)
                                .overlay(alignment: .bottom) {
                                    Rectangle().fill(Color.primary).frame(height: 1.5).opacity(filter == f ? 1 : 0)
                                }
                        }
                    }
                    .buttonStyle(.plain)
                    .accessibilityAddTraits(filter == f ? [.isSelected] : [])
                }
            }
            .padding(.horizontal, project == nil ? 16 : 20)
        }
        .padding(.top, project == nil ? 4 : 0)
    }

    private func grid(_ items: [LibraryItem]) -> some View {
        LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 2), count: 3), spacing: 2) {
            ForEach(items) { item in
                LibraryTile(item: item, selected: selecting ? selected.contains(item.path) : nil) {
                    if selecting { toggle(item) } else { viewing = item }
                } onDelete: { requestDelete([item]) }
            }
        }
    }

    private func files(_ items: [LibraryItem]) -> some View {
        VStack(spacing: 0) {
            ForEach(items) { item in
                HStack(spacing: 14) {
                    if selecting {
                        SelectionMark(on: selected.contains(item.path))
                            .transition(.move(edge: .leading).combined(with: .opacity))
                    }
                    Image(systemName: fileIcon(item.name))
                        .font(.system(size: 20))
                        .foregroundStyle(.secondary)
                        .frame(width: 40, height: 40)
                        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color(.secondarySystemFill)))
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.name).themeFont(.body).lineLimit(1).truncationMode(.middle)
                        Text("\(ByteCountFormatter.string(fromByteCount: Int64(item.size), countStyle: .file)) · \(item.date.formatted(.relative(presentation: .named)))")
                            .themeFont(.footnote).foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 8)
                .contentShape(Rectangle())
                .onTapGesture { if selecting { toggle(item) } }
                .contextMenu {
                    copyPath(item)
                    deleteButton(item)
                }
            }
        }
    }

    @ViewBuilder private func copyPath(_ item: LibraryItem) -> some View {
        Button { UIPasteboard.general.string = item.path } label: { Label("Copy path", systemImage: "doc.on.doc") }
    }

    private func deleteButton(_ item: LibraryItem) -> some View {
        Button(role: .destructive) { requestDelete([item]) } label: { Label("Delete", systemImage: "trash") }
    }

    private func fileIcon(_ name: String) -> String {
        switch (name as NSString).pathExtension.lowercased() {
        case "pdf": return "doc.richtext"
        case "zip", "gz", "tar": return "doc.zipper"
        case "txt", "md", "csv", "json": return "doc.text"
        case "mp3", "wav", "m4a": return "waveform"
        default: return "doc"
        }
    }

    private var allSelected: Bool {
        !shown.isEmpty && shown.allSatisfy { selected.contains($0.path) }
    }

    /// Select mode's bar: the count in the middle, the bin on its own on the right.
    private var bottomBar: some View {
        ZStack {
            Text(selected.isEmpty ? "Select Items" : "\(selected.count) Selected")
                .themeFont(.subheadline, weight: .semibold)
                .contentTransition(.numericText())
                .padding(.horizontal, 20)
                .frame(height: 50)
                .libraryGlass(in: Capsule())
            Button { requestDelete(shown.filter { selected.contains($0.path) }) } label: {
                ZStack {
                    if deleting {
                        ProgressView()
                    } else {
                        Image(systemName: "trash")
                            .font(.system(size: 20, weight: .medium))
                            .foregroundStyle(selected.isEmpty ? Color.secondary : Color.red)
                    }
                }
                .frame(width: 50, height: 50)
                .contentShape(Circle())
            }
            .buttonStyle(.plain)
            .libraryGlass(in: Circle(), interactive: true)
            .disabled(selected.isEmpty || deleting)
            .accessibilityLabel("Delete selected")
            .frame(maxWidth: .infinity, alignment: .trailing)
        }
        .padding(.horizontal, 16)
        .padding(.bottom, 4)
    }

    /// One item goes straight away; two or more ask first.
    private func requestDelete(_ doomed: [LibraryItem]) {
        if doomed.count == 1 {
            Task { await deleteConfirmed(doomed) }
        } else {
            confirming = doomed
        }
    }

    private func toggle(_ item: LibraryItem) {
        UISelectionFeedbackGenerator().selectionChanged()
        withAnimation(.snappy(duration: 0.15)) {
            if selected.remove(item.path) == nil { selected.insert(item.path) }
        }
    }

    private func endSelecting() {
        withAnimation(.snappy(duration: 0.2)) {
            selecting = false
            selected = []
        }
    }

    /// One request for the lot; whatever the machine deleted leaves the grid even if the rest failed.
    private func deleteConfirmed(_ doomed: [LibraryItem]) async {
        let paths = doomed.map(\.path)
        deleting = true
        defer { deleting = false }
        do {
            let done: Set<String>
            if let project {
                // A project's Library takes them one by one: only what was sent from here is deleted from the machine.
                var gone: Set<String> = []
                for path in paths {
                    do {
                        try await client.deleteLibraryItem(path: path, project: project)
                        gone.insert(path)
                    } catch {}
                }
                done = gone
            } else {
                done = Set(try await client.deleteLibraryItems(paths: paths))
            }
            UINotificationFeedbackGenerator().notificationOccurred(.success)
            withAnimation(.snappy(duration: 0.25)) {
                items?.removeAll { done.contains($0.path) }
                selected.subtract(done)
            }
            if done.count < paths.count {
                deleteError = "\(paths.count - done.count) of them weren't in the library any more."
                await load()
            }
            if selecting && (selected.isEmpty || shown.isEmpty) { endSelecting() }
        } catch {
            deleteError = error.localizedDescription
        }
    }

    private func delete(_ item: LibraryItem) async throws {
        try await client.deleteLibraryItem(path: item.path, project: project)
        withAnimation(.snappy(duration: 0.25)) { items?.removeAll { $0.path == item.path } }
    }

    private func load() async {
        do {
            let fresh = try await client.fetchLibrary(project: project)
            withAnimation(.snappy(duration: 0.25)) { items = fresh }
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}

/// The round tick Photos uses for a picked item; white over a picture, grey in the files list.
private struct SelectionMark: View {
    let on: Bool
    var overImage = false

    var body: some View {
        Image(systemName: on ? "checkmark.circle.fill" : "circle")
            .font(.system(size: 22))
            .symbolRenderingMode(on ? .palette : .monochrome)
            .foregroundStyle(on ? Color.white : overImage ? Color.white.opacity(0.9) : Color.secondary, Color.accentColor)
            .shadow(color: .black.opacity(overImage && !on ? 0.4 : 0), radius: 2)
            .contentTransition(.symbolEffect(.replace))
    }
}

/// A square cell: the thumbnail filling it, a play badge on clips, a tick while selecting.
private struct LibraryTile: View {
    @EnvironmentObject private var client: VisionClient
    let item: LibraryItem
    /// nil when not selecting, else whether this one is picked.
    let selected: Bool?
    let action: () -> Void
    let onDelete: () -> Void
    @State private var image: UIImage?

    var body: some View {
        Button(action: action) {
            Color(.secondarySystemFill)
                .aspectRatio(1, contentMode: .fit)
                .overlay {
                    if let image {
                        Image(uiImage: image).resizable().scaledToFill().transition(.opacity)
                    }
                }
                .clipped()
                .overlay(alignment: .bottomLeading) {
                    if item.kind == "video" {
                        Image(systemName: "play.fill")
                            .font(.system(size: 11, weight: .bold))
                            .foregroundStyle(.white)
                            .shadow(color: .black.opacity(0.5), radius: 3)
                            .padding(6)
                    }
                }
                .overlay {
                    if selected == true { Color.white.opacity(0.25) }
                }
                .overlay(alignment: .bottomTrailing) {
                    if let selected {
                        SelectionMark(on: selected, overImage: true).padding(5).transition(.opacity)
                    }
                }
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .contextMenu {
            if !item.caption.isEmpty { Text(item.caption) }
            Button { UIPasteboard.general.string = item.path } label: { Label("Copy path", systemImage: "doc.on.doc") }
            Button(role: .destructive, action: onDelete) { Label("Delete", systemImage: "trash") }
        }
        .task(id: item.path) {
            guard image == nil, let thumb = await client.thumbnail(path: item.path) else { return }
            withAnimation(.easeOut(duration: 0.2)) { image = thumb }
        }
        .accessibilityLabel(item.caption.isEmpty ? (item.kind == "video" ? "Video" : "Picture") : item.caption)
    }
}

private extension View {
    @ViewBuilder func libraryGlass<S: Shape>(in shape: S, interactive: Bool = false) -> some View {
        if #available(iOS 26, *) {
            glassEffect(interactive ? .regular.interactive() : .regular, in: shape)
        } else {
            background(.regularMaterial, in: shape)
        }
    }
}
