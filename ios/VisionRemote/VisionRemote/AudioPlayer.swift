import AVFoundation
import Combine

/// Plays the WAV sentences the machine streams, strictly in arrival order, with no gap handling needed
/// (the machine already pads each sentence).  It can also say a piece in the phone's own voice, in the
/// same queue: a reply read aloud while the computer's voice is off.
final class AudioPlayer: NSObject, ObservableObject, AVAudioPlayerDelegate, AVSpeechSynthesizerDelegate {
    @Published private(set) var isPlaying = false
    /// More pieces are on their way (a reply being read aloud fetches each while the one before plays):
    /// running dry meanwhile keeps the session and the Live Activity up instead of ending them.
    var holdOpen = false {
        didSet { if oldValue && !holdOpen && isPlaying && idle && queue.isEmpty { playNext() } }
    }
    /// Told when playback is stopped from outside (another chat, a call, the Live Activity's stop), not
    /// when it simply runs out.
    var stopped: (() -> Void)?

    private enum Piece { case wav(Data), words(String) }
    private var queue: [Piece] = []
    private var player: AVAudioPlayer?
    private let synth = AVSpeechSynthesizer()
    private var saying: AVSpeechUtterance?   // the piece the phone's own voice is on
    private var finishWork: DispatchWorkItem?
    private var sessionActive = false
    private var interruptionObserver: NSObjectProtocol?

    private var idle: Bool { player == nil && saying == nil }

    override init() {
        super.init()
        synth.delegate = self
        interruptionObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification, object: nil, queue: .main
        ) { [weak self] notification in
            guard let value = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                  AVAudioSession.InterruptionType(rawValue: value) == .began else { return }
            self?.stop()
        }
    }

    deinit {
        if let interruptionObserver { NotificationCenter.default.removeObserver(interruptionObserver) }
    }

    /// Activate only while speech is playing; simply opening Vision must leave other audio alone.
    private func configureSession() throws {
        guard !sessionActive else { return }
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playback, mode: .spokenAudio, options: [.mixWithOthers])
        try session.setActive(true)
        sessionActive = true
    }

    func enqueue(_ wav: Data) {
        add(.wav(wav))
    }

    /// Say `text` in the phone's own voice, after whatever is queued already.
    func say(_ text: String) {
        add(.words(text))
    }

    private func add(_ piece: Piece) {
        finishWork?.cancel()
        finishWork = nil
        queue.append(piece)
        if idle {
            playNext()
        }
    }

    func stop() {
        finish()
        stopped?()
    }

    private func finish() {
        let wasActive = sessionActive
        queue.removeAll()
        player?.stop()
        player = nil
        saying = nil
        synth.stopSpeaking(at: .immediate)
        isPlaying = false
        holdOpen = false   // after isPlaying: nothing is left to wind down
        finishWork?.cancel()
        finishWork = nil
        sessionActive = false
        if wasActive {
            MainActor.assumeIsolated { SpeechActivity.shared.stop() }
            Self.deactivateSession()
        }
    }

    static func deactivateSession() {
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    /// The phone's best voice for the language it is set to: premium, then enhanced (both downloaded in
    /// Settings › Accessibility › Spoken Content), then the standard one; the phone's own region first,
    /// never a novelty or Personal Voice.
    static func phoneVoice() -> AVSpeechSynthesisVoice? {
        let language = AVSpeechSynthesisVoice.currentLanguageCode()
        let base = String(language.prefix(while: { $0 != "-" }))
        let voices = AVSpeechSynthesisVoice.speechVoices().filter {
            $0.language.hasPrefix(base) && !$0.voiceTraits.contains(.isNoveltyVoice) && !$0.voiceTraits.contains(.isPersonalVoice)
        }
        let score = { (v: AVSpeechSynthesisVoice) in v.quality.rawValue * 2 + (v.language == language ? 1 : 0) }
        return voices.max { score($0) < score($1) } ?? AVSpeechSynthesisVoice(language: language)
    }

    private func playNext() {
        guard !queue.isEmpty else {
            player = nil
            saying = nil
            if !isPlaying { finish(); return }
            if holdOpen { return }   // the next piece is still being made: `holdOpen` going false comes back here
            // Consecutive sentences often arrive a moment apart. Keep the session and system
            // controls alive across that gap, including when the screen is locked.
            let work = DispatchWorkItem { [weak self] in
                guard let self, self.idle, self.queue.isEmpty, !self.holdOpen else { return }
                self.finishWork = nil
                self.finish()
            }
            finishWork = work
            DispatchQueue.main.asyncAfter(deadline: .now() + 1, execute: work)
            return
        }
        let piece = queue.removeFirst()
        do {
            try configureSession()
            switch piece {
            case .wav(let data):
                let p = try AVAudioPlayer(data: data, fileTypeHint: AVFileType.wav.rawValue)
                p.delegate = self
                player = p
                guard p.play() else {
                    player = nil
                    playNext()
                    return
                }
            case .words(let text):
                let utterance = AVSpeechUtterance(string: text)
                utterance.voice = Self.phoneVoice()
                saying = utterance
                synth.speak(utterance)
            }
            isPlaying = true
            MainActor.assumeIsolated { SpeechActivity.shared.start() }
        } catch {
            print("audio chunk: \(error)")
            playNext()
        }
    }

    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        DispatchQueue.main.async { if self.player === player { self.playNext() } }
    }

    func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        DispatchQueue.main.async { if self.player === player { self.playNext() } }
    }

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        DispatchQueue.main.async { if self.saying === utterance { self.playNext() } }
    }
}

