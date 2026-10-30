import AVFoundation
import Combine
import QuartzCore
import os

/// Full-duplex audio for live voice mode.
///
/// The microphone stays open while Vision talks, so cutting in works the way it does with a person.
/// That only holds up because capture runs through iOS's voice-processing IO unit: without its echo
/// cancellation the speaker's own output walks straight back into the utterance detector and Vision
/// interrupts herself.  For the same reason the spoken reply is played through *this* engine rather
/// than a separate `AVAudioPlayer` — the canceller can only subtract audio it knows about.
///
/// What comes out is what the machine already understands: one 16 kHz mono WAV per utterance, cut on
/// silence, for `POST /transcribe`.
final class VoiceEngine: ObservableObject {
    /// 0…1, smoothed the way the CLI's meter is, for the call's waveform.
    @Published private(set) var level: Double = 0
    /// Vision's voice is coming out of the speaker right now.
    @Published private(set) var speaking = false
    /// Your voice is being picked up: from the onset of an utterance until it is cut on silence.
    @Published private(set) var hearing = false
    /// Live words while you talk, from the machine's Whisper.
    @Published private(set) var interimTranscript = ""
    /// Asks the machine's Whisper for the words in a WAV of the utterance so far. When it throws,
    /// the preview stops for the rest of that utterance.
    var machinePreview: ((Data) async throws -> String)?
    private var machinePreviewLoop: Task<Void, Never>?
    /// Pause between the machine answering and the next snapshot going out.
    private static let machinePreviewSeconds = 0.8
    private var previewOpen = false {
        didSet { if !previewOpen { machinePreviewLoop?.cancel() } }
    }
    /// The microphone is off. The engine keeps running: it is still cancelling echo and the
    /// reply still plays, we simply hear nothing.
    @Published var muted = false {
        didSet {
            let value = muted
            if running, echoCancelled { engine.inputNode.isVoiceProcessingInputMuted = value }
            // A cut-in still waiting to be confirmed can't be now: let her carry on.
            if value { resumeHeldSpeech() }
            // Muting straight after a sentence lands inside the silence hangover: the utterance is
            // still open, so finish it now rather than throwing away what you just said.
            let flushed: Data? = state.withLock { capture in
                var utterance: Data?
                if value, capture.speech, !capture.bargeIn, capture.speechSeconds >= Self.minimumSpeechSeconds {
                    utterance = capture.samples
                    capture.diagnostics.utterances += 1
                }
                capture.muted = value
                capture.reset()
                return utterance
            }
            if value { publish(level: 0) }
            if value {
                if flushed == nil { previewOpen = false; interimTranscript = "" }
                hearing = false
            }
            if let flushed {
                DispatchQueue.main.async { [weak self] in
                    guard let self, let url = Self.writeWAV(flushed) else { return }
                    self.onUtterance?(url)
                }
            }
        }
    }

    /// Nothing is captured while this is set: the app is still busy with the previous utterance.
    var paused = false {
        didSet {
            let value = paused
            state.withLock { $0.paused = value; $0.reset() }
        }
    }

    /// A finished utterance: a 16 kHz mono WAV on disk, the caller's to delete. Main queue.
    var onUtterance: ((URL) -> Void)?
    /// You really are talking over Vision: the caller's cue to stop her for good. Main queue.
    ///
    /// Onset alone is not trusted while she talks, because what the canceller leaves of her voice
    /// can pass for yours.  So her voice is paused on onset, and this only fires once you keep going
    /// into the silence that leaves: her echo stops when the speaker does, you don't.  If it goes
    /// quiet instead, it was her, the utterance is dropped and she picks up where she paused.
    var onBargeIn: (() -> Void)?

    /// Counters for the call screen's diagnostics readout: with no way to attach a debugger to the
    /// phone mid-conversation, this is how "it says listening and nothing happens" gets diagnosed.
    struct Diagnostics: Equatable {
        var inputRate = 0.0       // what the input node handed us
        var buffers = 0           // tap callbacks
        var frames = 0            // frames that survived conversion to 16 kHz
        var onsets = 0            // times it decided you had started talking
        var utterances = 0        // utterances handed to the machine
        var tooShort = 0          // utterances thrown away as a cough
        var echoes = 0            // cut-ins that went quiet once she paused: her own voice
        var peak = 0.0            // loudest level since the call started
    }

