import UIKit

/// The composer's own keyboard, drawn to match the system one (geometry and colours measured from it) so every
/// touch is ours: while the attach card is open the keys go quiet and a touch on them only closes the card, no
/// character pop. Letters, numbers and symbols layers, shift and caps lock, predictive bar, key repeat on delete,
/// double-space full stop, auto-capitalisation, system key click. No Siri, no emoji layer; the mic is the app's own.
final class NativeKeyboard: UIInputView, UIInputViewAudioFeedback {
    weak var textView: UITextView?
    var onInertTouch: () -> Void = {}
    var onMic: () -> Void = {}
    /// The attach card is open: keys show nothing and do nothing but run `onInertTouch`.
    var inert = false {
        didSet {
            if inert { endTouch(perform: false) }
            // With the grid deaf, touches on it fall through to us and just close the card.
            emojiPanel?.isUserInteractionEnabled = !inert
        }
    }
    var enableInputClicksWhenVisible: Bool { true }

    private enum Layer { case letters, numbers, symbols, emoji }
    private enum Shift { case off, on, locked }
    private enum Kind: Equatable { case char(String), shift, delete, page(Layer), space, `return`, mic, emoji, category(Int) }
    private struct Key { let kind: Kind; let frame: CGRect }

    private var page: Layer = .letters
    private var shift: Shift = .on
    private var keys: [Key] = []
    private var keyViews: [UIView] = []
    private var suggestionLabels: [UILabel] = []
    private var separators: [UIView] = []
    private var emojiPanel: EmojiPanel?
    private var emojiPanelStyled = ""

    private var suggestions: [String] = []
    /// The key under the finger that's down, and that finger. One at a time: a second finger commits the first
    /// (rollover), the way the system keyboard keeps up with fast two-thumb typing.
    private var pressed: Int?
    private var pressTouch: ObjectIdentifier?
    private var suggestionsPending = false
    private var preview: UIView?
    private var repeatTimer: Timer?
    private var repeats = 0
    private var lastSpace: Date = .distantPast
    private var lastShift: Date = .distantPast
    private var dim: UIView?
    /// What the key views were last built for; layout passes that change none of it leave them alone.
    private var builtFor = ""

    // MARK: sizing (measured on a 402 pt wide phone, scaled by width)

    // Vertical geometry scales with width; horizontally iOS keeps a per-device gap (10 pt on a 402 pt phone, 7 pt
    // on the 420 pt iPhone Air) and lets the keys take the rest, so the widths derive from that gap.
    private var u: CGFloat { bounds.width / 402 }
    private var wideDevice: Bool { bounds.width >= 415 }
    private var gap: CGFloat { wideDevice ? 7 : 10 * u }
    private var margin: CGFloat { wideDevice ? 7 : 8.7 * u }
    private var keyW: CGFloat { (bounds.width - 2 * margin - 9 * gap) / 10 }
    private var keyH: CGFloat { 42.7 * u }
    private var wide: CGFloat { keyW + 12.5 * u }
    // Bottom row on the Air, measured off the real keyboard: 97 pt side keys, 5.8 pt gaps either side of space.
    private var side: CGFloat { wideDevice ? 97 : 2 * keyW + 28.5 * u }
    private var bottomGap: CGFloat { wideDevice ? 5.8 : gap }
    private var afterWide: CGFloat { gap + 7.4 * u }
    private var rowTops: [CGFloat] {
        (wideDevice ? [294.5, 238.8, 183.0, 127.2] : [282.3, 228.3, 174.3, 120.3].map { $0 * u }).map { bounds.height - $0 }
    }
    private var dark: Bool { traitCollection.userInterfaceStyle == .dark }
    private var keyFill: UIColor { dark ? UIColor(white: 68 / 255, alpha: 1) : .white }
    private var ink: UIColor { dark ? .white : .black }

