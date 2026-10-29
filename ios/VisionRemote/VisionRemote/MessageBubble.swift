import SwiftUI

struct MessageBubble: View {
    let message: Message
    /// Held, not observed: an observed client would redraw every bubble in the thread on each streamed token.
    let client: VisionClient
    @State private var preview: PreviewImage?
    /// "Select Text" on one of your own messages: its words are selectable in the bubble until let go.
    @State private var selecting = false
    @Namespace private var zoom   // the viewer grows out of the tapped picture and shrinks back into it
    @Environment(\.theme) private var theme

    var body: some View {
        content.imagePreview($preview)
    }

    @ViewBuilder
    private var content: some View {
        switch message.role {
        case .shell:
            ShellRow(message: message)
        case .note:
            if let card = message.checkin {
                CheckinCardView(card: card, text: message.text, project: client.activeProject)
            } else if let project = client.activeProject, ReportCardView.lines(message.text) != nil {
                ReportCardView(text: message.text, project: project)
            } else {
                Text(message.text)
                    .themeFont(.subheadline)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        case .user:
            VStack(alignment: .trailing, spacing: 6) {
                if !message.images.isEmpty {
                    HStack(spacing: 6) {
                        ForEach(Array(message.images.enumerated()), id: \.offset) { i, image in
                            let video = message.isVideo(at: i)
                            let single = message.images.count == 1
                            // The whole picture, never a square crop, sized to its own shape so it sits flush
                            // right like the text bubbles (a max-size frame would centre it in empty space).
                            let box = CGSize(width: single ? 240 : 120, height: single ? 320 : 120)
                            let scale = min(box.width / max(image.size.width, 1), box.height / max(image.size.height, 1))
                            Image(uiImage: image)
                                .resizable()
                                .frame(width: image.size.width * scale, height: image.size.height * scale)
                                .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
                                .overlay { if video { PlayOverlay(size: message.images.count == 1 ? 56 : 40) } }
                                .matchedTransitionSource(id: "\(message.id)-\(i)", in: zoom) {
                                    $0.clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
                                }
                                .contentShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
                                .onTapGesture {
                                    // Swipe through everything the message carries, photos and clips, opening on this one.
                                    let all = message.images.indices
                                    let path = { (j: Int) in message.imagePaths.indices.contains(j) ? message.imagePaths[j] : "" }
                                    var item = PreviewImage(images: Array(message.images), start: i,
                                                            paths: all.map { message.isVideo(at: $0) ? "" : path($0) },
                                                            clips: all.map { message.isVideo(at: $0) ? client.videoAsset(path: path($0)) : nil },
                                                            fetch: { [client] in $0.isEmpty ? nil : await client.replyImage(path: $0) })
                                    item.sources = all.map { "\(message.id)-\($0)" }
                                    item.zoom = zoom
                                    preview = item
                                }
                                .accessibilityAddTraits(.isButton)
                                .accessibilityLabel(video ? "Sent video, tap to play" : "Sent photo, tap to preview")
                        }
                    }
                }
                ForEach(message.files, id: \.self) { path in
                    FileChip(name: Message.fileName(path))
                }
                if !message.text.isEmpty {
                    if selecting {
                        SelectableBubbleText(text: message.text, lineSpacing: 4) { selecting = false }
                            .padding(.horizontal, 16)
                            .padding(.vertical, 10)
                            .modifier(UserBubble(theme: theme))
                            .drawerSwipeGuard()   // a handle dragged sideways is not a pull on the drawer
                    } else {
                        Text(message.text)
                            .themeFont(.body)
                            .lineSpacing(4)
                            .padding(.horizontal, 16)
                            .padding(.vertical, 10)
                            .foregroundStyle(theme.userInk ?? .primary)
                            .modifier(UserBubble(theme: theme))
                            // Held, the bubble lifts with a menu, as in ChatGPT's app: the whole message to
                            // copy or share, or its words made selectable right here.
                            .contentShape(.contextMenuPreview, RoundedRectangle(cornerRadius: theme.bubbleRadius, style: .continuous))
                            .contextMenu {
                                Button("Copy", systemImage: "doc.on.doc") {
                                    UIPasteboard.general.string = message.text
                                    client.toast = "Copied"
                                }
                                Button("Select Text", systemImage: "text.cursor") { selecting = true }
                                ShareLink(item: message.text) { Label("Share", systemImage: "square.and.arrow.up") }
                            }
                            .accessibilityHint("Long press to copy, select or share")
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .trailing)
            .padding(.leading, 48)
        case .assistant:
            let parts = message.replyParts   // split once: the body and the status line below both read it
            VStack(alignment: .leading, spacing: 6) {
                if message.text.isEmpty && message.answered.isEmpty && message.agents.isEmpty && message.tools.isEmpty && message.streaming {
                    // The status line below already says it's working; the spinner is only for a wait with no words.
                    if message.status.isEmpty { ProgressView().padding(.vertical, 4) }
                } else {
                    ForEach(Array(parts.enumerated()), id: \.offset) { index, part in
                        switch part {
                        case .text(let text):
                            ReplyMarkdown(text: text, streaming: message.streaming, selectable: true, isError: message.isError)
                                .themeFont(.body)
                                .lineSpacing(5)
                                .selectable(!message.streaming)   // selection costs a redraw; nobody selects mid-stream
                                .foregroundStyle(message.isError ? Color.red : Color.primary)
                        case .image(let path, let caption):
                            ReplyImage(path: path, caption: caption, client: client) { image in
                                var item = PreviewImage(image: image)
                                item.sources = ["reply-\(index)"]
                                item.zoom = zoom
                                preview = item
                            }
                                .matchedTransitionSource(id: "reply-\(index)", in: zoom)
                                .padding(.vertical, 4)
                        case .answered(let form):
                            AnsweredBlock(form: form)
                        case .plan(let text):
                            PlanBlock(text: text, streaming: message.streaming)
                        case .agents(let rows):
                            AgentBlock(rows: rows)
                        case .tools(let rows):
                            ToolBlock(rows: rows, settled: !message.streaming || parts[(index + 1)...].contains(where: \.isProse),
                                      live: message.streaming, chatID: client.active?.id ?? "")
                        }
                    }
                }
                // A live tool line is the status while it is last: no second line pulsing under it.
                // A running agent row last carries its own step and clock, so the footer goes altogether.
                let agentLive = message.streaming && parts.last?.hasRunningAgent == true
                let showsStatus = !agentLive && !message.status.isEmpty && !(message.streaming && !message.tools.isEmpty && parts.last?.isTools == true)
                // A finished reply with words in it can be copied or shared whole.
                let actions = !message.streaming && !message.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                if showsStatus || (timer != nil && !agentLive) || actions {
                    // The terminal's live line and footer: what it is doing, and how long the turn has taken.
                    HStack(spacing: 6) {
                        if showsStatus {
                            Label(message.status, systemImage: statusIcon)
                                .themeFont(.subheadline)
                                .foregroundStyle(.secondary)
                                .symbolEffect(.pulse, isActive: message.streaming)
                        }
                        if showsStatus && timer != nil {
                            Text("·").themeFont(.subheadline).foregroundStyle(.tertiary)
                        }
                        timer
                        if actions {
                            ReplyActions(text: message.text.trimmingCharacters(in: .whitespacesAndNewlines), client: client)
                                .padding(.leading, showsStatus || timer != nil ? 4 : -8)   // alone, its first icon lines up with the text
                                .drawerSwipeGuard()
                        }
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.trailing, 24)
        }
    }

    /// How long the turn has taken: counting while the reply streams, fixed once it is done. Nil for a
    /// reply with no time known (history from before the machine's server started).
    private var timer: ReplyTimer? {
        if message.streaming, let began = message.startedAt {
            if let paused = message.pausedAt { return ReplyTimer(began: nil, took: paused.timeIntervalSince(began)) }
            return ReplyTimer(began: began, took: nil)
        }
        if !message.streaming, let ms = message.durationMs { return ReplyTimer(began: nil, took: Double(ms) / 1000) }
        return nil
    }

    private var statusIcon: String {
        if message.status.hasPrefix("using") { return "wrench.and.screwdriver" }
        if message.status.hasPrefix("connection") { return "wifi.exclamationmark" }
        if message.status.contains("retrying") { return "arrow.clockwise" }
        return "brain"
    }

}

private extension View {
    @ViewBuilder
    func selectable(_ on: Bool) -> some View {
        if on { textSelection(.enabled) } else { textSelection(.disabled) }
    }
}

/// Your bubble: the theme's fill, a hairline where it has one, its radius.
private struct UserBubble: ViewModifier {
    let theme: Theme

    func body(content: Content) -> some View {
        let shape = RoundedRectangle(cornerRadius: theme.bubbleRadius, style: .continuous)
        if let colors = theme.userBubble {
            // Panes: the violet gradient, the text on it in the theme's ink (links and code too).
            content
                .foregroundStyle(theme.userInk ?? .primary)
                .tint(theme.userInk ?? .primary)
                .background(LinearGradient(colors: colors, startPoint: .topLeading, endPoint: .bottomTrailing), in: shape)
        } else {
            content
                .background(theme.bubbleColor, in: shape)
                .overlay { if let stroke = theme.bubbleStroke { shape.strokeBorder(stroke, lineWidth: 1) } }
        }
    }
}

/// Messages sent during a reply, waiting for it to end: faded copies of your own bubbles just above the
/// composer, so the reply streaming in never pushes them out of sight, with "Queued" under the last. A long
/// press sends one into the running reply now, takes it back to edit, or removes it. Each drops into the
/// transcript as an ordinary bubble when its turn starts. The transcript fades out behind them, so a reply
/// running underneath never reads through.
struct QueuedStrip: View {
    let messages: [Message]
    let client: VisionClient
    @Environment(\.theme) private var theme

    var body: some View {
        VStack(alignment: .trailing, spacing: 6) {
            ForEach(messages) { message in
                bubble(message)
                    .transition(.scale(scale: 0.92, anchor: .bottomTrailing).combined(with: .opacity))
            }
            Label(messages.count > 1 ? "\(messages.count) queued" : "Queued", systemImage: "clock")
                .themeFont(.footnote)
                .foregroundStyle(.tertiary)
                .padding(.trailing, 6)
                .contentTransition(.numericText())
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .padding(.leading, 48)
        .padding(.top, 20)
        .background {
            // The chat fades out behind the queue, so the reply streaming underneath never shows through it.
            LinearGradient(stops: [.init(color: theme.backgroundColor.opacity(0), location: 0),
                                   .init(color: theme.backgroundColor, location: 0.35)],
                           startPoint: .top, endPoint: .bottom)
                .padding(.horizontal, -12)
                .padding(.bottom, -12)
        }
        .animation(.snappy(duration: 0.25), value: messages.map(\.id))
    }

    private func bubble(_ message: Message) -> some View {
        VStack(alignment: .trailing, spacing: 4) {
            if !message.images.isEmpty || !message.imagePaths.isEmpty || !message.files.isEmpty {
                HStack(spacing: 4) {
                    ForEach(Array(message.images.prefix(3).enumerated()), id: \.offset) { _, image in
                        Image(uiImage: image)
                            .resizable()
                            .scaledToFill()
                            .frame(width: 44, height: 44)
                            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                    }
                    if message.images.isEmpty && !message.imagePaths.isEmpty {
                        attachmentIcon("photo")
                    }
                    ForEach(message.files.prefix(2), id: \.self) { _ in attachmentIcon("doc") }
                }
                .opacity(0.6)
            }
            if !message.text.isEmpty {
                Text(message.text)
                    .themeFont(.body)
                    .foregroundStyle(theme.userInk.map { AnyShapeStyle($0.opacity(0.8)) } ?? AnyShapeStyle(.secondary))
                    .lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)   // with a thumbnail row above, it was held to one line
                    .padding(.horizontal, 16)
                    .padding(.vertical, 10)
                    // Solid under the fill: faded text, not a see-through bubble.
                    .modifier(UserBubble(theme: theme))
                    .background(theme.backgroundColor, in: RoundedRectangle(cornerRadius: theme.bubbleRadius, style: .continuous))
            }
        }
        .contentShape(RoundedRectangle(cornerRadius: theme.bubbleRadius, style: .continuous))
        .contextMenu {
            Button("Send now", systemImage: "arrow.up") { client.sendNow(message) }
            Button("Edit", systemImage: "pencil") { client.unqueue(message, edit: true) }
            Button("Remove", systemImage: "xmark", role: .destructive) { client.unqueue(message) }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Queued: \(message.text)")
        .accessibilityHint("Long press to send now, edit or remove")
        .drawerSwipeGuard()
    }

    private func attachmentIcon(_ symbol: String) -> some View {
        Image(systemName: symbol)
            .font(.system(size: 15, weight: .medium))
            .foregroundStyle(.secondary)
            .frame(width: 44, height: 44)
            .background(Color(.secondarySystemFill), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}

/// Main reply tool calls: each edit is a card of its own with its diff (EditBlock), and the calls around
/// edits make quiet one-line groups (CallGroup), as the terminal draws them.
private struct ToolBlock: View {
    let rows: [ToolRow]
    let settled: Bool
    let live: Bool   // the reply is still coming in: its lines fade in as they change; history draws whole
    let chatID: String

    var body: some View {
        let groups = ToolRow.groups(rows)
        VStack(alignment: .leading, spacing: 4) {
            ForEach(Array(groups.enumerated()), id: \.element.first!.id) { index, group in
                if group[0].isEdit {
                    EditBlock(row: group[0], live: live)
                } else {
                    CallGroup(rows: group, settled: settled || index < groups.count - 1, live: live, chatID: chatID)
                }
            }
        }
    }
}

/// An edit as Claude Code shows one, in the app's own clothes: the file and its `+3 −1` on one line
/// (the words scanning while it runs), then the changed lines on a rounded card, numbered, removals on
/// red and additions on green, scrolling sideways for long lines. Past a dozen lines a tap shows the rest.
private struct EditBlock: View {
    let row: ToolRow
    let live: Bool
    @State private var full = false
    @State private var cardWidth: CGFloat = 0
    @State private var near = false
    @Environment(\.theme) private var theme
    @Environment(\.dynamicTypeSize) private var typeSize
    @Environment(\.displayScale) private var scale
    static let shown = 12
    private var shown: Int { Self.shown }

    var body: some View {
        if !near, let height = farHeight {
            // Far off screen, a finished edit is only its exact height until it comes within a screen of view: a reply
            // with a couple of hundred edits laid every one out, header and card, on each switch into its chat.
            Color.clear
                .frame(height: height)
                .background { nearing }
                .accessibilityElement()
                .accessibilityLabel(row.isError ? "Couldn't edit \(row.subject)" : "Edited \(row.subject)")
        } else {
            VStack(alignment: .leading, spacing: 8) {
                EditHeader(row: row, live: live)
                if row.done && !row.isError && !row.diff.isEmpty {
                    card
                        .transition(.opacity)
                }
            }
            .animation(.snappy, value: row.done)
            .padding(.vertical, 4)
            .accessibilityElement(children: .combine)
            #if DEBUG
            .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { drawn in
                if let far = farHeight, abs(far - drawn) > 0.5 { print("PERF edit height off: drawn \(drawn), standing in \(far)") }
            }
            #endif
        }
    }

    /// Turns `near` on, for good, once the block is within a screen of view.
    private var nearing: some View {
        Color.clear
            .padding(.vertical, -800)
            .onScrollVisibilityChange(threshold: 0.01) { if $0 { near = true } }
    }

    /// The drawn block's height, worked out without drawing it: the header and the "Show all" row are one line each,
    /// measured once by EditMetricsProbe, and the card's rows are a fixed pitch. Nil while it can't be known (still
    /// running, or not measured yet), and then it's drawn.
    private var farHeight: CGFloat? {
        guard row.done, !full, let header = EditMetrics.header[typeSize] else { return nil }
        guard !row.isError, !row.diff.isEmpty else { return 4 + header + 4 }
        guard let showAll = EditMetrics.showAll[typeSize] else { return nil }
        var rows: [Bool] = []   // true for a line, false for the gap between hunks; up to the card's cut
        for (i, hunk) in row.diff.enumerated() where rows.count < shown {
            if i > 0 { rows.append(false) }
            rows += Array(repeating: true, count: min(hunk.lines.count, max(0, shown - rows.count)))
        }
        let total = row.diff.reduce(0) { $0 + $1.lines.count } + row.diff.count - 1
        let sheet = DiffSheet.height(lines: rows.count, gaps: rows.filter { !$0 }.count, typeSize: typeSize, scale: scale)
        return 4 + header + 8 + sheet + (total > rows.count ? showAll : 0) + 4
    }

    private var card: some View {
        let lines = row.diffLines
        let cut = full ? lines : Array(lines.prefix(shown))
        let width = max(lines.compactMap { $0?.number.map { String($0).count } }.max() ?? 0, 1)
        let sheet = DiffSheet(lines: cut, numberWidth: CGFloat(width) * 7.5 + 4, minWidth: cardWidth, drawn: near)
        return VStack(alignment: .leading, spacing: 0) {
            if near {
                ScrollView(.horizontal, showsIndicators: false) { sheet }
                    .noScrollEdges()  // iOS 26's soft edge rounded off the rows' leading ends
                    .drawerSwipeGuard()  // a sideways swipe here scrolls the diff, not the chats drawer out
            } else {
                sheet
            }
            if lines.count > cut.count || full && lines.count > shown {
                Button {
                    withAnimation(.snappy) { full.toggle() }
                } label: {
                    ShowAllLabel(text: full ? "Show less" : "Show all \(lines.count) lines")
                }
                .buttonStyle(.plain)
                .background(Color(.quaternarySystemFill))
            }
        }
        .font(.system(.caption, design: .monospaced))
        .background(theme.surfaceColor, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Color(.separator).opacity(0.4), lineWidth: 0.5))
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { cardWidth = $0 }
        // Before EditMetricsProbe has measured, the block itself is drawn but its card stays blank at its drawn
        // size until it comes within a screen of view.
        .background { if !near { nearing } }
    }
}

/// An edit's one line: the file and its `+3 −1`, the words scanning while it runs.
private struct EditHeader: View {
    let row: ToolRow
    let live: Bool

    private var file: String { row.subject.isEmpty ? "file" : row.subject }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: row.isError ? "exclamationmark.circle" : row.name == "Write" && row.removals == 0 && row.done ? "doc.badge.plus" : "pencil")
                .foregroundStyle(row.isError ? Color.red : Color.secondary)
                .contentTransition(.symbolEffect(.replace))
                .frame(width: 18)
            ScanText(text: row.isError ? "Couldn't edit \(file)" : row.done ? "\(row.name == "Write" && row.removals == 0 ? "Wrote" : "Edited") \(file)" : "\(row.doing) \(file)",
                     scanning: !row.done, live: live)
                .id(row.done)
                .foregroundStyle(row.isError ? Color.red : Color.secondary)
            Spacer(minLength: 8)
            if row.done && !row.diff.isEmpty {
                HStack(spacing: 6) {
                    if row.additions > 0 { Text("+\(row.additions)").foregroundStyle(.green) }
                    if row.removals > 0 { Text("−\(row.removals)").foregroundStyle(.red) }
                }
                .font(.system(.caption, design: .monospaced).weight(.medium))
            }
        }
        .themeFont(.subheadline)
    }
}

/// The row under a long edit's card that shows the rest of it.
private struct ShowAllLabel: View {
    let text: String

    var body: some View {
        Text(text)
            .themeFont(.footnote)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 8)
    }
}

/// The heights of an edit's header and "Show all" row per text size, measured off a hidden sample: both are always
/// one line, so a far-off edit can stand in at its exact height without being drawn.
@MainActor enum EditMetrics {
    static var header: [DynamicTypeSize: CGFloat] = [:]
    static var showAll: [DynamicTypeSize: CGFloat] = [:]
}

/// Lays out a sample header and "Show all" row, unseen, and notes their heights in EditMetrics. Mounted behind the
/// transcript, so it has measured before any history lands.
struct EditMetricsProbe: View {
    @Environment(\.dynamicTypeSize) private var typeSize

    private static let sample: ToolRow = {
        var row = ToolRow(id: "probe", at: 0)
        row.apply(["name": "Edit", "detail": "file.swift", "done": true,
                   "diff": [["old": 1, "new": 1, "lines": ["-old", "+new"]]]])
        return row
    }()

    var body: some View {
        VStack(spacing: 0) {
            EditHeader(row: Self.sample, live: false)
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { EditMetrics.header[typeSize] = $0 }
            ShowAllLabel(text: "Show all 99 lines")
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { EditMetrics.showAll[typeSize] = $0 }
        }
        .hidden()
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}

/// An edit card's lines, drawn in one Canvas: number, sign and text on each, a red or green tint the width of the
/// card behind removals and additions, `⋯` between hunks. Laid out as views it was three texts and a tinted row per
/// line, and a long edit-heavy reply holds a couple of hundred cards: switching into one stalled for seconds.
/// Rows are a fixed pitch since a diff line never wraps (the card scrolls sideways instead).
private struct DiffSheet: View {
    let lines: [DiffLine?]
    let numberWidth: CGFloat
    /// The card's own width: every row's tint spans at least that, or the longest line.
    let minWidth: CGFloat
    /// False while the card is far off screen: only its height is taken, nothing is measured or drawn.
    var drawn = true
    @Environment(\.displayScale) private var scale
    @Environment(\.dynamicTypeSize) private var typeSize

    private static let font = Font.system(.caption, design: .monospaced)
    private static let leading: CGFloat = 6
    private static let signWidth: CGFloat = 18
    private static let trailing: CGFloat = 12

    /// The UIKit twin of `font` per text size, made once: building a UIFont costs far more than a card's whole
    /// layout, and asked for on every row it made the cards slower than the views they replaced.
    @MainActor private static var uiFonts: [DynamicTypeSize: UIFont] = [:]
    @MainActor private static func uiFont(_ typeSize: DynamicTypeSize) -> UIFont {
        if let hit = uiFonts[typeSize] { return hit }
        let size = UIFont.preferredFont(forTextStyle: .caption1, compatibleWith: UITraitCollection(preferredContentSizeCategory: UIContentSizeCategory(typeSize))).pointSize
        let made = UIFont.monospacedSystemFont(ofSize: size, weight: .regular)
        uiFonts[typeSize] = made
        return made
    }
    private var uiFont: UIFont { Self.uiFont(typeSize) }

    /// One line's height as a Text would take it, on whole pixels; a row adds 1.5 above and below, the gap between
    /// hunks 2.
    @MainActor private static func lineHeight(_ typeSize: DynamicTypeSize, scale: CGFloat) -> CGFloat {
        (uiFont(typeSize).lineHeight * scale).rounded(.up) / scale
    }
    private var lineHeight: CGFloat { Self.lineHeight(typeSize, scale: scale) }

    /// The sheet's height for so many lines and gaps between hunks, for a far-off edit standing in undrawn.
    @MainActor static func height(lines: Int, gaps: Int, typeSize: DynamicTypeSize, scale: CGFloat) -> CGFloat {
        let line = lineHeight(typeSize, scale: scale)
        return CGFloat(lines - gaps) * (line + 3) + CGFloat(gaps) * (line + 4)
    }

    // Sized from inside the body only: read from the card, the environment above isn't installed yet, and every
    // read logs a runtime fault with a backtrace (that alone cost seconds on a long reply).
    private var height: CGFloat {
        Self.height(lines: lines.count, gaps: lines.filter { $0 == nil }.count, typeSize: typeSize, scale: scale)
    }

    /// The longest line, so the card scrolls sideways as far as it needs.
    private var width: CGFloat {
        let font = uiFont
        let longest = lines.lazy.compactMap { $0 }.map { ($0.text as NSString).size(withAttributes: [.font: font]).width }.max() ?? 0
        return Self.leading + numberWidth + Self.signWidth + longest.rounded(.up) + Self.trailing
    }

    var body: some View {
        if drawn {
            canvas
        } else {
            Color.clear.frame(height: height)
        }
    }

    private var canvas: some View {
        let pitch = lineHeight
        return Canvas { ctx, size in
            var y: CGFloat = 0
            for line in lines {
                let row = pitch + (line == nil ? 4 : 3)
                let middle = y + row / 2
                if let line {
                    let tint: Color = line.sign == "-" ? .red : line.sign == "+" ? .green : .clear
                    if tint != .clear {
                        ctx.fill(Path(CGRect(x: 0, y: y, width: size.width, height: row)), with: .color(tint.opacity(0.16)))
                    }
                    if let number = line.number {
                        ctx.draw(Text(String(number)).font(Self.font).foregroundStyle(.tertiary),
                                 at: CGPoint(x: Self.leading + numberWidth, y: middle), anchor: .trailing)
                    }
                    if line.sign != " " {
                        ctx.draw(Text(String(line.sign)).font(Self.font).foregroundStyle(tint),
                                 at: CGPoint(x: Self.leading + numberWidth + Self.signWidth / 2, y: middle), anchor: .center)
                    }
                    if !line.text.isEmpty {
                        ctx.draw(Text(line.text).font(Self.font).foregroundStyle(line.sign == " " ? Color.secondary : Color.primary),
                                 at: CGPoint(x: Self.leading + numberWidth + Self.signWidth, y: middle), anchor: .leading)
                    }
                } else {
                    ctx.draw(Text("⋯").font(Self.font).foregroundStyle(.tertiary), at: CGPoint(x: 10, y: middle), anchor: .leading)
                }
                y += row
            }
        }
        .frame(width: max(width, minWidth), height: height)
        .accessibilityElement()
        .accessibilityLabel(lines.compactMap { $0.map { "\($0.sign == "-" ? "removed " : $0.sign == "+" ? "added " : "")\($0.text)" } }
            .joined(separator: "\n"))
    }
}

/// Calls with no prose or edit between them, as the terminal draws them: while the group is going it is one
/// quiet line, the tool's symbol and what it is doing ("Reading ChatView.swift"), with a light scan across the
/// words; each new call swaps the symbol and fades its words in left to right. Once something follows, or the
/// reply is done, the line settles into the group's total. A tap, live or settled, opens the calls in a sheet
/// (ToolCallsSheet), as the Claude app does: each call on a line, and a tap on one shows what it was called with
/// and what came back.
private struct CallGroup: View {
    let rows: [ToolRow]
    let settled: Bool
    let live: Bool
    /// Passed in, not read off an observed client: a long reply has dozens of these, and each would redraw on every
    /// streamed token of every chat.
    let chatID: String
    @State private var sheet: ToolCallsSheet.Opened?

    private var folded: Bool { settled && rows.allSatisfy(\.done) }
    private var failed: Bool { rows.contains(where: \.isError) }

    var body: some View {
        header
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, 4)
            .sheet(item: $sheet) { ToolCallsSheet(opened: $0) }
    }

    private var header: some View {
        let latest = rows.last
        let symbol = folded ? (failed ? "exclamationmark.circle" : "checkmark.circle") : latest?.symbol ?? "gearshape"
        // A tap, not a Button: a Button still fires when a slide that started on it lifts, so a pull on the
        // chats drawer from this line opened the sheet. A tap gives up once the finger moves.
        return HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: symbol)
                    .foregroundStyle(folded && failed ? Color.red : Color.secondary)
                    .contentTransition(.symbolEffect(.replace))
                    .frame(width: 18)
                ScanText(text: folded ? ToolRow.summary(rows) : latest.map(\.live) ?? "",
                         scanning: !folded && latest?.waitsOnUser != true, live: live)
                    .id(folded ? "total" : latest?.id ?? "")
                Image(systemName: "chevron.right")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.tertiary)
        }
        .themeFont(.subheadline)
        .foregroundStyle(.secondary)
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
        .onTapGesture { openSheet() }
        .animation(.snappy, value: folded ? "total" : latest?.id)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isButton)
        .accessibilityLabel(folded ? ToolRow.summary(rows) : latest.map(\.live) ?? "")
        .accessibilityHint("Shows every call")
        .accessibilityAction { openSheet() }
    }

    private func openSheet() {
        guard let first = rows.first else { return }
        sheet = ToolCallsSheet.Opened(firstID: first.id, chatID: chatID, rows: rows)
    }
}

