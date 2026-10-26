import SwiftUI

/// The transcript before a chat has anything in it: Pip, and nothing else.
///
/// He carries the state this screen used to spell out — asleep while the machine is away, thinking
/// while we look for it, an error face when it turned us down — so there is nothing to read and
/// nothing to tidy away once the first message lands.  The exceptions: "Set up Vision" before pairing,
/// and, once connected, the project or folder a fresh chat works in.
struct EmptyChatView: View {
    @EnvironmentObject private var client: VisionClient
    var onSetUp: () -> Void
    /// Tapping the folder a fresh chat works in: pick another one for it.
    var onFolder: (() -> Void)? = nil

    var body: some View {
        VStack(spacing: 16) {
            PipView(mood: mood, cell: 9)
            if !client.isConfigured {
                Button("Set up Vision", action: onSetUp)
                    .themeFont(.footnote)
                    .foregroundStyle(.tint)
                    .buttonStyle(.plain)
            } else if client.connection != .online {
                EmptyView()   // Pip's face says it isn't connected
            } else if let project = client.activeProject {
                // A project's fresh chat says whose it is and what for; its folder is the project's, so no picker.
                VStack(spacing: 6) {
                    Text(project.title)
                        .themeFont(.headline)
                        .multilineTextAlignment(.center)
                    if project.goal != project.title {
                        Text(project.goal)
                            .themeFont(.footnote)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                            .lineLimit(4)
                    }
                    if let folder = client.active?.folder, !folder.isEmpty {
                        Label(folder, systemImage: "folder")
                            .themeFont(.footnote)
                            .foregroundStyle(.tertiary)
                            .lineLimit(1)
                            .truncationMode(.head)
                            .padding(.top, 2)
                    }
                }
                .padding(.horizontal, 36)
                .accessibilityElement(children: .combine)
            } else if let folder = client.active?.folder, !folder.isEmpty {
                // A fresh chat says where it will work, the one thing that differs between two empty chats,
                // and a tap changes it: nothing has been said yet, so this is the moment to.
                Button { onFolder?() } label: {
                    HStack(spacing: 6) {
                        Label(folder, systemImage: "folder")
                            .lineLimit(1)
                            .truncationMode(.head)
                        if onFolder != nil {
                            Image(systemName: "chevron.down")
                                .font(.system(size: 10, weight: .bold))
                                .accessibilityHidden(true)
                        }
                    }
                    .themeFont(.footnote)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 14)
                    .frame(minHeight: 36)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(onFolder == nil)
                .padding(.horizontal, 24)
                .accessibilityLabel("Working in \(folder)")
                .accessibilityHint("Starts this chat in another folder")
            }
        }
        .frame(maxWidth: .infinity)
        // Centred in the transcript rather than stranded under the nav bar; the spare points keep
        // an empty chat from scrolling.
        .containerRelativeFrame(.vertical) { height, _ in max(220, height - 48) }
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

/// The first thing an unpaired phone shows, over the empty chat: what this app is, the one thing to
/// do on the computer, and the scan. Pairing is checked right here, so a code that can't be reached
/// says why on this screen instead of dropping you into a chat that never connects. "Not now" leaves
/// it for the empty chat's "Set up Vision", which brings it back.
struct WelcomeView: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.dismiss) private var dismiss
    @State private var showScanner = false
    @State private var showManual = false
    @State private var checking = false
    @State private var problem: String?
    @State private var connectedTo: String?

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                Spacer(minLength: 24)
                PipView(mood: mood, cell: 11)
                VStack(spacing: 10) {
                    Text("Vision")
                        .themeFont(.largeTitle, weight: .bold)
                    Text("Vision runs on your computer. This is how you chat and talk with it from your phone.")
                        .themeFont(.body)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
                .padding(.top, 28)
                steps
                    .padding(.top, 32)
                Spacer(minLength: 24)
                status
                    .frame(minHeight: 44)
                    .padding(.bottom, 12)
                actions
            }
            .padding(.horizontal, 28)
            .padding(.bottom, 20)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Not now") { dismiss() }
                        .themeFont(.body)
                }
            }
            .navigationDestination(isPresented: $showManual) {
                ConnectionEntry(title: "Add Vision", url: "", token: "")
            }
        }
        .sheet(isPresented: $showScanner) {
            PairingSheet(isPresented: $showScanner) { payload in
                guard client.pair(with: payload) else {
                    problem = PairingSheet.notACode
                    return
                }
                verify()
            }
        }
        // The manual page saves and closes itself; check what it saved once it is gone.
        .onChange(of: showManual) { _, shown in
            if !shown && client.isConfigured { verify() }
        }
    }

    private var steps: some View {
        VStack(alignment: .leading, spacing: 14) {
            step(1, Text("On your computer, run ") + Text("vision serve").monospaced().fontWeight(.semibold))
            step(2, Text("Scan the code it shows"))
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func step(_ number: Int, _ text: Text) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text("\(number)")
                .themeFont(.footnote, weight: .semibold)
                .foregroundStyle(.secondary)
                .frame(width: 24, height: 24)
                .background(Circle().fill(.quaternary))
            text.themeFont(.body)
        }
    }

    @ViewBuilder
    private var status: some View {
        if let connectedTo {
            Label("Connected to \(connectedTo)", systemImage: "checkmark.circle.fill")
                .themeFont(.body, weight: .medium)
                .foregroundStyle(.green)
        } else if checking {
            HStack(spacing: 8) {
                ProgressView()
                Text("Connecting…").themeFont(.footnote).foregroundStyle(.secondary)
            }
        } else if let problem {
            Text(.init(problem))
                .themeFont(.footnote)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
    }

    @ViewBuilder
    private var actions: some View {
        VStack(spacing: 14) {
            if retrying {
                // Paired, but nothing answered: the same address again first, since the usual cause
                // (the computer asleep, the local-network prompt still up) clears by itself.
                bigButton("Try again", systemImage: "arrow.clockwise") { verify() }
                if PairingScanner.isSupported {
                    Button("Scan a different code") { showScanner = true }
                }
                Button("Continue anyway") { dismiss() }
            } else if PairingScanner.isSupported {
                bigButton("Scan pairing code", systemImage: "qrcode.viewfinder") { showScanner = true }
                Button("Enter address manually") { showManual = true }
            } else {
                // No camera to scan with (the simulator): typing the address is the way in.
                bigButton("Enter address manually", systemImage: "keyboard") { showManual = true }
            }
        }
        .themeFont(.body)
        .disabled(checking || connectedTo != nil)
    }

    private func bigButton(_ title: String, systemImage: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: systemImage)
                .themeFont(.body, weight: .semibold)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 6)
        }
        .buttonStyle(.borderedProminent)
        .controlSize(.large)
    }

    /// Paired with an address that did not answer.
    private var retrying: Bool { client.isConfigured && problem != nil }

    private var mood: PipMood {
        if connectedTo != nil { return .idle }
        if checking { return .thinking }
        return problem == nil ? .idle : .error
    }

    /// Ask the Vision just paired how it is; a yes shows for a moment, then the chat takes over.
    private func verify() {
        problem = nil
        checking = true
        Task {
            let result = await SettingsView.check(client)
            checking = false
            guard result.passed else {
                problem = result.message
                return
            }
            connectedTo = client.currentConnection?.name ?? "Vision"
            try? await Task.sleep(for: .milliseconds(1500))   // long enough to read before the chat takes over
            dismiss()
        }
    }
}
