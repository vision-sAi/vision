import SwiftUI

/// A reply's markdown drawn the way the Claude and ChatGPT apps do: headings, bullet and numbered lists
/// with hanging indents, code in a box, quotes, rules and tables. Plans are where it shows most, since
/// they arrive as `## Context` and numbered steps. Inline bits (bold, code, links) go through Apple's
/// inline parser one block at a time, remembered, so a streaming reply only re-parses its last block.
/// The font, line spacing and colour come from the caller, as for a plain Text.
///
/// `selectable` lets a finger select part of a settled reply: a Text on iOS only offers its whole self
/// to copy, so each run of paragraphs, headings and lists is one text view then (SelectableProse), set
/// in the body style. Code, quotes and tables stay as they are, whole to copy.
struct ReplyMarkdown: View {
    let text: String
    var streaming = false
    var selectable = false
    var isError = false
    /// The stream has just closed and the last words are still fading up: the text views wait for them.
    @State private var settling = false

    var body: some View {
        let blocks = Self.blocks(of: text, cache: !streaming)
        let last = blocks.count - 1
        VStack(alignment: .leading, spacing: 10) {
            if selectable && !streaming && !settling {
                ForEach(Array(Self.stretches(of: blocks).enumerated()), id: \.offset) { i, stretch in
                    switch stretch {
                    case .prose(let run):
                        SelectableProse(blocks: run, isError: isError)
                            .padding(.top, i > 0 && run.first?.isHeading == true ? 6 : 0)
                    case .block(let block):
                        BlockView(block: block, first: i == 0, cache: true)
                    }
                }
            } else {
                ForEach(Array(blocks.enumerated()), id: \.offset) { i, block in
                    // The last block keeps its fading text once the reply ends, so the final words finish coming up
                    // rather than snapping in when the stream closes.
                    BlockView(block: block, first: i == 0, cache: !streaming || i < last,
                              fades: streaming || i == last, live: streaming && i == last)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .onChange(of: streaming) { was, now in
            if was && !now { settling = true }
        }
        .task(id: settling) {
            guard settling else { return }
            try? await Task.sleep(for: .seconds(0.5))
            settling = false
        }
    }

    // MARK: Blocks

    struct Item: Equatable {
        var marker: String     // "•" or "3."
        var depth: Int
        var text: String
    }

    enum Block: Equatable {
        case paragraph(String)
        case heading(Int, String)
        case list([Item])
        case code(String, language: String)   // `language`: what the opening fence named ("python"), or ""
        case quote(String)
        case rule
        case table([[String]])

        var isHeading: Bool {
            if case .heading = self { return true }
            return false
        }
    }

    /// The blocks as a settled reply draws them: runs of prose that select as one, and the rest one by one.
    enum Stretch: Equatable {
        case prose([Block])
        case block(Block)
    }

    static func stretches(of blocks: [Block]) -> [Stretch] {
        var out: [Stretch] = []
        for block in blocks {
            switch block {
            case .paragraph, .heading, .list:
                if case .prose(let run)? = out.last {
                    out[out.count - 1] = .prose(run + [block])
                } else {
                    out.append(.prose([block]))
                }
            default:
                out.append(.block(block))
            }
        }
        return out
    }

    @MainActor private static var blockCache: [String: [Block]] = [:]

    /// The text split into blocks by a line scanner: enough markdown for what replies actually use, and
    /// forgiving of a reply cut off mid-stream (an open fence is code to the end).
    @MainActor static func blocks(of text: String, cache: Bool) -> [Block] {
        if cache, let hit = blockCache[text] { return hit }
        var blocks: [Block] = []
        var paragraph: [String] = []
        var indents: [Int] = []       // the open list's indent per depth
        let lines = text.components(separatedBy: "\n")
        var i = 0

        func flush() {
            if !paragraph.isEmpty { blocks.append(.paragraph(paragraph.joined(separator: "\n"))) }
            paragraph = []
        }
        func endList() { indents = [] }

        while i < lines.count {
            let line = lines[i]
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            let indent = Self.indent(of: line)

            if trimmed.isEmpty {
                flush()
                i += 1
                continue
            }
            if trimmed.hasPrefix("```") || trimmed.hasPrefix("~~~") {
                flush(); endList()
                let fence = String(trimmed.prefix(3))
                let language = trimmed.dropFirst(3).trimmingCharacters(in: .whitespaces)
                    .split(separator: " ").first.map(String.init) ?? ""
                var code: [String] = []
                i += 1
                while i < lines.count, !lines[i].trimmingCharacters(in: .whitespaces).hasPrefix(fence) {
                    code.append(lines[i])
                    i += 1
                }
                i += 1  // the closing fence
                blocks.append(.code(code.joined(separator: "\n"), language: language))
                continue
            }
            if indent <= 3, let (level, title) = Self.heading(trimmed) {
                flush(); endList()
                blocks.append(.heading(level, title))
                i += 1
                continue
            }
            if Self.isRule(trimmed) {
                flush(); endList()
                blocks.append(.rule)
                i += 1
                continue
            }
            if trimmed.hasPrefix("|"), i + 1 < lines.count, Self.isTableSeparator(lines[i + 1]) {
                flush(); endList()
                var rows = [Self.cells(of: trimmed)]
                i += 2
                while i < lines.count {
                    let row = lines[i].trimmingCharacters(in: .whitespaces)
                    guard row.hasPrefix("|") else { break }
                    rows.append(Self.cells(of: row))
                    i += 1
                }
                let width = rows.map(\.count).max() ?? 0
                blocks.append(.table(rows.map { $0 + Array(repeating: "", count: width - $0.count) }))
                continue
            }
            if trimmed.hasPrefix(">") {
                flush(); endList()
                var quote: [String] = []
                while i < lines.count {
                    let q = lines[i].trimmingCharacters(in: .whitespaces)
                    guard q.hasPrefix(">") else { break }
                    quote.append(String(q.dropFirst()).trimmingCharacters(in: .whitespaces))
                    i += 1
                }
                blocks.append(.quote(quote.joined(separator: "\n")))
                continue
            }
            if let (marker, body) = Self.item(trimmed) {
                flush()
                while let last = indents.last, indent < last { indents.removeLast() }
                if indents.last.map({ indent > $0 }) ?? true { indents.append(indent) }
                let item = Item(marker: marker, depth: indents.count - 1, text: body)
                if case .list(let items)? = blocks.last {
                    blocks[blocks.count - 1] = .list(items + [item])
                } else {
                    blocks.append(.list([item]))
                }
                i += 1
                continue
            }
            // An indented line straight after an item carries that item on.
            if paragraph.isEmpty, indent >= 2, case .list(let items)? = blocks.last, let last = items.last,
               i > 0, !lines[i - 1].trimmingCharacters(in: .whitespaces).isEmpty {
                blocks[blocks.count - 1] = .list(items.dropLast() + [Item(marker: last.marker, depth: last.depth, text: last.text + " " + trimmed)])
                i += 1
                continue
            }
            endList()
            paragraph.append(line)
            i += 1
        }
        flush()
        if cache {
            if blockCache.count >= 256 { blockCache.removeAll(keepingCapacity: true) }
            blockCache[text] = blocks
        }
        return blocks
    }

    private static func indent(of line: String) -> Int {
        var n = 0
        for c in line {
            if c == " " { n += 1 } else if c == "\t" { n += 4 } else { break }
        }
        return n
    }

    private static func heading(_ s: String) -> (Int, String)? {
        let hashes = s.prefix(while: { $0 == "#" }).count
        guard (1...6).contains(hashes) else { return nil }
        let rest = s.dropFirst(hashes)
        guard rest.isEmpty || rest.first == " " else { return nil }
        var title = rest.trimmingCharacters(in: .whitespaces)
        while title.hasSuffix("#") { title.removeLast() }   // "## Title ##"
        return (hashes, title.trimmingCharacters(in: .whitespaces))
    }

    private static func isRule(_ s: String) -> Bool {
        let marks = s.filter { !$0.isWhitespace }
        guard marks.count >= 3, let c = marks.first, "-*_".contains(c) else { return false }
        return marks.allSatisfy { $0 == c }
    }

    private static func item(_ s: String) -> (String, String)? {
        if let c = s.first, "-*+".contains(c), s.dropFirst().first == " " {
            return ("•", s.dropFirst(2).trimmingCharacters(in: .whitespaces))
        }
        let digits = s.prefix(while: \.isNumber)
        guard (1...3).contains(digits.count) else { return nil }
        let rest = s.dropFirst(digits.count)
        guard let dot = rest.first, dot == "." || dot == ")", rest.dropFirst().first == " " else { return nil }
        return ("\(digits).", rest.dropFirst(2).trimmingCharacters(in: .whitespaces))
    }

    private static func isTableSeparator(_ line: String) -> Bool {
        let s = line.trimmingCharacters(in: .whitespaces)
        return s.contains("-") && s.contains("|") && s.allSatisfy { "|:- ".contains($0) }
    }

    private static func cells(of row: String) -> [String] {
        var s = Substring(row)
        if s.hasPrefix("|") { s = s.dropFirst() }
        if s.hasSuffix("|") { s = s.dropLast() }
        return s.components(separatedBy: "|").map { $0.trimmingCharacters(in: .whitespaces) }
    }

    // MARK: Inline

    @MainActor private static var inlineCache: [String: AttributedString] = [:]

    /// Bold, code and links in one block, with its line breaks kept.
    @MainActor static func inline(_ text: String, cache: Bool) -> AttributedString {
        if cache, let hit = inlineCache[text] { return hit }
        var options = AttributedString.MarkdownParsingOptions()
        options.interpretedSyntax = .inlineOnlyPreservingWhitespace
        let result = (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
        if cache {
            if inlineCache.count >= 512 { inlineCache.removeAll(keepingCapacity: true) }
            inlineCache[text] = result
        }
        return result
    }
}

/// A run of paragraphs, headings and lists as one text view, so a long press selects words and the
/// handles drag across blocks. It is laid out to match what BlockView draws for the same blocks: the
/// same faces, gaps and hanging indents, so a reply doesn't shift when it settles into this.
private struct SelectableProse: UIViewRepresentable {
    let blocks: [ReplyMarkdown.Block]
    let isError: Bool
    @Environment(\.theme) private var theme
    @Environment(\.lineSpacing) private var lineSpacing
    @Environment(\.dynamicTypeSize) private var typeSize

    /// What the text was built from: built again only when one of these moves.
    fileprivate struct Source: Equatable {
        let blocks: [ReplyMarkdown.Block]
        let isError: Bool
        let theme: ThemeID
        let lineSpacing: CGFloat
        let typeSize: DynamicTypeSize
    }

    final class Coordinator: NSObject, UITextViewDelegate {
        fileprivate var source: Source?
        var fitted: (width: CGFloat, height: CGFloat)?

        func textViewDidChangeSelection(_ view: UITextView) {
            if view.selectedRange.length > 0 { ReplySelection.began(in: view) }
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
        // Read out, and found by the UI tests, as the text it was before: one label, not a text field.
        view.isAccessibilityElement = true
        view.accessibilityTraits = .staticText
        return view
    }

    func updateUIView(_ view: UITextView, context: Context) {
        let source = Source(blocks: blocks, isError: isError, theme: theme.id, lineSpacing: lineSpacing, typeSize: typeSize)
        guard context.coordinator.source != source else { return }
        context.coordinator.source = source
        context.coordinator.fitted = nil
        let text = Self.attributed(blocks, theme: theme, lineSpacing: lineSpacing, color: isError ? .systemRed : .label)
        view.attributedText = text
        view.accessibilityLabel = text.string.replacingOccurrences(of: "\t", with: " ").replacingOccurrences(of: "\u{2028}", with: "\n")
    }

    func sizeThatFits(_ proposal: ProposedViewSize, uiView view: UITextView, context: Context) -> CGSize? {
        guard let width = proposal.width, width.isFinite, width > 0 else { return nil }
        if let fitted = context.coordinator.fitted, fitted.width == width { return CGSize(width: width, height: fitted.height) }
        let height = ceil(view.sizeThatFits(CGSize(width: width, height: .greatestFiniteMagnitude)).height)
        context.coordinator.fitted = (width, height)
        return CGSize(width: width, height: height)
    }

    // MARK: The text

    /// The gaps BlockView's stacks leave: 10 between blocks, 6 more above a heading, 6 between a list's items.
    @MainActor static func attributed(_ blocks: [ReplyMarkdown.Block], theme: Theme, lineSpacing: CGFloat, color: UIColor) -> NSAttributedString {
        let out = NSMutableAttributedString()
        func paragraph(_ text: NSAttributedString, gap: CGFloat, indent: CGFloat = 0, hang: CGFloat = 0) {
            let style = NSMutableParagraphStyle()
            style.lineSpacing = lineSpacing
            // A text view leaves the line gap between paragraphs as well as lines; the stacks' gaps already count it.
            style.paragraphSpacingBefore = out.length == 0 ? 0 : max(0, gap - lineSpacing)
            style.firstLineHeadIndent = indent
            style.headIndent = indent + hang
            if hang > 0 { style.tabStops = [NSTextTab(textAlignment: .natural, location: indent + hang)] }
            let line = NSMutableAttributedString(attributedString: text)
            line.addAttribute(.paragraphStyle, value: style, range: NSRange(location: 0, length: line.length))
            if out.length > 0 { out.append(NSAttributedString(string: "\n", attributes: [.paragraphStyle: style, .font: theme.uiFont(.body)])) }
            out.append(line)
        }
        for block in blocks {
            switch block {
            case .paragraph(let text):
                paragraph(inline(text, style: .body, weight: .regular, theme: theme, color: color), gap: 10)
            case .heading(let level, let title):
                let style: UIFont.TextStyle = level == 1 ? .title3 : level == 2 ? .headline : .body
                paragraph(inline(title, style: style, weight: .semibold, theme: theme, color: color), gap: 16)
            case .list(let items):
                for (n, item) in items.enumerated() {
                    let face = theme.uiFont(.body)
                    let digits = UIFont(descriptor: face.fontDescriptor.addingAttributes([.featureSettings: [[
                        UIFontDescriptor.FeatureKey.type: kNumberSpacingType, UIFontDescriptor.FeatureKey.selector: kMonospacedNumbersSelector]]]), size: 0)
                    let marker = NSMutableAttributedString(string: item.marker, attributes: [.font: digits, .foregroundColor: UIColor.secondaryLabel])
                    let width = marker.size().width
                    marker.append(NSAttributedString(string: "\t", attributes: [.font: face]))
                    marker.append(inline(item.text, style: .body, weight: .regular, theme: theme, color: color))
                    paragraph(marker, gap: n == 0 ? 10 : 6, indent: CGFloat(item.depth) * 18, hang: width + 8)
                }
            default:
                break   // never in a prose run (ReplyMarkdown.stretches)
            }
        }
        return out
    }

    /// One block's inline markdown in UIKit's terms: bold, italic, code, strikethrough and links.
    @MainActor private static func inline(_ text: String, style: UIFont.TextStyle, weight: UIFont.Weight, theme: Theme, color: UIColor) -> NSAttributedString {
        let parsed = ReplyMarkdown.inline(text, cache: true)
        let out = NSMutableAttributedString()
        for run in parsed.runs {
            let intent = run.inlinePresentationIntent ?? []
            let heavy = intent.contains(.stronglyEmphasized) ? UIFont.Weight.bold : weight
            var font = theme.uiFont(style, weight: heavy)
            if intent.contains(.code) { font = .monospacedSystemFont(ofSize: font.pointSize, weight: heavy) }
            if intent.contains(.emphasized), let slanted = font.fontDescriptor.withSymbolicTraits(font.fontDescriptor.symbolicTraits.union(.traitItalic)) {
                font = UIFont(descriptor: slanted, size: 0)
            }
            var attributes: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: color]
            if intent.contains(.strikethrough) { attributes[.strikethroughStyle] = NSUnderlineStyle.single.rawValue }
            if let link = run.link { attributes[.link] = link }
            // A line break inside a block stays inside its paragraph, so it takes the line gap, not the block's.
            let words = String(parsed[run.range].characters).replacingOccurrences(of: "\n", with: "\u{2028}")
            out.append(NSAttributedString(string: words, attributes: attributes))
        }
        return out
    }
}

/// The one selection a finger has made in the chat, in a reply or in one of your own messages.
enum ReplySelection {
    @MainActor private static weak var view: UITextView?

    /// A selection has begun in `view`: one held elsewhere is let go.
    @MainActor static func began(in view: UITextView) {
        guard Self.view !== view else { return }
        Self.view?.selectedTextRange = nil
        Self.view = view
    }

    /// For the chats drawer: a slide that starts on the selection drags a handle, so the drawer keeps its
    /// hands off (SideDrawer). The handles and the loupe reach a little past the text.
    @MainActor static func holds(_ point: CGPoint) -> Bool {
        guard let view, view.window != nil, view.selectedRange.length > 0 else { return false }
        return view.convert(view.bounds, to: nil).insetBy(dx: -28, dy: -28).contains(point)
    }

    /// A tap anywhere else on the chat lets the selection go.
    @MainActor static func release(unlessAt point: CGPoint) {
        guard let view, view.selectedRange.length > 0, !holds(point) else { return }
        view.selectedTextRange = nil
    }
}

private struct BlockView: View {
    let block: ReplyMarkdown.Block
    let first: Bool
    let cache: Bool
    /// Its text fades up as it lands while on screen.
    var fades = false
    /// It has just appeared at the end of a streaming reply: what it already holds fades up too.
    var live = false
    @Environment(\.theme) private var theme

    @ViewBuilder private func prose(_ text: String, live: Bool) -> some View {
        let attributed = ReplyMarkdown.inline(text, cache: cache)
        if fades { FadingText(attributed, live: live) } else { Text(attributed) }
    }

    var body: some View {
        switch block {
        case .paragraph(let text):
            prose(text, live: live)
        case .heading(let level, let title):
            prose(title, live: live)
                .font(theme.font(level == 1 ? .title3 : level == 2 ? .headline : .body, weight: .semibold))
                .padding(.top, first ? 0 : 6)
                .accessibilityAddTraits(.isHeader)
        case .list(let items):
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(items.enumerated()), id: \.offset) { n, item in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(item.marker)
                            .monospacedDigit()
                            .foregroundStyle(.secondary)
                        prose(item.text, live: live && n == items.count - 1)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .padding(.leading, CGFloat(item.depth) * 18)
                }
            }
        case .code(let code, let language):
            CodeBlock(code: code, language: language)
        case .quote(let text):
            HStack(alignment: .top, spacing: 10) {
                Capsule().fill(Color(.tertiaryLabel)).frame(width: 3)
                prose(text, live: live)
                    .foregroundStyle(.secondary)
            }
            .fixedSize(horizontal: false, vertical: true)
        case .rule:
            Divider().padding(.vertical, 4)
        case .table(let rows):
            Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 8) {
                ForEach(Array(rows.enumerated()), id: \.offset) { r, row in
                    GridRow {
                        ForEach(Array(row.enumerated()), id: \.offset) { _, cell in
                            Text(ReplyMarkdown.inline(cell, cache: cache))
                                .themeFont(.subheadline, weight: r == 0 ? .semibold : nil)
                        }
                    }
                    if r == 0 { Divider().gridCellUnsizedAxes(.horizontal) }
                }
            }
            .padding(.vertical, 2)
        }
    }
}

/// A block's text with its newest characters fading up, ChatGPT style: each bit the drip lets out lands dark and
/// comes up over a third of a second, so the reply's leading edge is soft instead of popping in at full strength.
/// Only text that lands while the block is on screen fades; a reply from history draws whole.
private struct FadingText: View {
    let text: AttributedString
    /// Stretches of the text still coming up, oldest first: each runs from its offset to the next one's, or the end.
    @State private var fresh: [Fresh]
    /// Something is still coming up, so the clock runs; it stops once the last stretch has settled.
    @State private var fading: Bool