    @Published private(set) var diagnostics = Diagnostics()

    /// Whether capture is running through the echo canceller.  It goes false only as a fallback,
    /// when the voice-processing unit delivers no audio at all; the call then works half duplex —
    /// no barge-in, and the microphone ignores everything while Vision is talking.
    @Published private(set) var echoCancelled = true

    // MARK: tuning

    /// Loud enough to be speech. The bar is higher while Vision is talking: what the canceller
    /// leaves behind is quiet, but it is not silent.  Higher still for the first moments of her
    /// voice after a silence, while the canceller is still learning the room.
    private static let onThreshold = 0.16, onThresholdOverSpeech = 0.38, onThresholdWarmingUp = 0.46
    private static let offThreshold = 0.13
    /// How long each side of the decision has to hold.
    private static let onsetSeconds = 0.10, hangoverSeconds = 0.75
    /// Over Vision the onset has to be sustained, not a burst: a syllable-sized dip is forgiven.
    private static let onsetOverSpeechSeconds = 0.25, onsetGapSeconds = 0.08
    /// How long the canceller gets to settle after her voice starts from quiet…
    private static let warmUpSeconds = 0.8, warmUpAfterQuietSeconds = 2.0
    /// …and how long the room keeps ringing after it stops.
    private static let echoTailSeconds = 0.35
    /// Once she is paused for a cut-in: the room's ring to ignore, then how much more of you it takes.
    private static let holdSettleSeconds = 0.15, bargeInConfirmSeconds = 0.2
    /// Shorter than this is a cough, a door, or the tail of Vision's own sentence.
    private static let minimumSpeechSeconds = 0.25
    private static let maximumUtteranceSeconds = 30.0
    /// Kept before the trigger so the first word survives.
    private static let preRollSeconds = 0.35

    private static let wavRate = 16000.0
    private static var wavBytesPerSecond: Int { Int(wavRate) * 2 }

    // MARK: engine

    private let engine = AVAudioEngine()
    private let playback = AVAudioPlayerNode()
    /// Where the microphone goes when nobody else wants it (see `start()`).
    private let sink = AVAudioMixerNode()
    private var playbackFormat: AVAudioFormat?
    private var playbackPending = 0
    private var playbackGeneration = 0
    /// Her voice is paused on a cut-in that is not confirmed yet.
    private var held = false
    private var converter: AVAudioConverter?
    private var running = false
    private var tapInstalled = false
    private var observers: [NSObjectProtocol] = []
    enum AudioEvent { case interrupted, interruptionEnded, configurationChanged }
    var onAudioEvent: ((AudioEvent) -> Void)?

    init() {
        let centre = NotificationCenter.default
        observers.append(centre.addObserver(forName: AVAudioSession.interruptionNotification,
                                            object: nil, queue: .main) { [weak self] notification in
            guard let self,
                  let value = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                  let type = AVAudioSession.InterruptionType(rawValue: value) else { return }
            switch type {
            case .began:
                guard self.running else { return }
                self.stop()
                self.onAudioEvent?(.interrupted)
            case .ended:
                self.onAudioEvent?(.interruptionEnded)
            @unknown default:
                break
            }
        })
        observers.append(centre.addObserver(forName: .AVAudioEngineConfigurationChange,
                                            object: engine, queue: .main) { [weak self] _ in
            guard let self, self.running else { return }
            // Enabling voice processing during start can queue a configuration notification.
            // By the time it reaches the main queue the engine is already running normally.
            // A real hardware change stops the engine before posting this notification.
            guard !self.engine.isRunning else { return }
            self.stop()
            self.onAudioEvent?(.configurationChanged)
        })
    }

    deinit {
        for observer in observers { NotificationCenter.default.removeObserver(observer) }
    }

    /// Everything the audio thread touches, behind one lock.
    private struct Capture {
        var muted = false
        var paused = false
        var speakingOut = false
        var speakingSince = 0.0   // when her voice last started from quiet, for the warm-up
        var speakingEnded = 0.0   // when it last stopped, for the echo tail
        var samples = Data()      // the utterance so far, 16-bit LE
        var preRoll = Data()      // the moments before it started
        var speech = false        // inside an utterance
        var loudSeconds = 0.0     // loud time before the utterance starts
        var gapSeconds = 0.0      // quiet time inside that, over her voice
        var quietSeconds = 0.0    // consecutive quiet time since the last loud frame
        var speechSeconds = 0.0   // loud time inside the utterance, for the too-short test
        var bargeIn = false       // this utterance began over her voice and is not confirmed yet
        var heldAt: Double?       // when her voice was paused for it
        var confirmSeconds = 0.0  // loud time since then
        var level = 0.0
        var published = 0.0       // when the meter last reached the main queue
        var halfDuplex = false    // no echo canceller: never listen while Vision talks
        var diagnostics = Diagnostics()

