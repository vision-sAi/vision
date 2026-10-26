import SwiftUI

/// Bottom sheet: `/usage` for every subscription brain, one card per provider with a ring per
/// window (session, week, per-model), when it resets, and the plan when the provider names one.
struct UsageView: View {
    @EnvironmentObject private var client: VisionClient

    /// Cards to show while the first report loads: the machine's providers with usage, else the usual three.
    private var placeholderProviders: [String] {
        let withUsage = client.providers.filter { $0.enabled && ($0.source != "config") }.map(\.name)
        return withUsage.isEmpty ? ["claude", "codex", "grok"] : withUsage.filter { $0 != "local" }
    }
    @Environment(\.dismiss) private var dismiss
    private var report: UsageReport? { client.usage }
    private var refreshing: Bool { client.usageRefreshing }

    private var providers: [UsageReport.Provider] {
        guard let report else { return [] }
        let order = ProviderStyle.sorted(report.providers.map(\.provider))
        return report.providers.sorted { (order.firstIndex(of: $0.provider) ?? 99) < (order.firstIndex(of: $1.provider) ?? 99) }
    }

    var body: some View {
        NavigationStack {
            content
                .background(Color(.systemGroupedBackground))
                .navigationTitle("Usage")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .topBarLeading) {
                        Button { Task { await load() } } label: {
                            Image(systemName: "arrow.clockwise")
                                .symbolEffect(.rotate, options: .repeat(.continuous), isActive: refreshing)
                        }
                        .disabled(refreshing)
                        .accessibilityLabel("Refresh")
                    }
                    ToolbarItem(placement: .topBarTrailing) {
                        Button { dismiss() } label: { Image(systemName: "xmark") }
                            .accessibilityLabel("Close")
                    }
                }
        }
        // Only fetches when nothing has loaded yet; otherwise the app keeps it fresh in the background.
        .task { if client.usage == nil { await load() } }
    }

    @ViewBuilder private var content: some View {
        if report == nil, !refreshing, let error = client.usageError {
            ContentUnavailableView {
                Label("Couldn't read usage", systemImage: "wifi.exclamationmark")
            } description: {
                Text(error)
            } actions: {
                Button("Try again") { Task { await load() } }.buttonStyle(.bordered)
            }
        } else {
            ScrollView {
                VStack(spacing: 14) {
                    if report != nil {
                        updatedLine
                        ForEach(providers) { ProviderUsageCard(provider: $0) }
                    } else {
                        ForEach(placeholderProviders, id: \.self) { ProviderUsageCard.placeholder($0) }
                    }
                }
                .padding(.horizontal, 16)
                .padding(.top, 8)
                .padding(.bottom, 28)
                .animation(.smooth, value: client.usageFetchedAt)
            }
            .refreshable { await load() }
        }
    }

    /// "Updated just now" / "Updated 3 min ago", ticking over while the sheet is open.
    private var updatedLine: some View {
        TimelineView(.periodic(from: .now, by: 15)) { context in
            Group {
                if refreshing {
                    Text("Refreshing…")
                } else if let at = client.usageFetchedAt {
                    let secs = context.date.timeIntervalSince(at)
                    if secs < 60 {
                        Text("Updated just now")
                    } else if secs < 3600 {
                        Text("Updated \(Int(secs / 60)) min ago")
                    } else {
                        Text("Updated \(at.formatted(date: .omitted, time: .shortened))")
                    }
                }
            }
            .themeFont(.footnote)
            .foregroundStyle(.secondary)
            .contentTransition(.opacity)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.leading, 4)
        }
    }

    private func load() async {
        await client.refreshUsage()
    }
}

// MARK: - card

/// What the machine said after a Use tap, shown on the card until the sheet closes.
struct ResetOutcome: Equatable {
    let ok: Bool
    let message: String
}

private struct ProviderUsageCard: View {
    let provider: UsageReport.Provider
    var loading = false
    @State private var resetOutcome: ResetOutcome?

