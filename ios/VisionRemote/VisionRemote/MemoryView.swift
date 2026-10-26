import SwiftUI

/// One remembered fact: its whole line in the file (what the machine deletes by), and the line taken apart.
struct MemoryFact: Codable, Identifiable, Hashable {
    let line: String
    let date: String            // YYYY-MM-DD, "" when the line carries none
    let text: String
    var id: String { line }

    private static let stored: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        f.locale = Locale(identifier: "en_US_POSIX")
        return f
    }()

    /// "27 Sep", with the year once it is not this one.
    var day: String {
        guard let d = Self.stored.date(from: date) else { return date }
        let thisYear = Calendar.current.isDate(d, equalTo: .now, toGranularity: .year)
        return d.formatted(thisYear ? .dateTime.day().month(.abbreviated) : .dateTime.day().month(.abbreviated).year())
    }
}

/// A topic file: facts about one project or area, read by Vision when a conversation touches it.
struct MemoryTopic: Codable, Identifiable, Hashable {
    let name: String            // the file's name without .md, e.g. "iphone-app"
    let about: String           // its first line: what it covers
    let count: Int
    var updated: Double? = nil  // when the file last changed
    var id: String { name }
}

struct MemoryPage: Codable {
    var facts: [MemoryFact]
    var topics: [MemoryTopic]
    var updated: Double? = nil  // the core file's last change
}

/// One memory file as a row and as a page: the core ("Profile", name "") or a topic.
struct MemoryFile: Identifiable, Hashable {
    let name: String
    let about: String
    let updated: Double?
    var id: String { name }

    /// The file's own heading names it when it has one ("Acme: the user's…" → "Acme");
    /// otherwise its file name does ("iphone-app" → "iPhone app").
    var title: String {
        guard !name.isEmpty else { return "Profile" }
        if about.contains(":"), let head = about.split(separator: ":", maxSplits: 1).first, !head.isEmpty, head.count <= 30 {
            return String(head)
        }
        let words = name.replacingOccurrences(of: "-", with: " ")
        let fixed = words.prefix(1).uppercased() + words.dropFirst()
        return fixed.replacingOccurrences(of: "iphone", with: "iPhone", options: .caseInsensitive)
            .replacingOccurrences(of: "ios", with: "iOS", options: [.caseInsensitive, .anchored])
    }

    var updatedText: String {
        guard let updated else { return "" }
        return "Updated " + Date(timeIntervalSince1970: updated).formatted(.relative(presentation: .named))
    }
}

/// The files Vision remembers by, pushed from Settings. "Profile" is the core, in every chat's prompt; each
/// area is a topic file read when a conversation touches it. Tap one for its facts.
struct MemoryView: View {
    @EnvironmentObject private var client: VisionClient
    @State private var page: MemoryPage?
    @State private var error: String?

    var body: some View {
        content
            .navigationTitle("Memory")
            .navigationBarTitleDisplayMode(.inline)
            .onAppear { Task { await load() } }   // also on the way back from a file: its date may have changed
    }

    @ViewBuilder private var content: some View {
        if page == nil, let error {
            ContentUnavailableView {
                Label("Couldn't load memory", systemImage: "wifi.exclamationmark")
            } description: {
                Text(error)
            } actions: {
                Button("Try again") { Task { await load() } }.buttonStyle(.bordered)
            }
        } else if let page {
            List {
                Section {
                    fileRow(MemoryFile(name: "", about: "Part of every conversation.", updated: page.updated))
                } header: {
                    header("You")
                }
                if !page.topics.isEmpty {
                    Section {
                        ForEach(page.topics) { topic in
                            fileRow(MemoryFile(name: topic.name, about: topic.about, updated: topic.updated))
                        }
                    } header: {
                        header("Areas")
                    }
                }
            }
            .listStyle(.insetGrouped)
            .refreshable { await load() }
        } else {
            ProgressView()
        }
    }

    private func header(_ title: String) -> some View {
        Text(title)
            .themeFont(.subheadline)
            .foregroundStyle(.secondary)
            .textCase(nil)
    }

    private func fileRow(_ file: MemoryFile) -> some View {
        // A destination link, like Settings' own row into this page: a value link pushed on top of one
        // re-pushed the file on the way back and then skipped this page.
        NavigationLink {
            MemoryFileView(file: file)
        } label: {
            VStack(alignment: .leading, spacing: 4) {
                Text(file.title).themeFont(size: 19, weight: .regular)
                if !file.updatedText.isEmpty {
                    Text(file.updatedText).themeFont(.subheadline).foregroundStyle(.secondary)
                }
            }
            .padding(.vertical, 6)
        }
    }

    private func load() async {
        do {
            let fresh = try await client.fetchMemory()
            withAnimation(.snappy(duration: 0.25)) { page = fresh }
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}

/// One file's facts, pushed from the list: long-press forgets a fact, + adds one.
private struct MemoryFileView: View {
    @EnvironmentObject private var client: VisionClient
    let file: MemoryFile
    @State private var facts: [MemoryFact]?
    @State private var error: String?
    @State private var adding = false
    @State private var draft = ""

    var body: some View {
        Group {
            if facts == nil, let error {
                ContentUnavailableView {
                    Label("Couldn't load this file", systemImage: "wifi.exclamationmark")
                } description: {
                    Text(error)
                } actions: {
                    Button("Try again") { Task { await load() } }.buttonStyle(.bordered)
                }
            } else if let facts, facts.isEmpty {
                ContentUnavailableView {
                    Label("Nothing here yet", systemImage: "brain")
                } description: {
                    Text(file.about)
                } actions: {
                    Button("Remember something") { draft = ""; adding = true }.buttonStyle(.borderedProminent)
                }
            } else if let facts {
                // The whole file on one page, a bullet per fact, as it reads in the file itself.
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        ForEach(facts) { fact in
                            MemoryFactRow(fact: fact) { Task { await forget(fact) } }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 22)
                    .padding(.vertical, 16)
                }
                .refreshable { await load() }
            } else {
                ProgressView()
            }
        }
        .navigationTitle(file.title)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button { draft = ""; adding = true } label: { Image(systemName: "plus") }
                    .accessibilityLabel("Remember something here")
            }
        }
        .task { await load() }
        .alert("Remember", isPresented: $adding) {
            TextField("A fact to keep", text: $draft)
            Button("Save") { Task { await add() } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Filed under \(file.title).")
        }
    }

    private func load() async {
        do {
            let fresh = try await client.fetchMemory(topic: file.name).facts
            withAnimation(.snappy(duration: 0.25)) { facts = fresh }
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func add() async {
        let fact = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !fact.isEmpty else { return }
        do {
            try await client.remember(fact, topic: file.name)
            await load()
        } catch {
            client.toast = error.localizedDescription
        }
    }

    private func forget(_ fact: MemoryFact) async {
        do {
            try await client.forget(fact, topic: file.name)
            await load()
        } catch {
            client.toast = error.localizedDescription
        }
    }
}

/// A fact as a bullet point, its words in full. Long-press to copy or forget it.
private struct MemoryFactRow: View {
    let fact: MemoryFact
    let onForget: () -> Void

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text("•").themeFont(.body).foregroundStyle(.secondary)
            Text(fact.text).themeFont(.body).fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
        .contextMenu {
            Button { UIPasteboard.general.string = fact.text } label: { Label("Copy", systemImage: "doc.on.doc") }
            Button(role: .destructive, action: onForget) { Label("Forget", systemImage: "trash") }
            if !fact.day.isEmpty { Text("Saved \(fact.day)") }
        }
    }
}