/// A group's calls in a sheet, as the Claude app shows them: half height first, pulled up to full; each call on a
/// line threaded to the next, a failed one in red with a warning, and a tap pushes its page (ToolCallPage), with a
/// swipe back. It follows the reply while it runs: the calls are looked up again on every update.
private struct ToolCallsSheet: View {
    struct Opened: Identifiable {
        let firstID: String
        let chatID: String
        let rows: [ToolRow]   // as they were on the tap, for when the chat no longer has them
        var id: String { firstID }
    }

    let opened: Opened
    @EnvironmentObject private var client: VisionClient
    @Environment(\.dismiss) private var dismiss

    /// The group as it is now: the calls that sit where its first one does, cut at edits as the chat cuts them,
    /// so a call that starts while the sheet is up joins it.
    static func rows(_ opened: Opened, in client: VisionClient) -> [ToolRow] {
        for message in (client.transcripts[opened.chatID] ?? []).reversed() {
            guard let first = message.tools.first(where: { $0.id == opened.firstID }) else { continue }
            let run = message.tools.filter { $0.at == first.at }
            return ToolRow.groups(run).first { $0.contains { $0.id == opened.firstID } } ?? opened.rows
        }
        return opened.rows
    }

    var body: some View {
        let rows = Self.rows(opened, in: client)
        NavigationStack {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(rows.enumerated()), id: \.element.id) { index, row in
                        NavigationLink(value: row.id) {
                            ToolCallLine(row: row, last: index == rows.count - 1)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 8)
            }
            .navigationTitle(rows.allSatisfy(\.done) ? ToolRow.summary(rows) : rows.last?.live ?? "Working")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button { dismiss() } label: { Image(systemName: "xmark") }
                        .accessibilityLabel("Close")
                }
            }
            .navigationDestination(for: String.self) { ToolCallPage(opened: opened, id: $0) }
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
        .presentationBackground(Color(.secondarySystemBackground))   // solid, as Claude's is: the glass let the chat wash it out
    }
}

