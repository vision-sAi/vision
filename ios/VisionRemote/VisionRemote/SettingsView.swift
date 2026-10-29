import AVFoundation
import SwiftUI

/// Settings, written for someone who has never seen Vision: Pip and the connection up top, then
/// grouped rows in the style of the system Settings app, each saying what it does in plain words.
/// Nothing here assumes whose machine Vision runs on or what kind it is.
struct SettingsView: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.dismiss) private var dismiss
    @State private var serverVersion: String?
    @State private var serverStatus: ServerStatus?
    @State private var defaults: ModelCatalogue?
    @State private var voiceRate: Double?
    @State private var voices: VoiceCatalogue?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    SettingsHero(client: client)
                }
                .listRowBackground(Color.clear)
                .listRowInsets(EdgeInsets())

                connectionSection

                if client.connection == .online, let drift = client.staleness {
                    Section {
                        Label {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(drift.title)
                                Text(.init(drift.detail))   // as markdown, so the command reads as one
                                    .themeFont(.footnote)
                                    .foregroundStyle(.secondary)
                            }
                        } icon: {
                            Image(systemName: drift.breaks ? "exclamationmark.triangle" : "arrow.down.circle")
                                .foregroundStyle(drift.breaks ? Color.orange : Color.blue)
                        }
                    }
                }

                if client.connection == .online {
                    Section {
                        NavigationLink {
                            MemoryView()
                        } label: {
                            SettingsLabel("Memory", systemImage: "brain", tint: .pink)
                        }
                    }
                }

                // The server's speech rows with the defaults for new chats under them, in one group.
                if client.connection == .online, showsServer || defaults != nil || client.canSwitchVoice {
                    Section {
                        if client.canSwitchVoice {
                            VoiceSwitchRow(status: $serverStatus)
                        }
                        if showsServer {
                            ServerRows(status: serverStatus, rate: $voiceRate, voices: $voices)
                        }
                        if defaults != nil {
                            DefaultsRows(catalogue: $defaults)
                        }
                    }
                }

                Section {
                    Text(versionLine)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity)
                }
                .listRowBackground(Color.clear)
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
            .task(id: client.connection == .online) {
                guard client.connection == .online else { return }
                let health = try? await client.health()
                serverVersion = health?.version
                serverStatus = health?.status
                defaults = try? await client.fetchDefaults()
                voiceRate = try? await client.fetchVoiceRate()
                voices = try? await client.fetchVoices()
                // The voice and the ears load in the background after `vision serve` starts: follow them
                // until they settle (a couple of minutes at most), while Settings is open.
                for _ in 0..<40 where serverStatus?.isLoading == true {
                    try? await Task.sleep(for: .seconds(3))
                    guard !Task.isCancelled, let fresh = try? await client.health() else { break }
                    serverStatus = fresh.status
                }
            }
            .onChange(of: client.currentConnection?.id) {
                serverVersion = nil
                serverStatus = nil
                defaults = nil
                voiceRate = nil
                voices = nil
            }
        }
    }

    private var showsServer: Bool {
        voiceRate != nil || serverStatus.map({ !$0.rows.isEmpty || !$0.problems.isEmpty }) == true
    }

    /// The quiet line at the bottom: "App 1.4 · Vision 0.9", the server half only once it answers.
    private var versionLine: String {
        let app = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "–"
        return ["App \(app)", serverVersion.map { "Vision \($0)" }].compactMap { $0 }.joined(separator: " · ")
    }

    // MARK: connection

    /// One row naming the Vision in use, or saying none is; pairing, switching and removing live behind it.
    private var connectionSection: some View {
        Section {
            NavigationLink {
                ConnectionsView()
            } label: {
                LabeledContent {
                    StatusLight(client: client)
                } label: {
                    SettingsLabel(client.isConfigured
                                  ? client.currentConnection?.name ?? client.baseURL?.host() ?? client.serverURL
                                  : "Not connected",
                                  systemImage: "network", tint: .blue)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
            }
        }
    }

    /// Ask the Vision in use how it is, in words for the result row.
    static func check(_ client: VisionClient) async -> (message: String, passed: Bool, version: String?) {
        do {
            let h = try await client.health()
            let chats = h.chats.count
            return ("Connected to Vision \(h.version)"
                    + (chats == 0 ? "." : ", \(chats) chat\(chats == 1 ? "" : "s") open."), true, h.version)
        } catch {
            return (explain(error), false, nil)
        }
    }

    /// Turns a failed connection into what to actually do about it.
    static func explain(_ error: Error) -> String {
        if case ClientError.http(401, _) = error {
            return "The access key wasn't accepted. Run `vision serve` again and rescan the code."
        }
        if case ClientError.http(404, _) = error {
            return "That Vision is older than this app. Update it, then restart `vision serve`."
        }
        guard let error = error as? URLError else { return error.localizedDescription }
        switch error.code {
        case .notConnectedToInternet:
            return "This phone is offline. Check Wi-Fi or mobile data."
        case .cannotFindHost, .dnsLookupFailed:
            return "Couldn't find that address. Check it's typed right, and if it's a Tailscale name, that Tailscale is on here too."
        case .cannotConnectToHost, .timedOut, .networkConnectionLost:
            return "Nothing answered there. Make sure the computer is awake, `vision serve` is running, and the phone can reach it."
        case .secureConnectionFailed, .serverCertificateUntrusted, .serverCertificateHasBadDate, .serverCertificateNotYetValid:
            return "The secure connection failed. Use the exact address `vision serve` prints."
        case .appTransportSecurityRequiresSecureConnection:
            return "That address needs to start with https://."
        case .badURL, .unsupportedURL:
            return "That doesn't look like a web address. It should look like https://vision.example.com."
        default:
            return error.localizedDescription
        }
    }
}

/// Every Vision this phone has paired with, in one place: tap one to switch, add another, or unpair.
private struct ConnectionsView: View {
    @EnvironmentObject private var client: VisionClient
    @State private var showScanner = false
    @State private var removing: SavedConnection?
    @State private var renaming: SavedConnection?
    @State private var newName = ""
    @State private var result: (message: String, passed: Bool)?