    init() {
        let w = UIScreen.main.bounds.width
        let h: CGFloat = w >= 415 ? 345 : (w * 0.836).rounded()   // 345 pt on the Air, measured
        super.init(frame: CGRect(x: 0, y: 0, width: w, height: h), inputViewStyle: .keyboard)
        allowsSelfSizing = true
        isMultipleTouchEnabled = true
        for _ in 0..<2 {
            let sep = UIView()
            sep.isUserInteractionEnabled = false
            addSubview(sep)
            separators.append(sep)
        }
        for _ in 0..<3 {
            let l = UILabel()
            l.textAlignment = .center
            l.isUserInteractionEnabled = true
            l.addGestureRecognizer(UITapGestureRecognizer(target: self, action: #selector(suggestionTapped)))
            addSubview(l)
            suggestionLabels.append(l)
        }
        refreshSuggestions()
    }

    required init?(coder: NSCoder) { fatalError() }

    override var intrinsicContentSize: CGSize { CGSize(width: UIView.noIntrinsicMetric, height: frame.height) }

    override func traitCollectionDidChange(_ previous: UITraitCollection?) {
        super.traitCollectionDidChange(previous)
        setNeedsLayout()
    }

    // MARK: layout

    override func layoutSubviews() {
        super.layoutSubviews()
        guard bounds.width > 0 else { return }
        // Only rebuild when something visible changed. Adding the press preview triggers a layout pass, and
        // rebuilding then would drop the highlight on the key under the finger.
        let signature = "\(bounds.size) \(page) \(shift) \(dark)"
        guard signature != builtFor else { return }
        builtFor = signature
        dim?.removeFromSuperview()
        dim = nil
        if dark {
            // The system dark keyboard is near-opaque; the input view's own blur alone lets too much through.
            let d = UIView(frame: bounds)
            d.backgroundColor = UIColor(white: 0.125, alpha: 0.85)
            d.isUserInteractionEnabled = false
            insertSubview(d, at: 0)
            dim = d
        }
        if page == .emoji, emojiPanel == nil { _ = makeEmojiPanel() }   // the category bar reads its symbols
        keys = buildKeys()
        keyViews.forEach { $0.removeFromSuperview() }
        keyViews = keys.map(makeKeyView)
        keyViews.forEach(addSubview)
        let barY = rowTops[0] - 47 * u, barH = 42 * u, third = bounds.width / 3
        for (i, l) in suggestionLabels.enumerated() {
            l.frame = CGRect(x: CGFloat(i) * third, y: barY, width: third, height: barH)
            l.font = .systemFont(ofSize: 15.8 * u)
            l.textColor = ink.withAlphaComponent(0.72)
        }
        // Hairlines between the suggestions, as on the system bar.
        for (i, sep) in separators.enumerated() {
            let lineH = 22 * u
            sep.frame = CGRect(x: CGFloat(i + 1) * third - 0.5, y: barY + (barH - lineH) / 2, width: 1, height: lineH)
            sep.backgroundColor = ink.withAlphaComponent(dark ? 0.15 : 0.07)
        }
        let onEmoji = page == .emoji
        suggestionLabels.forEach { $0.isHidden = onEmoji }
        separators.forEach { $0.isHidden = onEmoji }
        if onEmoji {
            let panel = emojiPanel ?? makeEmojiPanel()
            panel.isHidden = false
            bringSubviewToFront(panel)
            panel.frame = CGRect(x: 0, y: barY, width: bounds.width, height: stripY - 4 * u - barY)
            let style = "\(panel.frame) \(dark)"
            if style != emojiPanelStyled {
                emojiPanelStyled = style
                panel.style(ink: ink, unit: u, margin: margin)
            }
            markCategory(panel.current)
        } else {
            emojiPanel?.isHidden = true
        }
        if let preview { bringSubviewToFront(preview) }
    }

    private var stripY: CGFloat { bounds.height - 60 * u }

    private func makeEmojiPanel() -> EmojiPanel {
        let panel = EmojiPanel(frame: .zero)
        panel.isUserInteractionEnabled = !inert
        panel.onPick = { [weak self] e in
            UIDevice.current.playInputClick()
            self?.textView?.insertText(e)
        }
        panel.onSection = { [weak self] i in self?.markCategory(i) }
        addSubview(panel)
        emojiPanel = panel
        return panel
    }

    /// The system's grey disc behind the category in view.
    private func markCategory(_ section: Int) {
        for (i, key) in keys.enumerated() where i < keyViews.count {
            if case .category(let j) = key.kind {
                keyViews[i].backgroundColor = j == section ? ink.withAlphaComponent(dark ? 0.2 : 0.1) : .clear
            }
        }
    }

    /// Keys drawn as bare glyphs in the bottom strip rather than as keycaps.
    private func isPlain(_ kind: Kind) -> Bool {
        switch kind {
        case .mic, .emoji, .category: return true
        case .delete, .page: return page == .emoji
        default: return false
        }
    }

    private func buildKeys() -> [Key] {
        var out: [Key] = []
        let w = bounds.width
        if page == .emoji {
            // Bottom strip: ABC where the emoji key sits, the categories across the middle, delete where the mic is.
            let strip = CGRect(x: 0, y: stripY, width: w, height: 40 * u)
            out.append(Key(kind: .page(.letters), frame: CGRect(x: 10 * u, y: strip.minY, width: 60 * u, height: strip.height)))
            out.append(Key(kind: .delete, frame: CGRect(x: w - 60.4 * u, y: strip.minY, width: 40 * u, height: strip.height)))
            let count = emojiPanel?.categories.count ?? 9
            let left = 76 * u, span = (w - 2 * left) / CGFloat(count), d = min(span, 32 * u)
            for i in 0..<count {
                let cx = left + span * (CGFloat(i) + 0.5)
                out.append(Key(kind: .category(i), frame: CGRect(x: cx - d / 2, y: strip.midY - d / 2, width: d, height: d)))
            }
            return out
        }
        func row(_ chars: [String], top: CGFloat, keyWidth: CGFloat, startX: CGFloat) {
            for (i, c) in chars.enumerated() {
                out.append(Key(kind: .char(c), frame: CGRect(x: startX + CGFloat(i) * (keyWidth + gap), y: top, width: keyWidth, height: keyH)))
            }
        }
        let rows: [[String]]
        switch page {
        case .letters: rows = ["qwertyuiop", "asdfghjkl", "zxcvbnm"].map { $0.map(String.init) }
        case .numbers: rows = ["1234567890", "-/:;()$&@\"", ".,?!'"].map { $0.map(String.init) }
        case .symbols, .emoji: rows = ["[]{}#%^*+=", "_\\|~<>€£¥•", ".,?!'"].map { $0.map(String.init) }
        }
        row(rows[0], top: rowTops[0], keyWidth: keyW, startX: margin)
        let row2Width = keyW
        let row2Start = margin + (w - 2 * margin - CGFloat(rows[1].count) * keyW - CGFloat(rows[1].count - 1) * gap) / 2
        row(rows[1], top: rowTops[1], keyWidth: row2Width, startX: row2Start)
        // Third row: a wide key each side (shift or the layer switch, and delete), the rest shared between.
        let leftKind: Kind = page == .letters ? .shift : .page(page == .numbers ? .symbols : .numbers)
        out.append(Key(kind: leftKind, frame: CGRect(x: margin, y: rowTops[2], width: wide, height: keyH)))
        out.append(Key(kind: .delete, frame: CGRect(x: w - margin - wide, y: rowTops[2], width: wide, height: keyH)))
        let inner = w - 2 * (margin + wide + afterWide)
        let n = CGFloat(rows[2].count)
        let midW = (inner - (n - 1) * gap) / n
        row(rows[2], top: rowTops[2], keyWidth: midW, startX: margin + wide + afterWide)
        // Bottom row: layer switch, space, return.
        out.append(Key(kind: .page(page == .letters ? .numbers : .letters), frame: CGRect(x: margin, y: rowTops[3], width: side, height: keyH)))
        out.append(Key(kind: .space, frame: CGRect(x: margin + side + bottomGap, y: rowTops[3], width: w - 2 * (margin + side + bottomGap), height: keyH)))
        out.append(Key(kind: .return, frame: CGRect(x: w - margin - side, y: rowTops[3], width: side, height: keyH)))
        // Strip: emoji bottom left, the app's mic bottom right.
        out.append(Key(kind: .emoji, frame: CGRect(x: 20.4 * u, y: stripY, width: 40 * u, height: 40 * u)))
        out.append(Key(kind: .mic, frame: CGRect(x: w - 60.4 * u, y: bounds.height - 60 * u, width: 40 * u, height: 40 * u)))
        return out
    }

    private func makeKeyView(_ key: Key) -> UIView {
        let v = UIView(frame: key.frame)
        // Touches must land on the keyboard itself, never a key view: key views get rebuilt, and a touch whose
        // view leaves the hierarchy never delivers its end, so the key wouldn't type and its preview would stick.
        v.isUserInteractionEnabled = false
        if isPlain(key.kind) {
            var symbol = "mic", size: CGFloat = 24, weight = UIImage.SymbolWeight.medium
            switch key.kind {
            case .emoji: symbol = "face.smiling"
            case .delete: symbol = "delete.left"; size = 20; weight = .regular
            case .category(let i): symbol = emojiPanel?.categories[i].symbol ?? "circle"; size = 16; weight = .regular
            case .page:
                let l = UILabel(frame: v.bounds)
                l.text = "ABC"
                l.textAlignment = .center
                l.textColor = ink
                l.font = .systemFont(ofSize: 17 * u)
                v.addSubview(l)
                return v
            default: break
            }
            let iv = UIImageView(image: UIImage(systemName: symbol, withConfiguration: UIImage.SymbolConfiguration(pointSize: size * u, weight: weight)))
            if case .category = key.kind { iv.tintColor = ink.withAlphaComponent(0.6) } else { iv.tintColor = ink }
            iv.contentMode = .center
            iv.frame = v.bounds
            v.addSubview(iv)
            v.layer.cornerRadius = v.bounds.height / 2
            return v
        }
        v.backgroundColor = keyFill
        v.layer.cornerRadius = 7 * u
        v.layer.cornerCurve = .continuous
        v.layer.shadowColor = UIColor.black.cgColor
        v.layer.shadowOpacity = dark ? 0.25 : 0
        v.layer.shadowOffset = CGSize(width: 0, height: 1)
        v.layer.shadowRadius = 0
        var title: String?, symbol: String?, size: CGFloat = 22.5
        switch key.kind {
        case .char(let c): title = shift == .off ? c : c.uppercased()
        case .shift: symbol = shift == .off ? "shift" : shift == .on ? "shift.fill" : "capslock.fill"; size = 18.5
        case .delete: symbol = "delete.left"; size = 18.5
        case .page(let l): title = l == .letters ? "ABC" : l == .numbers ? "123" : "#+="; size = 17
        case .emoji, .category: break
        case .space: title = nil
        case .return: symbol = "return"; size = 18.5
        case .mic: break
        }
        if let title {
            let l = UILabel(frame: v.bounds)
            l.text = title
            l.textAlignment = .center
            l.textColor = ink
            l.font = .systemFont(ofSize: size * u)
            v.addSubview(l)
        } else if let symbol {
            let iv = UIImageView(image: UIImage(systemName: symbol, withConfiguration: UIImage.SymbolConfiguration(pointSize: size * u, weight: .regular)))
            iv.tintColor = ink
            iv.contentMode = .center
            iv.frame = v.bounds
            v.addSubview(iv)
        }
        return v
    }

    // MARK: touches

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent?) {
        if inert { onInertTouch(); return }
        for touch in touches.sorted(by: { $0.timestamp < $1.timestamp }) {
            // Rollover: a new finger commits the key still held, so fast typing never drops a letter.
            endTouch(perform: true)
            guard let i = keyIndex(at: touch.location(in: self)) else { continue }
            pressed = i
            pressTouch = ObjectIdentifier(touch)
            highlight(i, true)
            if keys[i].kind != .mic { UIDevice.current.playInputClick() }
            if case .char(let c) = keys[i].kind { showPreview(over: keys[i].frame, text: shift == .off ? c : c.uppercased()) }
            if keys[i].kind == .delete {
                // Like the system: one character goes on touch-down, then it repeats, then whole words.
                deleteOnce()
                repeats = 0
                repeatTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: false) { [weak self] _ in
                    self?.repeatTimer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
                        guard let self else { return }
                        self.repeats += 1
                        if self.repeats > 20 { self.deleteWord() } else { self.deleteOnce() }
                    }
                }
            }
        }
    }

    override func touchesMoved(_ touches: Set<UITouch>, with event: UIEvent?) {
        guard !inert, let i = pressed, let touch = touches.first(where: { ObjectIdentifier($0) == pressTouch }) else { return }
        // Sliding onto another letter moves the press there, as on the system keyboard; delete stays put.
        guard keys[i].kind != .delete, let j = keyIndex(at: touch.location(in: self)), j != i else { return }
        highlight(i, false)
        preview?.removeFromSuperview()
        preview = nil
        pressed = j
        highlight(j, true)
        if case .char(let c) = keys[j].kind { showPreview(over: keys[j].frame, text: shift == .off ? c : c.uppercased()) }
    }

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent?) {
        // A finger already committed by rollover lifts to nothing.
        if touches.contains(where: { ObjectIdentifier($0) == pressTouch }) { endTouch(perform: true) }
    }

    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent?) {
        if touches.contains(where: { ObjectIdentifier($0) == pressTouch }) { endTouch(perform: false) }
    }

    private func keyIndex(at p: CGPoint) -> Int? {
        keys.firstIndex(where: { $0.frame.insetBy(dx: -gap / 2, dy: -gap / 2).contains(p) })
    }

    private func endTouch(perform: Bool) {
        repeatTimer?.invalidate()
        repeatTimer = nil
        preview?.removeFromSuperview()
        preview = nil
        pressTouch = nil
        guard let i = pressed else { return }
        pressed = nil
        highlight(i, false)
        // Delete already did its work on touch-down.
        if perform, keys[i].kind != .delete { act(keys[i].kind) }
    }

    private func deleteOnce() {
        textView?.deleteBackward()
    }

    /// Back to the start of the previous word, trailing spaces included.
    private func deleteWord() {
        let before = textBeforeCaret()
        let trailing = before.reversed().prefix(while: { $0.isWhitespace }).count
        let word = before.dropLast(trailing).reversed().prefix(while: { !$0.isWhitespace }).count
        for _ in 0..<max(1, trailing + word) { textView?.deleteBackward() }
    }

    private func highlight(_ i: Int, _ on: Bool) {
        guard i < keyViews.count, !isPlain(keys[i].kind) else { return }
        keyViews[i].backgroundColor = on ? (dark ? UIColor(white: 0.5, alpha: 1) : UIColor(white: 0.82, alpha: 1)) : keyFill
    }

    private func showPreview(over frame: CGRect, text: String) {
        let w = frame.width * 1.7, h = keyH * 1.45
        let v = UIView(frame: CGRect(x: frame.midX - w / 2, y: frame.maxY - h, width: w, height: h))
        v.isUserInteractionEnabled = false
        v.backgroundColor = keyFill
        v.layer.cornerRadius = 10 * u
        v.layer.cornerCurve = .continuous
        v.layer.shadowColor = UIColor.black.cgColor
        v.layer.shadowOpacity = 0.3
        v.layer.shadowOffset = CGSize(width: 0, height: 2)
        v.layer.shadowRadius = 4
        let l = UILabel(frame: CGRect(x: 0, y: 0, width: w, height: h * 0.62))
        l.text = text
        l.textAlignment = .center
        l.textColor = ink
        l.font = .systemFont(ofSize: 40 * u)
        v.addSubview(l)
        addSubview(v)
        preview = v
        // Safety net: no preview outlives a press, whatever UIKit does with the touch's end.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self, weak v] in
            if let v, self?.preview === v { v.removeFromSuperview(); self?.preview = nil }
        }
    }

    // MARK: actions

    private func act(_ kind: Kind) {
        guard let tv = textView else { return }
        switch kind {
        case .char(let c):
            tv.insertText(shift == .off ? c : c.uppercased())
            if shift == .on { shift = .off }
        case .shift:
            let now = Date()
            if shift != .locked, now.timeIntervalSince(lastShift) < 0.3 { shift = .locked }
            else { shift = shift == .off ? .on : .off }
            lastShift = now
            setNeedsLayout()
        case .delete:
            tv.deleteBackward()
        case .page(let l):
            page = l
            setNeedsLayout()
        case .space:
            let now = Date()
            let before = textBeforeCaret()
            if now.timeIntervalSince(lastSpace) < 0.35, before.hasSuffix(" "), before.count >= 2,
               before[before.index(before.endIndex, offsetBy: -2)].isLetter {
                tv.deleteBackward()
                tv.insertText(". ")
            } else {
                tv.insertText(" ")
            }
            lastSpace = now
            if page != .letters { page = .letters; setNeedsLayout() }
        case .return:
            tv.insertText("\n")
        case .mic:
            onMic()
        case .emoji:
            emojiPanel?.reloadRecents()
            page = .emoji
            setNeedsLayout()
        case .category(let i):
            emojiPanel?.jump(to: i)
        }
    }

    /// Standing in for the system keyboard: start from its default look, letters with shift as the text calls for.
    func prepareStandIn() {
        page = .letters
        textChanged()
    }

    /// After every edit: auto-capitalise at the start of a sentence, refresh the predictive bar.
    func textChanged() {
        if shift != .locked {
            let before = textBeforeCaret()
            let wantsCap = before.isEmpty || before.hasSuffix(". ") || before.hasSuffix("? ") || before.hasSuffix("! ") || before.hasSuffix("\n")
            let newShift: Shift = wantsCap ? .on : .off
            if newShift != shift { shift = newShift }
        }
        setNeedsLayout()
        // The spell-checker lookup waits for the run loop, so a burst of keys costs one lookup, not one each.
        guard !suggestionsPending else { return }
        suggestionsPending = true
        DispatchQueue.main.async { [weak self] in
            self?.suggestionsPending = false
            self?.refreshSuggestions()
        }
    }

    private func textBeforeCaret() -> String {
        guard let tv = textView else { return "" }
        let text = tv.text as NSString
        let loc = min(tv.selectedRange.location, text.length)
        return text.substring(to: loc)
    }

    private func currentWord() -> String {
        let before = textBeforeCaret()
        var word = ""
        for ch in before.reversed() {
            if ch.isLetter || ch == "'" || ch == "’" { word.insert(ch, at: word.startIndex) } else { break }
        }
        return word
    }

    private func refreshSuggestions() {
        let word = currentWord()
        if word.isEmpty {
            suggestions = ["I", "The", "I’m"]
        } else {
            let checker = UITextChecker()
            let completions = checker.completions(forPartialWordRange: NSRange(location: 0, length: word.utf16.count), in: word, language: "en_US") ?? []
            let capped = word.first?.isUppercase == true
            let extra = completions.filter { $0.lowercased() != word.lowercased() }.prefix(2).map { capped ? $0.prefix(1).uppercased() + $0.dropFirst() : $0 }
            suggestions = ["“\(word)”"] + extra
        }
        for (i, l) in suggestionLabels.enumerated() { l.text = i < suggestions.count ? suggestions[i] : "" }
    }

    @objc private func suggestionTapped(_ g: UITapGestureRecognizer) {
        if inert { onInertTouch(); return }
        guard let l = g.view as? UILabel, let i = suggestionLabels.firstIndex(of: l), i < suggestions.count, let tv = textView else { return }
        UIDevice.current.playInputClick()
        let word = currentWord()
        if i == 0 && !word.isEmpty {
            tv.insertText(" ")
        } else {
            for _ in 0..<word.count { tv.deleteBackward() }
            tv.insertText(suggestions[i] + " ")
        }
        textChanged()
    }
}