/// One call in the sheet: its symbol on a thread down to the next call's, what it did, and its detail under that.
private struct ToolCallLine: View {
    let row: ToolRow
    let last: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            VStack(spacing: 4) {
                Image(systemName: row.symbol)
                    .foregroundStyle(row.isError ? Color.red : Color.secondary)
                    .frame(width: 24, height: 24)
                if !last {
                    Rectangle()
                        .fill(Color(.separator))
                        .frame(width: 1)
                        .frame(maxHeight: .infinity)
                }
            }
            .padding(.top, 12)
            VStack(alignment: .leading, spacing: 2) {
                ScanText(text: row.done ? row.did : row.doing, scanning: !row.done && !row.waitsOnUser, live: false)
                    .id(row.done)
                    .foregroundStyle(row.isError ? Color.red : Color.primary)
                if !row.detail.isEmpty {
                    Text(row.detail)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            .padding(.vertical, 12)
            Spacer(minLength: 0)
            if row.isError {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(.red)
                    .padding(.top, 14)
            }
        }
        .themeFont(.body)
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityHint("Shows what it ran and what came back")
    }
}

/// One call's page: what it was called with, field by field (a command in code colours), then what came back.
private struct ToolCallPage: View {
    let opened: ToolCallsSheet.Opened
    let id: String
    @EnvironmentObject private var client: VisionClient