    var body: some View {
        Form {
            if !client.savedConnections.isEmpty {
                Section {
                    ForEach(client.savedConnections) { saved in
                        row(saved)
                            .contextMenu {
                                Button {
                                    newName = saved.name
                                    renaming = saved
                                } label: {
                                    Label("Rename", systemImage: "pencil")
                                }
                                Button(role: .destructive) {
                                    // Wait out the menu's fade so the alert doesn't land on top of it.
                                    Task {
                                        try? await Task.sleep(for: .milliseconds(400))
                                        removing = saved
                                    }
                                } label: {
                                    Label("Remove", systemImage: "trash")
                                }
                            }
                    }
                } header: {
                    Text("Saved")
                }
            }

            Section {
                if PairingScanner.isSupported {
                    Button {
                        showScanner = true
                    } label: {
                        SettingsLabel("Scan pairing code", systemImage: "qrcode.viewfinder", tint: .indigo)
                    }
                    .foregroundStyle(.primary)
                }
                NavigationLink {
                    ConnectionEntry(title: "Add Vision", url: "", token: "")
                } label: {
                    SettingsLabel("Enter address manually", systemImage: "keyboard", tint: .gray)
                }
                if let result, !result.passed { ResultRow(text: result.message, passed: false) }
            } header: {
                Text(client.savedConnections.isEmpty ? "Add a Vision" : "Add another")
            }
        }
        .navigationTitle("Addresses")
        .navigationBarTitleDisplayMode(.inline)
        .sheet(isPresented: $showScanner) {
            PairingSheet(isPresented: $showScanner) { payload in
                guard client.pair(with: payload) else {
                    result = (PairingSheet.notACode, false)
                    return
                }
                result = nil
            }
        }
        .alert("Remove \(removing?.name ?? "this Vision")?",
               isPresented: Binding(get: { removing != nil }, set: { if !$0 { removing = nil } }),
               presenting: removing) { saved in
            Button("Cancel", role: .cancel) {}
            Button("Remove", role: .destructive) {
                client.remove(saved)
                result = nil
            }
        } message: { saved in
            Text(saved.id == client.currentConnection?.id
                 ? "You'll be disconnected. Your chats stay safe on the computer running Vision."
                 : "Your chats stay safe on the computer running Vision.")
        }
        .alert("Rename", isPresented: Binding(get: { renaming != nil }, set: { if !$0 { renaming = nil } })) {
            TextField("Name", text: $newName)
            Button("Cancel", role: .cancel) {}
            Button("Save") {
                if let renaming { client.rename(renaming, to: newName) }
            }
        }
    }

    /// The selected one just says so; any other switches to it on a tap.
    @ViewBuilder
    private func row(_ saved: SavedConnection) -> some View {
        let host = URL(string: saved.url)?.host() ?? saved.url
        if saved.id == client.currentConnection?.id {
            LabeledContent {
                Text("Selected")
            } label: {
                AddressLabel(name: saved.name, host: host, light: client)
            }
        } else {
            Button {
                result = nil
                client.switchTo(saved)
            } label: {
                AddressLabel(name: saved.name, host: host)
            }
            .foregroundStyle(.primary)
        }
    }
}

/// The camera, full screen in its own sheet, reading a `vision serve` pairing code.
struct PairingSheet: View {
    static let notACode = "That isn't a Vision pairing code. Scan the one `vision serve` shows."

    @Binding var isPresented: Bool
    let onScan: (String) -> Void

    var body: some View {
        PairingScanner { payload in
            isPresented = false
            onScan(payload)
        }
        .ignoresSafeArea()
        // A pull-down chevron instead of a close button: the sheet swipes away, this says so.
        .overlay(alignment: .top) {
            Button { isPresented = false } label: {
                Image(systemName: "chevron.compact.down")
                    .font(.system(size: 34, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.9))
                    .shadow(color: .black.opacity(0.4), radius: 4)
                    .frame(width: 80, height: 44)
                    .contentShape(Rectangle())
            }
            .padding(.top, 8)
            .accessibilityLabel("Close")
        }
    }
}

/// Address and access key typed by hand, on their own page. Saving adds it to the list and closes the page; only the first one connects.
struct ConnectionEntry: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.dismiss) private var dismiss
    let title: String
    @State var url: String
    @State var token: String

    var body: some View {
        Form {
            Section {
                TextField("https://vision.example.com", text: $url)
                    .keyboardType(.URL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
            } header: {
                Text("Address")
            }
            Section {
                // A plain field on purpose: any SecureField makes iOS offer Passwords and ask to save a login.
                TextField("Access key", text: $token)
                    .fontDesign(.monospaced)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
            } header: {
                Text("Access key")
            }
            Section {
                Button("Save") {
                    client.save(url: url, token: token)
                    dismiss()
                }
                .disabled(url.trimmingCharacters(in: .whitespaces).isEmpty || token.isEmpty)
            }
        }
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
    }
}

private struct ResultRow: View {
    let text: String
    let passed: Bool

    var body: some View {
        Label {
            Text(.init(text)).themeFont(.footnote)
        } icon: {
            Image(systemName: passed ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                .foregroundStyle(passed ? .green : .orange)
        }
    }
}

private struct SettingsLabel: View {
    let title: String
    var subtitle: String?
    let systemImage: String
    let tint: Color

    init(_ title: String, subtitle: String? = nil, systemImage: String, tint: Color) {
        self.title = title
        self.subtitle = subtitle
        self.systemImage = systemImage
        self.tint = tint
    }

    var body: some View {
        Label {
            if let subtitle {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                    Text(subtitle)
                        .themeFont(.footnote)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
            } else {
                Text(title)
            }
        } icon: {
            SettingsIcon(systemImage: systemImage, tint: tint)
        }
    }
}

/// The white symbol on a tinted rounded square that starts each Settings row, as in the system Settings app.
private struct SettingsIcon: View {
    let systemImage: String
    let tint: Color

    var body: some View {
        Image(systemName: systemImage)
            .font(.system(size: 14, weight: .semibold))
            .foregroundStyle(.white)
            .frame(width: 29, height: 29)
            .background(tint.gradient, in: RoundedRectangle(cornerRadius: 7, style: .continuous))
    }
}

/// A saved address: name over host, with the status light out front only on the selected one.
private struct AddressLabel: View {
    let name: String
    let host: String
    var light: VisionClient?