    struct Fresh: Equatable {
        var from: Int
        var at: TimeInterval
    }

    init(_ text: AttributedString, live: Bool) {
        self.text = text
        _fresh = State(initialValue: live ? [Fresh(from: 0, at: Date.timeIntervalSinceReferenceDate)] : [])
        _fading = State(initialValue: live)
    }

    var body: some View {
        TimelineView(.animation(paused: !fading)) { context in
            let now = context.date.timeIntervalSinceReferenceDate
            composed(now: now).textRenderer(ArrivalFade(now: now))
        }
        .onChange(of: text.characters.count) { old, new in
            let now = Date.timeIntervalSinceReferenceDate
            if new > old {
                fresh.removeAll { now - $0.at >= ArrivalFade.duration }   // the oldest, a prefix: they've settled
                fresh.append(Fresh(from: old, at: now))
                fading = true
            } else {
                // Text drawn shorter, as when a closing ** turns literal stars into bold: the stretches it still
                // holds keep their timing, so the edge doesn't snap.
                fresh.removeAll { $0.from >= new }
            }
        }
        .task(id: fresh.last?.at) {
            guard fading else { return }
            try? await Task.sleep(for: .seconds(ArrivalFade.duration + 0.05))
            if !Task.isCancelled {
                fading = false
                fresh = []
            }
        }
    }