    private let mono = Font.system(.subheadline, design: .monospaced)

    var body: some View {
        let row = ToolCallsSheet.rows(opened, in: client).first { $0.id == id } ?? ToolRow(id: id, at: 0)
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                // A machine from before the call page sends no input: its one-line detail stands in.
                if row.input.isEmpty && !row.detail.isEmpty {
                    field("Detail") { Text(row.detail) }
                }
                ForEach(row.input, id: \.key) { item in
                    field(item.label) {
                        if item.isCode { Text(ShellColours.attributed(item.value)).font(mono) } else { Text(item.value) }
                    }
                }
                field("Output") {
                    if row.done {
                        Text(row.output.isEmpty ? "(no output)" : row.output)
                            .font(mono)
                            .foregroundStyle(row.isError ? Color.red : Color.primary)
                    } else {
                        ScanText(text: "Running…", scanning: true, live: false)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .padding(20)
        }
        .navigationTitle(row.name.isEmpty ? "Tool" : row.name)
        .navigationBarTitleDisplayMode(.inline)
    }

    private func field<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .themeFont(.subheadline, weight: .medium)
                .foregroundStyle(.secondary)
            content()
                .themeFont(.body)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(14)
                .background(Color(.tertiarySystemFill), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Color(.separator).opacity(0.6), lineWidth: 0.5))
        }
    }
}

