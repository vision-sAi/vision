import SwiftUI
import UIKit

/// Growing message field backed by UITextView.  Return inserts a new line (the key reads "return"),
/// sending is the button's job.
struct ComposerField: UIViewRepresentable {
    @Binding var text: String
    @Binding var focused: Bool
    var placeholder: String
    /// The machine's guess at the next message, shown as the placeholder: tapping into the empty field puts it
    /// in, selected, so it can be changed or typed straight over.
    var suggestion = ""
    /// Speech preview is drawn in the field without altering the editable typed draft.
    var liveText = ""
    /// Live voice: no blinking caret, which sat on top of the speech preview and looked glitchy.
    var hidesCaret = false
    var disabled = false
    /// The attach card or a model/effort slider is open: the real keyboard stays on screen but dead, a press anywhere on it only runs
    /// `onInertKey` (like iMessage's and ChatGPT's "+"): no key pop, no typing, no page switch. The keys are drawn
    /// in-process, in UIKit's keyboard window, which sits in a scene of its own above every app window, so
    /// `KeyboardLid` goes inside that window, over the keys.
    var inert = false
    var onInertKey: () -> Void = {}
    /// A Genmoji picked on the keyboard, lifted out of the text: it's an image, so it travels as a photo.
    var onGenmoji: (UIImage) -> Void = { _ in }
    /// Pictures pasted from the clipboard (a screenshot, an image copied in Safari or Photos): they travel
    /// as photos too.
    var onPictures: ([UIImage]) -> Void = { _ in }
    /// `!` typed into the empty field (shell mode, ChatView): nil types it as usual.
    var onBang: (() -> Void)? = nil
    /// Backspace on the empty field (leaves shell mode).
    var onDeleteEmpty: () -> Void = {}
    /// Shell mode: a command is typed, so the keyboard leaves it as typed (no capital first letter, no
    /// autocorrect, straight quotes and dashes). `pwd` must not become `Pwd`.
    var plain = false
    var maxLines = 6
    @Environment(\.theme) private var theme

    /// Empty room on each side of the text, inside the view: a selection handle's grab area reaches past the
    /// letter it sits on, and with the text flush to the view's edge the start handle's lay mostly outside it,
    /// where a press never reached the text. The field's place in the composer takes it back (ChatView), so
    /// the text sits where it did.
    static let handleRoom: CGFloat = 14

    static func applyPlain(_ plain: Bool, to view: UITextView) {
        let caps: UITextAutocapitalizationType = plain ? .none : .sentences
        guard view.autocapitalizationType != caps else { return }
        view.autocapitalizationType = caps
        view.autocorrectionType = plain ? .no : .default
        view.spellCheckingType = plain ? .no : .default
        view.smartQuotesType = plain ? .no : .default
        view.smartDashesType = plain ? .no : .default
        view.smartInsertDeleteType = plain ? .no : .default
        if view.isFirstResponder { view.reloadInputViews() }   // the keyboard on screen takes the new traits now
    }

    func makeUIView(context: Context) -> UITextView {
        let view = PlaceholderTextView()
        view.delegate = context.coordinator
        view.font = theme.uiFont(size: 18)
        view.adjustsFontForContentSizeCategory = true
        view.backgroundColor = .clear
        view.textContainerInset = UIEdgeInsets(top: 0, left: Self.handleRoom, bottom: 0, right: Self.handleRoom)
        view.textContainer.lineFragmentPadding = 0
        view.returnKeyType = .default
        view.isScrollEnabled = false
        view.placeholder = placeholder
        view.liveText = liveText
        view.accessibilityIdentifier = "messageInput"
        // Genmoji on the keyboard, and no Writing Tools button in its bar (ChatGPT's keyboard has neither the
        // missing Genmoji nor the extra button).
        view.supportsAdaptiveImageGlyph = true
        view.writingToolsBehavior = .none
        view.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        view.setContentHuggingPriority(.required, for: .vertical)
        view.onPictures = { [weak coordinator = context.coordinator] in coordinator?.parent.onPictures($0) }
        return view
    }