    var body: some View {
        HStack(spacing: 10) {
            Group {
                if let light { StatusLight(client: light) }
            }
            .frame(width: 14)
            VStack(alignment: .leading, spacing: 2) {
                Text(name)
                Text(host)
                    .themeFont(.footnote)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
        }
    }
}

/// What the Vision runs for speech, in the server's own words: a row per part with its state, then any
/// problem, each saying what to run on the computer to fix it. The voice row is also where the voice is
/// picked ([voice] voice on the machine), with the speaking speed under it ([voice] rate), saved when the
/// slider is let go; the pitch stays the same, only the pace changes. Rows only: Settings puts them in the
/// same group as the defaults.
private struct ServerRows: View {
    @EnvironmentObject private var client: VisionClient
    let status: ServerStatus?
    @Binding var rate: Double?
    @Binding var voices: VoiceCatalogue?
    @State private var draft: Double = 1.0
    @State private var saving = false

    /// With the voice switch above (VoiceSwitchRow) the switch is "Voice", so the voice row says which one it speaks as;
    /// switched off, nothing is loaded, so the speech rows step aside.
    private var switched: Bool { client.canSwitchVoice }
    private var speechShown: Bool { !switched || client.voiceOn }

    var body: some View {
        Group {
            ForEach(speechShown ? status?.rows ?? [] : []) { row in
                if row.label == "Voice" {
                    voiceRow(row)
                    speedRows
                } else {
                    StatusRow(row: row, systemImage: "ear")
                }
            }
            // A Vision that reports no voice row still lets the voice and its speed be set.
            if speechShown, status?.rows.contains(where: { $0.label == "Voice" }) != true {
                if let voices, voices.voices.count > 1 {
                    NavigationLink {
                        VoiceList(voices: $voices)
                    } label: {
                        LabeledContent {
                            Text(voices.voice.capitalized)
                        } label: {
                            SettingsLabel(switched ? "Speaks as" : "Voice", systemImage: "waveform", tint: .indigo)
                        }
                    }
                }
                speedRows
            }
            ForEach(status?.problems ?? [], id: \.self) { problem in
                Label {
                    Text(problem)
                        .themeFont(.footnote)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                } icon: {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                }
            }
        }
        .disabled(saving)
        // The status can arrive before the speed does.
        .onChange(of: rate, initial: true) { draft = rate ?? 1.0 }
    }

    /// The server's voice row; with more than one voice to pick from, tapping it opens the list
    /// (VoiceList), where each can be heard before it is picked. The name shown is the one picked
    /// here, so it never lags the server's report.
    @ViewBuilder
    private func voiceRow(_ row: ServerStatus.Row) -> some View {
        if let voices, voices.voices.count > 1 {
            NavigationLink {
                VoiceList(voices: $voices)
            } label: {
                StatusRow(row: row, systemImage: "waveform", picked: voices.voice.capitalized, title: switched ? "Speaks as" : nil)
            }
        } else {
            StatusRow(row: row, systemImage: "waveform", title: switched ? "Speaks as" : nil)
        }
    }

    @ViewBuilder
    private var speedRows: some View {
        if rate != nil {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    SettingsLabel("Speaking speed", systemImage: "speedometer", tint: .orange)
                    Spacer()
                    Text("\(speed)×")
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                }
                HStack(spacing: 12) {
                    Slider(value: $draft, in: 0.5...2.0, step: 0.05) { editing in
                        if !editing { save(draft) }
                    }
                    .accessibilityLabel("Speaking speed")
                    .accessibilityValue("\(speed) times")
                    // A tinted capsule, faded at normal speed so the slider never changes width.
                    // Borderless, or the Form makes the whole row one button.
                    let normal = abs(draft - 1.0) < 0.001
                    Button {
                        draft = 1.0
                        save(1.0)
                    } label: {
                        Text("Reset to normal")
                            .themeFont(.footnote, weight: .semibold)
                            .foregroundStyle(.tint)
                            .lineLimit(1)
                            .fixedSize()
                            .padding(.horizontal, 12)
                            .padding(.vertical, 6)
                            .background(.tint.opacity(0.15), in: Capsule())
                    }
                    .buttonStyle(.borderless)
                    .disabled(normal)
                    .opacity(normal ? 0.4 : 1)
                    .animation(.easeOut(duration: 0.2), value: normal)
                    .accessibilityLabel("Reset to normal speed")
                }
            }
        }
    }

    /// "1.15", "1", "0.5": no trailing zeros.
    private var speed: String { draft.formatted(.number.precision(.fractionLength(0...2))) }

    private func save(_ value: Double) {
        let value = min(2.0, max(0.5, value))
        guard abs(value - (rate ?? 1.0)) > 0.001 else { return }
        saving = true
        Task {
            defer { saving = false }
            do {
                rate = try await client.saveVoiceRate(value)
            } catch {
                client.toast = "Couldn't save the speaking speed: \(SettingsView.explain(error))"
            }
            draft = rate ?? 1.0
        }
    }

    /// A part's name over the server's words for it, its light at the end. Not LabeledContent: a value
    /// that wraps would push the light under the text.
    private struct StatusRow: View {
        let row: ServerStatus.Row
        let systemImage: String
        /// What tapping the row picks, shown in place of the first of the server's words.
        var picked: String?
        /// In place of the server's label (the voice row under the voice switch: "Speaks as").
        var title: String?