/// A shell command in code colours, as the Claude app draws one: quoted strings green, the word in command
/// position (the start, and after `&&`, `||`, `|` or `;`) amber, the operators between them dimmed.
private enum ShellColours {
    static func attributed(_ command: String) -> AttributedString {
        var out = AttributedString()
        var commandNext = true
        var i = command.startIndex

        func add(_ s: Substring, _ color: Color?) {
            var part = AttributedString(String(s))
            if let color { part.foregroundColor = color }
            out += part
        }

        while i < command.endIndex {
            let c = command[i]
            var j = command.index(after: i)
            if c == "'" || c == "\"" {
                while j < command.endIndex && command[j] != c {
                    let escaped = c == "\"" && command[j] == "\\"
                    j = command.index(j, offsetBy: escaped ? 2 : 1, limitedBy: command.endIndex) ?? command.endIndex
                }
                if j < command.endIndex { j = command.index(after: j) }
                add(command[i..<j], .green)
                commandNext = false
            } else if "&|;".contains(c) {
                while j < command.endIndex && "&|;".contains(command[j]) { j = command.index(after: j) }
                add(command[i..<j], .secondary)
                commandNext = true
            } else if c.isWhitespace {
                while j < command.endIndex && command[j].isWhitespace { j = command.index(after: j) }
                add(command[i..<j], nil)
            } else {
                // A word runs to a space, a quote or an operator; the `&` of a redirect (`2>&1`) is the word's own.
                while j < command.endIndex {
                    let ch = command[j]
                    if ch.isWhitespace || "'\";|".contains(ch) { break }
                    if ch == "&" && !"<>".contains(command[command.index(before: j)]) { break }
                    j = command.index(after: j)
                }
                let word = command[i..<j]
                let assignment = word.contains("=") && !word.hasPrefix("-")
                add(word, commandNext && !assignment ? .orange : nil)
                if !assignment { commandNext = false }   // `FOO=1 make`: the command is the word after
            }
            i = j
        }
        return out
    }
}