    /// The text cut into its settled part and one tagged stretch per fresh arrival, for the renderer to fade.
    private func composed(now: TimeInterval) -> Text {
        guard let first = fresh.firstIndex(where: { now - $0.at < ArrivalFade.duration }) else { return Text(text) }
        let chars = text.characters
        func at(_ offset: Int) -> AttributedString.Index {
            chars.index(chars.startIndex, offsetBy: min(offset, chars.count))
        }
        var out = Text(AttributedString(text[text.startIndex..<at(fresh[first].from)]))
        for k in first..<fresh.count {
            let lo = at(fresh[k].from)
            let hi = k + 1 < fresh.count ? at(fresh[k + 1].from) : text.endIndex
            guard lo < hi else { continue }
            let stretch = Text(AttributedString(text[lo..<hi])).customAttribute(Arrival(at: fresh[k].at))
            out = Text("\(out)\(stretch)")
        }
        return out
    }
}

/// When a stretch of streaming text landed.
private struct Arrival: TextAttribute {
    let at: TimeInterval
}

/// Draws each tagged stretch at the opacity its age gives it; untagged text draws as usual.
private struct ArrivalFade: TextRenderer {
    static let duration: TimeInterval = 0.32
    let now: TimeInterval

    func draw(layout: Text.Layout, in ctx: inout GraphicsContext) {
        for line in layout {
            for run in line {
                guard let arrival = run[Arrival.self] else {
                    ctx.draw(run)
                    continue
                }
                let t = min(max((now - arrival.at) / Self.duration, 0), 1)
                var copy = ctx
                copy.opacity = 1 - (1 - t) * (1 - t)
                copy.draw(run)
            }
        }
    }
}

/// A code block: the language the reply named over it, and a button that copies exactly what is in it (no
/// fences, no markdown), as the Claude and ChatGPT apps have. The reply's own Copy takes the whole reply.
private struct CodeBlock: View {
    let code: String
    let language: String
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 6) {
                Text(language)
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(.secondary)
                Spacer(minLength: 8)
                Button {
                    UIPasteboard.general.string = code
                    copied = true
                    Task {
                        try? await Task.sleep(for: .seconds(1.5))
                        copied = false
                    }
                } label: {
                    HStack(spacing: 4) {
                        Image(systemName: copied ? "checkmark" : "doc.on.doc")
                        if copied { Text("Copied") }
                    }
                    .font(.caption2.weight(.medium))
                    .foregroundStyle(.secondary)
                    .frame(minWidth: 30, minHeight: 26, alignment: .trailing)
                    .contentShape(Rectangle())
                }
                .buttonStyle(PressHapticButtonStyle())
                .accessibilityLabel(copied ? "Copied" : "Copy code")
            }
            .padding(.leading, 12)
            .padding(.trailing, 8)
            .padding(.top, 4)
            ScrollView(.horizontal, showsIndicators: false) {
                Text(code)
                    .font(.system(.footnote, design: .monospaced))
                    .lineSpacing(2)
                    .padding(.horizontal, 12)
                    .padding(.bottom, 12)
                    .padding(.top, 2)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.secondarySystemFill), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}