        var body: some View {
            HStack(spacing: 8) {
                Label {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(title ?? row.label)
                            .foregroundStyle(Color.primary)
                        Group {
                            if let picked {
                                // The pick tinted like a picker's value, the server's words after it.
                                let rest = row.value.components(separatedBy: " · ").dropFirst().map { " · " + $0 }.joined()
                                Text("\(Text(picked).foregroundStyle(.tint))\(Text(rest).foregroundStyle(Color.secondary))")
                            } else {
                                Text(row.value)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .themeFont(.footnote)
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                    }
                } icon: {
                    SettingsIcon(systemImage: systemImage, tint: .indigo)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                RowState(state: row.state)
            }
            .contentShape(Rectangle())
            .accessibilityElement(children: .combine)
        }
    }

    private struct RowState: View {
        let state: String

        var body: some View {
            Group {
                if state == "loading" {
                    ProgressView().controlSize(.mini)
                } else {
                    Circle().fill(color).frame(width: 9, height: 9)
                }
            }
            .accessibilityLabel(label)
        }

        private var color: Color {
            switch state {
            case "ready": return .green
            case "failed": return .orange
            default: return .secondary
            }
        }

        private var label: String {
            switch state {
            case "ready": return "Ready"
            case "loading": return "Loading"
            case "failed": return "Failed"
            default: return "Not loaded yet"
            }
        }
    }
}

/// The connection's state as a small light: green when connected, orange when it can't get through.
private struct StatusLight: View {
    @ObservedObject var client: VisionClient

    var body: some View {
        Group {
            if client.connection == .connecting {
                ProgressView().controlSize(.mini)
            } else {
                Circle().fill(color).frame(width: 9, height: 9)
            }
        }
        .accessibilityLabel(label)
    }

    private var color: Color {
        switch client.connection {
        case .online: return .green
        case .failed: return .orange
        case .connecting, .offline: return .secondary
        }
    }

    private var label: String {
        switch client.connection {
        case .online: return "Connected"
        case .connecting: return "Connecting"
        case .offline: return "Not connected"
        case .failed: return "Can't reach it"
        }
    }
}

/// The top of the page: Pip in the mood of the connection, and one line saying where things stand.
private struct SettingsHero: View {
    @ObservedObject var client: VisionClient

    var body: some View {
        VStack(spacing: 14) {
            PipView(mood: mood, cell: 7)
                .padding(.top, 8)
            VStack(spacing: 6) {
                Text("Vision")
                    .themeFont(.title2, weight: .semibold)
                if !client.isConfigured {   // once paired, the Address row carries the light
                    Text("Not paired yet")
                        .themeFont(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.bottom, 4)
        .accessibilityElement(children: .combine)
    }

    private var mood: PipMood {
        guard client.isConfigured else { return .asleep }
        switch client.connection {
        case .online: return .idle
        case .connecting: return .thinking
        case .offline: return .asleep
        case .failed: return .error
        }
    }
}

/// The model and effort every new chat starts on, saved where Vision runs (the terminal's /default).
/// Chats already open keep their own; the pills by the message box change those.
/// Only shown once the catalogue has loaded; rows only, under the server's.
private struct DefaultsRows: View {
    @EnvironmentObject private var client: VisionClient
    @Binding var catalogue: ModelCatalogue?
    @State private var saving = false

    var body: some View {
        Group {
            if let catalogue {
                Picker(selection: Binding(get: { catalogue.current },
                                          set: { save(model: $0, effort: catalogue.effort) })) {
                    ForEach(catalogue.tabs.filter { !$0.models.isEmpty }) { provider in
                        Section(provider.tab) {
                            ForEach(provider.models) { Text($0.label).tag($0.id) }
                        }
                    }
                } label: {
                    SettingsLabel("Default model", systemImage: "sparkles", tint: .purple)
                }
                .pickerStyle(.menu)
                if catalogue.efforts.contains(where: { !$0.id.isEmpty }) {
                    Picker(selection: Binding(get: { catalogue.effort },
                                              set: { save(model: catalogue.current, effort: $0) })) {
                        ForEach(catalogue.efforts) { Text(ModelPickerState.effortTitle($0.label)).tag($0.id) }
                    } label: {
                        SettingsLabel("Default effort", systemImage: "gauge.with.dots.needle.67percent", tint: .teal)
                    }
                    .pickerStyle(.menu)
                }
            }
        }
        .disabled(saving)
    }

    private func save(model: String, effort: String) {
        guard let catalogue, model != catalogue.current || effort != catalogue.effort else { return }
        saving = true
        Task {
            defer { saving = false }
            do {
                self.catalogue = try await client.saveDefaults(model: model, effort: effort)
            } catch {
                client.toast = "Couldn't save the default: \(SettingsView.explain(error))"
            }
        }
    }
}

/// The voices made on the machine. A tap on one speaks in it from the next reply on; its play button plays
/// the clip it was made from first, so it can be heard before it is chosen.
private struct VoiceList: View {
    @EnvironmentObject private var client: VisionClient
    @Binding var voices: VoiceCatalogue?
    @StateObject private var sampler = VoiceSampler()
    @State private var saving: String?

    var body: some View {
        List {
            Section {
                ForEach(voices?.voices ?? []) { voice in
                    let name = voice.id.capitalized
                    HStack(spacing: 12) {
                        Button { save(voice.id) } label: {
                            HStack {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(name).foregroundStyle(.primary)
                                    if !voice.note.isEmpty {
                                        Text(voice.note.capitalized).themeFont(.footnote).foregroundStyle(.secondary)
                                    }
                                }
                                Spacer()
                                if saving == voice.id {
                                    ProgressView()
                                } else if voice.id == voices?.voice {
                                    Image(systemName: "checkmark").fontWeight(.semibold).foregroundStyle(.tint)
                                }
                            }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityElement(children: .ignore)
                        .accessibilityLabel(name)
                        .accessibilityAddTraits(voice.id == voices?.voice ? [.isButton, .isSelected] : .isButton)
                        if voice.sample == true {
                            let playing = sampler.playing == voice.id
                            Button { sampler.toggle(voice.id, client: client) } label: {
                                Image(systemName: playing ? "stop.circle.fill" : "play.circle").font(.title2)
                            }
                            .buttonStyle(.borderless)
                            .accessibilityLabel(playing ? "Stop \(name)" : "Hear \(name)")
                        }
                    }
                }
            } footer: {
                if voices?.voices.contains(where: { $0.sample == true }) == true {
                    Text("Play is the clip each voice was made from.")
                }
            }
        }
        .navigationTitle("Voice")
        .navigationBarTitleDisplayMode(.inline)
        .disabled(saving != nil)
        .onDisappear { sampler.stop() }
    }

    private func save(_ voice: String) {
        guard let voices, voice != voices.voice else { return }
        saving = voice
        Task {
            defer { saving = nil }
            do {
                self.voices = try await client.saveVoice(voice)
            } catch {
                client.toast = "Couldn't change the voice: \(SettingsView.explain(error))"
            }
        }
    }
}

/// Plays one voice's sample at a time, fetched from the machine when it is asked for.
private final class VoiceSampler: NSObject, ObservableObject, AVAudioPlayerDelegate {
    @Published private(set) var playing: String?
    private var player: AVAudioPlayer?

    /// Play `id`'s sample, or stop it if it is the one playing.
    @MainActor func toggle(_ id: String, client: VisionClient) {
        let was = playing
        stop()
        guard was != id else { return }
        playing = id
        Task { @MainActor in
            do {
                let wav = try await client.voiceSample(id)
                guard playing == id, player == nil else { return }   // stopped, or another asked for, while it came
                let session = AVAudioSession.sharedInstance()
                try session.setCategory(.playback, mode: .spokenAudio, options: [.mixWithOthers])
                try session.setActive(true)
                let p = try AVAudioPlayer(data: wav, fileTypeHint: AVFileType.wav.rawValue)
                p.delegate = self
                player = p
                if !p.play() { stop() }
            } catch {
                guard playing == id else { return }
                stop()
                client.toast = "Couldn't play that voice: \(SettingsView.explain(error))"
            }
        }
    }

    func stop() {
        if let player {
            player.stop()
            self.player = nil
            AudioPlayer.deactivateSession()
        }
        playing = nil
    }

    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        DispatchQueue.main.async { if self.player === player { self.stop() } }
    }

    func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        DispatchQueue.main.async { if self.player === player { self.stop() } }
    }
}

// MARK: - model and effort pickers

/// Which compact pill a frame or an expansion belongs to: both pickers share one preference key
/// and one overlay chrome, so each has to say which it is.
enum PickerKind: Hashable {
    case model, effort
}

/// Shared state for the model and effort pills and their sliders: the catalogue, the provider
/// tab, and the model or effort previewed while dragging. Lifting a finger sends one change.
@MainActor
final class ModelPickerState: ObservableObject {
    @Published var catalogue: ModelCatalogue?
    @Published var error: String?
    @Published var tab: String?
    @Published var model: String?
    @Published var effort: String?
    @Published var rememberedModels: [String: String] = [:]

