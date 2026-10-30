import SwiftUI

/// Bottom sheet (a Next Up widget's tap): what's set to come back to a chat later, reminders and prompts set
/// from that chat ("in an hour, check the deploy"). A timed job of its own is a scout, on the scout's page.
/// Toggle one off to pause it, swipe to delete, tap to edit.
struct ScheduledView: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.dismiss) private var dismiss
    @State private var tasks: [ScheduledTask]?
    @State private var error: String?
    @State private var editing: ScheduledTask?

    var body: some View {
        NavigationStack {
            content
                .navigationTitle("Coming up")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button { dismiss() } label: { Image(systemName: "xmark") }
                            .accessibilityLabel("Close")
                    }
                }
        }
        .task { await load() }
        .onChange(of: client.scheduledVersion) { _, _ in Task { await load() } }
        .sheet(item: $editing) { task in
            ScheduledEditor(task: task) { saved in
                Task { await save(saved) }
            } onDelete: {
                Task { await delete(task) }
            } onRun: {
                Task {
                    do { try await client.runScheduled(task.id); dismiss() } catch { self.error = error.localizedDescription }
                }
            }
            .presentationDetents([.large])
        }
    }

    @ViewBuilder private var content: some View {
        if let tasks, tasks.isEmpty {
            ContentUnavailableView {
                Label("Nothing coming up", systemImage: "clock")
            } description: {
                Text("Ask in a chat (\"remind me at six\", \"in an hour, check the deploy\") and it shows here. For a job that runs every day or week, send a scout.")
            }
        } else if tasks == nil, let error {
            ContentUnavailableView {
                Label("Couldn't load scheduled tasks", systemImage: "wifi.exclamationmark")
            } description: {
                Text(error)
            } actions: {
                Button("Try again") { Task { await load() } }.buttonStyle(.bordered)
            }
        } else if let tasks {
            List {
                Section {
                    ForEach(tasks) { task in
                        row(task)
                            .contentShape(Rectangle())
                            .onTapGesture { editing = task }
                            .swipeActions(edge: .trailing) {
                                Button(role: .destructive) { Task { await delete(task) } } label: { Label("Delete", systemImage: "trash") }
                            }
                    }
                } footer: {
                    Text("Runs while `vision serve` is on, by the clock of the computer it runs on.")
                }
            }
            .listStyle(.insetGrouped)
            .refreshable { await load() }
        } else {
            ProgressView()
        }
    }

    private func row(_ task: ScheduledTask) -> some View {
        HStack(spacing: 14) {
            Image(systemName: task.isReminder ? "bell" : "clock")
                .font(.system(size: 17, weight: .medium))
                .foregroundStyle(task.enabled ? Color.primary : Color.secondary)
                .frame(width: 36, height: 36)
                .background(Circle().fill(Color(.secondarySystemFill)))
            VStack(alignment: .leading, spacing: 3) {
                Text(task.title.isEmpty ? task.prompt : task.title)
                    .themeFont(.body, weight: .medium)
                    .lineLimit(1)
                Text(task.isReminder ? "Reminder · \(task.summary)" : task.summary).themeFont(.footnote).foregroundStyle(.secondary)
                if task.enabled, let next = task.nextRun {
                    Text("Next \(Date(timeIntervalSince1970: next).formatted(.relative(presentation: .named)))")
                        .themeFont(.footnote).foregroundStyle(.tertiary)
                }
            }
            Spacer(minLength: 8)
            Toggle("", isOn: Binding(get: { task.enabled }, set: { on in
                var t = task
                t.enabled = on
                Task { await save(t) }
            }))
            .labelsHidden()
        }
        .padding(.vertical, 4)
    }

    private func load() async {
        do {
            let fresh = try await client.fetchScheduled()
            withAnimation(.snappy(duration: 0.25)) { tasks = fresh.filter { !$0.isScoutReport } }  // a scout keeps its own
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func save(_ task: ScheduledTask) async {
        do {
            _ = try await client.saveScheduled(task)
            await load()
        } catch {
            client.toast = error.localizedDescription
        }
    }

    private func delete(_ task: ScheduledTask) async {
        do {
            try await client.deleteScheduled(task.id)
            await load()
        } catch {
            client.toast = error.localizedDescription
        }
    }
}

/// Add or edit one task: what to ask, and when.
private struct ScheduledEditor: View {
    @Environment(\.dismiss) private var dismiss
    @State var task: ScheduledTask
    let onSave: (ScheduledTask) -> Void
    let onDelete: () -> Void
    let onRun: () -> Void
    @State private var when = Date()
    @FocusState private var promptFocused: Bool

    private var isNew: Bool { task.id.isEmpty }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Name (optional)", text: $task.title)
                    TextField("What should Vision do?", text: $task.prompt, axis: .vertical)
                        .lineLimit(4...10)
                        .focused($promptFocused)
                } footer: {
                    Text(task.isReminder ? "Sent back to you as it is: a notification and a note in its chat."
                         : "Sent as a message in the chat it was set from.")
                }
                Section {
                    Picker("Repeat", selection: $task.repeatRule) {
                        Text("Once").tag("once")
                        Text("Every day").tag("daily")
                        Text("Weekdays").tag("weekdays")
                        Text("Every week").tag("weekly")
                    }
                    if task.repeatRule == "once" {
                        DatePicker("When", selection: $when, in: Date()..., displayedComponents: [.date, .hourAndMinute])
                    } else {
                        DatePicker("Time", selection: $when, displayedComponents: .hourAndMinute)
                    }
                    if task.repeatRule == "weekly" {
                        Picker("Day", selection: Binding(get: { task.weekday ?? 0 }, set: { task.weekday = $0 })) {
                            ForEach(0..<7, id: \.self) { i in
                                Text(Calendar.current.standaloneWeekdaySymbols[(i + 1) % 7]).tag(i)
                            }
                        }
                    }
                }
                if !isNew {
                    Section {
                        Button { onRun(); dismiss() } label: { Label("Run now", systemImage: "play") }
                        Button(role: .destructive) { onDelete(); dismiss() } label: { Label("Delete", systemImage: "trash") }
                    }
                }
            }
            .navigationTitle(isNew ? "New task" : "Edit task")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        var t = task
                        apply(when, to: &t)
                        t.enabled = true
                        onSave(t)
                        dismiss()
                    }
                    .fontWeight(.semibold)
                    .disabled(task.prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
            .onAppear {
                when = initialDate()
                if isNew { promptFocused = true }
            }
        }
    }

    private func initialDate() -> Date {
        let cal = Calendar.current
        let parts = task.time.split(separator: ":").compactMap { Int($0) }
        var day = Date()
        if let date = task.date, let d = ScheduledTask.dayFormat.date(from: date) { day = d }
        let d = cal.date(bySettingHour: parts.first ?? 9, minute: parts.count > 1 ? parts[1] : 0, second: 0, of: day) ?? Date()
        if isNew && task.repeatRule == "once" && d < Date() { return cal.date(byAdding: .day, value: 1, to: d) ?? d }
        return d
    }

    private func apply(_ date: Date, to t: inout ScheduledTask) {
        let c = Calendar.current.dateComponents([.hour, .minute], from: date)
        t.time = String(format: "%02d:%02d", c.hour ?? 9, c.minute ?? 0)
        t.date = t.repeatRule == "once" ? ScheduledTask.dayFormat.string(from: date) : nil
        if t.repeatRule == "weekly" && t.weekday == nil { t.weekday = 0 }
    }
}
