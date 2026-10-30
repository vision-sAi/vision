import Combine
import SwiftUI
import UIKit

/// Live voice mode's controls, in the composer's bottom row while a call runs: mute and end.  The call lives in the chat itself, the way ChatGPT's does: what you say and what
/// Vision says stream into the transcript as ordinary turns, and the keyboard stays live, so you can
/// type into the same call.  A spoken turn is sent as soon as it is heard.
struct CallControls: View {
    @ObservedObject var call: VoiceCall
    var onEnd: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            Button { call.engine.muted.toggle() } label: {
                Image(systemName: call.engine.muted ? "mic.slash.fill" : "mic.fill")
                    .font(.system(size: 16, weight: .semibold))
                    .frame(width: 36, height: 36)
                    .background(Color(.tertiarySystemFill), in: Circle())
                    .foregroundStyle(call.engine.muted ? Color.red : Color.primary)
                    .contentShape(Circle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(call.engine.muted ? "Unmute" : "Mute")
            Button(action: onEnd) {
                Image(systemName: "phone.down.fill")
                    .font(.system(size: 15, weight: .semibold))
                    .frame(width: 36, height: 36)
                    .background(Color.red, in: Circle())
                    .foregroundStyle(.white)
                    .contentShape(Circle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("End voice chat")
        }
        .fixedSize()
    }
}

// MARK: - the call itself

/// The loop behind live voice mode: listen → finish the preview → send → speak → listen.
/// It owns the audio engine; the chat screen's own player is handed the microphone back on the way out.
@MainActor
final class VoiceCall: ObservableObject {
    enum Phase: Equatable { case starting, listening, transcribing, missed, waiting, speaking, failed(String) }

    @Published private(set) var phase: Phase = .starting

    let engine = VoiceEngine()

    private weak var client: VisionClient?
    /// Each UI start gets an identity so a permission request from an old chat cannot affect a newer call.
    private var sessionID: UUID?
    private var busy = false
    private var speakingWatch: AnyCancellable?
    /// Vision's sentences arrive one at a time; a gap between two of them is not the end of the reply.
    private var settle: Task<Void, Never>?
    /// Watches for a microphone that never delivers (see `start`).
    private var watchdog: Task<Void, Never>?
    private var audioRecovery: Task<Void, Never>?
    private var waitingForAudio = false
    /// The engine is its own observable; the screen watches this object, so pass its changes on.
    private var relay: AnyCancellable?
    /// The Dynamic Island / Lock Screen view of the call, so it carries on with the app in the background.
    private let liveActivity = CallLiveActivity()
    private var activityWatch: AnyCancellable?

    init() {
        relay = engine.objectWillChange.sink { [weak self] in self?.objectWillChange.send() }
        engine.onAudioEvent = { [weak self] event in self?.audioEvent(event) }
    }

    var mood: VisionFaceMood {
        if engine.muted && phase == .listening { return .idle }
        switch phase {
        case .starting: return .idle
        case .listening: return .listening
        case .transcribing, .waiting: return .thinking
        case .missed: return .idle
        case .speaking: return .speaking
        case .failed: return .error
        }
    }

    var caption: String {
        if engine.muted && (phase == .listening || phase == .starting) { return "muted" }
        switch phase {
        case .starting: return "starting"
        // The mic is open the whole call; "listening" only while it actually hears you.
        case .listening:
            let state = engine.hearing ? "listening" : "ready"
            return engine.echoCancelled ? state : state + " · half duplex"
        case .transcribing: return "hearing you out"
        case .missed: return "didn't catch that"
        case .waiting: return "thinking"
        case .speaking: return "speaking"
        case .failed(let why): return why
        }
    }

    /// The composer's placeholder during a call: where it is, and that typing works too.
    var prompt: String {
        switch phase {
        case .starting: return "Starting…"
        case .listening: return engine.muted ? "Muted, type instead" : engine.hearing ? "Listening…" : "Talk or type"
        case .transcribing: return "Hearing you out…"
        case .missed: return "Didn't catch that"
        case .waiting: return "Thinking…"
        case .speaking: return "Speaking…"
        case .failed(let why): return why
        }
    }

    func start(client: VisionClient, sessionID: UUID) async {
        guard self.client == nil else { return }
        self.client = client
        self.sessionID = sessionID
        phase = .starting
        engine.muted = false
        UIApplication.shared.isIdleTimerDisabled = true
        guard await VoiceEngine.requestPermission() else {
            phase = .failed("microphone is off — enable it in Settings")
            return
        }
        guard self.client === client, self.sessionID == sessionID else { return }
        engine.onUtterance = { [weak self] url in self?.utterance(url) }
        engine.machinePreview = { [weak client] wav in
            guard let client else { throw CancellationError() }
            return try await client.previewWords(wav)
        }
        engine.onBargeIn = { [weak self] in self?.bargedIn() }
        // Vision's voice comes through the engine from here on, so the canceller knows about it.
        client.audioSink = { [weak self] wav in self?.engine.enqueue(wav) }
        client.stopCallSpeech = { [weak self] in self?.stopSpeechFromSystem() }
        speakingWatch = engine.$speaking.sink { [weak self] speaking in
            Task { @MainActor in self?.speakingChanged(speaking) }
        }
        do {
            try engine.start()
            busy = client.busy
            phase = .listening
            watchForSilentInput()
        } catch {
            phase = .failed(error.localizedDescription)
        }
        CallRemote.toggleMute = { [weak self] in self?.engine.muted.toggle() }
        liveActivity.start(activityState)
        // Both publish before the value lands; hop to the next main-queue turn to read the new state.
        activityWatch = Publishers.Merge3($phase.map { _ in () }, engine.$muted.map { _ in () },
                                          engine.$hearing.map { _ in () })
            .receive(on: DispatchQueue.main)
            .sink { [weak self] in
                guard let self else { return }
                self.liveActivity.update(self.activityState)
            }
    }

    private var activityState: VisionCallAttributes.ContentState {
        let mood: VisionCallAttributes.Mood
        if engine.muted && (phase == .listening || phase == .starting) {
            mood = .muted
        } else if phase == .listening && engine.hearing {
            return .init(mood: .hearing, caption: "Hearing you", muted: false)
        } else {
            switch phase {
            case .starting, .missed: mood = .starting
            case .listening: mood = .listening
            case .transcribing, .waiting: mood = .thinking
            case .speaking: mood = .speaking
            case .failed: mood = .error
            }
        }
        let caption: String
        switch phase {
        case .listening where !engine.echoCancelled && !engine.muted: caption = "Ready"
        default: caption = self.caption.prefix(1).uppercased() + self.caption.dropFirst()
        }
        return .init(mood: mood, caption: caption, muted: engine.muted)
    }

    /// `chat`: the chat this call was speaking for, which the machine stops sending speech; the chat on
    /// screen when it is not given (a switch away passes the one just left).
    func stop(sessionID: UUID? = nil, chat: String? = nil) {
        guard sessionID == nil || self.sessionID == sessionID else { return }
        speakingWatch = nil
        activityWatch = nil
        liveActivity.end()
        CallRemote.toggleMute = nil
        watchdog?.cancel()
        audioRecovery?.cancel()
        waitingForAudio = false
        settle?.cancel()
        client?.audioSink = nil
        client?.stopCallSpeech = nil
        client?.hush(chat: chat)
        engine.machinePreview = nil
        engine.stop()
        // Hung up while an utterance was still being transcribed: its task returns without
        // unpausing, and a paused engine would leave the next call deaf.
        engine.paused = false
        UIApplication.shared.isIdleTimerDisabled = false
        client = nil
        self.sessionID = nil
    }

    private func watchForSilentInput() {
        watchdog?.cancel()
        watchdog = Task { [weak self] in
            // The voice-processing unit can start without delivering microphone buffers.
            try? await Task.sleep(for: .milliseconds(1500))
            guard let self, !Task.isCancelled else { return }
            if !self.engine.fallBackToPlainInput() && !self.engine.isRunning {
                self.phase = .failed("microphone stopped")
            }
        }
    }

    private func audioEvent(_ event: VoiceEngine.AudioEvent) {
        guard client != nil else { return }
        switch event {
        case .interrupted:
            watchdog?.cancel()
            audioRecovery?.cancel()
            waitingForAudio = true
            phase = .failed("audio interrupted")
        case .interruptionEnded:
            guard waitingForAudio else { return }
            waitingForAudio = false
            recoverAudio(after: .milliseconds(100))
        case .configurationChanged:
            watchdog?.cancel()
            guard !waitingForAudio else { return }
            // A route or voice-processing change may briefly stop AVAudioEngine.
            // Give iOS time to finish switching before reopening the input.
            recoverAudio(after: .milliseconds(250))
        }
    }

    private func recoverAudio(after delay: Duration) {
        audioRecovery?.cancel()
        phase = .starting
        let currentSession = sessionID
        audioRecovery = Task { [weak self] in
            try? await Task.sleep(for: delay)
            guard let self, !Task.isCancelled, self.sessionID == currentSession,
                  self.client != nil else { return }
            for attempt in 0..<3 {
                do {
                    try self.engine.start()
                    self.phase = self.busy ? .waiting : .listening
                    self.watchForSilentInput()
                    return
                } catch {
                    guard attempt < 2 else {
                        self.phase = .failed(error.localizedDescription)
                        return
                    }
                    try? await Task.sleep(for: .milliseconds(250 * (attempt + 1)))
                    guard !Task.isCancelled, self.sessionID == currentSession else { return }
                }
            }
        }
    }

    /// You talked over Vision, and kept going once the engine paused her (so it wasn't her own echo).
    /// That means stop — both the speaker and, if she is still generating, the turn itself.  Over only
    /// the filler, before the reply has said a word, the filler stops and the turn carries on: what
    /// you're saying queues behind it.
    private func bargedIn() {
        guard engine.speaking else { return }
        settle?.cancel()
        engine.stopSpeaking()
        if busy, client?.replyStarted == true { client?.cancel() }
        phase = .listening
    }

    private func stopSpeechFromSystem() {
        settle?.cancel()
        engine.stopSpeaking()
        if busy { client?.cancel() }
        phase = busy ? .waiting : .listening
    }

    /// An utterance finished. The machine's Whisper decides the words sent (it also draws the live
    /// preview); if its final pass fails, the last preview it drew is sent instead.
    /// An empty final result means nothing was said, so the preview is not sent then.
    private func utterance(_ url: URL) {
        guard let client, phase != .transcribing else {
            try? FileManager.default.removeItem(at: url)
            return
        }
        engine.paused = true
        phase = .transcribing
        Task {
            let preview = engine.interimTranscript.trimmingCharacters(in: .whitespacesAndNewlines)
            if !preview.isEmpty { engine.freezeTranscript(preview) }
            var heardBack = await words(of: url, from: client)
            guard self.client === client else {
                try? FileManager.default.removeItem(at: url)
                return
            }
            if case .failure = heardBack, !preview.isEmpty { heardBack = .success(preview) }
            try? FileManager.default.removeItem(at: url)
            guard self.client === client else { return }
            engine.finishTranscript()
            engine.paused = false   // listening again before anything else is decided
            switch heardBack {
            case .success(let text) where !text.trimmingCharacters(in: .whitespaces).isEmpty:
                client.send(text, voice: true)
                busy = true
                phase = .waiting
            case .success:
                // Whisper returned nothing: say so rather than dropping silently back to listening.
                phase = .missed
                try? await Task.sleep(for: .seconds(1.2))
                if phase == .missed { phase = .listening }
            case .failure(let error):
                // A failed transcription is not a failed call.
                phase = .failed(error.localizedDescription)
                try? await Task.sleep(for: .seconds(1.8))
                if case .failed = phase { phase = .listening }
            }
        }
    }

    private func words(of url: URL, from client: VisionClient) async -> Result<String, Error> {
        do { return .success(try await client.transcribe(url)) } catch { return .failure(error) }
    }

    private func speakingChanged(_ speaking: Bool) {
        settle?.cancel()
        if speaking {
            phase = .speaking
            return
        }
        guard phase == .speaking else { return }
        // Between two sentences of one reply the speaker falls quiet for a moment; only treat it as
        // the end of her turn if it stays quiet.
        settle = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(600))
            guard !Task.isCancelled, let self, !self.engine.speaking, self.phase == .speaking else { return }
            self.phase = self.busy ? .waiting : .listening
        }
    }

    func busyChanged(_ value: Bool) {
        busy = value
        guard !value else { return }
        if phase == .waiting, !engine.speaking { phase = .listening }
    }
}