/// The speaker button under a finished reply: reads it aloud in Vision's voice while the computer has
/// it on, otherwise (or once the computer's voice fails part way) in the phone's own. One reply at a
/// time; tapping the one being read stops it, and so does anything else that stops the player.
/// The machine cuts the reply into speakable pieces (POST /speak/pieces: no code, no markdown) and
/// makes the WAV for each (POST /speak) while the one before plays.
@MainActor
final class ReadAloud: ObservableObject {
    /// The reply being read, by its text: the bubble's id changes when the history is fetched again.
    @Published private(set) var reading: String?
    private weak var client: VisionClient?
    private var task: Task<Void, Never>?
    private var fetching = false
    private var watch: AnyCancellable?

    func attach(_ client: VisionClient) {
        self.client = client
        client.player.stopped = { [weak self] in MainActor.assumeIsolated { self?.interrupted() } }
        watch = client.player.$isPlaying.dropFirst().sink { [weak self] playing in
            MainActor.assumeIsolated {
                guard let self, !playing, !self.fetching else { return }
                self.reading = nil   // every piece has been said
            }
        }
    }

    func toggle(_ text: String) {
        guard let client else { return }
        let wasThis = reading == text
        client.player.stop()   // this reply or another one: either way it stops here
        guard !wasThis else { return }
        reading = text
        fetching = true
        let player = client.player
        task = Task { [weak self] in
            let pieces: [String]
            do { pieces = try await client.readAloudPieces(text) } catch {
                guard !Task.isCancelled else { return }
                self?.reading = nil
                self?.fetching = false
                client.toast = "Couldn't read that aloud"
                return
            }
            var ownVoice = !client.voiceOn
            player.holdOpen = true
            for piece in pieces {
                guard !Task.isCancelled else { return }
                if !ownVoice {
                    do {
                        let wav = try await client.speakWav(piece)
                        guard !Task.isCancelled else { return }
                        player.enqueue(wav)
                        continue
                    } catch {
                        guard !Task.isCancelled else { return }
                        ownVoice = true   // the voice is off, loading or busy: the phone's own carries on
                    }
                }
                player.say(piece)
            }
            guard let self, !Task.isCancelled else { return }
            self.fetching = false
            player.holdOpen = false
            if !player.isPlaying { self.reading = nil }
        }
    }

    var isReading: Bool { reading != nil }

    private func interrupted() {
        task?.cancel()
        task = nil
        fetching = false
        reading = nil
    }
}

/// A spoken reply outside a call shows as Vision's own Live Activity ("Speaking", with a stop
/// button), not as a system Now Playing player.  Its stop lands in `CallRemote.stopSpeaking`.
@MainActor
final class SpeechActivity {
    static let shared = SpeechActivity()

    private let activity = CallLiveActivity()

    func start() {
        activity.start(.init(mood: .speaking, caption: "Speaking", muted: false), call: false)
    }

    func stop() {
        activity.end()
    }
}