/// One line that fades in a character at a time, left to right, when it appears, and while `scanning`
/// has a soft band of light sweep across it, the way a system "thinking" label does. A project thread's
/// activity line wears it too (ThreadViews.swift).
struct ScanText: View {
    let text: String
    let scanning: Bool
    @State private var reveal: Double
    @State private var sweep = 0.0

    /// `live`: the line belongs to a reply still coming in, so it fades in; a line from history draws whole,
    /// as a thread opening with dozens of them fading at once looked ghostly and cost the drawer its frames.
    init(text: String, scanning: Bool, live: Bool) {
        self.text = text
        self.scanning = scanning
        _reveal = State(initialValue: live ? 0 : 1)
    }

    var body: some View {
        Text(text)
            .lineLimit(1)
            .truncationMode(.middle)
            .textRenderer(ScanRenderer(reveal: reveal, sweep: sweep, scanning: scanning))
            .onAppear {
                if reveal < 1 { withAnimation(.easeOut(duration: 0.45)) { reveal = 1 } }
                // Only a live line sweeps: a settled one looping forever redraws every frame and drags the thread.
                // Callers re-id the view when `scanning` flips, so this runs again for the settled line.
                if scanning { withAnimation(.linear(duration: 1.6).repeatForever(autoreverses: false)) { sweep = 1 } }
            }
    }
}

private struct ScanRenderer: TextRenderer, Animatable {
    var reveal: Double
    var sweep: Double
    let scanning: Bool

    var animatableData: AnimatablePair<Double, Double> {
        get { AnimatablePair(reveal, sweep) }
        set { reveal = newValue.first; sweep = newValue.second }
    }

    func draw(layout: Text.Layout, in ctx: inout GraphicsContext) {
        let glyphs = layout.flatMap { $0 }.flatMap { $0 }
        let count = Double(max(glyphs.count, 1))
        let width = max(layout.map { $0.typographicBounds.rect.maxX }.max() ?? 1, 1)
        let spread = 0.35  // of the fade-in, how long one glyph takes to come up
        let band = 0.22    // the scan's half-width, as a share of the line
        let centre = -band + sweep * (1 + 2 * band)
        for (i, glyph) in glyphs.enumerated() {
            let start = Double(i) / count * (1 - spread)
            var alpha = min(max((reveal - start) / spread, 0), 1)
            if scanning {
                let x = glyph.typographicBounds.rect.midX / width
                let light = max(0, 1 - abs(x - centre) / band)
                alpha *= 0.55 + 0.45 * light * light
            }
            var copy = ctx
            copy.opacity = alpha
            copy.draw(glyph)
        }
    }
}

/// A picture from a reply, fetched from the machine; tap for the full-screen viewer.
struct ReplyImage: View {
    let path: String
    let caption: String
    /// Held, not observed, as the bubble holds it.
    let client: VisionClient
    let open: (UIImage) -> Void
    @State private var image: UIImage?
    @State private var failed = false

    init(path: String, caption: String, client: VisionClient, open: @escaping (UIImage) -> Void) {
        self.path = path
        self.caption = caption
        self.client = client
        self.open = open
        // One already in memory is drawn in the first frame, not a frame of placeholder first.
        _image = State(initialValue: client.cachedReplyImage(path: path))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let image {
                Image(uiImage: image)
                    .resizable()
                    .scaledToFit()
                    .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
                    .frame(maxHeight: 440, alignment: .leading)
                    .contentShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
                    .onTapGesture { open(image) }
                    .accessibilityAddTraits(.isButton)
                    .accessibilityLabel(caption.isEmpty ? "Picture, tap to preview" : "\(caption), tap to preview")
            } else if failed {
                FileChip(name: (path as NSString).lastPathComponent)
            } else if let size = client.replyImageSize(path: path) {
                // Fetched before: the picture's own room, laid out as the picture is, so its arrival moves nothing.
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .fill(Color(.tertiarySystemFill))
                    .aspectRatio(size, contentMode: .fit)
                    .overlay { ProgressView() }
                    .frame(maxHeight: 440, alignment: .leading)
            } else {
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .fill(Color(.tertiarySystemFill))
                    .frame(height: 200)
                    .overlay { ProgressView() }
            }
            if !caption.isEmpty {
                Text(caption)
                    .themeFont(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
        .task(id: path) {
            image = await client.replyImage(path: path)
            failed = image == nil
        }
    }
}

/// A plan the reply proposed, set apart from the prose around it: a card edged in plan mode's green.
private struct PlanBlock: View {
    let text: String
    let streaming: Bool

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: 16, style: .continuous)
        VStack(alignment: .leading, spacing: 8) {
            Label("Plan", systemImage: "list.bullet.clipboard")
                .themeFont(.subheadline, weight: .semibold)
                .foregroundStyle(Color.green)
            ReplyMarkdown(text: text, streaming: streaming, selectable: true)
                .themeFont(.body)
                .lineSpacing(5)
                .selectable(!streaming)
                .foregroundStyle(Color.primary)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.green.opacity(0.07), in: shape)
        .overlay(shape.strokeBorder(Color.green.opacity(0.35), lineWidth: 1))
        .padding(.vertical, 4)
    }
}

/// A settled question form inside a reply, as the terminal prints it: a headline, then one
/// dim "question → answer" line per question (the options offered, when it was declined).
private struct AnsweredBlock: View {
    let form: AnsweredForm

    var body: some View {
        let plural = form.rows.count > 1 ? "s" : ""
        VStack(alignment: .leading, spacing: 4) {
            Text(form.declined ? "You declined to answer Vision's question\(plural)" : "You answered Vision's question\(plural):")
                .themeFont(.subheadline)
                .foregroundStyle(.primary)
            ForEach(Array(form.rows.enumerated()), id: \.offset) { _, row in
                Text(form.declined ? "\(row.question) (\(row.answer))" : "\(row.question) → \(row.answer)")
                    .themeFont(.subheadline)
                    .foregroundStyle(.secondary)
                    .padding(.leading, 12)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 4)
        .accessibilityElement(children: .combine)
    }
}

/// Subagents a reply ran, two lines each: what it was asked, with a green tick or red cross and how long
/// it took, then the model, effort and tool count. Running ones sit here too, where they started, with a
/// pulsing sparkle, a live clock and a verb for what they're doing. More than three fold to one line; tap to open it.
private struct AgentBlock: View {
    let rows: [AgentRow]
    @EnvironmentObject private var client: VisionClient
    @State private var open = false
    @State private var selected: AgentRow?
    @State private var selectedChat = ""

    private var folded: Bool { rows.count > 3 && !open }

    var body: some View {
        if !rows.isEmpty { block }
    }