    func updateUIView(_ view: UITextView, context: Context) {
        context.coordinator.parent = self
        let font = theme.uiFont(size: 18)
        if view.font != font { view.font = font }   // a theme picked in Settings restyles the draft too
        if view.text != text { view.text = text }
        (view as? PlaceholderTextView)?.placeholder = placeholder
        (view as? PlaceholderTextView)?.liveText = liveText
        (view as? PlaceholderTextView)?.hidesCaret = hidesCaret
        view.isEditable = !disabled
        Self.applyPlain(plain, to: view)
        Self.fitScrolling(view, maxLines: maxLines)
        if focused, !view.isFirstResponder, !disabled {
            DispatchQueue.main.async {
                guard context.coordinator.parent.focused, !context.coordinator.parent.disabled else { return }
                view.becomeFirstResponder()
            }
        } else if !focused, view.isFirstResponder {
            DispatchQueue.main.async {
                guard !context.coordinator.parent.focused else { return }
                view.resignFirstResponder()
            }
        }
        view.invalidateIntrinsicContentSize()
        context.coordinator.setLid(inert, over: view)
    }

    func sizeThatFits(_ proposal: ProposedViewSize, uiView view: UITextView, context: Context) -> CGSize? {
        let width = proposal.width ?? UIScreen.main.bounds.width
        let lineHeight = view.font?.lineHeight ?? 20
        let fitted = max(view.sizeThatFits(CGSize(width: width, height: .greatestFiniteMagnitude)).height,
                         (view as? PlaceholderTextView)?.displayHeight(for: width) ?? 0)
        let height = min(max(fitted, Self.oneLine(like: view)), ceil(lineHeight * CGFloat(maxLines)))
        return CGSize(width: width, height: height)
    }

    /// One laid-out line is a touch taller than `font.lineHeight`, which is all an empty field measures: floored on
    /// that, the composer jumped up a few points at the first key and back down when the draft was cleared.
    /// The floor is a real line instead, measured on a twin of the field, once per font and inset: SwiftUI asks the
    /// field's size several times a layout pass, and laying the twin out afresh every time stalled the drawer's snap.
    private static let measure = UITextView()
    private struct LineKey: Hashable {
        let font: UIFont?
        let top: CGFloat, left: CGFloat, bottom: CGFloat, right: CGFloat, padding: CGFloat
    }
    private static var lines: [LineKey: CGFloat] = [:]
    static func oneLine(like view: UITextView) -> CGFloat {
        let inset = view.textContainerInset
        let key = LineKey(font: view.font, top: inset.top, left: inset.left, bottom: inset.bottom, right: inset.right,
                          padding: view.textContainer.lineFragmentPadding)
        if let line = lines[key] { return line }
        measure.font = view.font
        measure.textContainerInset = inset
        measure.textContainer.lineFragmentPadding = key.padding
        measure.isScrollEnabled = false
        measure.text = "Wg"
        let line = ceil(measure.sizeThatFits(CGSize(width: 1000, height: CGFloat.greatestFiniteMagnitude)).height)
        lines[key] = line
        return line
    }

