import SwiftUI

/// Bottom sheet: every saved conversation, newest first under Today / Yesterday / … headers, with
/// search and a provider filter. Tapping one opens it as its own chat (or jumps to the chat that
/// already has it).
struct SessionsView: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.dismiss) private var dismiss
    @State private var rows: [SessionRow]?
    @State private var error: String?
    @State private var filter = ""          // "" = every provider
    @State private var query = ""

    private var providers: [String] { ProviderStyle.sorted((rows ?? []).map(\.provider)) }

    private var shown: [SessionRow] {
        var list = (rows ?? []).filter { filter.isEmpty || $0.provider == filter }
        let q = query.trimmingCharacters(in: .whitespaces)
        if !q.isEmpty { list = list.filter { $0.displayTitle.localizedCaseInsensitiveContains(q) } }
        // Newest first across every provider; a machine that sends no timestamps is sorted by its age text.
        return list.enumerated()
            .sorted { a, b in
                let (x, y) = (a.element.sortKey, b.element.sortKey)
                return x != y ? x > y : a.offset < b.offset
            }
            .map(\.element)
    }

    var body: some View {
        NavigationStack {
            content
                .navigationTitle("Conversations")
                .navigationBarTitleDisplayMode(.inline)
                .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always), prompt: "Search conversations")
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button { dismiss() } label: { Image(systemName: "xmark") }
                            .accessibilityLabel("Close")
                    }
                }
        }
        .task { await load() }
    }

    @ViewBuilder private var content: some View {
        if let rows, rows.isEmpty {
            ContentUnavailableView("No earlier conversations", systemImage: "bubble.left.and.bubble.right",
                                   description: Text("Chats you have with Vision, here or on the computer, show up in this list."))
        } else if rows == nil, let error {
            ContentUnavailableView {
                Label("Couldn't load conversations", systemImage: "wifi.exclamationmark")
            } description: {
                Text(error)
            } actions: {
                Button("Try again") { Task { await load() } }.buttonStyle(.bordered)
            }
        } else {
            // Laid out like Usage: a solid backdrop, each date's conversations on one rounded card.
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    if rows != nil, providers.count > 1 {
                        filterChips.padding(.bottom, 6)
                    }
                    if rows == nil {
                        groupCard(title: "Today", rows: (0..<7).map { _ in nil })
                            .redacted(reason: .placeholder)
                    } else {
                        ForEach(groups, id: \.title) { group in
                            groupCard(title: group.title, rows: group.rows)
                        }
                    }
                }
                .padding(.top, 4)
                .padding(.bottom, 28)
                .animation(.smooth, value: filter)
            }
            .background(Color(.systemGroupedBackground))
            .overlay {
                if rows != nil, shown.isEmpty {
                    if query.isEmpty {
                        ContentUnavailableView("Nothing here yet", systemImage: ProviderStyle.icon(filter))
                    } else {
                        ContentUnavailableView.search(text: query)
                    }
                }
            }
            .refreshable { await load() }
        }
    }

    private func load() async {
        do {
            rows = try await client.fetchSessions()
            error = nil
        } catch {
            if rows == nil { self.error = error.localizedDescription }
        }
    }

    // MARK: - filter

    private var filterChips: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                chip(id: "", label: "All", count: rows?.count ?? 0)
                ForEach(providers, id: \.self) { p in
                    chip(id: p, label: ProviderStyle.title(p), count: (rows ?? []).filter { $0.provider == p }.count)
                }
            }
            .padding(.horizontal, 20)
        }
        .scrollClipDisabled()
    }

    private func chip(id: String, label: String, count: Int) -> some View {
        let selected = filter == id
        return Button {
            withAnimation(.snappy(duration: 0.25)) { filter = id }
        } label: {
            HStack(spacing: 6) {
                if !id.isEmpty {
                    Image(systemName: ProviderStyle.icon(id))
                        .themeFont(.caption, weight: .bold)
                        .foregroundStyle(selected ? AnyShapeStyle(Color(.systemBackground)) : AnyShapeStyle(ProviderStyle.tint(id)))
                }
                Text(label).themeFont(.footnote, weight: .semibold)
                Text("\(count)")
                    .themeFont(.caption, weight: .semibold, digits: true)
                    .foregroundStyle(selected ? AnyShapeStyle(Color(.systemBackground).opacity(0.7)) : AnyShapeStyle(.secondary))
            }
            .padding(.horizontal, 12)
            .frame(height: 32)
            .foregroundStyle(selected ? AnyShapeStyle(Color(.systemBackground)) : AnyShapeStyle(.primary))
            .background(selected ? AnyShapeStyle(Color(.label)) : AnyShapeStyle(Color(.secondarySystemGroupedBackground)),
                        in: Capsule())
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    // MARK: - date groups

    private struct DateGroup { let title: String; let rows: [SessionRow] }

    /// Today, Yesterday, Previous 7 days, Previous 30 days, then one per month. An older machine
    /// that sends no timestamps gets one unlabelled group in its own order.
    private var groups: [DateGroup] {
        let list = shown
        guard list.allSatisfy({ $0.updated != nil }) else { return list.isEmpty ? [] : [DateGroup(title: "", rows: list)] }
        var out: [DateGroup] = []
        for row in list {
            let title = Self.groupTitle(Date(timeIntervalSince1970: row.updated ?? 0))
            if out.last?.title == title {
                out[out.count - 1] = DateGroup(title: title, rows: out[out.count - 1].rows + [row])
            } else {
                out.append(DateGroup(title: title, rows: [row]))
            }
        }
        return out
    }

    private static func groupTitle(_ date: Date) -> String {
        let cal = Calendar.current
        if cal.isDateInToday(date) { return "Today" }
        if cal.isDateInYesterday(date) { return "Yesterday" }
        let days = cal.dateComponents([.day], from: cal.startOfDay(for: date), to: cal.startOfDay(for: .now)).day ?? 0
        if days < 7 { return "Previous 7 days" }
        if days < 30 { return "Previous 30 days" }
        let sameYear = cal.component(.year, from: date) == cal.component(.year, from: .now)
        return date.formatted(sameYear ? .dateTime.month(.wide) : .dateTime.month(.wide).year())
    }

    /// Time of day for today and yesterday, weekday within the week, date beyond.
    private static func stamp(_ row: SessionRow) -> String {
        guard let t = row.updated else { return row.age }
        let date = Date(timeIntervalSince1970: t)
        let cal = Calendar.current
        if cal.isDateInToday(date) || cal.isDateInYesterday(date) { return date.formatted(date: .omitted, time: .shortened) }
        let days = cal.dateComponents([.day], from: date, to: .now).day ?? 0
        if days < 7 { return date.formatted(.dateTime.weekday(.wide)) }
        return date.formatted(.dateTime.month(.abbreviated).day())
    }

    // MARK: - rows

    /// A date's title, then its conversations on one card with hairlines between them. A nil row is a placeholder.
    private func groupCard(title: String, rows: [SessionRow?]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if !title.isEmpty {
                Text(title)
                    .themeFont(.footnote, weight: .semibold)
                    .foregroundStyle(.secondary)
                    .padding(.leading, 20)
                    .accessibilityAddTraits(.isHeader)
            }
            VStack(spacing: 0) {
                ForEach(rows.indices, id: \.self) { i in
                    if i > 0 { Divider().padding(.leading, 56) }
                    if let r = rows[i] { row(r) } else { placeholderRow }
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 4)
            .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 22, style: .continuous))
            .padding(.horizontal, 16)
        }
        .padding(.top, 14)
    }

    private func row(_ s: SessionRow) -> some View {
        Button {
            client.resume(sessionID: s.id, provider: s.provider)
            dismiss()
        } label: {
            HStack(spacing: 12) {
                ProviderBadge(provider: s.provider, size: 30)
                VStack(alignment: .leading, spacing: 3) {
                    Text(s.displayTitle)
                        .themeFont(.subheadline, weight: .semibold)
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                    HStack(spacing: 5) {
                        Text(s.brainLabel)
                        Text("·")
                        Text(Self.stamp(s)).monospacedDigit()
                        if s.hasAttachment {
                            Image(systemName: "paperclip")
                                .imageScale(.small)
                                .accessibilityLabel("Attachment")
                        }
                    }
                    .themeFont(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                }
                Spacer(minLength: 4)
                if s.current {
                    HStack(spacing: 4) {
                        Circle().fill(.green).frame(width: 6, height: 6)
                        Text("Open")
                    }
                    .themeFont(.caption2, weight: .semibold)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 8).padding(.vertical, 3)
                    .background(Color(.tertiarySystemFill), in: Capsule())
                }
            }
            .padding(.vertical, 10)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityHint(s.current ? "Already open, switches to it" : "Opens this conversation")
    }

    private var placeholderRow: some View {
        HStack(spacing: 12) {
            RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color(.tertiarySystemFill)).frame(width: 30, height: 30)
            VStack(alignment: .leading, spacing: 3) {
                Text("A conversation title that runs").themeFont(.subheadline, weight: .semibold)
                Text("Claude · 2:14 PM").themeFont(.caption)
            }
            Spacer()
        }
        .padding(.vertical, 10)
    }
}