    private var block: some View {
        VStack(alignment: .leading, spacing: 2) {
            if folded {
                let failed = rows.filter { $0.done && $0.failed }.count
                let running = rows.filter { !$0.done }.count
                let state = running > 0 ? "\(running) running" : failed == 0 ? "all done" : "\(failed) failed"
                // Taps, not Buttons, as on CallGroup: a slide from here pulls the chats drawer and opens nothing.
                Label("\(rows.count) agents · \(state)", systemImage: "chevron.right")
                    .labelStyle(.titleAndIcon)
                    .themeFont(.subheadline)
                    .foregroundStyle(failed == 0 || running > 0 ? Color.secondary : Color.red)
                    .contentShape(Rectangle())
                    .onTapGesture { withAnimation(.snappy) { open = true } }
                    .accessibilityAddTraits(.isButton)
            } else {
                ForEach(rows) { row in
                    AgentLine(row: row)
                        .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                        .contentShape(Rectangle())
                        .onTapGesture {
                            selectedChat = client.active?.id ?? ""
                            selected = row
                        }
                        .accessibilityElement(children: .combine)
                        .accessibilityAddTraits(.isButton)
                        .accessibilityHint("Open task, activity and result")
                }
                if open {
                    Button("Show less") { withAnimation(.snappy) { open = false } }
                        .themeFont(.footnote)
                        .foregroundStyle(.secondary)
                        .buttonStyle(.plain)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 4)
        .sheet(item: $selected) { row in
            AgentDetailView(initial: row, chatID: selectedChat)
        }
    }
}

private struct AgentDetailView: View {
    let initial: AgentRow
    let chatID: String
    @EnvironmentObject private var client: VisionClient
    @Environment(\.dismiss) private var dismiss

    // Resolve the row on every update so the sheet follows live frames and completion.
    private var row: AgentRow {
        for message in (client.transcripts[chatID] ?? []).reversed() {
            if let current = message.agents.first(where: { $0.id == initial.id }) { return current }
        }
        return initial
    }

    var body: some View {
        let current = row
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    AgentLine(row: current)
                    if !current.done && client.connection != .online {
                        Text("Reconnecting. Activity will update when connected.")
                            .themeFont(.footnote)
                            .foregroundStyle(.secondary)
                    }
                    section("Task") {
                        Text(current.prompt.isEmpty ? current.label : current.prompt)
                            .textSelection(.enabled)
                    }
                    section("Activity") {
                        if current.steps.isEmpty {
                            Text(current.done ? "No activity was recorded for this agent." : "Waiting for activity…")
                                .foregroundStyle(.secondary)
                        } else {
                            LazyVStack(alignment: .leading, spacing: 16) {
                                ForEach(Array(current.steps.enumerated()), id: \.offset) { index, step in
                                    HStack(alignment: .top, spacing: 12) {
                                        Text("\(index + 1)")
                                            .monospacedDigit()
                                            .foregroundStyle(.tertiary)
                                            .frame(minWidth: 24, alignment: .trailing)
                                        VStack(alignment: .leading, spacing: 4) {
                                            Text(step.tool).bold()
                                            if !step.detail.isEmpty {
                                                Text(step.detail).foregroundStyle(.secondary)
                                            }
                                        }
                                        .textSelection(.enabled)
                                    }
                                }
                            }
                        }
                    }
                    if !current.summary.isEmpty || !current.details.isEmpty {
                        section("Result") {
                            if !current.summary.isEmpty { Text(current.summary).textSelection(.enabled) }
                            ForEach(Array(current.details.enumerated()), id: \.offset) { _, detail in
                                Text(detail).textSelection(.enabled)
                            }
                        }
                    } else if current.done {
                        Text("No result was recorded for this agent.")
                            .foregroundStyle(.secondary)
                    }
                }
                .themeFont(.subheadline)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .navigationTitle("Agent")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
        .presentationDragIndicator(.visible)
    }

    private func section<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(title).themeFont(.headline)
            content()
        }
    }
}

private struct AgentLine: View {
    let row: AgentRow

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            badge
                .themeFont(.footnote, weight: .semibold)
                .frame(width: 16)
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    // The task wraps rather than truncating: it is the one thing on the row worth reading whole.
                    Text(row.label.isEmpty ? row.kind.capitalized : row.label)
                        .themeFont(.subheadline, weight: .medium)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 4)
                    clock
                        .themeFont(.footnote)
                        .monospacedDigit()
                        .foregroundStyle(.tertiary)
                    Image(systemName: "chevron.right")
                        .themeFont(.caption2, weight: .semibold)
                        .foregroundStyle(.tertiary)
                }
                detail
                    .themeFont(.footnote)
                    .lineLimit(1)
            }
        }
        .padding(.vertical, 6)
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder private var badge: some View {
        if !row.done {
            Image(systemName: "sparkle")
                .foregroundStyle(Color.cyan)
                .symbolEffect(.pulse, options: .repeating)
        } else if row.failed || row.cutOff {
            Image(systemName: "xmark.circle.fill").foregroundStyle(Color.red)
        } else {
            Image(systemName: "checkmark.circle.fill").foregroundStyle(Color.green)
        }
    }

    /// Running: a live clock. Done: how long it took, the time out of the status (`3 tools · 17.3s · ↓1,204`).
    @ViewBuilder private var clock: some View {
        if !row.done {
            TimelineView(.periodic(from: row.started, by: 1)) { context in
                Text(AgentRow.clock(Int(context.date.timeIntervalSince(row.started))))
            }
        } else if let took = statusParts.first(where: { $0.first?.isNumber == true && !$0.contains("tool") }) {
            Text(took)
        }
    }

    private var statusParts: [String] {
        row.status.components(separatedBy: " · ")
    }

    /// Under the task. Running: who's on it, steady, then a plain verb for what it's doing, darker and
    /// shimmering since that's the part that changes. Done: who did it and how many tools it used.
    @ViewBuilder private var detail: some View {
        if !row.done {
            HStack(alignment: .firstTextBaseline, spacing: 0) {
                if !brain.isEmpty {
                    Text(row.step.isEmpty ? brain : brain + " · ").foregroundStyle(Color.primary.opacity(0.75))
                }
                if !row.step.isEmpty {
                    ScanText(text: verb, scanning: true, live: false)
                        .foregroundStyle(.secondary)
                        .id(verb)
                }
            }
        } else {
            let end = row.cutOff ? "Cut off" : row.failed ? "Failed" : ""
            let tools = row.tools > 0 ? "\(row.tools) tool\(row.tools == 1 ? "" : "s")" : ""
            let rest = [brain, tools].filter { !$0.isEmpty }.joined(separator: " · ")
            if end.isEmpty {
                Text(rest).foregroundStyle(.secondary)
            } else {
                Text(end).foregroundStyle(Color.red) + Text(rest.isEmpty ? "" : " · \(rest)").foregroundStyle(.secondary)
            }
        }
    }

    private var brain: String {
        [row.model, row.effort].filter { !$0.isEmpty }.joined(separator: " · ")
    }

    /// The newest step's tool as a verb; the full step stays in the detail sheet.
    private var verb: String {
        let tool = row.steps.last?.tool ?? row.step.split(separator: " ").first.map(String.init) ?? ""
        switch tool {
        case "Read", "NotebookRead": return "Reading"
        case "Grep", "Glob", "LS", "ToolSearch": return "Searching"
        case "Edit", "MultiEdit", "NotebookEdit": return "Editing"
        case "Write": return "Writing"
        case "Bash", "BashOutput": return "Running a command"
        case "WebSearch": return "Searching the web"
        case "WebFetch": return "Reading a page"
        case "Agent", "Task": return "Handing off"
        case "TodoWrite": return "Planning"
        case "message": return "Messaging"
        default: return "Working"
        }
    }
}