    /// Scrolling switches on once the text is taller than `maxLines`, judged on the text's own height: with scrolling
    /// off, UIKit pins `contentSize` to the frame, so a check on that never tripped and a long paste ran out of
    /// sight below the box. Past the cap the caret is kept in view, so the line being typed always shows.
    static func fitScrolling(_ view: UITextView, maxLines: Int) {
        let width = view.bounds.width
        guard width > 0 else { return }
        let maxHeight = ceil((view.font?.lineHeight ?? 20) * CGFloat(maxLines))
        let needed = view.sizeThatFits(CGSize(width: width, height: .greatestFiniteMagnitude)).height
        let scrolls = needed > maxHeight
        if view.isScrollEnabled != scrolls { view.isScrollEnabled = scrolls }
        guard scrolls, view.isFirstResponder else { return }
        DispatchQueue.main.async {
            view.layoutIfNeeded()
            view.scrollRangeToVisible(view.selectedRange)
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    final class Coordinator: NSObject, UITextViewDelegate {
        var parent: ComposerField
        /// Where the system keyboard ends up, in screen coordinates.
        private var keyboardFrame: CGRect = .zero
        private var lid: KeyboardLid?

        init(_ parent: ComposerField) {
            self.parent = parent
            super.init()
            NotificationCenter.default.addObserver(self, selector: #selector(keyboardWillChange(_:)),
                                                   name: UIResponder.keyboardWillChangeFrameNotification, object: nil)
        }

        @objc private func keyboardWillChange(_ note: Notification) {
            guard let frame = note.userInfo?[UIResponder.keyboardFrameEndUserInfoKey] as? CGRect else { return }
            keyboardFrame = frame
            if let lid, let window = lid.window { lid.frame = window.convert(frame, from: window.screen.coordinateSpace) }
        }

        /// Lays `KeyboardLid` over the live keys while the attach card is open, lifts it when the card closes.
        func setLid(_ on: Bool, over view: UITextView) {
            guard on, view.isFirstResponder, !keyboardFrame.isEmpty, let window = KeyboardWindow.current(),
                  keyboardFrame.minY < window.screen.bounds.maxY else {
                lid?.removeFromSuperview()
                lid = nil
                return
            }
            let lid = self.lid ?? KeyboardLid()
            lid.onTouch = { [weak self] in self?.parent.onInertKey() }
            lid.frame = window.convert(keyboardFrame, from: window.screen.coordinateSpace)
            // Over the keys but under the attach menu and cards when they live in this window (KeyboardLayer).
            if let layer = window.viewWithTag(KeyboardLayerTag) {
                window.insertSubview(lid, belowSubview: layer)
            } else {
                if lid.superview !== window { window.addSubview(lid) }
                window.bringSubviewToFront(lid)
            }
            self.lid = lid
        }

        func textViewDidChange(_ view: UITextView) {
            liftGenmoji(from: view)
            parent.text = view.text
            view.invalidateIntrinsicContentSize()
            ComposerField.fitScrolling(view, maxLines: parent.maxLines)
        }

        func textView(_ view: UITextView, shouldChangeTextIn range: NSRange, replacementText text: String) -> Bool {
            if parent.inert { parent.onInertKey(); return false }
            // `!` on an empty field, or over all of it (a suggestion tapped in, still selected), is shell mode.
            let whole = range.location == 0 && range.length == (view.text as NSString).length
            if text == "!", whole, let bang = parent.onBang {
                view.text = ""
                parent.text = ""
                bang()
                return false
            }
            if view.text.isEmpty, range.length == 0, text.isEmpty { parent.onDeleteEmpty() }
            return true
        }

        private func liftGenmoji(from view: UITextView) {
            let storage = view.textStorage
            var found: [(NSRange, NSAdaptiveImageGlyph)] = []
            storage.enumerateAttribute(.adaptiveImageGlyph, in: NSRange(location: 0, length: storage.length)) { value, range, _ in
                if let glyph = value as? NSAdaptiveImageGlyph { found.append((range, glyph)) }
            }
            guard !found.isEmpty else { return }
            let caret = view.selectedRange.location
            var removedBeforeCaret = 0
            for (range, glyph) in found.reversed() {
                storage.deleteCharacters(in: range)
                if range.location < caret { removedBeforeCaret += range.length }
                if let image = UIImage(data: glyph.imageContent) { parent.onGenmoji(image) }
            }
            view.selectedRange = NSRange(location: max(0, caret - removedBeforeCaret), length: 0)
        }

        func textViewDidBeginEditing(_ view: UITextView) {
            parent.focused = true
            let guess = parent.suggestion
            guard !guess.isEmpty, view.text.isEmpty else { return }
            view.text = guess
            parent.text = guess
            DispatchQueue.main.async { view.selectedRange = NSRange(location: 0, length: (guess as NSString).length) }
        }
        func textViewDidEndEditing(_ view: UITextView) { parent.focused = false }
    }
}

/// Sits over the live keys, inside the keyboard's own window, while the attach card is open: a touch lands here
/// instead of on a key, so nothing types, pops or switches page, and it only closes the card. The faint white
/// (ChatGPT's keyboard is exactly this much lighter under its menu) keeps the system from treating it as empty glass.
private final class KeyboardLid: UIView {
    var onTouch: () -> Void = {}

    init() {
        super.init(frame: .zero)
        backgroundColor = UIColor(white: 1, alpha: 0.004)
        isMultipleTouchEnabled = true
    }

    required init?(coder: NSCoder) { fatalError() }

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent?) { onTouch() }
    override func touchesMoved(_ touches: Set<UITouch>, with event: UIEvent?) {}
    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent?) {}
    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent?) {}
}

