import SwiftUI

/// The phone-side AskUserQuestion form, a bottom card like the Claude and ChatGPT apps: one question per page,
/// options as big tappable rows, "Something else" opens free text in place. A single-select pick moves straight on.
struct QuestionSheet: View {
    let form: QuestionForm
    let onAnswer: ([String: String]?) -> Void

    @State private var page = 0
    @State private var forward = true
    @State private var picks: [String: Set<String>] = [:]
    @State private var other: [String: String] = [:]
    @State private var writing: Set<String> = []        // questions whose "Something else" row is open
    @State private var height: CGFloat = 380
    @State private var listHeight: CGFloat = 0
    @FocusState private var otherFocused: Bool

    private var q: Question { form.questions[page] }
    private var last: Bool { page == form.questions.count - 1 }
    private var maxList: CGFloat { UIScreen.main.bounds.height * 0.5 }

    /// A question can carry a body after a blank line: a worker's plan arrives as "Carry out this
    /// plan?\n\n<plan>". The first part is the title; the rest is drawn as markdown above the options.
    private var title: String { String(q.question.components(separatedBy: "\n\n")[0]) }
    private var detail: String? {
        guard let gap = q.question.range(of: "\n\n") else { return nil }
        let rest = q.question[gap.upperBound...].trimmingCharacters(in: .whitespacesAndNewlines)
        return rest.isEmpty ? nil : rest
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            top
            VStack(alignment: .leading, spacing: 14) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(title)
                        .themeFont(.title3, weight: .semibold)
                        .fixedSize(horizontal: false, vertical: true)
                    if q.multiSelect == true {
                        Text("Pick any that apply").themeFont(.subheadline).foregroundStyle(.secondary)
                    }
                }
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        if let detail {
                            ReplyMarkdown(text: detail)
                                .themeFont(.body)
                                .lineSpacing(4)
                                .textSelection(.enabled)
                        }
                        rows
                    }
                    .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { listHeight = $0 }
                }
                .frame(height: min(listHeight, maxList))
                .scrollBounceBehavior(.basedOnSize)
                .scrollClipDisabled(listHeight <= maxList)
            }
            .id(page)
            .transition(.asymmetric(
                insertion: .move(edge: forward ? .trailing : .leading).combined(with: .opacity),
                removal: .move(edge: forward ? .leading : .trailing).combined(with: .opacity)))
            bottom
        }
        .padding(.horizontal, 20)
        .padding(.top, 22)
        .padding(.bottom, 8)
        .fixedSize(horizontal: false, vertical: true)
        .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { height = $0 }
        .frame(maxHeight: .infinity, alignment: .top)
        .clipped()
        .presentationDetents([.height(height)])
        .presentationDragIndicator(.hidden)
        .presentationBackground(Color(.secondarySystemBackground))   // solid: the glass let the chat wash it out
        .sensoryFeedback(.selection, trigger: picks)
        .sensoryFeedback(.impact(weight: .light), trigger: page)
    }

    // MARK: Pieces

    private var top: some View {
        HStack(spacing: 10) {
            if let h = q.header, !h.isEmpty {
                Text(h)
                    .themeFont(.footnote, weight: .semibold)
                    .foregroundStyle(.tint)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 5)
                    .background(Capsule().fill(Color.accentColor.opacity(0.14)))
                    .contentTransition(.opacity)
            } else {
                Label("Vision asks", systemImage: "sparkles")
                    .themeFont(.footnote, weight: .semibold)
                    .foregroundStyle(.secondary)
            }
            if form.questions.count > 1 {
                HStack(spacing: 5) {
                    ForEach(form.questions.indices, id: \.self) { i in
                        Capsule()
                            .fill(i == page ? Color.primary : Color.primary.opacity(i < page ? 0.45 : 0.18))
                            .frame(width: i == page ? 16 : 6, height: 6)
                    }
                }
                .accessibilityElement()
                .accessibilityLabel("Question \(page + 1) of \(form.questions.count)")
            }
            Spacer(minLength: 0)
            Button { onAnswer(nil) } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(.secondary)
                    .frame(width: 32, height: 32)
                    .background(Circle().fill(Color.primary.opacity(0.08)))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Skip")
        }
    }

    private var rows: some View {
        VStack(spacing: 8) {
            ForEach(Array(q.options.enumerated()), id: \.element.id) { i, option in
                Button { pick(option.label) } label: {
                    row(selected: selected(option.label)) {
                        badge(i + 1, selected: selected(option.label))
                    } content: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(option.label).themeFont(.body, weight: .medium).foregroundStyle(.primary)
                            if let d = option.description, !d.isEmpty {
                                Text(d).themeFont(.footnote).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
                .buttonStyle(Pressable())
            }
            somethingElse
        }
    }

    @ViewBuilder private var somethingElse: some View {
        let open = writing.contains(q.id)
        let typed = !(other[q.id] ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        if open {
            row(selected: typed) {
                icon("pencil", selected: typed)
            } content: {
                TextField("Something else…", text: binding(for: q.id), axis: .vertical)
                    .themeFont(.body)
                    .lineLimit(1...4)
                    .focused($otherFocused)
                    .submitLabel(last ? .done : .next)
            }
        } else {
            Button {
                withAnimation(.snappy) { _ = writing.insert(q.id) }
                if q.multiSelect != true { picks[q.id] = [] }
                DispatchQueue.main.async { otherFocused = true }
            } label: {
                row(selected: false) {
                    icon("pencil", selected: false)
                } content: {
                    Text("Something else").themeFont(.body, weight: .medium).foregroundStyle(.secondary)
                }
            }
            .buttonStyle(Pressable())
        }
    }

    private var bottom: some View {
        HStack(spacing: 10) {
            if page > 0 {
                Button { go(to: page - 1) } label: {
                    Image(systemName: "chevron.left")
                        .font(.system(size: 17, weight: .semibold))
                        .foregroundStyle(.primary)
                        .frame(width: 50, height: 50)
                        .background(Circle().fill(Color.primary.opacity(0.08)))
                }
                .buttonStyle(Pressable())
                .accessibilityLabel("Back")
                .transition(.scale.combined(with: .opacity))
            }
            Button {
                // Send from the last page, or first go back to a question skipped on the way.
                if !last { advance() }
                else if let gap = form.questions.firstIndex(where: { !complete($0) }) { go(to: gap) }
                else { onAnswer(answers) }
            } label: {
                Text(last ? "Send" : "Next")
                    .themeFont(.body, weight: .semibold)
                    .frame(maxWidth: .infinity)
                    .frame(height: 50)
            }
            .buttonStyle(PrimaryPill())
            .disabled(!complete(q))
        }
        .animation(.snappy, value: page)
    }

    private func row<Lead: View, Content: View>(selected: Bool, @ViewBuilder lead: () -> Lead,
                                                @ViewBuilder content: () -> Content) -> some View {
        let shape = RoundedRectangle(cornerRadius: 18, style: .continuous)
        return HStack(alignment: .center, spacing: 12) {
            lead()
            content()
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 13)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(shape.fill(selected ? Color.accentColor.opacity(0.14) : Color.primary.opacity(0.06)))
        .overlay(shape.strokeBorder(selected ? Color.accentColor : .clear, lineWidth: 1.5))
        .contentShape(shape)
        .animation(.snappy(duration: 0.2), value: selected)
    }

    /// Numbered circle for single-select, a check box for multi-select; filled with a tick once picked.
    private func badge(_ n: Int, selected: Bool) -> some View {
        let multi = q.multiSelect == true
        return ZStack {
            if multi {
                RoundedRectangle(cornerRadius: 7, style: .continuous)
                    .fill(selected ? Color.accentColor : Color.primary.opacity(0.08))
            } else {
                Circle().fill(selected ? Color.accentColor : Color.primary.opacity(0.08))
            }
            if selected {
                Image(systemName: "checkmark").font(.system(size: 12, weight: .bold)).foregroundStyle(.white)
                    .transition(.scale.combined(with: .opacity))
            } else if !multi {
                Text("\(n)").themeFont(.footnote, weight: .semibold).foregroundStyle(.secondary)
            }
        }
        .frame(width: 28, height: 28)
    }

    private func icon(_ name: String, selected: Bool) -> some View {
        Image(systemName: name)
            .font(.system(size: 12, weight: .semibold))
            .foregroundStyle(selected ? Color.white : Color.secondary)
            .frame(width: 28, height: 28)
            .background(Circle().fill(selected ? Color.accentColor : Color.primary.opacity(0.08)))
    }

    // MARK: State

    private func binding(for id: String) -> Binding<String> {
        Binding(get: { other[id] ?? "" }, set: { text in
            // A vertical field takes return as a newline and never fires onSubmit, so treat it as submit.
            let returned = text.contains("\n")
            let text = text.replacingOccurrences(of: "\n", with: "")
            other[id] = text
            // Single-select: typing your own answer replaces a picked option.
            if q.multiSelect != true, !text.isEmpty { picks[id] = [] }
            if returned { if complete(q) { advance() } else { otherFocused = false } }
        })
    }

    private func selected(_ label: String) -> Bool { picks[q.id, default: []].contains(label) }

    private func pick(_ label: String) {
        var set = picks[q.id, default: []]
        if q.multiSelect == true {
            if set.contains(label) { set.remove(label) } else { set.insert(label) }
            picks[q.id] = set
            return
        }
        set = set.contains(label) ? [] : [label]
        picks[q.id] = set
        // Picking an option drops the free-text answer, and moves on like the Claude app does.
        other[q.id] = nil
        otherFocused = false
        withAnimation(.snappy) { _ = writing.remove(q.id) }
        guard !set.isEmpty, !last else { return }
        let at = page
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
            if page == at, selected(label) { advance() }
        }
    }

    private func advance() {
        if last { otherFocused = false } else { go(to: page + 1) }
    }

    private func go(to target: Int) {
        otherFocused = false
        forward = target > page
        withAnimation(.snappy(duration: 0.32)) { page = target }
    }

    private func answer(for q: Question) -> String {
        // Keep the machine's option order; free text goes last, matching the terminal form's "Other".
        var parts = q.options.map(\.label).filter { picks[q.id, default: []].contains($0) }
        let free = (other[q.id] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if !free.isEmpty { parts.append(free) }
        return parts.joined(separator: ", ")
    }

    private func complete(_ q: Question) -> Bool { !answer(for: q).isEmpty }

    private var answers: [String: String] {
        Dictionary(uniqueKeysWithValues: form.questions.map { ($0.question, answer(for: $0)) })
    }
}

/// Rows and buttons sink a touch and dim while held, and fade when disabled.
private struct Pressable: ButtonStyle {
    @Environment(\.isEnabled) private var enabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .opacity(enabled ? 1 : 0.3)
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .opacity(configuration.isPressed ? 0.85 : 1)
            .animation(.snappy(duration: 0.15), value: configuration.isPressed)
    }
}

/// The Next / Send pill: solid ink when it can go, a quiet grey capsule until the question has an answer.
private struct PrimaryPill: ButtonStyle {
    @Environment(\.isEnabled) private var enabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(enabled ? Color(.systemBackground) : Color.secondary)
            .background(Capsule().fill(enabled ? Color.primary : Color.primary.opacity(0.08)))
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .opacity(configuration.isPressed ? 0.85 : 1)
            .animation(.snappy(duration: 0.2), value: enabled)
            .animation(.snappy(duration: 0.15), value: configuration.isPressed)
    }
}