    var currentTab: ModelCatalogue.Tab? {
        catalogue?.tabs.first { $0.tab == tab } ?? catalogue?.tabs.first
    }

    var providers: [ModelCatalogue.Tab] { catalogue?.tabs.filter { !$0.models.isEmpty } ?? [] }

    /// The levels the current model honours, weakest first as the server lists them.
    var efforts: [ModelCatalogue.Entry] { catalogue?.efforts.filter { !$0.id.isEmpty } ?? [] }

    func selectedLabel(fallback: String) -> String {
        currentTab?.models.first { $0.id == model }?.label ?? (fallback.isEmpty ? "Model" : fallback)
    }

    func selectedEffortTitle(fallback: String) -> String {
        Self.effortTitle(effort ?? fallback)
    }

    static func effortTitle(_ level: String) -> String {
        switch level {
        case "": return "Off"
        case "xhigh": return "xHigh"
        case "ultracode": return "Ultracode"
        default: return level.capitalized
        }
    }

    func select(provider value: String) {
        guard let provider = catalogue?.tabs.first(where: { $0.tab == value }) else { return }
        tab = value
        let remembered = rememberedModels[value]
        model = provider.models.first { $0.id == remembered }?.id ?? provider.models.first?.id
    }

    func commit(_ client: VisionClient) {
        guard client.connection == .online,
              let model, let provider = currentTab,
              provider.models.contains(where: { $0.id == model }) else { return }
        rememberedModels[provider.tab] = model
        guard model != catalogue?.current else { return }
        client.switchModel(model)
    }

    func commitEffort(_ client: VisionClient) {
        guard client.connection == .online,
              let effort, let current = catalogue?.current,
              efforts.contains(where: { $0.id == effort }),
              effort != catalogue?.effort else { return }
        client.switchModel(current, effort: effort)
    }

    func load(_ client: VisionClient) async {
        guard client.connection == .online else { return }
        error = nil
        do {
            let result = try await client.fetchModels()
            guard !Task.isCancelled else { return }
            catalogue = result
            let provider = result.tabs.first { $0.models.contains { $0.id == result.current } }
                ?? result.tabs.first
            tab = provider?.tab
            model = provider?.models.first { $0.id == result.current }?.id ?? provider?.models.first?.id
            effort = result.effort
            if let tab, let model { rememberedModels[tab] = model }
        } catch {
            guard !Task.isCancelled else { return }
            self.error = error.localizedDescription
        }
    }
}

/// Where each compact pill sits on screen, so its picker can grow out of that exact spot.
struct PillFrameKey: PreferenceKey {
    static let defaultValue: [PickerKind: CGRect] = [:]
    static func reduce(value: inout [PickerKind: CGRect], nextValue: () -> [PickerKind: CGRect]) {
        value.merge(nextValue()) { _, next in next }
    }
}

/// `.plain`, plus a light tap the moment a finger lands rather than when it lifts.
struct PressHapticButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .opacity(configuration.isPressed ? 0.6 : 1)
            .sensoryFeedback(.impact(weight: .light), trigger: configuration.isPressed) { _, pressed in pressed }
    }
}

enum PickerMotion {
    static let animation = Animation.smooth(duration: 0.32)
}

/// The bare text button both pickers share: a short title and an up chevron, no chrome. Tapping it hands
/// over to the matching overlay, which scales up out of this pill's position into the big slider.
private struct PickerPillButton: View {
    let kind: PickerKind
    let title: String
    @Binding var isExpanded: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.drawerAway) private var away