    static func placeholder(_ p: String) -> some View {
        ProviderUsageCard(
            provider: .init(provider: p, label: ProviderStyle.title(p), plan: nil,
                            windows: [.init(label: "Current session", usedPercent: nil, resetsAt: nil, resets: "in 2h"),
                                      .init(label: "Current week", usedPercent: nil, resetsAt: nil, resets: "Thu 11 PM")],
                            notes: [], source: nil, at: nil, error: nil),
            loading: true)
        .redacted(reason: .placeholder)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            header
            if let error = provider.error {
                Text(LocalizedStringKey(error))
                    .themeFont(.footnote)
                    .foregroundStyle(.secondary)
            } else if provider.windows.isEmpty {
                Text("Nothing to show yet.").themeFont(.footnote).foregroundStyle(.secondary)
            } else {
                rings
            }
            if let banked = provider.banked, banked.count > 0 {
                BankedResets(provider: provider.provider, label: provider.label, banked: banked,
                             tint: ProviderStyle.tint(provider.provider), usable: !loading, result: $resetOutcome)
            }
            // Outside the banked rows: spending the last reset takes them away.
            if let r = resetOutcome {
                Label(r.message, systemImage: r.ok ? "checkmark.circle.fill" : "exclamationmark.circle")
                    .themeFont(.footnote)
                    .foregroundStyle(r.ok ? AnyShapeStyle(.green) : AnyShapeStyle(.secondary))
                    .transition(.opacity)
            }
            if !provider.notes.isEmpty {
                Text(provider.notes.joined(separator: " · "))
                    .themeFont(.caption)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 24, style: .continuous))
        .animation(.smooth, value: resetOutcome)
    }

    private var header: some View {
        HStack(spacing: 10) {
            ProviderBadge(provider: provider.provider, size: 30)
            Text(provider.label).themeFont(.headline)
            if let plan = provider.plan, !plan.isEmpty {
                Text(plan)
                    .themeFont(.caption, weight: .semibold)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 8).padding(.vertical, 3)
                    .background(Color(.tertiarySystemFill), in: Capsule())
            }
            Spacer()
            if provider.source == "snapshot" || provider.source == "rollout" {
                Label(provider.at.map { "as of \(Date(timeIntervalSince1970: $0).formatted(date: .omitted, time: .shortened))" } ?? "snapshot",
                      systemImage: "clock")
                    .themeFont(.caption)
                    .foregroundStyle(.tertiary)
            }
        }
    }

    /// Up to three rings a row, each row sharing the card's width evenly.
    private var rings: some View {
        let chunks = stride(from: 0, to: provider.windows.count, by: 3).map { Array(provider.windows[$0..<min($0 + 3, provider.windows.count)]) }
        return VStack(spacing: 18) {
            ForEach(chunks.indices, id: \.self) { i in
                HStack(alignment: .top, spacing: 8) {
                    ForEach(chunks[i]) { w in
                        UsageRing(window: w, tint: ProviderStyle.tint(provider.provider), loading: loading)
                            .frame(maxWidth: .infinity)
                    }
                }
            }
        }
    }
}

/// Every saved reset on its own row: what it clears, when it lapses, and a Use button that spends
/// exactly that one after a confirmation. The machine's answer shows on the card underneath.
private struct BankedResets: View {
    @EnvironmentObject private var client: VisionClient
    let provider: String
    let label: String
    let banked: UsageReport.Banked
    let tint: Color
    var usable = true
    @Binding var result: ResetOutcome?
    @State private var confirming: Int?
    @State private var spending: Int?

    var body: some View {
        let items = banked.each
        VStack(alignment: .leading, spacing: 0) {
            Text("Banked resets")
                .themeFont(.caption, weight: .semibold)
                .foregroundStyle(.secondary)
                .padding(.bottom, 8)
            ForEach(items.indices, id: \.self) { i in
                if i > 0 { Divider().padding(.leading, 46) }
                row(items[i], index: i)
            }
        }
        .confirmationDialog(confirming.map { "Use \(kind(items[$0])) now?" } ?? "",
                            isPresented: Binding(get: { confirming != nil }, set: { if !$0 { confirming = nil } }),
                            titleVisibility: .visible,
                            presenting: confirming) { i in
            Button("Use reset") { Task { await spend(items[i], index: i) } }
            Button("Cancel", role: .cancel) {}
        } message: { i in
            Text("This clears \(clears(items[i])) straight away. It can't be undone.")
        }
    }

    private func row(_ item: UsageReport.Banked.Item, index i: Int) -> some View {
        HStack(spacing: 12) {
            Image(systemName: "arrow.counterclockwise")
                .font(.system(size: 15, weight: .bold))
                .foregroundStyle(tint)
                .frame(width: 34, height: 34)
                .background(tint.opacity(0.14), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text(kind(item)).themeFont(.subheadline, weight: .semibold)
                expiry(item.expiresAt)
            }
            Spacer(minLength: 8)
            if usable { useButton(index: i) }
        }
        .padding(.vertical, 7)
        .accessibilityElement(children: .contain)
    }

    private func useButton(index i: Int) -> some View {
        Button { confirming = i } label: {
            ZStack {
                Text("Use").opacity(spending == i ? 0 : 1)
                if spending == i { ProgressView().tint(.white).controlSize(.small) }
            }
            .themeFont(.subheadline, weight: .semibold)
            .foregroundStyle(.white)
            .frame(width: 64, height: 32)
            .background(tint.gradient, in: Capsule())
            .opacity(spending != nil && spending != i ? 0.4 : 1)
        }
        .buttonStyle(.plain)
        .disabled(spending != nil)
        .accessibilityLabel("Use \(kind(banked.each[i]))")
    }