/// UITextView with a placeholder label, since UIKit still doesn't ship one.
private final class PlaceholderTextView: UITextView {
    private let label = UILabel()

    var placeholder: String = "" {
        didSet { refresh() }
    }

    var liveText: String = "" {
        didSet { refresh(); invalidateIntrinsicContentSize() }
    }

    var hidesCaret = false {
        didSet {
            guard hidesCaret != oldValue else { return }
            // The clear tint is what reliably hides it on newer iOS; the caret rect covers older drawing paths.
            tintColor = hidesCaret ? .clear : nil
            selectedTextRange = selectedTextRange
        }
    }

    override func caretRect(for position: UITextPosition) -> CGRect {
        hidesCaret ? .zero : super.caretRect(for: position)
    }

    var onPictures: ([UIImage]) -> Void = { _ in }

    /// A text view only offers Paste for text: a picture on the clipboard is one to attach.
    override func canPerformAction(_ action: Selector, withSender sender: Any?) -> Bool {
        if action == #selector(paste(_:)), isEditable, UIPasteboard.general.hasImages { return true }
        return super.canPerformAction(action, withSender: sender)
    }

    /// A picture wins over whatever came along with it (Safari copies an image with its address).
    override func paste(_ sender: Any?) {
        if UIPasteboard.general.hasImages, let pictures = UIPasteboard.general.images, !pictures.isEmpty {
            onPictures(pictures)
            return
        }
        super.paste(sender)
    }

    override init(frame: CGRect, textContainer: NSTextContainer?) {
        super.init(frame: frame, textContainer: textContainer)
        label.textColor = .placeholderText
        label.font = font
        label.numberOfLines = 6
        label.lineBreakMode = .byTruncatingHead
        label.translatesAutoresizingMaskIntoConstraints = false
        addSubview(label)
        // The frame guide, not the view's own anchors: on a scroll view those pin to the content, which gives
        // the label no width, so a long speech preview ran off the edge on one line instead of wrapping.
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: frameLayoutGuide.leadingAnchor, constant: ComposerField.handleRoom),
            label.topAnchor.constraint(equalTo: frameLayoutGuide.topAnchor),
            label.trailingAnchor.constraint(equalTo: frameLayoutGuide.trailingAnchor, constant: -ComposerField.handleRoom),
        ])
        NotificationCenter.default.addObserver(self, selector: #selector(refresh), name: UITextView.textDidChangeNotification, object: self)
    }

    required init?(coder: NSCoder) { fatalError() }

    override var text: String! {
        didSet { refresh() }
    }

    override var font: UIFont? {
        didSet { label.font = font }
    }

    @objc private func refresh() {
        label.isHidden = !(text ?? "").isEmpty
        label.text = liveText.isEmpty ? placeholder : liveText
        label.textColor = liveText.isEmpty ? .placeholderText : .label
    }

    func displayHeight(for width: CGFloat) -> CGFloat {
        guard !label.isHidden, !(label.text ?? "").isEmpty else { return 0 }  // a suggestion can wrap too
        return label.sizeThatFits(CGSize(width: width - 2 * ComposerField.handleRoom, height: .greatestFiniteMagnitude)).height
    }
}