    var body: some View {
        // The pill stays in the tree while the slider is open (just invisible) so its frame keeps
        // being reported and the composer's layout doesn't shift.
        // A second tap on the pill puts its slider away again.
        Button {
            withAnimation(reduceMotion ? nil : PickerMotion.animation) {
                isExpanded.toggle()
            }
        } label: {
            HStack(spacing: 4) {
                Text(title)
                    .lineLimit(1)
                    .truncationMode(.tail)
                Image(systemName: "chevron.up")
                    .font(.system(size: 10, weight: .bold))
            }
            .themeFont(.subheadline, weight: .medium)
            .padding(.horizontal, 2)
            .frame(height: 38)
            .foregroundStyle(.primary)
            .opacity(isExpanded ? 0 : 1)
            .background(GeometryReader { geometry in
                // Nothing while the chat slides for the drawer (SideDrawer.away): read afresh once it is at rest.
                Color.clear.preference(key: PillFrameKey.self, value: away ? [:] : [kind: geometry.frame(in: .global)])
            })
            .frame(minHeight: 40)
            .contentShape(Rectangle())
        }
        .buttonStyle(PressHapticButtonStyle())
        .accessibilityValue(title)
    }
}

/// The compact model button; also the one that loads the catalogue for both pickers.
struct ModelPickerButton: View {
    @ObservedObject var state: ModelPickerState
    @Binding var isExpanded: Bool
    @EnvironmentObject private var client: VisionClient

    var body: some View {
        PickerPillButton(kind: .model, title: state.selectedLabel(fallback: client.modelLabel), isExpanded: $isExpanded)
            .accessibilityLabel("Choose model and provider")
            .disabled(client.connection != .online || client.busy)
            .onChange(of: client.activeChatID) { _, _ in isExpanded = false }
            // Effort is in the id too, so a level changed from the machine shows up on the effort pill.
            .task(id: "\(client.activeChatID ?? "")|\(client.modelLabel)|\(client.effort)|\(client.nextModel ?? "")|\(client.nextEffort ?? "")|\(client.connection == .online)") {
                await state.load(client)
            }
            .task {
                #if DEBUG
                // Screenshot hook: open the slider once the chat has connected.
                if CommandLine.arguments.contains("--model-expanded") {
                    try? await Task.sleep(for: .seconds(3))
                    isExpanded = true
                }
                // Motion hook: animate open, hold, animate closed, exactly like a tap and a tap outside.
                if CommandLine.arguments.contains("--model-toggle") {
                    try? await Task.sleep(for: .seconds(3))
                    withAnimation(.spring(duration: 0.4, bounce: 0.15)) { isExpanded = true }
                    try? await Task.sleep(for: .seconds(2))
                    withAnimation(.easeOut(duration: 0.2)) { isExpanded = false }
                }
                #endif
            }
    }
}

/// The compact effort button, "High ⌃", sitting next to the model one. Same pill, same motion.
struct EffortPickerButton: View {
    @ObservedObject var state: ModelPickerState
    @Binding var isExpanded: Bool
    @EnvironmentObject private var client: VisionClient

    var body: some View {
        PickerPillButton(kind: .effort, title: state.selectedEffortTitle(fallback: client.effort), isExpanded: $isExpanded)
            .accessibilityLabel("Choose effort")
            .disabled(client.connection != .online || client.busy)
            .onChange(of: client.activeChatID) { _, _ in isExpanded = false }
            .task {
                #if DEBUG
                if CommandLine.arguments.contains("--effort-toggle") {
                    try? await Task.sleep(for: .seconds(3))
                    withAnimation(.spring(duration: 0.4, bounce: 0.15)) { isExpanded = true }
                    try? await Task.sleep(for: .seconds(2))
                    withAnimation(.easeOut(duration: 0.2)) { isExpanded = false }
                }
                #endif
            }
    }
}

/// The full-screen chrome under both pickers: the chat dims, and the content scales out of the
/// compact pill's position and back into it. A tap on the dim, or the chat going busy, closes it.
private struct PickerOverlay<Content: View>: View {
    @Binding var isExpanded: Bool
    /// The compact pill's frame in global coordinates: the whole picker scales out of, and back into, that point.
    let pillFrame: CGRect
    /// The composer card in global coordinates: the slider's track sits centred over it while it blurs underneath.
    let composer: CGRect
    let closeLabel: String
    @ViewBuilder let content: () -> Content
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @EnvironmentObject private var client: VisionClient
    /// True from the moment the shrink starts, so a stray tap on the departing slider can't change anything.
    @State private var closing = false

    var body: some View {
        // Always mounted, empty when closed: the `if` has to sit right here so the dim and the
        // picker get their own transitions (a transition buried inside an inserted subtree is ignored).
        GeometryReader { geometry in
            let frame = geometry.frame(in: .global)
            // Scale around the pill's centre so nothing travels: the picker just grows out of the pill.
            let anchor = pillFrame == .zero ? UnitPoint.bottom : UnitPoint(
                x: (pillFrame.midX - frame.minX) / max(1, frame.width),
                y: (pillFrame.midY - frame.minY) / max(1, frame.height))
            ZStack(alignment: .bottom) {
                if isExpanded {
                    Color.black.opacity(0.6)
                        .ignoresSafeArea()
                        .contentShape(Rectangle())
                        .onTapGesture { collapse() }
                        .transition(.opacity)
                        .accessibilityLabel(closeLabel)
                        .accessibilityAddTraits(.isButton)
                    content()
                        .padding(.horizontal, 28)
                        .padding(.bottom, composer.isEmpty ? 16
                                 : max(16, frame.maxY - composer.maxY + (composer.height - GlassChoiceSlider.height) / 2))
                        .allowsHitTesting(!closing)
                        .frame(width: geometry.size.width, height: geometry.size.height, alignment: .bottom)
                        .transition(reduceMotion ? .opacity : .scale(scale: 0.28, anchor: anchor).combined(with: .opacity))
                }
            }
        }
        .allowsHitTesting(isExpanded)
        .onChange(of: isExpanded) { _, open in if open { closing = false } }
        .onChange(of: client.busy) { _, busy in if busy { collapse() } }
    }

    private func collapse() {
        closing = true
        withAnimation(reduceMotion ? nil : PickerMotion.animation) { isExpanded = false }
    }
}

/// Full-screen model picker: the model name sits above a big glass slider laid over the blurred composer
/// card, and the provider is a small menu above that. Release to apply and shrink back.
struct ModelPickerOverlay: View {
    @ObservedObject var state: ModelPickerState
    @Binding var isExpanded: Bool
    let pillFrame: CGRect
    let composer: CGRect
    @EnvironmentObject private var client: VisionClient
    /// Bumped on every provider pick, so a pick ticks even when the catalogue reload moves `tab` quietly.
    @State private var providerPicks = 0