        /// Between utterances. The diagnostics counters are the caller's to carry over.
        mutating func reset() {
            samples.removeAll(keepingCapacity: true)
            preRoll.removeAll(keepingCapacity: true)
            speech = false
            loudSeconds = 0
            gapSeconds = 0
            quietSeconds = 0
            speechSeconds = 0
            bargeIn = false
            heldAt = nil
            confirmSeconds = 0
        }
    }

    private let state = OSAllocatedUnfairLock(initialState: Capture())

    var isRunning: Bool { running }

    // MARK: lifecycle

    static func requestPermission() async -> Bool {
        await AVAudioApplication.requestRecordPermission()
    }

    func start() throws {
        try start(echoCancelling: true)
    }

    /// The microphone produced nothing at all: start over on the plain input unit, which always
    /// does.  Returns false when there was nothing to fall back from, or the retry failed too.
    @discardableResult
    func fallBackToPlainInput() -> Bool {
        guard running, echoCancelled, diagnostics.buffers == 0 else { return false }
        teardown()
        do {
            try start(echoCancelling: false)
            echoCancelled = false
            state.withLock { $0.halfDuplex = true }
            return true
        } catch {
            return false
        }
    }

    private func start(echoCancelling: Bool) throws {
        guard !running else { return }
        let session = AVAudioSession.sharedInstance()
        // `.voiceChat` is what puts the IO unit into its echo-cancelling mode.
        try session.setCategory(.playAndRecord, mode: echoCancelling ? .voiceChat : .spokenAudio,
                                options: [.defaultToSpeaker, .allowBluetooth, .allowBluetoothA2DP])
        try session.setActive(true)

        let input = engine.inputNode
        try input.setVoiceProcessingEnabled(echoCancelling)
        if echoCancelling {
            input.isVoiceProcessingBypassed = false
            input.isVoiceProcessingInputMuted = muted
        }

        if playback.engine == nil { engine.attach(playback) }
        if sink.engine == nil { engine.attach(sink) }
        _ = engine.mainMixerNode   // touching it connects the mixer to the output

        // Read the format only after voice processing is on: enabling it changes the node.
        let hardware = input.outputFormat(forBus: 0)
        guard hardware.sampleRate > 0, let wav = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                                               sampleRate: Self.wavRate,
                                                               channels: 1, interleaved: true),
              let converter = AVAudioConverter(from: hardware, to: wav) else {
            throw VoiceEngineError.noInput
        }
        converter.sampleRateConverterQuality = AVAudioQuality.medium.rawValue
        self.converter = converter

        // The input goes somewhere, at zero volume.  A tap alone is supposed to be enough to make
        // the engine pull from the microphone, but with the voice-processing unit — one device doing
        // input and output together — a render chain that nothing consumes can simply never run, and
        // then the tap is never called and the screen sits there saying "listening".
        engine.connect(input, to: sink, format: hardware)
        sink.outputVolume = 0
        engine.connect(sink, to: engine.mainMixerNode, format: hardware)