    private func kind(_ item: UsageReport.Banked.Item) -> String { item.kind ?? "Usage reset" }

    /// "Expires Oct 22 · 28 days left", orange in its last three days; "No expiry date" when unknown.
    @ViewBuilder private func expiry(_ ts: Double?) -> some View {
        if let ts {
            let date = Date(timeIntervalSince1970: ts)
            let days = max(0, Int(ceil(date.timeIntervalSinceNow / 86_400)))
            let left = days <= 1 ? "last day" : "\(days) days left"
            Text("Expires \(date.formatted(.dateTime.month(.abbreviated).day())) · \(left)")
                .themeFont(.caption)
                .foregroundStyle(days <= 3 ? AnyShapeStyle(.orange) : AnyShapeStyle(.secondary))
        } else {
            Text("No expiry date").themeFont(.caption).foregroundStyle(.secondary)
        }
    }

    private func clears(_ item: UsageReport.Banked.Item) -> String {
        switch item.kind {
        case "Full Reset": return "your \(label) session and weekly limits"
        case "Session Reset": return "your \(label) session limit"
        case "Weekly Reset": return "your \(label) weekly limit"
        default: return "your \(label) usage limits"
        }
    }

    private func spend(_ item: UsageReport.Banked.Item, index i: Int) async {
        spending = i
        defer { spending = nil }
        do {
            let r = try await client.useBankedReset(provider, id: item.id)
            result = ResetOutcome(ok: r.ok, message: r.message)
        } catch {
            result = ResetOutcome(ok: false, message: error.localizedDescription)
        }
    }
}

// MARK: - ring

private struct UsageRing: View {
    let window: UsageReport.Window
    let tint: Color
    var loading = false
    @State private var shown: Double = 0

    private var pct: Double? { window.usedPercent.map { max(0, min(100, $0)) } }

    /// Status colours only when a limit is close; below that the ring wears its provider's tint.
    private var levelColor: Color {
        guard let pct else { return tint }
        if pct >= 90 { return .red }
        if pct >= 75 { return .orange }
        return tint
    }

    private var title: String {
        switch window.label {
        case "Current session": return "Session"
        case "Current week": return "Week"
        default: return window.label
        }
    }

    var body: some View {
        VStack(spacing: 10) {
            ZStack {
                if pct == nil && !loading {
                    Circle().strokeBorder(Color(.tertiarySystemFill), style: StrokeStyle(lineWidth: 8, dash: [3, 5]))
                } else {
                    Circle().strokeBorder(Color(.tertiarySystemFill), lineWidth: 8)
                    Circle()
                        .inset(by: 4)
                        .trim(from: 0, to: shown / 100)
                        .stroke(levelColor.gradient, style: StrokeStyle(lineWidth: 8, lineCap: .round))
                        .rotationEffect(.degrees(-90))
                }
                if let pct {
                    HStack(alignment: .firstTextBaseline, spacing: 1) {
                        Text("\(Int(pct.rounded()))").themeFont(.title3, weight: .bold)
                        Text("%").themeFont(.caption, weight: .bold).foregroundStyle(.secondary)
                    }
                    .monospacedDigit()
                    .contentTransition(.numericText(value: pct))
                } else {
                    Text("—").themeFont(.title3, weight: .semibold).foregroundStyle(.tertiary)
                }
            }
            .frame(width: 76, height: 76)

            VStack(spacing: 2) {
                Text(title)
                    .themeFont(.subheadline, weight: .semibold)
                    .lineLimit(1)
                Text(resets)
                    .themeFont(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
            }
        }
        .onAppear { withAnimation(.smooth(duration: 0.8).delay(0.1)) { shown = pct ?? 0 } }
        .onChange(of: pct) { _, new in withAnimation(.smooth) { shown = new ?? 0 } }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(title)
        .accessibilityValue(pct.map { "\(Int($0.rounded())) percent used, resets \(resets)" } ?? "not reported")
    }

    /// "in 4h 12m" inside a day, "Thu 10:59 PM" beyond; the machine's own wording otherwise.
    private var resets: String {
        if pct == nil, !loading, window.resetsAt == nil { return "not reported" }
        guard let ts = window.resetsAt else { return window.resets ?? "" }
        let date = Date(timeIntervalSince1970: ts)
        let secs = date.timeIntervalSinceNow
        // A snapshot can outlive its window; its percent is then from before the reset.
        if secs < -60 { return "already reset" }
        if secs <= 60 { return "resets now" }
        if secs < 3600 { return "in \(Int(secs / 60))m" }
        if secs < 86400 {
            let h = Int(secs / 3600), m = Int(secs.truncatingRemainder(dividingBy: 3600) / 60)
            return m == 0 ? "in \(h)h" : "in \(h)h \(m)m"
        }
        return date.formatted(.dateTime.weekday(.abbreviated).hour().minute())
    }
}