    var body: some View {
        PickerOverlay(isExpanded: $isExpanded, pillFrame: pillFrame, composer: composer, closeLabel: "Close model slider") {
            picker
        }
    }

    private var picker: some View {
        VStack(spacing: 10) {
            Menu {
                ForEach(state.providers) { provider in
                    Button {
                        providerPicks += 1
                        state.select(provider: provider.tab)
                        state.commit(client)     // the label already shows the new model; switch for real too
                    } label: {
                        if provider.tab == state.tab {
                            Label(provider.tab, systemImage: "checkmark")
                        } else {
                            Text(provider.tab)
                        }
                    }
                }
            } label: {
                HStack(spacing: 4) {
                    Text(state.tab ?? "Provider")
                    Image(systemName: "chevron.down").font(.system(size: 9, weight: .bold))
                }
                .themeFont(.subheadline, weight: .medium)
                .foregroundStyle(.white.opacity(0.7))
                .padding(.horizontal, 12)
                .frame(height: 32)
                .contentShape(Capsule())
            }
            // Opening upward from low on the screen, a menu would put the first provider at the bottom.
            .menuOrder(.fixed)
            .accessibilityLabel("Provider")
            .sensoryFeedback(.selection, trigger: providerPicks)
            Group {
                if state.catalogue != nil {
                    GlassChoiceSlider(title: "Model",
                                      selection: $state.model,
                                      // The server lists strongest first; the slider runs weakest → strongest left to right.
                                      options: (state.currentTab?.models ?? []).reversed().map { .init(id: $0.id, title: $0.label) },
                                      empty: "No models available",
                                      only: "The only model on \(state.tab ?? "this provider")",
                                      onCommit: { state.commit(client) })
                } else if let error = state.error {
                    Button("Retry loading models") { Task { await state.load(client) } }
                        .foregroundStyle(.white)
                        .accessibilityHint(error)
                } else {
                    ProgressView().tint(.white).frame(height: GlassChoiceSlider.height)
                }
            }
        }
    }
}

/// Full-screen effort picker: the level's name above a glass slider running low → high.
struct EffortPickerOverlay: View {
    @ObservedObject var state: ModelPickerState
    @Binding var isExpanded: Bool
    let pillFrame: CGRect
    let composer: CGRect
    @EnvironmentObject private var client: VisionClient

    var body: some View {
        PickerOverlay(isExpanded: $isExpanded, pillFrame: pillFrame, composer: composer, closeLabel: "Close effort slider") {
            if state.catalogue != nil {
                GlassChoiceSlider(title: "Effort",
                                  selection: $state.effort,
                                  options: state.efforts.map { .init(id: $0.id, title: ModelPickerState.effortTitle($0.label)) },
                                  empty: "No effort levels for this model",
                                  only: "The only level for this model",
                                  onCommit: { state.commitEffort(client) })
            } else {
                ProgressView().tint(.white).frame(height: GlassChoiceSlider.height)
            }
        }
    }
}

/// A discrete pill slider: native Liquid Glass, a white fill and an inset dark thumb, with the
/// chosen title in big type above. VoiceOver adjusts the same choices without a drag gesture.
private struct GlassChoiceSlider: View {
    struct Option: Identifiable {
        let id: String
        let title: String
    }

    static let height: CGFloat = 68
    private let inset: CGFloat = 11
    private let diameter: CGFloat = 46

    @Environment(\.isEnabled) private var isEnabled
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.layoutDirection) private var layoutDirection
    @State private var dragging = false
    let title: String
    @Binding var selection: String?
    let options: [Option]
    /// Shown as the title when there is nothing to choose from.
    let empty: String
    /// Shown inside the track when there is exactly one choice, in place of a slider that can't move.
    let only: String
    /// Called on release; the slider stays open, the dimmed chat behind it closes it.
    let onCommit: () -> Void

    private var index: Int { options.firstIndex { $0.id == selection } ?? 0 }
    private var selectedTitle: String { options.isEmpty ? empty : options[index].title }
    private var fraction: CGFloat { CGFloat(index) / CGFloat(max(1, options.count - 1)) }

    var body: some View {
        VStack(spacing: 14) {
            Text(selectedTitle)
                .themeFont(.title2, weight: .semibold)
                .foregroundStyle(.white)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
                .frame(maxWidth: .infinity)
                .contentTransition(.numericText())
            if options.count == 1 {
                single
            } else {
                track
            }
        }
        .opacity(isEnabled ? 1 : 0.45)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(title)
        .accessibilityValue(options.count == 1 ? "\(selectedTitle), \(only)" : selectedTitle)
        .accessibilityAdjustableAction { direction in
            guard isEnabled, options.count > 1 else { return }
            let next: Int
            switch direction {
            case .increment: next = min(index + 1, options.count - 1)
            case .decrement: next = max(index - 1, 0)
            @unknown default: return
            }
            guard next != index else { return }
            withAnimation(.snappy(duration: 0.18)) { selection = options[next].id }
            onCommit()
        }
        .sensoryFeedback(.selection, trigger: index)
    }

    /// One choice: nothing to slide, so the track becomes a quiet glass pill with the thumb
    /// already settled, holding a check, and a line saying why it can't move.
    private var single: some View {
        HStack(spacing: 12) {
            Circle()
                .fill(.white)
                .frame(width: diameter, height: diameter)
                .overlay(Image(systemName: "checkmark").font(.system(size: 17, weight: .bold)).foregroundStyle(Color(white: 0.035)))
            Text(only)
                .themeFont(.subheadline, weight: .medium)
                .foregroundStyle(.white.opacity(0.7))
                .lineLimit(1)
                .minimumScaleFactor(0.8)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, inset)
        .frame(maxWidth: .infinity)
        .frame(height: Self.height)
        .modifier(ChoiceGlass(interactive: false))
    }

    private var track: some View {
        GeometryReader { geometry in
            let travel = max(0, geometry.size.width - 2 * inset - diameter)
            let centre = inset + diameter / 2 + travel * fraction
            ZStack(alignment: .leading) {
                Capsule()
                    .fill(.white)
                    .frame(width: diameter + travel * fraction, height: diameter)
                    .offset(x: inset)
                ForEach(Array(options.enumerated()), id: \.element.id) { offset, _ in
                    Circle()
                        .fill(offset <= index ? Color.black.opacity(0.2) : Color.white.opacity(0.3))
                        .frame(width: 8, height: 8)
                        .offset(x: inset + diameter / 2 + travel * CGFloat(offset) / CGFloat(max(1, options.count - 1)) - 4)
                }
                Circle()
                    .fill(Color(white: 0.035))
                    .overlay(Circle().strokeBorder(.white, lineWidth: 3))
                    .frame(width: diameter, height: diameter)
                    .scaleEffect(dragging ? 1.06 : 1)
                    .offset(x: centre - diameter / 2)
            }
            .frame(width: geometry.size.width, height: Self.height, alignment: .leading)
            .modifier(ChoiceGlass(interactive: false))
            .scaleEffect(x: layoutDirection == .rightToLeft ? -1 : 1, y: 1)
            // UIKit touch tracking rather than a SwiftUI DragGesture: the thumb jumps the instant
            // a finger lands, with nothing deciding first whether this is a drag.
            .overlay(TouchTrack(
                onChange: { x in
                    dragging = true
                    select(at: x, width: geometry.size.width, travel: travel)
                },
                onEnd: { x in
                    dragging = false
                    if let x { select(at: x, width: geometry.size.width, travel: travel) }
                    onCommit()
                }))
        }
        .frame(height: Self.height)
    }

    private func select(at position: CGFloat, width: CGFloat, travel: CGFloat) {
        guard isEnabled, !options.isEmpty, travel > 0 else { return }
        let x = layoutDirection == .rightToLeft ? width - position : position
        let value = min(max((x - inset - diameter / 2) / travel, 0), 1)
        let next = options[Int((value * CGFloat(options.count - 1)).rounded())].id
        guard next != selection else { return }
        // withAnimation rather than .animation(value:): that modifier would also swallow the
        // picker's scale transition on its subtree.
        withAnimation(.interactiveSpring(duration: 0.15)) { selection = next }
    }
}