extension View {
    @ViewBuilder
    func noScrollEdges() -> some View {
        if #available(iOS 26.0, *) {
            scrollEdgeEffectHidden(true, for: .all)
        } else {
            self
        }
    }
}

/// "12.3s" under a reply, as the terminal shows it: ticking from `began` while the reply streams, or
/// the fixed `took` once it is done. Only this text redraws on each tick, not the bubble above it.
struct ReplyTimer: View {
    let began: Date?
    let took: TimeInterval?

    var body: some View {
        Group {
            if let took {
                Text(Message.took(took))
            } else if let began {
                TimelineView(.periodic(from: began, by: 0.1)) { context in
                    Text(Message.took(context.date.timeIntervalSince(began)))
                }
            }
        }
        .themeFont(.footnote)
        .monospacedDigit()
        .foregroundStyle(.tertiary)
        .accessibilityLabel(took != nil ? "Took \(Message.took(took ?? 0))" : "Working")
    }
}

/// One of your own messages with its words selectable where they sit, after "Select Text": laid out as the
/// Text it stands in for, all of it selected to begin with, the handles there to drag. Letting the
/// selection go (a tap on it, or anywhere else on the chat) hands the bubble back.
private struct SelectableBubbleText: UIViewRepresentable {
    let text: String
    let lineSpacing: CGFloat
    let onDone: () -> Void
    @Environment(\.theme) private var theme

    final class Coordinator: NSObject, UITextViewDelegate {
        var onDone: () -> Void = {}
        /// The opening selection is in: from here an empty one means it was let go.
        var armed = false

        func textViewDidChangeSelection(_ view: UITextView) {
            if view.selectedRange.length > 0 {
                ReplySelection.began(in: view)
            } else if armed {
                armed = false
                onDone()
            }
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> UITextView {
        let view = UITextView()
        view.delegate = context.coordinator
        view.isEditable = false
        view.isScrollEnabled = false
        view.backgroundColor = .clear
        view.textContainerInset = .zero
        view.textContainer.lineFragmentPadding = 0
        view.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        view.isAccessibilityElement = true
        view.accessibilityTraits = .staticText
        view.accessibilityLabel = text
        let style = NSMutableParagraphStyle()
        style.lineSpacing = lineSpacing
        view.attributedText = NSAttributedString(string: text, attributes: [
            .font: theme.uiFont(.body), .foregroundColor: theme.userInk.map { UIColor($0) } ?? UIColor.label, .paragraphStyle: style])
        DispatchQueue.main.async { [coordinator = context.coordinator] in
            view.becomeFirstResponder()
            view.selectedRange = NSRange(location: 0, length: (text as NSString).length)
            coordinator.armed = true
        }
        return view
    }

    func updateUIView(_ view: UITextView, context: Context) {
        context.coordinator.onDone = onDone
    }

    func sizeThatFits(_ proposal: ProposedViewSize, uiView view: UITextView, context: Context) -> CGSize? {
        guard let width = proposal.width, width.isFinite, width > 0 else { return nil }
        let size = view.sizeThatFits(CGSize(width: width, height: .greatestFiniteMagnitude))
        return CGSize(width: min(width, ceil(size.width)), height: ceil(size.height))
    }
}

/// Copy, Share and Read aloud under a finished reply, beside its time: the whole reply as the machine wrote it
/// (markdown and all), for when selecting it by hand is too fiddly. Small and quiet, like the time.
struct ReplyActions: View {
    let text: String
    /// Held, not observed, as the bubble holds it.
    let client: VisionClient

    var body: some View {
        HStack(spacing: 0) {
            Button {
                UIPasteboard.general.string = text
                client.toast = "Copied"
            } label: {
                ReplyActionIcon(name: "doc.on.doc")
            }
            .accessibilityLabel("Copy reply")
            ShareLink(item: text) { ReplyActionIcon(name: "square.and.arrow.up") }
                .accessibilityLabel("Share reply")
            if client.canReadAloud {
                ReadAloudButton(text: text, readAloud: client.readAloud)
            }
        }
        .buttonStyle(PressHapticButtonStyle())
        .foregroundStyle(.tertiary)
    }
}

private struct ReplyActionIcon: View {
    let name: String

    var body: some View {
        Image(systemName: name)
            .font(.system(size: 13, weight: .medium))
            .frame(width: 34, height: 30)
            .contentShape(Rectangle())
    }
}

/// Reads the reply aloud, or stops it while it is being read. Observes only the read-aloud state, which
/// changes when reading starts or stops, so streaming text elsewhere never redraws it.
private struct ReadAloudButton: View {
    let text: String
    @ObservedObject var readAloud: ReadAloud

    var body: some View {
        let reading = readAloud.reading == text
        Button {
            readAloud.toggle(text)
        } label: {
            ReplyActionIcon(name: reading ? "stop.fill" : "speaker.wave.2")
                .contentTransition(.symbolEffect(.replace))
        }
        .foregroundStyle(reading ? AnyShapeStyle(.secondary) : AnyShapeStyle(.tertiary))
        .accessibilityLabel(reading ? "Stop reading" : "Read aloud")
    }
}


extension Color {
    /// Shell mode (`!`): its composer border, send button and rows. Claude Code's bash pink, as the terminal's.
    static let shellPink = Color(red: 0xfd / 255, green: 0x5d / 255, blue: 0xb1 / 255)
}

/// A command run in shell mode: `! command` in pink, then what it printed, dim and monospaced (the first
/// lines until tapped), and how it ended when that wasn't cleanly.
private struct ShellRow: View {
    let message: Message
    @State private var expanded = false
    private static let folded = 12

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text("!").font(.system(.body, design: .monospaced).bold()).foregroundStyle(Color.shellPink)
                Text(message.text).font(.system(.body, design: .monospaced)).textSelection(.enabled)
            }
            if message.shellRunning {
                if !message.shellOutput.isEmpty {
                    // So far, the end of it: a command that prints and then waits (a login's code) shows it now.
                    let lines = message.shellOutput.split(separator: "\n", omittingEmptySubsequences: false)
                    Text(lines.suffix(Self.folded).joined(separator: "\n"))
                        .font(.system(.footnote, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text("running…").themeFont(.footnote).foregroundStyle(.secondary)
                }
            } else {
                let lines = message.shellOutput.split(separator: "\n", omittingEmptySubsequences: false)
                let long = lines.count > Self.folded
                Text(message.shellOutput.isEmpty ? "(no output)" : long && !expanded ? lines.prefix(Self.folded).joined(separator: "\n") : message.shellOutput)
                    .font(.system(.footnote, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if long {
                    Button(expanded ? "Show less" : "Show all \(lines.count) lines") { expanded.toggle() }
                        .font(.footnote)
                }
                if message.shellCode != 0 {
                    Text(message.shellCode.map { "exit \($0)" } ?? "stopped")
                        .font(.system(.footnote, design: .monospaced))
                        .foregroundStyle(.red)
                }
            }
        }
        .padding(12)
        .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}