        let (isMuted, isPaused) = (muted, paused)
        state.withLock {
            $0.reset()
            $0.muted = isMuted
            $0.paused = isPaused
            $0.halfDuplex = !echoCancelling
            $0.diagnostics = Diagnostics(inputRate: hardware.sampleRate)
        }
        input.installTap(onBus: 0, bufferSize: 1024, format: hardware) { [weak self] buffer, _ in
            self?.capture(buffer, with: converter, to: wav)
        }
        tapInstalled = true
        engine.prepare()
        do {
            try engine.start()
        } catch {
            teardown()
            AudioPlayer.deactivateSession()
            throw error
        }
        running = true
    }

    func stop() {
        guard running || engine.isRunning else { return }
        teardown()
        previewOpen = false
        hearing = false
        interimTranscript = ""
        echoCancelled = true
        setSpeaking(false)
        level = 0
        // Hand the session back to the rest of the app (spoken audio, not a call).
        AudioPlayer.deactivateSession()
    }

    /// Everything but the session: shared by `stop()` and the plain-input retry.
    private func teardown() {
        running = false
        previewOpen = false
        if tapInstalled {
            engine.inputNode.removeTap(onBus: 0)
            tapInstalled = false
        }
        engine.disconnectNodeOutput(sink)
        held = false
        playback.stop()
        engine.stop()
        converter = nil
        playbackFormat = nil
        playbackPending = 0
        state.withLock { $0.reset() }
    }

    // MARK: Vision's voice

    /// A sentence of the reply, as the machine streams it.
    func enqueue(_ wav: Data) {
        guard running, engine.isRunning, let buffer = Self.buffer(from: wav) else { return }
        if playbackFormat != buffer.format {
            if playbackFormat != nil { engine.disconnectNodeOutput(playback) }
            engine.connect(playback, to: engine.mainMixerNode, format: buffer.format)
            playbackFormat = buffer.format
        }
        playbackPending += 1
        setSpeaking(true)
        let generation = playbackGeneration
        playback.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            DispatchQueue.main.async {
                guard let self, generation == self.playbackGeneration else { return }
                self.playbackPending = max(0, self.playbackPending - 1)
                if self.playbackPending == 0 { self.setSpeaking(false) }
            }
        }
        // Paused for a cut-in: the sentence waits in the queue until that is settled.
        if !held, !playback.isPlaying { playback.play() }
    }

    /// Cut Vision off: drop everything queued and anything still sounding.
    func stopSpeaking() {
        held = false
        playbackGeneration += 1
        playbackPending = 0
        playback.stop()
        setSpeaking(false)
    }

    /// A cut-in began over her voice: pause it, queue intact, while it proves itself.
    private func holdSpeech() {
        let now = CACurrentMediaTime()
        let pending = state.withLock { capture -> Bool in
            guard capture.bargeIn else { return false }
            capture.heldAt = now
            capture.speakingOut = false
            return true
        }
        guard pending, !held else { return }
        // Held even in the echo tail with nothing playing, so the next sentence waits too.
        held = true
        if speaking { playback.pause() }
    }

    /// It was her own voice after all: carry on from where she paused.
    private func resumeHeldSpeech() {
        guard held else { return }
        held = false
        guard running, engine.isRunning, speaking else { return }
        state.withLock { $0.speakingOut = true }
        playback.play()
    }

    private func setSpeaking(_ value: Bool) {
        guard speaking != value else { return }
        speaking = value
        let now = CACurrentMediaTime(), heard = !held
        state.withLock { capture in
            capture.speakingOut = value && heard
            if value {
                // Back-to-back sentences keep the canceller trained; only a real silence resets it.
                if now - capture.speakingEnded > Self.warmUpAfterQuietSeconds { capture.speakingSince = now }
            } else {
                capture.speakingEnded = now
            }
        }
        // No Now Playing here: it puts the system's own speaker island up beside the call's Live
        // Activity and squeezes that down to the scout alone, timer gone.  The scout already says it's talking.
    }

    // MARK: capture

    private func capture(_ buffer: AVAudioPCMBuffer, with converter: AVAudioConverter, to format: AVAudioFormat) {
        let ratio = format.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 64
        guard let out = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: capacity) else { return }
        var supplied = false
        var error: NSError?
        _ = converter.convert(to: out, error: &error) { _, status in
            if supplied {
                status.pointee = .noDataNow
                return nil
            }
            supplied = true
            status.pointee = .haveData
            return buffer
        }
        guard error == nil, out.frameLength > 0, let channel = out.int16ChannelData else { return }

        let frames = Int(out.frameLength)
        let seconds = Double(frames) / format.sampleRate
        let chunk = Data(bytes: channel[0], count: frames * 2)

        // RMS in the same dB window the CLI's meter uses, so the bars mean the same thing here.
        var sum = 0.0
        for i in 0..<frames {
            let sample = Double(channel[0][i]) / 32768
            sum += sample * sample
        }
        let rms = (sum / Double(frames)).squareRoot()
        let db = 20 * log10(max(rms, 1e-7))
        let instant = max(0, min(1, (db + 50) / 50))

        // One pass under the lock; what the main queue needs comes back out of it.
        let now = CACurrentMediaTime()
        struct Outcome {
            var level: Double?
            var onset: Data?
            var overSpeech = false   // the onset came over her voice: pause it, don't stop it yet
            var confirmed = false    // …and you kept talking once she paused
            var dismissed = false    // …or it went quiet: that was her
            var ended = false
            var utterance: Data?
        }
        let outcome: Outcome = state.withLock { capture in
            capture.diagnostics.buffers += 1
            capture.diagnostics.frames += frames
            capture.diagnostics.peak = max(capture.diagnostics.peak, instant)
            // Fast attack, slow release: a waveform that settles rather than flickers.
            capture.level = max(instant, capture.level * 0.72)
            // The meter drives a view; twenty updates a second is plenty and keeps SwiftUI quiet.
            var result = Outcome()
            if now - capture.published > 0.05 {
                capture.published = now
                result.level = capture.level
            }
            // Half duplex: with no canceller, everything heard while Vision talks is Vision.
            guard !capture.muted, !capture.paused, !(capture.halfDuplex && capture.speakingOut) else {
                capture.level = 0
                return Outcome(level: 0)
            }
            // Her voice is in the room while it plays and for a moment after it stops.
            let overSpeech = capture.speakingOut || now - capture.speakingEnded < Self.echoTailSeconds
            let threshold: Double
            if !overSpeech {
                threshold = Self.onThreshold
            } else if capture.speakingOut, now - capture.speakingSince < Self.warmUpSeconds {
                threshold = Self.onThresholdWarmingUp
            } else {
                threshold = Self.onThresholdOverSpeech
            }
            let loud = instant > threshold

            if !capture.speech {
                // Waiting: keep a rolling pre-roll so the first word is not clipped off.
                capture.preRoll.append(chunk)
                let cap = Int(Self.preRollSeconds * Double(Self.wavBytesPerSecond))
                if capture.preRoll.count > cap { capture.preRoll.removeFirst(capture.preRoll.count - cap) }
                if loud {
                    capture.loudSeconds += seconds
                    capture.gapSeconds = 0
                } else {
                    capture.gapSeconds += seconds
                    if !overSpeech || capture.gapSeconds > Self.onsetGapSeconds { capture.loudSeconds = 0 }
                }
                let needed = overSpeech ? Self.onsetOverSpeechSeconds : Self.onsetSeconds
                guard capture.loudSeconds >= needed else { return result }
                capture.speech = true
                capture.samples = capture.preRoll
                capture.preRoll.removeAll(keepingCapacity: true)
                capture.speechSeconds = capture.loudSeconds
                capture.quietSeconds = 0
                capture.bargeIn = overSpeech && !capture.halfDuplex
                capture.diagnostics.onsets += 1
                result.onset = capture.samples
                result.overSpeech = capture.bargeIn
                return result
            }

            capture.samples.append(chunk)
            if loud {
                capture.speechSeconds += seconds
                capture.quietSeconds = 0
            } else {
                capture.quietSeconds += seconds
            }
            // Paused for a cut-in: past the room's ring, only you can still be making noise.
            if capture.bargeIn, let heldAt = capture.heldAt, now - heldAt > Self.holdSettleSeconds, loud {
                capture.confirmSeconds += seconds
                if capture.confirmSeconds >= Self.bargeInConfirmSeconds {
                    capture.bargeIn = false
                    result.confirmed = true
                }
            }
            let duration = Double(capture.samples.count) / Double(Self.wavBytesPerSecond)
            let ended = capture.quietSeconds >= Self.hangoverSeconds || duration >= Self.maximumUtteranceSeconds
            guard ended else { return result }
            result.ended = true
            if capture.bargeIn {
                // Never confirmed: her own voice, not worth a transcription.
                result.dismissed = true
                capture.diagnostics.echoes += 1
            } else if capture.speechSeconds >= Self.minimumSpeechSeconds {
                result.utterance = capture.samples
                capture.diagnostics.utterances += 1
            } else {
                capture.diagnostics.tooShort += 1
            }
            let carried = capture.diagnostics
            capture.reset()
            capture.diagnostics = carried
            return result
        }

        if outcome.ended && outcome.utterance == nil {
            DispatchQueue.main.async { [weak self] in
                self?.previewOpen = false
                self?.interimTranscript = ""
            }
        }

        if let meter = outcome.level {
            publish(level: meter)
            let snapshot = state.withLock { $0.diagnostics }
            DispatchQueue.main.async { [weak self] in
                guard let self, self.diagnostics != snapshot else { return }
                self.diagnostics = snapshot
            }
        }
        if outcome.onset != nil {
            DispatchQueue.main.async { [weak self] in
                guard let self, self.running else { return }
                self.previewOpen = true
                self.hearing = true
                self.interimTranscript = ""
                self.startMachinePreview()
                if outcome.overSpeech { self.holdSpeech() }
            }
        }
        if outcome.confirmed {
            DispatchQueue.main.async { [weak self] in
                guard let self, self.running else { return }
                self.onBargeIn?()
                // If that didn't stop her, nothing more was going to: let her go on.
                self.resumeHeldSpeech()
            }
        }
        if outcome.dismissed {
            DispatchQueue.main.async { [weak self] in
                guard let self, self.running else { return }
                self.resumeHeldSpeech()
            }
        }
        if outcome.ended {
            DispatchQueue.main.async { [weak self] in
                if self?.hearing == true { self?.hearing = false }
            }
        }
        if let utterance = outcome.utterance {
            DispatchQueue.main.async { [weak self] in
                guard let self, let url = Self.writeWAV(utterance) else { return }
                self.onUtterance?(url)
            }
        }
    }

    /// While the utterance is open, send the machine what has been said so far and show its words.
    /// One request at a time: a slow answer means fewer snapshots, never a queue of them.
    private func startMachinePreview() {
        machinePreviewLoop?.cancel()
        guard let ask = machinePreview else { return }
        machinePreviewLoop = Task { @MainActor [weak self] in
            var sent = 0
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(Self.machinePreviewSeconds))
                guard let self, self.previewOpen, !Task.isCancelled else { return }
                let samples = self.state.withLock { $0.speech ? $0.samples : Data() }
                guard samples.count > sent else { continue }
                sent = samples.count
                do {
                    let words = try await ask(Self.wavData(samples))
                    guard self.previewOpen, !Task.isCancelled else { return }
                    if !words.isEmpty, self.interimTranscript != words { self.interimTranscript = words }
                } catch {
                    return
                }
            }
        }
    }

    /// Silence is one update, not forty a second: an unchanged meter never wakes the view.
    private func publish(level value: Double) {
        DispatchQueue.main.async { [weak self] in
            guard let self, self.level != value else { return }
            self.level = value
        }
    }

    func finishTranscript() {
        previewOpen = false
        interimTranscript = ""
    }

    func freezeTranscript(_ text: String) {
        previewOpen = false
        interimTranscript = text
    }

    // MARK: WAV

    private static func writeWAV(_ samples: Data) -> URL? {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("vision-voice-\(UUID().uuidString).wav")
        guard (try? wavData(samples).write(to: url)) != nil else { return nil }
        return url
    }

    private static func wavData(_ samples: Data) -> Data {
        var file = Data()
        let rate = UInt32(wavRate)
        let bytes = UInt32(samples.count)
        func append<T: FixedWidthInteger>(_ value: T) { withUnsafeBytes(of: value.littleEndian) { file.append(contentsOf: $0) } }
        file.append(contentsOf: Array("RIFF".utf8))
        append(UInt32(36 + bytes))
        file.append(contentsOf: Array("WAVEfmt ".utf8))
        append(UInt32(16))            // PCM header length
        append(UInt16(1))             // PCM
        append(UInt16(1))             // mono
        append(rate)
        append(rate * 2)              // byte rate
        append(UInt16(2))             // block align
        append(UInt16(16))            // bits
        file.append(contentsOf: Array("data".utf8))
        append(bytes)
        file.append(samples)
        return file
    }

    /// The machine's TTS sends whole WAV sentences; `AVAudioFile` reads whatever rate and depth they use.
    private static func buffer(from wav: Data) -> AVAudioPCMBuffer? {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("vision-tts-\(UUID().uuidString).wav")
        defer { try? FileManager.default.removeItem(at: url) }
        guard (try? wav.write(to: url)) != nil, let file = try? AVAudioFile(forReading: url) else { return nil }
        let frames = AVAudioFrameCount(file.length)
        guard frames > 0, let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: frames),
              (try? file.read(into: buffer)) != nil else { return nil }
        return buffer
    }
}

enum VoiceEngineError: LocalizedError {
    case noInput

    var errorDescription: String? { "The microphone isn't available." }
}