/// A clear view that reports where a finger is on it from the moment it lands, in its own
/// coordinates, then where it lifted (nil when the system cancelled the touch). It claims the
/// touch with a zero-duration press recogniser, so the drawer's swipe (a SwiftUI drag on an
/// ancestor) can't recognise mid-slide and cut the touch off; raw touchesMoved got cancelled that way.
private struct TouchTrack: UIViewRepresentable {
    let onChange: (CGFloat) -> Void
    let onEnd: (CGFloat?) -> Void

    func makeUIView(context: Context) -> TrackView { TrackView() }

    func updateUIView(_ view: TrackView, context: Context) {
        view.onChange = onChange
        view.onEnd = onEnd
    }

    final class TrackView: UIView, UIGestureRecognizerDelegate {
        var onChange: ((CGFloat) -> Void)?
        var onEnd: ((CGFloat?) -> Void)?

        override init(frame: CGRect) {
            super.init(frame: frame)
            backgroundColor = .clear
            isMultipleTouchEnabled = false
            let press = UILongPressGestureRecognizer(target: self, action: #selector(handle))
            press.minimumPressDuration = 0
            press.allowableMovement = .greatestFiniteMagnitude
            press.cancelsTouchesInView = false
            press.delegate = self
            addGestureRecognizer(press)
        }

        required init?(coder: NSCoder) { nil }

        @objc private func handle(_ press: UILongPressGestureRecognizer) {
            let x = press.location(in: self).x
            switch press.state {
            case .began, .changed: onChange?(x)
            case .ended: onEnd?(x)
            case .cancelled, .failed: onEnd?(nil)
            default: break
            }
        }

        /// Nothing else gets a say on a touch that started on the track: every other recogniser
        /// on the way up (the drawer swipe, the transcript's scroll) has to wait for this one to fail.
        func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer,
                               shouldBeRequiredToFailBy other: UIGestureRecognizer) -> Bool { true }
    }
}

private struct ChoiceGlass: ViewModifier {
    @Environment(\.colorScheme) private var colorScheme
    /// Interactive glass runs its own press recogniser, which holds the first touch back a beat;
    /// the pill wants that press feel, the slider track wants the touch the instant it lands.
    var interactive = true

    func body(content: Content) -> some View {
        if #available(iOS 26, *) {
            let glass = Glass.regular.tint(colorScheme == .light ? .black.opacity(0.1) : .clear)
            content.glassEffect(interactive ? glass.interactive() : glass, in: Capsule())
        } else {
            content
                .background(.regularMaterial, in: Capsule())
                .overlay(Capsule().strokeBorder(.white.opacity(0.2), lineWidth: 0.5))
        }
    }
}

/// The voice switch ([voice] enabled on the computer). Off unloads the voice and speech recognition there, so
/// the GPU is free; calls and spoken replies wait until it is back on. The status rows under it follow.
private struct VoiceSwitchRow: View {
    @EnvironmentObject private var client: VisionClient
    @Binding var status: ServerStatus?
    @State private var saving = false

    var body: some View {
        Toggle(isOn: Binding(get: { client.voiceOn }, set: { save($0) })) {
            SettingsLabel("Voice",
                          subtitle: client.voiceOn ? "Loaded on the computer's GPU" : "Off · the computer's GPU is free",
                          systemImage: "waveform", tint: .indigo)
        }
        .disabled(saving)
        .accessibilityIdentifier("voiceSwitch")
    }

    private func save(_ on: Bool) {
        saving = true
        Task {
            defer { saving = false }
            do {
                _ = try await client.setVoicePower(on)
            } catch {
                client.toast = "Couldn't switch the voice: \(error.localizedDescription)"
                return
            }
            // Follow the rows as the voice unloads, or loads again (a minute or two).
            for _ in 0..<40 {
                guard let fresh = try? await client.health() else { break }
                status = fresh.status
                if fresh.status?.isLoading != true { break }
                try? await Task.sleep(for: .seconds(3))
            }
        }
    }
}
