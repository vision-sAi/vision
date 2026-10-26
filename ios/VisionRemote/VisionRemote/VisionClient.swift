import AVFoundation
import Combine
import Foundation
import UIKit
import UserNotifications
import WidgetKit

/// A Vision this phone has paired with. Its access key lives in the keychain under "token.<id>".
struct SavedConnection: Codable, Identifiable, Equatable {
    let id: UUID
    var url: String
    var name: String
    /// Its address on the computer's Wi-Fi, when `url` is another (Tailscale, a tunnel): tried when `url`
    /// doesn't answer, say at home with Tailscale off. From the pairing code; nil for older ones.
    var local: String?
}

/// The phone's end of `vision serve`: one WebSocket for the conversation, plain HTTPS for speech and lists.
@MainActor
final class VisionClient: ObservableObject {
    /// The app's one client, for AppDelegate's notification callbacks (they arrive outside SwiftUI).
    static weak var shared: VisionClient?

    enum Connection: Equatable {
        case offline, connecting, online
        case failed(String)
    }

    // MARK: settings (persisted)
    @Published var serverURL: String { didSet { UserDefaults.standard.set(serverURL, forKey: "serverURL"); sharePairing() } }
    @Published var token: String { didSet { Keychain.set(token, for: "token"); sharePairing() } }
    /// Every Vision paired so far, in the order they were added; the one in use is among them.
    @Published private(set) var savedConnections: [SavedConnection] = [] {
        didSet {
            UserDefaults.standard.set(try? JSONEncoder().encode(savedConnections), forKey: "savedConnections")
            sharePairing()   // the Wi-Fi address may have come or gone
        }
    }

    // MARK: live state
    @Published private(set) var connection: Connection = .offline {
        didSet { followConnection() }
    }
    /// Why Vision can't be reached, worked out while the app keeps trying: nil while it connects normally.
    /// The socket alone can't say (a phone on the wrong Wi-Fi waits out its 60 s timeout, over and over).
    @Published private(set) var trouble: String?
    /// Every chat the machine has open, oldest first; each runs on its own.
    @Published private(set) var chats: [ChatInfo] = []
    /// The machine's projects, latest activity first (none from a Vision older than projects).
    @Published private(set) var projects: [ProjectInfo] = []
    /// Each project's threads, oldest first, by project id: fetched when its chat opens, kept live by `thread` frames.
    @Published private(set) var threads: [String: [ProjectThread]] = [:]
    /// Projects whose whole thread list has been fetched since the socket last came up; for the rest, a
    /// `thread` frame alone is only part of the list, so their counts come from the project.
    private var threadsLoaded: Set<String> = []
    /// A project thread to put on screen once its project's chat is (a tapped notification): ThreadStrip opens
    /// the project with that thread on top, then clears it.
    @Published var shownThread: ShownThread?
    struct ShownThread: Equatable {
        let project: String
        let thread: String
    }
    @Published private(set) var transcripts: [String: [Message]] = [:]
    @Published private(set) var activeChatID: String? { didSet { UserDefaults.standard.set(activeChatID, forKey: "activeChatID") } }
    @Published var pendingQuestions: QuestionForm?
    /// Subscription usage, fetched when the app connects and every `usageInterval` after while it is open,
    /// so the Usage sheet opens on it straight away.
    @Published private(set) var usage: UsageReport?
    @Published private(set) var usageFetchedAt: Date?
    @Published private(set) var usageRefreshing = false
    @Published private(set) var usageError: String?
    static let usageInterval: TimeInterval = 5 * 60
    private var usageTask: Task<Void, Never>?
    @Published var toast: String?
    /// What this app needs from the server and the server from it, as one number: the server's
    /// PROTOCOL (vision/server.py). Raise both in the change that makes one side need something new.
    static let protocolVersion = 1
    /// Whether the app and the Vision it is connected to have drifted apart (Settings shows it).
    @Published private(set) var staleness: Staleness?
    private var serverProtocol: Int?   // hello's "protocol"; nil from a Vision before the number
    /// The computer's Vision runs shell-mode commands (hello's "shell"): the composer offers `!` only then.
    @Published var canShell = false
    /// The computer's Vision cuts a reply up to read aloud (hello's "read_aloud"): the speaker button shows only then.
    @Published var canReadAloud = false
    /// The voice switch on the computer ([voice] enabled): off, nothing is loaded on its GPU for speech, so calls
    /// and spoken replies wait. `canSwitchVoice`: the computer's Vision has the switch (hello's "voice_on").
    @Published var voiceOn = true
    @Published var canSwitchVoice = false
    @Published var providers: [ProviderInfo] = []   // GET /providers, taken on connect; ProviderStyle draws from it
    @Published var editDraft: EditDraft?   // a queued message taken back to edit: the composer picks it up
    /// The model and effort picked with no chat open: for the next chat alone, sent with its `new`.
    /// The saved default (Settings) is left alone.
    @Published private(set) var nextModel: String?
    @Published private(set) var nextEffort: String?
    private var questionsByChat: [String: QuestionForm] = [:]
    /// Chats closed while this app ran: a late frame about one (its reply still winding down) can't bring it back.
    private var closedChats: Set<String> = []
    private var wantsNewChat = false
    /// The `new` on its way went to the remembered folder rather than one just picked: if the machine
    /// says it's gone, the folder is forgotten and the chat opens in the machine's own instead.
    private var newChatInLastFolder = false
    /// The folder just picked for the `new` on its way: remembered once the machine has opened the chat there.
    private var pickedFolder: String?
    /// A message typed with no chat open: sent into the chat the machine opens for it.
    private var pendingSend: (text: String, voice: Bool, images: [String], thumbnails: [UIImage])?
    /// Uploaded photos fetched back from the machine, by their path there.
    private var uploadCache = NSCache<NSString, UIImage>()
    /// Pictures replies show, fetched from the machine by their path there.
    private var replyImageCache = NSCache<NSString, UIImage>()
    /// Those pictures' sizes, kept across launches though the cache above only fits a few screenshots: a bubble
    /// holds its picture's exact room before it arrives, so a late picture never shoves the transcript.
    private var replyImageSizes: [String: CGSize] = [:]
    private var thumbCache = NSCache<NSString, UIImage>()
    private var loadingImages: Set<UUID> = []
    /// Bumped when the machine says the scheduled tasks changed (or one ran), so an open list refetches.
    @Published private(set) var scheduledVersion = 0

    // The chat on screen.
    var active: ChatInfo? { chats.first { $0.id == activeChatID } ?? chats.last }
    var messages: [Message] { active.flatMap { transcripts[$0.id] } ?? [] }
    var busy: Bool { active?.busy ?? false }
    var modelLabel: String { active?.model ?? "" }
    /// How full the active chat's context is, once its brain has reported a turn.
    var context: (tokens: Int, window: Int)? {
        guard let chat = active, let tokens = chat.contextTokens, let window = chat.contextWindow else { return nil }
        return (tokens, window)
    }
    var effort: String { active?.effort ?? "" }
    /// "auto" or "plan" for the chat on screen; "" with no chat open, or from an older Vision.
    var mode: String { active?.mode ?? "" }
    var sessionID: String? { active?.sessionID }
    var chatTitle: String { active.map { $0.title.isEmpty ? "New chat" : $0.title } ?? "Vision" }
    /// The project whose chat is on screen, if it is one's.
    var activeProject: ProjectInfo? {
        guard let id = active?.project, !id.isEmpty else { return nil }
        return projects.first { $0.id == id }
    }

    let player = AudioPlayer()
    /// The speaker button under a finished reply.
    let readAloud = ReadAloud()
    /// The live call owns speech playback while it is open.
    var stopCallSpeech: (() -> Void)?
    /// Live voice mode takes the spoken reply while it is up, so it can play through the engine that
    /// is cancelling the echo.  Nil the rest of the time: then `player` has it.
    var audioSink: ((Data) -> Void)?
    /// A live voice call is running in the chat on screen: typed messages go to its model.
    /// A typed turn queued behind another reply stays silent.
    @Published var inCall = false

    private let session = URLSession(configuration: .default)
    private var socket: URLSessionWebSocketTask?
    private var wantConnection = false
    private var reconnectAttempt = 0
    private var reconnectTask: Task<Void, Never>?
    private var diagnoseTask: Task<Void, Never>?
    private var pingTask: Task<Void, Never>?

    init() {
        serverURL = UserDefaults.standard.string(forKey: "serverURL") ?? ""
        // VISION_TOKEN in the environment pairs a simulator run without the QR (development only).
        token = Keychain.get("token") ?? ProcessInfo.processInfo.environment["VISION_TOKEN"] ?? ""
        activeChatID = UserDefaults.standard.string(forKey: "activeChatID")
        if let data = UserDefaults.standard.data(forKey: "savedConnections"),
           let saved = try? JSONDecoder().decode([SavedConnection].self, from: data) {
            savedConnections = saved
        }
        if let sizes = UserDefaults.standard.dictionary(forKey: "replyImageSizes") as? [String: [Double]] {
            replyImageSizes = sizes.compactMapValues { $0.count == 2 ? CGSize(width: $0[0], height: $0[1]) : nil }
        }
        rememberCurrent()   // a phone paired before the saved list existed keeps its one
        sharePairing()      // a phone paired before the widgets existed hands them its pairing now
        VisionClient.shared = self
        readAloud.attach(self)
        #if DEBUG
        // Development: `--pair '{"url": …, "token": …}'` on launch pairs as a scanned code would (UI tests).
        if let i = CommandLine.arguments.firstIndex(of: "--pair"), i + 1 < CommandLine.arguments.count {
            pair(with: CommandLine.arguments[i + 1])
        }
        #endif
        ThreadsLiveActivity.shared.watch { [weak self] kind, token in self?.registerActivity(kind: kind, token: token) }
        CallRemote.stopSpeaking = { [weak self] in   // the spoken reply's stop, in the Dynamic Island
            if let stopCallSpeech = self?.stopCallSpeech { stopCallSpeech() }
            else if self?.readAloud.isReading == true { self?.player.stop() }   // a reply read back: no turn to cancel
            else { self?.cancel() }
        }
        for cache in [uploadCache, replyImageCache, thumbCache] {
            cache.countLimit = 30
            cache.totalCostLimit = 96 * 1024 * 1024
        }
    }

    private func cacheCost(_ image: UIImage) -> Int {
        guard let bitmap = image.cgImage else { return 1024 * 1024 }
        return bitmap.bytesPerRow * bitmap.height
    }

    var isConfigured: Bool { baseURL != nil && !token.isEmpty }

    var baseURL: URL? { Self.normalize(serverURL).flatMap(URL.init(string:)) }

    /// The address actually in use when it isn't `baseURL`: the saved Wi-Fi address that answered when the
    /// main one didn't (see followConnection). Kept until that stops answering in turn, or another Vision.
    private var route: URL?
    private var activeBase: URL? { route ?? baseURL }

    /// The Home Screen widgets ask the computer themselves (Glance.swift): they get the addresses and the key
    /// from here, through the keychain group the app shares with its extensions.
    private func sharePairing() {
        SharedPairing.save(isConfigured ? glancePairing : nil)
    }

    var glancePairing: SharedPairing.Pairing { .init(addresses: addresses.map(\.absoluteString), token: token) }

    /// The addresses this Vision answers on: the main one, then its Wi-Fi one if the pairing code had it.
    private var addresses: [URL] {
        let local = currentConnection?.local.flatMap(Self.normalize).flatMap(URL.init(string:))
        return [baseURL, local].compactMap { $0 }.reduce(into: []) { if !$0.contains($1) { $0.append($1) } }
    }

    private static func normalize(_ address: String) -> String? {
        var s = address.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty else { return nil }
        if !s.contains("://") { s = "https://" + s }
        while s.hasSuffix("/") { s.removeLast() }
        return s
    }

    private static func sameServer(_ a: String, _ b: String) -> Bool {
        guard let a = normalize(a), let b = normalize(b) else { return false }
        return a.lowercased() == b.lowercased()
    }

    // MARK: - saved connections

    /// The saved entry for the address in use.
    var currentConnection: SavedConnection? {
        savedConnections.first { Self.sameServer($0.url, serverURL) }
    }

    /// Keep the address in use in the saved list: a new one is added, a known one takes the new key.
    /// `name` is what the computer calls itself (the pairing code carries it); a new entry is listed
    /// under it, and a known one takes it only while it still shows its bare address.
    private func rememberCurrent(name: String? = nil) {
        guard isConfigured else { return }
        let host = baseURL?.host() ?? serverURL
        let name = name?.trimmingCharacters(in: .whitespacesAndNewlines)
        let given = (name?.isEmpty ?? true) ? nil : name
        if let i = savedConnections.firstIndex(where: { Self.sameServer($0.url, serverURL) }) {
            if let given, savedConnections[i].name == (URL(string: savedConnections[i].url)?.host() ?? savedConnections[i].url) {
                savedConnections[i].name = given
            }
            savedConnections[i].url = serverURL
            Keychain.set(token, for: "token.\(savedConnections[i].id)")
        } else {
            let saved = SavedConnection(id: UUID(), url: serverURL, name: given ?? host)
            Keychain.set(token, for: "token.\(saved.id)")
            savedConnections.append(saved)
        }
    }

    /// Point the app at an address and key, saving them. Another Vision's chats replace this one's.
    func use(url: String, token newToken: String, name: String? = nil) {
        let url = url.trimmingCharacters(in: .whitespacesAndNewlines)
        let otherServer = !Self.sameServer(url, serverURL)
        if otherServer { route = nil }
        guard otherServer || newToken != token else {
            rememberCurrent(name: name)
            if connection != .online { connect() }
            return
        }
        if otherServer { unregisterPush(from: activeBase, bearer: token) }
        disconnect()
        serverURL = url
        token = newToken
        if otherServer { clearServerState() }
        rememberCurrent(name: name)
        connect()
    }

    /// Add an address and key to the saved list without switching to it; with nothing paired yet it is used straight away.
    func save(url: String, token newToken: String) {
        let url = url.trimmingCharacters(in: .whitespacesAndNewlines)
        guard isConfigured, !Self.sameServer(url, serverURL) else { use(url: url, token: newToken); return }
        if let i = savedConnections.firstIndex(where: { Self.sameServer($0.url, url) }) {
            savedConnections[i].url = url
            Keychain.set(newToken, for: "token.\(savedConnections[i].id)")
        } else {
            let saved = SavedConnection(id: UUID(), url: url, name: URL(string: url)?.host() ?? url)
            Keychain.set(newToken, for: "token.\(saved.id)")
            savedConnections.append(saved)
        }
    }

    func switchTo(_ saved: SavedConnection) {
        guard saved.id != currentConnection?.id else { return }
        use(url: saved.url, token: Keychain.get("token.\(saved.id)") ?? "")
    }

    func rename(_ saved: SavedConnection, to name: String) {
        guard let i = savedConnections.firstIndex(where: { $0.id == saved.id }) else { return }
        let name = name.trimmingCharacters(in: .whitespacesAndNewlines)
        savedConnections[i].name = name.isEmpty ? (URL(string: saved.url)?.host() ?? saved.url) : name
    }

    /// Delete a saved Vision; the one in use is unpaired.
    func remove(_ saved: SavedConnection) {
        if saved.id == currentConnection?.id { forget(); return }
        unregisterPush(from: Self.normalize(saved.url).flatMap(URL.init(string:)), bearer: Keychain.get("token.\(saved.id)") ?? "")
        Keychain.delete("token.\(saved.id)")
        savedConnections.removeAll { $0.id == saved.id }
    }

    /// Everything that belongs to the Vision in use, dropped when the app points somewhere else.
    private func clearServerState() {
        chats.removeAll()
        transcripts.removeAll()
        activeChatID = nil
        questionsByChat.removeAll()
        pendingQuestions = nil
        nextModel = nil
        nextEffort = nil
        usage = nil
        usageFetchedAt = nil
        usageError = nil
        providers = []
        for cache in [uploadCache, replyImageCache, thumbCache] { cache.removeAllObjects() }
        replyImageSizes.removeAll()
        UserDefaults.standard.removeObject(forKey: "replyImageSizes")
    }

    /// Apply a scanned pairing code: {"url": "...", "token": "...", "name": "..."} as printed by
    /// `vision serve` (the name, the computer's own, is missing from an older Vision's code).
    @discardableResult
    func pair(with payload: String) -> Bool {
        guard let data = payload.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let url = obj["url"] as? String, let tok = obj["token"] as? String, !url.isEmpty, !tok.isEmpty
        else { return false }
        use(url: url, token: tok, name: obj["name"] as? String)
        // The Wi-Fi address it also answers on (a newer `vision serve`, published through Tailscale or a tunnel).
        let local = (obj["local"] as? String).flatMap { $0.isEmpty || Self.sameServer($0, url) ? nil : $0 }
        if let i = savedConnections.firstIndex(where: { Self.sameServer($0.url, url) }), savedConnections[i].local != local {
            savedConnections[i].local = local
            route = nil
        }
        return true
    }

    // MARK: - connection

    func connect() {
        guard isConfigured else { return }
        wantConnection = true
        reconnectAttempt = 0
        guard socket == nil else { return }
        openSocket()
    }

    /// Close the socket cleanly when iOS is about to suspend us, so the
    /// inevitable teardown isn't mistaken for a dropped connection.
    /// `wantConnection` stays true; `connect()` picks up again on `.active`.
    func suspend() {
        reconnectTask?.cancel()
        pingTask?.cancel()
        usageTask?.cancel()
        socket?.cancel(with: .goingAway, reason: nil)
        socket = nil
        connection = .offline
    }

    /// Unpair: drop the connection and forget its address and token. The chats live on the machine,
    /// so pairing again brings them back.
    func forget() {
        unregisterPush(from: activeBase, bearer: token)
        disconnect()
        route = nil
        if let saved = currentConnection {
            Keychain.delete("token.\(saved.id)")
            savedConnections.removeAll { $0.id == saved.id }
        }
        serverURL = ""
        token = ""
        clearServerState()
    }

    func disconnect() {
        wantConnection = false
        reconnectTask?.cancel()
        pingTask?.cancel()
        usageTask?.cancel()
        socket?.cancel(with: .normalClosure, reason: nil)
        socket = nil
        pendingSend = nil
        connection = .offline
    }

    private var socketURL: URL? {
        guard let base = activeBase, var parts = URLComponents(url: base, resolvingAgainstBaseURL: false) else { return nil }
        parts.scheme = base.scheme == "http" ? "ws" : "wss"
        parts.path = base.path + "/ws"
        return parts.url
    }

    private func openSocket() {
        guard let url = socketURL else { return }
        connection = .connecting
        var request = URLRequest(url: url)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 60
        let task = session.webSocketTask(with: request)
        socket = task
        task.resume()
        receive(on: task)
        startPinging(task)
    }

    private func receive(on task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            Task { @MainActor in
                guard let self, self.socket === task else { return }
                switch result {
                case .success(let message):
                    if case .string(let text) = message, let data = text.data(using: .utf8),
                       let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                        self.handle(obj)
                    }
                    self.receive(on: task)
                case .failure(let error):
                    self.socketDropped(task, error)
                }
            }
        }
    }

    private func startPinging(_ task: URLSessionWebSocketTask) {
        pingTask?.cancel()
        pingTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(20))
                guard let self, self.socket === task else { return }
                task.sendPing { error in
                    if let error {
                        Task { @MainActor in self.socketDropped(task, error) }
                    }
                }
            }
        }
    }

    private func socketDropped(_ task: URLSessionWebSocketTask, _ error: Error) {
        guard socket === task else { return }
        socket = nil
        pingTask?.cancel()
        for chat in chats where chat.busy {
            withStreaming(chat.id) { $0.status = "connection lost — Vision carries on; reconnecting…" }
        }
        let reason = task.closeReason.map { String(decoding: $0, as: UTF8.self) }
        if task.closeCode.rawValue == 4401 || reason == "bad token" {
            connection = .failed("The access key was rejected — pair again in Settings")
            wantConnection = false
            return
        }
        connection = wantConnection ? .connecting : .offline
        scheduleReconnect()
    }

    /// Still connecting after a few seconds: look at the server directly, with a short timeout, and say why
    /// it isn't answering; again every little while until it connects. Anything but connecting ends it.
    private func followConnection() {
        guard connection == .connecting else {
            diagnoseTask?.cancel()
            diagnoseTask = nil
            trouble = nil
            return
        }
        guard diagnoseTask == nil else { return }
        diagnoseTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(8))
            while !Task.isCancelled {
                guard let self, self.connection == .connecting else { return }
                var why = await self.reachProblem(self.activeBase)
                if why != nil, let other = await self.answeringAddress() {
                    self.reroute(to: other)   // e.g. home with Tailscale off: its Wi-Fi address answers
                    why = nil
                }
                guard !Task.isCancelled, self.connection == .connecting else { return }
                self.trouble = why
                try? await Task.sleep(for: .seconds(15))
            }
        }
    }

    /// One quick request to the server at `base`: nil if it answers (the socket is about to make it), else what's wrong.
    private func reachProblem(_ base: URL?) async -> String? {
        guard var req = try? request("health", base: base) else { return nil }
        req.timeoutInterval = 6
        do {
            _ = try await perform(req)
            return nil
        } catch {
            return Self.reachAdvice(error, host: base?.host())
        }
    }

    /// Another of this Vision's addresses that answers right now, if any.
    private func answeringAddress() async -> URL? {
        for address in addresses where address != activeBase {
            if await reachProblem(address) == nil { return address }
        }
        return nil
    }

    /// Talk to this Vision at `address` from now on, starting a fresh socket there straight away.
    private func reroute(to address: URL) {
        route = address == baseURL ? nil : address
        reconnectTask?.cancel()
        pingTask?.cancel()
        socket?.cancel(with: .goingAway, reason: nil)
        socket = nil
        openSocket()
    }

    /// The usual explanation, plus, for a home-network address the phone can't reach, why that is.
    static func reachAdvice(_ error: Error, host: String?) -> String {
        let why = SettingsView.explain(error)
        let unreachable: Set<URLError.Code> = [.cannotConnectToHost, .timedOut, .networkConnectionLost, .cannotFindHost]
        guard let host, isHomeNetwork(host), let code = (error as? URLError)?.code, unreachable.contains(code) else {
            return why
        }
        return why + " \(host) is a home-network address, so the phone has to be on the same Wi-Fi as that computer."
            + " To reach Vision from anywhere, set up Tailscale on both (see Remote in Vision's README)."
    }

    /// A private network address (10.x, 172.16-31.x, 192.168.x, 169.254.x, fd00::/8, fe80::/10) or a .local name.
    static func isHomeNetwork(_ host: String) -> Bool {
        let h = host.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        if h.hasSuffix(".local") { return true }
        if h.contains(":") { return h.hasPrefix("fd") || h.hasPrefix("fe8") || h.hasPrefix("fe9") || h.hasPrefix("fea") || h.hasPrefix("feb") }
        let parts = h.split(separator: ".").compactMap { Int($0) }
        guard parts.count == 4 else { return false }
        switch (parts[0], parts[1]) {
        case (10, _), (192, 168), (169, 254): return true
        case (172, 16...31): return true
        default: return false
        }
    }

    private func scheduleReconnect() {
        guard wantConnection else { return }
        reconnectTask?.cancel()
        let delay = min(15.0, pow(2.0, Double(reconnectAttempt)))
        reconnectAttempt += 1
        reconnectTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(delay))
            guard let self, !Task.isCancelled, self.wantConnection, self.socket == nil else { return }
            self.openSocket()
        }
    }

    // MARK: - outgoing frames

    private func sendFrame(_ obj: [String: Any]) {
        guard let socket, let data = try? JSONSerialization.data(withJSONObject: obj), let text = String(data: data, encoding: .utf8) else {
            toast = "Not connected to Vision"
            return
        }
        socket.send(.string(text)) { [weak self] error in
            if let error {
                Task { @MainActor in self?.socketDropped(socket, error) }
            }
        }
    }

    private func sendFrame(_ obj: [String: Any], chat: String?) {
        var obj = obj
        if let chat { obj["chat"] = chat }
        sendFrame(obj)
    }

    /// `voice` (spoken) and a call being up (`talk`) select the conversational model; playback alone
    /// never changes input routing.
    func send(_ text: String, voice: Bool = false, images: [String] = [], thumbnails: [UIImage] = [], now: Bool = false) {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty || !images.isEmpty else { return }
        if text.lowercased() == "/restart" && images.isEmpty {
            // Not a message: the machine restarts Vision once every reply has finished (a toast says so).
            sendFrame(["type": "restart"])
            return
        }
        guard let chat = active else {
            // Nothing open (the last chat was closed): the first message opens one.
            newConversation()
            pendingSend = (text, voice, images, thumbnails)
            return
        }
        // A message sent during a reply is queued by the machine.  Keep current speech playing;
        // stopping it here would make a follow-up look like an interruption.
        if !chat.busy { player.stop() }
        // `images` carries every uploaded path; photos and videos get thumbnails, other files are chips.
        var message = Message(role: .user, text: text, images: thumbnails,
                              imagePaths: images.filter(Message.isMedia),
                              files: images.filter { !Message.isMedia($0) })
        // Sent mid-reply it waits for that reply to end (Send now puts it in straight away); a `start`
        // or `steered` frame takes the mark off.
        message.queued = chat.busy
        message.attached = images
        if let idx = chats.firstIndex(where: { $0.id == chat.id }) { chats[idx].suggestion = "" }
        transcripts[chat.id, default: []].append(message)
        // A typed follow-up queued during a call waits for its turn in text; only the call speaks.
        let speak = voice || (inCall && !message.queued)
        var frame: [String: Any] = ["type": "message", "text": text, "speak": speak, "voice": voice, "talk": inCall]
        if !images.isEmpty { frame["images"] = images }
        if now { frame["now"] = true }
        sendFrame(frame, chat: chat.id)
    }

    /// A queued message out of the queue, here and on the machine; `edit` puts its text back in the composer.
    func unqueue(_ message: Message, edit: Bool = false) {
        guard message.queued, let chat = active else { return }
        var frame: [String: Any] = ["type": "unqueue", "text": message.text]
        let paths = message.attached.isEmpty ? message.imagePaths + message.files : message.attached
        if !paths.isEmpty { frame["images"] = paths }
        if edit { frame["keep"] = true }  // its uploads come back with it, so the machine keeps them
        sendFrame(frame, chat: chat.id)
        leftQueue[chat.id, default: []].append(message)
        transcripts[chat.id]?.removeAll { $0.id == message.id }
        if edit { editDraft = EditDraft(text: message.text, attachments: reattach(message, paths: paths)) }
    }

    /// A queued message's photos and files as composer attachments again, pointing at the uploads the
    /// machine already has; a photo whose thumbnail isn't here yet gets the cached one or a placeholder.
    private func reattach(_ message: Message, paths: [String]) -> [Attachment] {
        var media = 0
        return paths.map { path in
            guard Message.isMedia(path) else {
                return Attachment(image: UIImage(), file: URL(fileURLWithPath: path), uploaded: path)
            }
            defer { media += 1 }
            let image = media < message.images.count ? message.images[media]
                : uploadCache.object(forKey: path as NSString) ?? UIImage(systemName: "photo") ?? UIImage()
            return Attachment(image: image, uploaded: path)
        }
    }

    /// A queued message into the running reply now; Claude takes it at its next step. A model that
    /// can't take one mid-reply leaves it queued (the machine says so).
    func sendNow(_ message: Message) {
        guard message.queued, let chat = active else { return }
        var frame: [String: Any] = ["type": "steer", "text": message.text]
        let paths = message.attached.isEmpty ? message.imagePaths + message.files : message.attached
        if !paths.isEmpty { frame["images"] = paths }
        sendFrame(frame, chat: chat.id)
        leftQueue[chat.id, default: []].append(message)
    }

    /// A call hung up: the machine stops speaking that chat's reply to us, so a call started again
    /// straight after doesn't pick up the rest of it. The reply itself carries on in text, and other
    /// chats keep talking to whoever is on a call with them. With no chat known, every reply goes quiet.
    func hush(chat: String?) {
        guard socket != nil else { return }
        sendFrame(["type": "hush"], chat: chat ?? activeChatID)
    }

    func cancel() {
        player.stop()
        sendFrame(["type": "cancel"], chat: activeChatID)
    }

    /// Shell mode: run `command` in the open chat's folder on the computer. Its row comes back as `shell` frames,
    /// and what it printed goes to the model with the next message.
    func runShell(_ command: String) {
        let command = command.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !command.isEmpty, let chat = active else { return }
        if let idx = chats.firstIndex(where: { $0.id == chat.id }) { chats[idx].suggestion = "" }
        sendFrame(["type": "shell", "text": command], chat: chat.id)
    }

    /// Stop the open chat's running shell-mode command.
    func stopShell() {
        sendFrame(["type": "shell_stop"], chat: activeChatID)
    }

    var shellRunning: Bool { active?.shellRunning ?? false }

    /// Open a fresh chat on the machine; the reply (`opened`) makes it the active one. `folder`: the
    /// machine folder it should work in (from the picker), remembered for later chats; nil opens it in
    /// the folder last picked on this machine, or the machine's own if none was.
    func newConversation(in folder: String? = nil) {
        pendingSend = nil
        wantsNewChat = true
        let picked = folder.flatMap { $0.isEmpty ? nil : $0 }
        pickedFolder = picked
        let workdir = picked ?? lastFolder
        newChatInLastFolder = picked == nil && workdir != nil
        var frame: [String: Any] = ["type": "new"]
        if let nextModel { frame["model"] = nextModel }
        if let nextEffort { frame["effort"] = nextEffort }
        if let workdir { frame["workdir"] = workdir }
        sendFrame(frame)
    }

    /// The folder last picked for a new chat, kept per machine: a path on one means nothing on another.
    private var lastFolder: String? {
        get { (UserDefaults.standard.dictionary(forKey: "lastFolders") as? [String: String])?[serverURL] }
        set {
            var all = UserDefaults.standard.dictionary(forKey: "lastFolders") as? [String: String] ?? [:]
            all[serverURL] = newValue
            UserDefaults.standard.set(all, forKey: "lastFolders")
        }
    }

    /// Open a project's chat: the one already open is switched to here; otherwise the machine opens it in the
    /// project's folder and its reply (`opened`) makes it the active one.
    func openProject(_ project: ProjectInfo) {
        if !project.chat.isEmpty, chats.contains(where: { $0.id == project.chat }) {
            select(chat: project.chat)
            return
        }
        pendingSend = nil
        wantsNewChat = true
        sendFrame(["type": "new", "project": project.id])
    }

    /// Make a project on the machine (`folder`: a machine path from the picker; nil for the machine's default)
    /// and open its chat.
    func createProject(goal: String, title: String, instructions: String, folder: String?,
                       settings: [String: Any] = [:], scout: ScoutLook? = nil, checkins: [String: Any]? = nil) async throws {
        var body: [String: Any] = ["goal": goal, "title": title, "instructions": instructions, "settings": settings]
        if let folder, !folder.isEmpty { body["workdir"] = folder }
        if let scout { body["scout"] = scout.json.filter { !($0.key == "name" && ($0.value as? String ?? "").isEmpty) } }
        let data = try await perform(request("projects", method: "POST", json: body))
        guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any], let project = ProjectInfo(json) else {
            throw ClientError.badResponse
        }
        upsert(project)   // its `project` frame may still be on the way
        if let checkins { _ = try? await setCheckins(project.id, checkins) }
        openProject(project)
    }

    /// Archive a project: it leaves the list and its chat carries on as an ordinary one. Nothing is deleted.
    func archiveProject(_ id: String) async throws {
        _ = try await perform(request("projects/\(id)", method: "DELETE"))
        projects.removeAll { $0.id == id }
    }

    private func upsert(_ project: ProjectInfo) {
        if let i = projects.firstIndex(where: { $0.id == project.id }) {
            projects[i] = project
        } else {
            projects.insert(project, at: 0)
        }
    }

    // MARK: project threads

    /// How many of a project's threads are in each state: counted here once its list is fetched (frames keep
    /// it live), else as the machine last summed them up on the project.
    func threadCounts(_ project: ProjectInfo) -> [String: Int] {
        guard threadsLoaded.contains(project.id), let list = threads[project.id] else { return project.threads ?? [:] }
        return list.reduce(into: [:]) { $0[$1.state, default: 0] += 1 }
    }

    /// A project's threads, full (with their runs), oldest first.
    @discardableResult
    func fetchThreads(project: String) async throws -> [ProjectThread] {
        let list = try await JSONDecoder().decode([ProjectThread].self, from: perform(request("projects/\(project)/threads")))
        threads[project] = list
        threadsLoaded.insert(project)
        return list
    }

    /// The same, for when the list opens or the socket comes back: a failure leaves what is shown.
    func refreshThreads(project: String) async {
        _ = try? await fetchThreads(project: project)
    }

    /// One thread, full.
    @discardableResult
    func fetchThread(project: String, id: String) async throws -> ProjectThread {
        try await threadCall("projects/\(project)/threads/\(id)", method: "GET")
    }

    /// Set a new thread going on the project. `edits`: false keeps it read-only.
    @discardableResult
    func startThread(project: String, objective: String, title: String = "", edits: Bool = true) async throws -> ProjectThread {
        var body: [String: Any] = ["objective": objective, "edits": edits]
        if !title.isEmpty { body["title"] = title }
        return try await threadCall("projects/\(project)/threads", json: body)
    }

    /// Steer a working thread, answer a waiting one, or give a stopped one more to do. A closed one stays
    /// closed: a new thread carries it on, and that is what comes back. From the user directly, so the
    /// project's chat hears of it too.
    @discardableResult
    func tellThread(project: String, id: String, text: String) async throws -> ProjectThread {
        try await threadCall("projects/\(project)/threads/\(id)/message", json: ["text": text])
    }

    /// Merge a finished thread's branch into the project's folder and close it. A 409 says why it couldn't
    /// (uncommitted changes, a conflict); everything is then left as it was.
    @discardableResult
    func landThread(project: String, id: String) async throws -> ProjectThread {
        try await threadCall("projects/\(project)/threads/\(id)/land", json: [:])
    }

    /// What landing a thread would bring in; `path` adds that one file's patch.
    func threadDiff(project: String, id: String, path: String = "") async throws -> ThreadDiff {
        let query = path.isEmpty ? [:] : ["path": path]
        return try await JSONDecoder().decode(ThreadDiff.self, from: perform(request("projects/\(project)/threads/\(id)/diff", query: query)))
    }

    /// A thread's conversation as its provider keeps it, in plain words (empty when it can't be read).
    func threadHistory(project: String, id: String) async throws -> [ThreadMessage] {
        struct Body: Decodable { let messages: [ThreadMessage] }
        return try await JSONDecoder().decode(Body.self, from: perform(request("projects/\(project)/threads/\(id)/history"))).messages
    }

    // MARK: projects: settings, pausing, memory

    /// Change a project: any of goal, title, instructions, and settings (only the keys given change).
    @discardableResult
    func saveProject(_ id: String, goal: String? = nil, title: String? = nil, instructions: String? = nil,
                     settings: [String: Any]? = nil) async throws -> ProjectInfo {
        var body: [String: Any] = ["id": id]
        if let goal { body["goal"] = goal }
        if let title { body["title"] = title }
        if let instructions { body["instructions"] = instructions }
        if let settings { body["settings"] = settings }
        return try await projectCall("projects", json: body)
    }

    // MARK: scouts: the leash, rules, check-ins and approvals

    /// The scout's name, colour or look.
    @discardableResult
    func saveScout(_ id: String, _ look: ScoutLook) async throws -> ProjectInfo {
        let project = try await projectCall("projects", json: ["id": id, "scout": look.json])
        upsert(project)
        return project
    }

    /// How far it goes on its own: on_call | checkins | autonomous.
    @discardableResult
    func setLeash(_ id: String, _ leash: String) async throws -> ProjectInfo {
        let project = try await projectCall("projects", json: ["id": id, "settings": ["leash": leash]])
        upsert(project)
        return project
    }

    /// One action's rule: does | asks | never, or "default" for the leash's.
    @discardableResult
    func setRule(_ id: String, action: String, rule: String) async throws -> ProjectInfo {
        let project = try await projectCall("projects", json: ["id": id, "settings": ["rules": [action: rule]]])
        upsert(project)
        return project
    }

    /// When it checks in: {repeat: daily|weekdays|every|off, time?, every_minutes?}.
    @discardableResult
    func setCheckins(_ id: String, _ schedule: [String: Any]) async throws -> ProjectInfo {
        let project = try await projectCall("projects/\(id)/checkins", json: schedule)
        upsert(project)
        return project
    }

    /// Have it look at everything now and report back in its chat.
    func checkInNow(_ id: String) async throws {
        _ = try await perform(request("projects/\(id)/checkin", method: "POST", json: [:]))
    }

    /// Approve a request (as asked, or with `command`, an edited shell command) or deny it.
    func decideApproval(project: String, id: String, approve: Bool, command: String? = nil) async throws {
        var body: [String: Any] = ["decision": approve ? "approve" : "deny"]
        if let command, !command.isEmpty { body["command"] = command }
        _ = try await perform(request("projects/\(project)/approvals/\(id)", method: "POST", json: body))
        dropApproval(project: project, id: id)
    }

    private func dropApproval(project: String, id: String) {
        guard let i = projects.firstIndex(where: { $0.id == project }) else { return }
        projects[i].approvals.removeAll { $0.id == id }
    }

    /// Pause a project (its threads stop where they are, nothing new starts) or resume it.
    @discardableResult
    func setProjectPaused(_ id: String, _ paused: Bool) async throws -> ProjectInfo {
        try await projectCall("projects/\(id)/\(paused ? "pause" : "resume")", json: [:])
    }

    private func projectCall(_ path: String, json: [String: Any]) async throws -> ProjectInfo {
        let data = try await perform(request(path, method: "POST", json: json))
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any], let project = ProjectInfo(object) else {
            throw ClientError.badResponse
        }
        upsert(project)
        return project
    }

    /// The project memory, oldest first: what its threads decided and what the user asked it to keep.
    func projectMemory(_ id: String) async throws -> [String] {
        struct Body: Decodable { let facts: [String] }
        return try await JSONDecoder().decode(Body.self, from: perform(request("projects/\(id)/memory"))).facts
    }

    func rememberInProject(_ id: String, fact: String) async throws -> [String] {
        struct Body: Decodable { let facts: [String] }
        return try await JSONDecoder().decode(Body.self, from: perform(request("projects/\(id)/memory", method: "POST", json: ["fact": fact]))).facts
    }

    /// Take out the fact at `index` (0 = the oldest).
    func forgetInProject(_ id: String, index: Int) async throws -> [String] {
        struct Body: Decodable { let facts: [String] }
        return try await JSONDecoder().decode(Body.self, from: perform(request("projects/\(id)/memory/\(index + 1)", method: "DELETE"))).facts
    }

    @discardableResult
    func stopThread(project: String, id: String) async throws -> ProjectThread {
        try await threadCall("projects/\(project)/threads/\(id)/stop", json: [:])
    }

    /// Close a thread: it is done with, and leaves the strip.
    @discardableResult
    func resolveThread(project: String, id: String) async throws -> ProjectThread {
        try await threadCall("projects/\(project)/threads/\(id)/resolve", json: [:])
    }

    private func threadCall(_ path: String, method: String = "POST", json: [String: Any]? = nil) async throws -> ProjectThread {
        let thread = try await JSONDecoder().decode(ProjectThread.self, from: perform(request(path, method: method, json: json)))
        upsert(thread)   // its `thread` frame may still be on the way
        return thread
    }

    /// A thread in, new or changed. A frame carries no runs, so the runs a full view brought are kept.
    private func upsert(_ thread: ProjectThread) {
        var list = threads[thread.project] ?? []
        if let i = list.firstIndex(where: { $0.id == thread.id }) {
            var fresh = thread
            if fresh.runs == nil { fresh.runs = list[i].runs }
            list[i] = fresh
        } else {
            list.append(thread)
        }
        threads[thread.project] = list
    }

    /// A folder on the machine and its subfolders; "" is where the picker starts (the default folder,
    /// with the recent ones).
    func fetchFolders(path: String = "") async throws -> FolderListing {
        let query = path.isEmpty ? [:] : ["path": path]
        return try await JSONDecoder().decode(FolderListing.self, from: perform(request("folders", query: query)))
    }

    /// Bring an earlier conversation up as its own chat (or jump to it if it is already open).
    func resume(sessionID id: String, provider: String? = nil) {
        pendingSend = nil
        wantsNewChat = true
        var frame: [String: Any] = ["type": "resume", "session_id": id]
        if let provider, !provider.isEmpty { frame["provider"] = provider }
        sendFrame(frame)
    }

    func select(chat id: String) {
        guard id != activeChatID else { return }
        player.stop()   // only the chat on screen is heard
        activeChatID = id
        pendingQuestions = questionsByChat[id]
        if transcripts[id] == nil { Task { await fetchHistory(chat: id) } }
    }

    func close(chat id: String) {
        sendFrame(["type": "close"], chat: id)
    }

    /// The chat on screen, if the machine still lists it.
    var activeChat: ChatInfo? { chats.first { $0.id == activeChatID } }

    /// Open the chat on screen in a terminal window on the machine (`vision --join`); the machine keeps
    /// running it, the window mirrors it. A terminal chat is already one, so there is nothing to open.
    func openOnMachine() {
        guard let chat = activeChat, chat.source != "terminal" else { return }
        sendFrame(["type": "open_terminal"], chat: chat.id)
    }

    func switchModel(_ id: String, effort: String? = nil) {
        guard active != nil else {
            // No chat open: held for the one the next message opens.
            nextModel = id
            if let effort { nextEffort = effort }
            return
        }
        var frame: [String: Any] = ["type": "model", "model": id]
        if let effort { frame["effort"] = effort }
        sendFrame(frame, chat: activeChatID)
    }

    /// Auto or plan for the chat on screen, as /mode in the terminal. The machine's next summary of the
    /// chat confirms it; until then the button shows what it was.
    func setMode(_ mode: String) {
        guard let chat = active?.id else { return }
        sendFrame(["type": "mode", "mode": mode], chat: chat)
    }

    func answer(_ answers: [String: String]?) {
        // `active`, as the form is shown for it: with no chat picked yet that is the fallback one.
        guard let chat = active?.id else { return }
        pendingQuestions = nil
        questionsByChat[chat] = nil
        sendFrame(["type": "answer", "answers": answers ?? NSNull()], chat: chat)
    }

    // MARK: - incoming frames

    private func handle(_ ev: [String: Any]) {
        let kind = ev["type"] as? String ?? ""
        if kind != "delta" { flushDeltas() }   // text still dripping lands before anything that follows it
        switch kind {
        case "hello":
            reconnectAttempt = 0
            connection = .online
            applyHello(ev)
            keepUsageFresh()
            fetchProviders()
            enablePush()
            ThreadsLiveActivity.shared.reregister { [weak self] kind, token in self?.registerActivity(kind: kind, token: token) }
            return
        case "chat":
            applyChat(ev)
            return
        case "chat_closed":
            // `moved_to`: the machine closed a duplicate of a conversation a terminal has open; follow it there.
            if let id = ev["chat"] as? String { remove(chat: id, movedTo: ev["moved_to"] as? String) }
            return
        case "error":
            let text = ev["text"] as? String ?? "error"
            // These are the texts of `usable_folder` in vision/server.py: reword them there and this stops matching.
            let folderGone = ["no such folder", "not a folder", "Vision can't open"].contains(where: text.hasPrefix)
            if wantsNewChat && folderGone { pickedFolder = nil }   // a folder that failed is not remembered
            if wantsNewChat && newChatInLastFolder && folderGone {
                // The remembered folder has gone from the machine: forget it and open the chat in the
                // machine's own folder, keeping the message that was waiting for it.
                let waiting = pendingSend
                lastFolder = nil
                newConversation()
                pendingSend = waiting
                return
            }
            toast = text
            return
        case "toast":
            toast = ev["text"] as? String
            return
        case "voice_power":
            // Switched in Settings here, on another phone, or with /voice on|off in a terminal.
            voiceOn = ev["on"] as? Bool ?? true
            return
        case "update":
            // The machine's look at its git remote changed its answer while we were connected.
            applyUpdate(ev["update"] as? String)
            return
        case "pong":
            return
        case "scheduled":
            scheduledVersion += 1
            return
        case "project":
            if let snapshot = ev["project"] as? [String: Any], let project = ProjectInfo(snapshot) { upsert(project) }
            return
        case "project_removed":
            if let id = ev["project"] as? String { projects.removeAll { $0.id == id } }
            return
        case "approval":
            // A scout asked leave to run something, or the request was decided (here, elsewhere, or from a notification).
            if let pid = ev["project"] as? String, let data = ev["approval"] as? [String: Any], let id = data["id"] as? String,
               let i = projects.firstIndex(where: { $0.id == pid }) {
                projects[i].approvals.removeAll { $0.id == id }
                if let pending = ScoutApproval(data) { projects[i].approvals.append(pending) }
            }
            return
        case "checkin":
            // A check-in began or ended; its card arrives with the chat's history after the turn's `done`.
            if let pid = ev["project"] as? String, let i = projects.firstIndex(where: { $0.id == pid }) {
                var checkin = projects[i].checkin ?? ScoutCheckins([:])
                checkin.running = ev["state"] as? String == "started" ? ev["started"] as? Double : nil
                projects[i].checkin = checkin
            }
            return
        case "activity":
            return
        case "threads_live":
            if let state = Self.threadsCard(ev["state"]) { ThreadsLiveActivity.shared.apply(state) }
            return
        case "thread":
            if var snapshot = ev["thread"] as? [String: Any] {
                if snapshot["project"] == nil, let project = ev["project"] { snapshot["project"] = project }
                if let thread = ProjectThread(snapshot) { upsert(thread) }
            }
            return
        default:
            break
        }
        guard let chat = ev["chat"] as? String, !closedChats.contains(chat) else { return }
        let onScreen = chat == active?.id
        switch kind {
        case "start":
            setBusy(chat, true)
            if let idx = chats.firstIndex(where: { $0.id == chat }) { chats[idx].suggestion = "" }
            leftQueue[chat] = nil
            streamed[chat] = 0
            splitAt[chat] = 0
            var turn = Message.fromServer(role: .user, text: ev["text"] as? String ?? "")
            var list = transcripts[chat] ?? []
            if let note = ev["note"] as? String {
                // Not the user's words: a project's threads reporting back. One line, as the history shows it.
                list.append(Message(role: .note, text: note))
            } else if let i = list.firstIndex(where: { $0.queued && $0.sameTurn(as: turn) }) {
                // Its turn has come: it stops being queued and sits right above its reply.
                turn = list.remove(at: i)
                turn.queued = false
                list.append(turn)
            } else if let i = list.lastIndex(where: { $0.role != .note }), list[i].sameTurn(as: turn) {
                // Sent from here. A model or effort switch is said in a note just before the turn starts,
                // after the bubble went up: the bubble moves under it, to sit right above its reply.
                turn = list.remove(at: i)
                list.append(turn)
            } else {
                list.append(turn)   // the turn was started from another client
            }
            list.append(Self.liveReply(since: Date()))
            transcripts[chat] = list
            loadImages(chat: chat)
        case "delta":
            guard let delta = ev["text"] as? String else { return }
            streamed[chat, default: 0] += delta.unicodeScalars.count
            pendingDeltas[chat, default: ""] += delta
            if drip == nil { drip = FrameTicker { [weak self] dt in self?.dripDeltas(dt) } }
        case "steered":
            // A message went into the running reply (Send now, here or elsewhere): it sits where it went
            // in, and the rest of the reply follows it in a bubble of its own, as the terminal shows it.
            let sent = Message.fromServer(role: .user, text: ev["text"] as? String ?? "")
            guard var list = transcripts[chat], let idx = list.lastIndex(where: { $0.streaming }) else { return }
            // Each part times itself: the one before the message keeps what it took as its footer, and
            // the reply to the message counts from now, as the terminal shows it.
            let ended = list[idx].pausedAt ?? Date()
            list[idx].durationMs = list[idx].startedAt.map { Int(ended.timeIntervalSince($0) * 1000) }
            list[idx].pausedAt = nil
            list[idx].streaming = false
            list[idx].status = ""
            var user = list.firstIndex(where: { $0.queued && $0.sameTurn(as: sent) }).map { list.remove(at: $0) } ?? sent
            user.queued = false
            let waiting = list.filter(\.queued)     // still queued: they stay at the bottom
            list.removeAll(where: \.queued)
            list.append(user)
            list.append(Self.liveReply(since: Date()))
            transcripts[chat] = list + waiting
            splitAt[chat] = streamed[chat] ?? 0
            loadImages(chat: chat)
        case "status":
            let tool = ev["tool"] as? String ?? ""
            let label = ev["label"] as? String ?? (tool.isEmpty ? "working…" : "using \(tool)…")
            withStreaming(chat) { $0.status = label }
        case "agent":
            guard let id = ev["id"] as? String else { return }
            // A row that started before a message was sent into the reply lives in the bubble above it.
            if var list = transcripts[chat], let (m, i) = agentRow(id, in: list) {
                list[m].agents[i].apply(ev)
                transcripts[chat] = list
                return
            }
            withStreaming(chat) { m in
                var row = AgentRow(id: id, at: m.text.unicodeScalars.count)
                row.apply(ev)
                m.agents.append(row)
            }
        case "tool":
            guard let id = ev["id"] as? String else { return }
            // Completion can arrive after a steered message split the reply. Keep it on its starting bubble.
            if var list = transcripts[chat], let (m, i) = toolRow(id, in: list) {
                list[m].tools[i].apply(ev)
                transcripts[chat] = list
                return
            }
            withStreaming(chat) { m in
                let globalAt = ev["at"] as? Int
                let at = globalAt.map { $0 - (splitAt[chat] ?? 0) } ?? m.text.unicodeScalars.count
                var row = ToolRow(id: id, at: min(max(0, at), m.text.unicodeScalars.count))
                row.apply(ev)
                m.tools.append(row)
            }
        case "audio":
            if onScreen, let b64 = ev["wav"] as? String, let wav = Data(base64Encoded: b64) {
                // Only a live call talks: a reply to something said on a call that has since hung up
                // stays silent.
                audioSink?(wav)
            }
        case "question":
            if let form = questionForm(ev["questions"]) {
                if questionsByChat[chat]?.questions != form.questions {
                    questionsByChat[chat] = form
                    if onScreen { pendingQuestions = form }
                }
                withStreaming(chat) { m in if m.pausedAt == nil { m.pausedAt = .now } }
            }
        case "answered":
            questionsByChat[chat] = nil
            if onScreen { pendingQuestions = nil }
            withStreaming(chat) { m in
                // The wait doesn't count: the timer carries on from where it stopped.
                if let paused = m.pausedAt { m.startedAt = m.startedAt?.addingTimeInterval(Date().timeIntervalSince(paused)) }
                m.pausedAt = nil
                if let form = AnsweredForm(ev, at: m.text.unicodeScalars.count) { m.answered.append(form) }
                m.status = "working…"
            }
        case "done":
            // After a split the last bubble holds only what came after the last message sent in.
            var text = ev["text"] as? String
            if let full = text, let cut = splitAt[chat], cut > 0 { text = Self.dropCodePoints(full, cut) }
            splitAt[chat] = 0
            finishStreaming(chat, text: text, error: ev["error"] as? String ?? "", durationMs: ev["duration_ms"] as? Int)
            let stillBusy = ev["busy"] as? Bool ?? false
            setBusy(chat, stillBusy)
            questionsByChat[chat] = nil
            if onScreen { pendingQuestions = nil }
            if let idx = chats.firstIndex(where: { $0.id == chat }) {
                chats[idx].sessionID = ev["session_id"] as? String ?? chats[idx].sessionID
                chats[idx].historyID = ev["history_id"] as? String ?? chats[idx].historyID
            }
            if !stillBusy { Task { await fetchHistory(chat: chat) } }
        case "note":
            if let text = ev["text"] as? String { transcripts[chat, default: []].append(Message(role: .note, text: text)) }
        case "shell":
            // A shell-mode command: one frame when it starts, one with what it printed when it ends
            // (shell_output frames in between say what it has printed so far).
            let command = ev["text"] as? String ?? ""
            var list = transcripts[chat] ?? []
            if ev["running"] as? Bool ?? false {
                var row = Message(role: .shell, text: command)
                row.shellRunning = true
                list.append(row)
            } else if let i = list.lastIndex(where: { $0.role == .shell && $0.shellRunning && $0.text == command }) {
                list[i].shellRunning = false
                list[i].shellOutput = ev["output"] as? String ?? ""
                list[i].shellCode = ev["code"] as? Int
            } else {
                var row = Message(role: .shell, text: command)
                row.shellOutput = ev["output"] as? String ?? ""
                row.shellCode = ev["code"] as? Int
                list.append(row)
            }
            transcripts[chat] = list
        case "shell_output":
            // What a running command has printed so far (the end of it): shown under "running…" until it ends.
            let command = ev["text"] as? String ?? ""
            if let i = transcripts[chat]?.lastIndex(where: { $0.role == .shell && $0.shellRunning && $0.text == command }) {
                transcripts[chat]?[i].shellOutput = ev["output"] as? String ?? ""
            }
        case "suggestion":
            if let idx = chats.firstIndex(where: { $0.id == chat }), !chats[idx].busy {
                chats[idx].suggestion = ev["text"] as? String ?? ""
            }
        default:
            break
        }
    }

    /// Settings' notice from the protocol numbers and what the machine says to run for an update. A
    /// mismatch that breaks things is toasted once per Vision and pair of numbers, kept across launches,
    /// so an old machine isn't announced every time the app opens; an update that is merely out, once a
    /// day per Vision while it waits. Settings keeps saying it.
    private func applyUpdate(_ how: String?) {
        let drift = Staleness(protocol: serverProtocol, update: how, appProtocol: Self.protocolVersion)
        if let drift {
            let waiting = "\(serverURL) update "
            var key = "\(serverURL) \(serverProtocol ?? 1) \(Self.protocolVersion)"
            if !drift.breaks { key = waiting + Date().formatted(.iso8601.year().month().day()) }
            var told = UserDefaults.standard.stringArray(forKey: "stalenessToasted") ?? []
            if !told.contains(key) {
                toast = drift.toast
                if !drift.breaks { told.removeAll { $0.hasPrefix(waiting) } }  // yesterday's
                told.append(key)
                UserDefaults.standard.set(told, forKey: "stalenessToasted")
            }
        }
        staleness = drift
    }

    private func applyHello(_ ev: [String: Any]) {
        WidgetCenter.shared.reloadAllTimelines()   // the app is in use: the widgets catch up (the app going away does it again)
        serverProtocol = ev["protocol"] as? Int
        canShell = ev["shell"] as? Bool ?? false
        canReadAloud = ev["read_aloud"] as? Bool ?? false
        canSwitchVoice = ev["voice_on"] is Bool
        voiceOn = ev["voice_on"] as? Bool ?? true
        applyUpdate(ev["update"] as? String)
        if let state = Self.threadsCard(ev["threads_live"]) { ThreadsLiveActivity.shared.apply(state) }
        let snapshots = ev["chats"] as? [[String: Any]] ?? []
        let list = snapshots.compactMap(ChatInfo.init)
        chats = list
        projects = (ev["projects"] as? [[String: Any]] ?? []).compactMap(ProjectInfo.init)
        // Threads may have moved on while the socket was down: the counts are the project's again until
        // a list is fetched, which the one on screen does straight away (ChatView).
        threadsLoaded = []
        threads = threads.filter { id, _ in projects.contains { $0.id == id } }
        let ids = Set(list.map(\.id))
        transcripts = transcripts.filter { ids.contains($0.key) }
        // A form answered elsewhere (the machine, another phone) while we were away is gone.
        let waiting = Set(list.filter(\.waiting).map(\.id))
        questionsByChat = questionsByChat.filter { waiting.contains($0.key) }
        if activeChatID.map({ !ids.contains($0) }) ?? true {
            activeChatID = list.last?.id
        }
        for snapshot in snapshots {
            guard let chat = ChatInfo(snapshot) else { continue }
            syncQuestions(for: chat, from: snapshot)
            reconcile(chat)
            if chat.busy {
                applyAgents(snapshot["agents"], chat: chat.id)
                applyTools(snapshot["tools"], chat: chat.id)
            }
        }
        pendingQuestions = active.flatMap { questionsByChat[$0.id] }
    }

    /// A chat's snapshot from the machine: new, changed, or the reply to our own new/resume.
    private func applyChat(_ ev: [String: Any]) {
        guard let info = ChatInfo(ev), !closedChats.contains(info.id) else { return }
        if let idx = chats.firstIndex(where: { $0.id == info.id }) {
            chats[idx] = info
        } else {
            chats.append(info)
        }
        // Nothing was open and a chat appeared (a terminal, a scheduled run): it is the one on screen,
        // so say so, rather than leave stop, model and answers aimed at no chat. Not while a chat of our
        // own is on its way (`new` sent, `opened` not back): adopting a stranger then would make our
        // chat's arrival look like a switch away, which ends a call started on the empty screen.
        if activeChatID == nil && !wantsNewChat { activeChatID = info.id }
        syncQuestions(for: info, from: ev)
        let opened = ev["opened"] as? Bool ?? false
        if opened && wantsNewChat {
            wantsNewChat = false
            newChatInLastFolder = false
            if let pickedFolder { lastFolder = pickedFolder }
            pickedFolder = nil
            nextModel = nil      // spent on this chat
            nextEffort = nil
            player.stop()
            activeChatID = info.id
            pendingQuestions = questionsByChat[info.id]
            if let queued = pendingSend {
                pendingSend = nil
                send(queued.text, voice: queued.voice, images: queued.images, thumbnails: queued.thumbnails)
            }
        }
        reconcile(info)
        if info.busy {
            applyAgents(ev["agents"], chat: info.id)
            applyTools(ev["tools"], chat: info.id)
        }
        if info.id == active?.id { pendingQuestions = questionsByChat[info.id] }
    }

    private func questionForm(_ raw: Any?) -> QuestionForm? {
        guard let raw, let data = try? JSONSerialization.data(withJSONObject: raw),
              let questions = try? JSONDecoder().decode([Question].self, from: data), !questions.isEmpty
        else { return nil }
        return QuestionForm(questions: questions)
    }

    private func syncQuestions(for chat: ChatInfo, from snapshot: [String: Any]) {
        if !chat.waiting {
            questionsByChat[chat.id] = nil
        } else if let form = questionForm(snapshot["questions"]) {
            if questionsByChat[chat.id]?.questions != form.questions {
                questionsByChat[chat.id] = form
            }
        }
    }

    /// Bring the local transcript in line with the machine's view of a chat (after connect or a change).
    private func reconcile(_ chat: ChatInfo) {
        var list = transcripts[chat.id] ?? []
        if chat.busy {
            // Mid-reply (or reconnected mid-reply): rebuild the streaming bubble from what the machine has so far.
            let turn = Message.fromServer(role: .user, text: chat.userText)
            // A live bubble whose turn is not the one running: that reply ended and another began while
            // we were away (suspended, a dropped socket), so neither its `done` nor the next `start` landed.
            let stale = list.lastIndex(where: { $0.streaming }).map { idx in
                !chat.userText.isEmpty && !list[..<idx].contains { $0.role == .user && !$0.queued && $0.sameTurn(as: turn) }
            } ?? false
            if stale, let idx = list.lastIndex(where: { $0.streaming }) {
                // Close it as it stands; history brings its finished text in under the new turn.
                list[idx].streaming = false
                list[idx].status = ""
                streamed[chat.id] = 0
                splitAt[chat.id] = 0
                loadedHistory[chat.id] = nil
            }
            if !stale, let idx = list.lastIndex(where: { $0.streaming }) {
                // The live bubble already belongs to this turn. After Send now, userText still
                // names the original prompt, so comparing it with the latest user bubble would
                // briefly append that prompt a second time.
                // partial is the whole reply; this bubble holds only the text after Send now.
                if !chat.partial.isEmpty {
                    list[idx].text = Self.dropCodePoints(chat.partial, splitAt[chat.id] ?? 0)
                }
                list[idx].status = list[idx].text.isEmpty ? "working…" : ""
                if list[idx].startedAt == nil { list[idx].startedAt = chat.replyBegan }
                // The machine's elapsed time leaves out time spent waiting on a question, and holds still during one.
                if chat.waiting, list[idx].pausedAt == nil {
                    list[idx].startedAt = chat.replyBegan
                    list[idx].pausedAt = .now
                } else if !chat.waiting, list[idx].pausedAt != nil {
                    list[idx].startedAt = chat.replyBegan
                    list[idx].pausedAt = nil
                }
            } else {
                if let i = list.firstIndex(where: { $0.queued && $0.sameTurn(as: turn) }) {
                    // Queued here and started while we were away: its turn has come, as `start` would have it.
                    var sent = list.remove(at: i)
                    sent.queued = false
                    list.append(sent)
                } else if !chat.userText.isEmpty, !(list.last(where: { $0.role == .user })?.sameTurn(as: turn) ?? false) {
                    list.append(turn)
                }
                let waiting = list.filter(\.queued)     // still queued: they stay at the bottom
                list.removeAll(where: \.queued)
                list.append(Self.liveReply(text: chat.partial, since: chat.replyBegan))
                list += waiting
            }
            transcripts[chat.id] = list
            syncQueued(chat)
            streamed[chat.id] = max(streamed[chat.id] ?? 0, chat.partial.unicodeScalars.count)
            loadImages(chat: chat.id)
            // A cold launch mid-reply has only the in-flight turn: pull the earlier turns in underneath it.
            if chat.historyID != loadedHistory[chat.id] { Task { await fetchHistory(chat: chat.id) } }
        } else {
            // The turn ended while we were away (suspended, or a dropped socket), so the `done` frame never
            // landed: the bubble would spin for ever. Close it here and pull the finished reply in.
            let stale = list.contains { $0.streaming }
            if stale { finishStreaming(chat.id, text: chat.partial.isEmpty ? nil : chat.partial, error: "") }
            syncQueued(chat)   // idle: nothing waits on the machine, so a bubble it had listed is gone
            if stale || transcripts[chat.id]?.isEmpty ?? true || chat.historyID != loadedHistory[chat.id] {
                loadedHistory[chat.id] = nil
                Task { await fetchHistory(chat: chat.id) }
            }
        }
    }

    private func remove(chat id: String, movedTo: String? = nil) {
        closedChats.insert(id)
        chats.removeAll { $0.id == id }
        transcripts[id] = nil
        questionsByChat[id] = nil
        loadedHistory[id] = nil
        if activeChatID == id {
            player.stop()
            if let movedTo, chats.contains(where: { $0.id == movedTo }) {
                select(chat: movedTo)
            } else {
                activeChatID = chats.last?.id
                pendingQuestions = active.flatMap { questionsByChat[$0.id] }
            }
        }
    }

    private func setBusy(_ chat: String, _ value: Bool) {
        if let idx = chats.firstIndex(where: { $0.id == chat }) {
            chats[idx].busy = value
            if !value { chats[idx].waiting = false }
        }
    }

    private var streamed: [String: Int] = [:]  // chat id → characters of the running reply so far
    /// The chat on screen has had words of its running reply.  Before that, all it can have said is the
    /// "Hmm." filler.
    var replyStarted: Bool { active.map { (streamed[$0.id] ?? 0) > 0 } ?? false }
    private var splitAt: [String: Int] = [:]   // …and where the last message sent into it split it
    /// Streamed text not drawn yet.  The model's words reach the machine in bursts (a handful of chunks at once, then
    /// a pause of a third of a second), so each burst is let out a few characters a frame and the reply flows.
    private var pendingDeltas: [String: String] = [:]
    private var drip: FrameTicker?

    private static func dropCodePoints(_ text: String, _ count: Int) -> String {
        let scalars = text.unicodeScalars
        let cut = scalars.index(scalars.startIndex, offsetBy: min(max(0, count), scalars.count))
        return String(text[cut...])
    }

    private func dripDeltas(_ dt: Double) {
        let share = min(1, dt / 0.2)   // the backlog drains with a fifth-of-a-second time constant
        for (chat, text) in pendingDeltas {
            let n = max(1, Int((Double(text.count) * share).rounded(.up)))
            let out = String(text.prefix(n))
            pendingDeltas[chat] = n < text.count ? String(text.dropFirst(n)) : nil
            withStreaming(chat) { $0.text += out; $0.status = "" }
        }
        if pendingDeltas.isEmpty { drip?.stop(); drip = nil }
    }

    private func flushDeltas() {
        drip?.stop()
        drip = nil
        let pending = pendingDeltas
        pendingDeltas = [:]
        for (chat, text) in pending { withStreaming(chat) { $0.text += text; $0.status = "" } }
    }
    /// Where an agent row already sits: (message index, row index).
    private func agentRow(_ id: String, in list: [Message]) -> (Int, Int)? {
        for m in list.indices.reversed() where list[m].role == .assistant {
            if let i = list[m].agents.firstIndex(where: { $0.id == id }) { return (m, i) }
        }
        return nil
    }

    private func toolRow(_ id: String, in list: [Message]) -> (Int, Int)? {
        for m in list.indices.reversed() where list[m].role == .assistant {
            if let i = list[m].tools.firstIndex(where: { $0.id == id }) { return (m, i) }
        }
        return nil
    }

    /// A chat snapshot mid-reply carries the reply's agent rows (with where each started and when):
    /// a phone that reconnects, or opens a chat another device started, shows them with real timers.
    private func applyAgents(_ raw: Any?, chat: String) {
        guard let frames = raw as? [[String: Any]], !frames.isEmpty, var list = transcripts[chat],
              let idx = list.lastIndex(where: { $0.streaming }) else { return }
        for frame in frames {
            guard let id = frame["id"] as? String else { continue }
            if let (m, i) = agentRow(id, in: list) {
                list[m].agents[i].apply(frame)
            } else {
                let at = min(max(0, frame["at"] as? Int ?? list[idx].text.unicodeScalars.count), list[idx].text.unicodeScalars.count)
                var row = AgentRow(id: id, at: at)
                row.apply(frame)
                list[idx].agents.append(row)
            }
        }
        list[idx].agents.sort { $0.at < $1.at }
        transcripts[chat] = list
    }

    /// Rebuild calls already in progress when this phone joins or reconnects mid-reply.
    private func applyTools(_ raw: Any?, chat: String) {
        guard let frames = raw as? [[String: Any]], !frames.isEmpty, var list = transcripts[chat],
              let idx = list.lastIndex(where: { $0.streaming }) else { return }
        for frame in frames {
            guard let id = frame["id"] as? String else { continue }
            if let (m, i) = toolRow(id, in: list) {
                list[m].tools[i].apply(frame)
            } else {
                let globalAt = frame["at"] as? Int ?? list[idx].text.unicodeScalars.count
                let at = min(max(0, globalAt - (splitAt[chat] ?? 0)), list[idx].text.unicodeScalars.count)
                var row = ToolRow(id: id, at: at)
                row.apply(frame)
                list[idx].tools.append(row)
            }
        }
        list[idx].tools.sort { $0.at < $1.at }
        transcripts[chat] = list
    }

    private func withStreaming(_ chat: String, _ change: (inout Message) -> Void) {
        guard var list = transcripts[chat], let idx = list.lastIndex(where: { $0.streaming }) else { return }
        change(&list[idx])
        transcripts[chat] = list
    }

    /// The bubble a reply streams into: `since` is when its turn began, which its timer counts from.
    private static func liveReply(text: String = "", since: Date?) -> Message {
        var reply = Message(role: .assistant, text: text, status: text.isEmpty ? "working…" : "", streaming: true)
        reply.startedAt = since
        return reply
    }

    private func finishStreaming(_ chat: String, text: String?, error: String, durationMs: Int? = nil) {
        var list = transcripts[chat] ?? []
        guard let idx = list.lastIndex(where: { $0.streaming }) else {
            if !error.isEmpty { list.append(Message(role: .note, text: error)); transcripts[chat] = list }
            return
        }
        if let text, !text.isEmpty { list[idx].text = text }
        if !error.isEmpty {
            if error == "cancelled" {
                list[idx].text += (list[idx].text.isEmpty ? "" : "\n\n") + "(cancelled)"
            } else {
                let body = list[idx].text.trimmingCharacters(in: .whitespacesAndNewlines)
                let note = "Vision could not answer: \(error)"
                if body != error && body != note && !body.hasSuffix(note) {
                    list[idx].text += (list[idx].text.isEmpty ? "" : "\n\n") + note
                }
                list[idx].isError = true
            }
        }
        list[idx].status = ""
        list[idx].streaming = false
        // The machine's figure when it gave one (an older Vision did not): else what this phone counted.
        list[idx].durationMs = durationMs ?? list[idx].startedAt.map { Int(Date().timeIntervalSince($0) * 1000) }
        transcripts[chat] = list
    }

    private var loadedHistory: [String: String] = [:]   // chat id → history_id the transcript was loaded from
    private var leftQueue: [String: [Message]] = [:]     // chat id → queued messages removed or sent now here since its last turn began

    // MARK: - HTTP

    private func request(_ path: String, query: [String: String] = [:], method: String = "GET", json: [String: Any]? = nil,
                         base: URL? = nil, bearer: String? = nil) throws -> URLRequest {
        let token = bearer ?? token
        guard let base = base ?? activeBase, !token.isEmpty, var parts = URLComponents(url: base, resolvingAgainstBaseURL: false) else {
            throw ClientError.notConfigured
        }
        parts.path = base.path + "/" + path
        if !query.isEmpty { parts.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) } }
        guard let url = parts.url else { throw ClientError.notConfigured }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.timeoutInterval = 60
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let json {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: json)
        }
        return req
    }

    private func perform(_ req: URLRequest) async throws -> Data {
        let (data, response) = try await session.data(for: req)
        guard let http = response as? HTTPURLResponse else { throw ClientError.badResponse }
        guard (200..<300).contains(http.statusCode) else {
            let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"]
            throw ClientError.http(http.statusCode, detail.map { "\($0)" } ?? "")
        }
        return data
    }

    func health() async throws -> Hello {
        try await JSONDecoder().decode(Hello.self, from: perform(request("health")))
    }

    /// A finished reply as the pieces to read it aloud in: speakable sentences, code and markdown left out.
    func readAloudPieces(_ text: String) async throws -> [String] {
        let reply = try await JSONSerialization.jsonObject(with: perform(request("speak/pieces", method: "POST", json: ["text": text])))
        return (reply as? [String: Any])?["pieces"] as? [String] ?? []
    }

    /// One piece in Vision's voice, as a WAV.  Throws when the voice is off on the computer (503) or can't load.
    func speakWav(_ piece: String) async throws -> Data {
        var req = try request("speak", method: "POST", json: ["text": piece, "spoken": true])
        req.timeoutInterval = 30   // the first piece may wait for the voice to load; past this the phone's own takes over
        return try await perform(req)
    }

    /// Upload a WAV recording; the machine's Whisper returns the words.
    func transcribe(_ file: URL) async throws -> String {
        try await transcribe(wav: Data(contentsOf: file), preview: false)
    }

    /// The live words while you talk: the utterance so far, not logged by the machine. Throws when the
    /// machine's Whisper isn't on the GPU, so the phone's own preview takes over.
    func previewWords(_ wav: Data) async throws -> String {
        try await transcribe(wav: wav, preview: true)
    }

    private func transcribe(wav: Data, preview: Bool) async throws -> String {
        let boundary = "vision-\(UUID().uuidString)"
        var body = Data()
        body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"speech.wav\"\r\nContent-Type: audio/wav\r\n\r\n".utf8))
        body.append(wav)
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))
        var req = try request("transcribe", query: preview ? ["preview": "1"] : [:], method: "POST")
        if preview { req.timeoutInterval = 8 }
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        let reply = try await JSONDecoder().decode([String: String].self, from: perform(req))
        return reply["text"] ?? ""
    }

    /// Upload a photo; the machine saves it and returns the path the brain will read it from.
    func upload(_ jpeg: Data) async throws -> String {
        let boundary = "vision-\(UUID().uuidString)"
        var body = Data()
        body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"photo.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".utf8))
        body.append(jpeg)
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))
        var req = try request("upload", method: "POST")
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        let reply = try await JSONDecoder().decode([String: String].self, from: perform(req))
        guard let path = reply["path"] else { throw ClientError.badResponse }
        return path
    }

    /// Upload a video clip. The machine cuts it into frames and transcribes its sound before answering,
    /// so this waits longer than a photo.
    func uploadVideo(_ file: URL) async throws -> String {
        try await uploadFile(file, name: "video.mp4", type: "video/mp4")
    }

    /// Upload a file from disk under its own name; the body streams from disk rather than memory.
    func uploadFile(_ file: URL, name: String, type: String = "application/octet-stream") async throws -> String {
        let boundary = "vision-\(UUID().uuidString)"
        let body = FileManager.default.temporaryDirectory.appendingPathComponent("upload-\(UUID().uuidString)")
        FileManager.default.createFile(atPath: body.path, contents: nil)
        defer { try? FileManager.default.removeItem(at: body) }
        let out = try FileHandle(forWritingTo: body)
        let quoted = name.replacingOccurrences(of: "\"", with: "'").replacingOccurrences(of: "\r", with: "").replacingOccurrences(of: "\n", with: "")
        try out.write(contentsOf: Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(quoted)\"\r\nContent-Type: \(type)\r\n\r\n".utf8))
        let input = try FileHandle(forReadingFrom: file)
        while let chunk = try input.read(upToCount: 1 << 20), !chunk.isEmpty { try out.write(contentsOf: chunk) }
        try input.close()
        try out.write(contentsOf: Data("\r\n--\(boundary)--\r\n".utf8))
        try out.close()
        var req = try request("upload", method: "POST")
        req.timeoutInterval = 300
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await session.upload(for: req, fromFile: body)
        guard let http = response as? HTTPURLResponse else { throw ClientError.badResponse }
        guard (200..<300).contains(http.statusCode) else {
            let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"]
            throw ClientError.http(http.statusCode, detail.map { "\($0)" } ?? "")
        }
        guard let path = try JSONDecoder().decode([String: String].self, from: data)["path"] else { throw ClientError.badResponse }
        return path
    }

    /// A picture a reply shows (`![caption](/abs/path.png)`), fetched from the machine by its path there.
    func replyImage(path: String) async -> UIImage? {
        if let cached = replyImageCache.object(forKey: path as NSString) { return cached }
        guard let req = try? request("image", query: ["path": path]), let data = try? await perform(req),
              let image = UIImage(data: data) else { return nil }
        replyImageCache.setObject(image, forKey: path as NSString, cost: cacheCost(image))
        if replyImageSizes[path] != image.size {
            if replyImageSizes.count >= 500 { replyImageSizes.removeAll() }   // kept small; a size lost is learned again
            replyImageSizes[path] = image.size
            UserDefaults.standard.set(replyImageSizes.mapValues { [Double($0.width), Double($0.height)] }, forKey: "replyImageSizes")
        }
        return image
    }

    /// A reply picture already in memory, so its bubble draws it in the very first frame.
    func cachedReplyImage(path: String) -> UIImage? {
        replyImageCache.object(forKey: path as NSString)
    }

    /// A reply picture's size, once it has been fetched before.
    func replyImageSize(path: String) -> CGSize? {
        replyImageSizes[path]
    }

    /// A video sent earlier, streamed from the machine (by its path there) for the in-app player.
    func videoAsset(path: String) -> AVURLAsset? {
        guard let url = try? request("videos/\((path as NSString).lastPathComponent)").url else { return nil }
        return AVURLAsset(url: url, options: ["AVURLAssetHTTPHeaderFieldsKey": ["Authorization": "Bearer \(token)"]])
    }

    /// The machine's queue for a chat is the one that counts: a message queued elsewhere (the machine, another
    /// phone, or before the app was reopened) shows up, and one taken out elsewhere goes. One sent from here that
    /// the machine hasn't listed yet is still on its way, so it stays.
    private func syncQueued(_ chat: ChatInfo) {
        guard var list = transcripts[chat.id] else { return }
        let theirs = chat.queued.map { text -> Message in
            var m = Message.fromServer(role: .user, text: text)
            m.queued = true
            m.queueSeen = true
            return m
        }
        let left = leftQueue[chat.id] ?? []
        var changed = false
        for i in list.indices.reversed() where list[i].queued {
            if theirs.contains(where: { $0.sameTurn(as: list[i]) }) {
                if !list[i].queueSeen { list[i].queueSeen = true; changed = true }
            } else if list[i].queueSeen {
                list.remove(at: i)   // listed before, gone now: removed or sent in from elsewhere
                changed = true
            }
        }
        for m in theirs where !list.contains(where: { $0.queued && $0.sameTurn(as: m) }) && !left.contains(where: { $0.sameTurn(as: m) }) {
            list.append(m)
            changed = true
        }
        if changed { transcripts[chat.id] = list }
    }

    /// Recover a chat's typed and spoken turns from the machine, including after a reconnect.
    func fetchHistory(chat id: String) async {
        guard let wanted = chats.first(where: { $0.id == id })?.historyID else { return }
        guard let req = try? request("history", query: ["limit": "60", "chat": id]), let data = try? await perform(req),
              let items = try? JSONDecoder().decode([HistoryItem].self, from: data) else { return }
        guard let now = chats.first(where: { $0.id == id }), now.historyID == wanted else { return }
        let current = transcripts[id] ?? []
        // The part of the transcript history must not touch: the turn in flight (its user bubble onward)
        // while busy, otherwise just the trailing notes.
        let inFlight = Message.fromServer(role: .user, text: now.userText)
        let tailStart: Int
        if now.busy {
            let userStart = current.lastIndex { !now.userText.isEmpty && $0.sameTurn(as: inFlight) }
            tailStart = userStart ?? current.lastIndex { $0.streaming } ?? current.endIndex
        } else {
            tailStart = current.endIndex - current.reversed().prefix(while: { $0.role == .note }).count
        }
        let tail = Array(current[tailStart...])
        var fresh = items.map { item -> Message in
            // "note": a project's threads reporting back, a line rather than anyone's bubble.
            if item.role == "shell" {
                var row = Message(role: .shell, text: item.text)
                row.shellOutput = item.output ?? ""
                row.shellCode = item.code
                row.shellRunning = item.running ?? false
                return row
            }
            if item.role == "checkin" {
                // A scout's check-in: one card with its report (and how many things need you after it).
                var row = Message(role: .note, text: item.text)
                row.checkin = CheckinCard(label: item.label ?? "Check-in", started: item.started, ended: item.ended,
                                          needsYou: item.needs_you ?? 0)
                row.durationMs = item.duration_ms
                return row
            }
            var m = Message.fromServer(role: item.role == "user" ? .user : item.role == "note" ? .note : .assistant, text: item.text)
            if let error = item.error, !error.isEmpty {
                m.text += (m.text.isEmpty ? "" : "\n\n") + "Vision could not answer: \(error)"
                m.isError = true
            }
            m.agents = (item.agents ?? []).map(AgentRow.init)
            m.tools = (item.tools ?? []).map(ToolRow.init)
            m.durationMs = item.duration_ms
            return m
        }
        if now.busy, !now.userText.isEmpty, let i = fresh.lastIndex(where: { $0.sameTurn(as: inFlight) }) {
            // The session log may already hold the in-flight turn; the streaming bubble owns it until done.
            fresh.removeSubrange(i...)
        }
        // Keep the ids (and photos) of bubbles that are already on screen so the view (and its pinned turn) doesn't reset.
        // Notes are matched past on both sides: the machine keeps a thread report's, but not the screen's own.
        let shown = current[..<tailStart].filter { $0.role != .note }
        let spoken = fresh.indices.filter { fresh[$0].role != .note }
        var matched = 0
        for (i, j) in zip(spoken.reversed(), shown.indices.reversed()) {
            guard fresh[i].sameTurn(as: shown[j]) else { break }
            let restoredAgents = fresh[i].agents
            let restoredTools = fresh[i].tools
            let took = fresh[i].durationMs ?? shown[j].durationMs   // the machine forgets it on a restart; the screen keeps it
            let shell = fresh[i]
            fresh[i] = Message(id: shown[j].id, role: fresh[i].role, text: fresh[i].text, images: shown[j].images, imagePaths: fresh[i].imagePaths, files: fresh[i].files)
            fresh[i].shellOutput = shell.shellOutput
            fresh[i].shellCode = shell.shellCode
            fresh[i].shellRunning = shell.shellRunning
            fresh[i].answered = shown[j].answered
            fresh[i].agents = restoredAgents.isEmpty ? shown[j].agents : restoredAgents
            fresh[i].tools = restoredTools.isEmpty ? shown[j].tools : restoredTools
            fresh[i].durationMs = took
            matched += 1
        }
        // The machine agrees with what is on screen: leave the list alone.  Swapping it for the (limit-trimmed,
        // note-free) copy would change the content above the pinned turn and shift the transcript.
        if !now.busy, matched >= 2 {
            var updated = current
            for message in spoken.suffix(matched).map({ fresh[$0] }) where message.role == .assistant {
                if let i = updated.firstIndex(where: { $0.id == message.id }) {
                    updated[i].agents = message.agents
                    updated[i].tools = message.tools
                    updated[i].durationMs = message.durationMs
                }
            }
            if updated != current { transcripts[id] = updated }
            loadedHistory[id] = wanted
            return
        }
        transcripts[id] = fresh + tail
        loadedHistory[id] = wanted
        loadImages(chat: id)
    }

    /// Photos the machine knows about but the phone hasn't got (a reloaded transcript, a turn sent from
    /// another client): fetch them by name and drop them into their bubbles.
    private func loadImages(chat id: String) {
        for message in transcripts[id] ?? [] where message.role == .user && message.images.count < message.imagePaths.count && !loadingImages.contains(message.id) {
            let paths = message.imagePaths
            loadingImages.insert(message.id)
            Task {
                defer { loadingImages.remove(message.id) }
                var images: [UIImage] = []
                for path in paths {
                    if let cached = uploadCache.object(forKey: path as NSString) { images.append(cached); continue }
                    let name = (path as NSString).lastPathComponent
                    guard let req = try? request("uploads/\(name)"), let data = try? await perform(req), let image = UIImage(data: data) else {
                        images.append(UIImage(systemName: "photo") ?? UIImage())
                        continue
                    }
                    let preview = await image.byPreparingThumbnail(ofSize: CGSize(width: 1024, height: 1024)) ?? image
                    uploadCache.setObject(preview, forKey: path as NSString, cost: cacheCost(preview))
                    images.append(preview)
                }
                guard var list = transcripts[id], let idx = list.firstIndex(where: { $0.id == message.id }) else { return }
                list[idx].images = images
                transcripts[id] = list
            }
        }
    }

    func fetchModels() async throws -> ModelCatalogue {
        var query: [String: String] = [:]
        if let id = active?.id {
            query["chat"] = id
        } else {
            // No chat open: the catalogue for the pick held for the next one, with that model's levels.
            if let nextModel { query["model"] = nextModel }
            if let nextEffort { query["effort"] = nextEffort }
        }
        return try await JSONDecoder().decode(ModelCatalogue.self, from: perform(request("models", query: query)))
    }

    /// The model and effort new chats start on (config.toml on the machine), with every model to choose from.
    func fetchDefaults() async throws -> ModelCatalogue {
        try await JSONDecoder().decode(ModelCatalogue.self, from: perform(request("defaults")))
    }

    /// Save a new default; the machine fits the effort to the model and answers with what it saved.
    func saveDefaults(model: String, effort: String) async throws -> ModelCatalogue {
        let req = try request("defaults", method: "POST", json: ["model": model, "effort": effort])
        return try await JSONDecoder().decode(ModelCatalogue.self, from: perform(req))
    }

    private struct VoiceRate: Decodable { let rate: Double }

    /// How fast the voice speaks ([voice] rate on the machine): 1.0 as spoken, 0.5 to 2.0.
    func fetchVoiceRate() async throws -> Double {
        try await JSONDecoder().decode(VoiceRate.self, from: perform(request("voice/rate"))).rate
    }

    /// Save a new speaking speed; the machine clamps it to 0.5–2.0 and answers with what it saved.
    func saveVoiceRate(_ rate: Double) async throws -> Double {
        let req = try request("voice/rate", method: "POST", json: ["rate": min(2.0, max(0.5, rate))])
        return try await JSONDecoder().decode(VoiceRate.self, from: perform(req)).rate
    }

    private struct VoicePower: Decodable { let on: Bool }

    /// The voice switch: off unloads the voice and speech recognition on the computer, freeing its GPU; on loads
    /// them again (in the background: /health shows them loading). Answers with the state saved.
    func setVoicePower(_ on: Bool) async throws -> Bool {
        let req = try request("voice/power", method: "POST", json: ["on": on])
        let saved = try await JSONDecoder().decode(VoicePower.self, from: perform(req)).on
        voiceOn = saved
        return saved
    }

    /// The voice Vision speaks in ([voice] voice on the machine) and the ones made there to choose from.
    func fetchVoices() async throws -> VoiceCatalogue {
        try await JSONDecoder().decode(VoiceCatalogue.self, from: perform(request("voice")))
    }

    /// A voice to hear before choosing it: the clip it was made from, as a WAV.
    func voiceSample(_ id: String) async throws -> Data {
        try await perform(request("voice/sample", query: ["voice": id]))
    }

    /// Save a new voice; the machine speaks in it from the next reply on and answers with what it saved.
    func saveVoice(_ id: String) async throws -> VoiceCatalogue {
        let req = try request("voice", method: "POST", json: ["voice": id])
        return try await JSONDecoder().decode(VoiceCatalogue.self, from: perform(req))
    }

    /// The machine's providers, drawn its way (a server added there gets its own icon and tint).
    func fetchProviders() {
        Task { [weak self] in
            guard let self else { return }
            if let list = try? await JSONDecoder().decode(ProviderList.self, from: perform(request("providers"))) {
                await MainActor.run {
                    ProviderStyle.take(list.providers)
                    self.providers = list.providers
                }
            }
        }
    }

    func fetchSessions() async throws -> [SessionRow] {
        try await JSONDecoder().decode([SessionRow].self, from: perform(request("sessions")))
    }

    /// Subscription usage for every provider (each is a CLI call on the machine; a few seconds).
    func fetchUsage() async throws -> UsageReport {
        var query: [String: String] = ["provider": "all"]
        if let id = activeChatID { query["chat"] = id }
        return try await JSONDecoder().decode(UsageReport.self, from: perform(request("usage", query: query)))
    }

    /// Fetch usage now; a failure keeps the last report on screen.
    func refreshUsage() async {
        guard !usageRefreshing else { return }
        usageRefreshing = true
        defer { usageRefreshing = false }
        do {
            usage = try await fetchUsage()
            usageFetchedAt = Date()
            usageError = nil
        } catch {
            usageError = error.localizedDescription
        }
    }

    /// Spend one of a provider's banked resets (`id`'s, else the provider's pick). The fresh usage row it comes back with replaces the old one.
    func useBankedReset(_ provider: String, id: String? = nil) async throws -> UsageReport.ResetResult {
        var body: [String: Any] = ["provider": provider]
        if let id { body["id"] = id }
        if let id = activeChatID { body["chat"] = id }
        let result = try await JSONDecoder().decode(UsageReport.ResetResult.self,
                                                    from: perform(request("usage/reset", method: "POST", json: body)))
        if let i = usage?.providers.firstIndex(where: { $0.provider == provider }) {
            usage?.providers[i] = result.provider
            usageFetchedAt = Date()
        }
        return result
    }

    // MARK: - Library

    /// The Library, or one project's (what was sent in its chat, what its threads made, its replies' pictures).
    func fetchLibrary(project: String? = nil) async throws -> [LibraryItem] {
        let query = project.map { ["project": $0] } ?? [:]
        return try await JSONDecoder().decode([LibraryItem].self, from: perform(request("library", query: query)))
    }

    /// Delete a Library item from the machine, and forget any copy of it this session holds. From a project's
    /// Library, a thread's file only leaves the list (it belongs to the repo); something sent from here goes.
    func deleteLibraryItem(path: String, project: String? = nil) async throws {
        var query = ["path": path]
        if let project { query["project"] = project }
        _ = try await perform(request("library", query: query, method: "DELETE"))
        replyImageCache.removeObject(forKey: path as NSString)
        thumbCache.removeAllObjects()
    }

    /// Delete several Library items in one go; the paths the machine actually deleted.
    func deleteLibraryItems(paths: [String]) async throws -> [String] {
        struct Deleted: Decodable { let deleted: [String] }
        let data = try await perform(request("library/delete", method: "POST", json: ["paths": paths]))
        let done = try JSONDecoder().decode(Deleted.self, from: data).deleted
        for path in done { replyImageCache.removeObject(forKey: path as NSString) }
        thumbCache.removeAllObjects()
        return done
    }

    /// A small picture of a library item (a video's first frame), cached for the session.
    func thumbnail(path: String, size: Int = 360) async -> UIImage? {
        let key = "\(size):\(path)"
        if let cached = thumbCache.object(forKey: key as NSString) { return cached }
        guard let req = try? request("thumb", query: ["path": path, "size": "\(size)"]), let data = try? await perform(req),
              let image = UIImage(data: data) else { return nil }
        thumbCache.setObject(image, forKey: key as NSString, cost: cacheCost(image))
        return image
    }

    // MARK: - Scheduled tasks

    func fetchScheduled() async throws -> [ScheduledTask] {
        try await JSONDecoder().decode([ScheduledTask].self, from: perform(request("scheduled")))
    }

    func saveScheduled(_ task: ScheduledTask) async throws -> ScheduledTask {
        let req = try request("scheduled", method: "POST", json: task.body)
        return try await JSONDecoder().decode(ScheduledTask.self, from: perform(req))
    }

    func deleteScheduled(_ id: String) async throws {
        _ = try await perform(request("scheduled/\(id)", method: "DELETE"))
    }

    /// Run a task now; the machine opens a chat for it, which this switches to.
    func runScheduled(_ id: String) async throws {
        let data = try await perform(request("scheduled/\(id)/run", method: "POST"))
        if let chat = (try? JSONDecoder().decode([String: String].self, from: data))?["chat"] {
            try? await Task.sleep(for: .milliseconds(300))   // its `chat` frame is on the way
            select(chat: chat)
        }
    }

    // MARK: - Memory

    /// The core's facts and the list of topic files, or one topic's facts with `topic`.
    func fetchMemory(topic: String = "") async throws -> MemoryPage {
        let query = topic.isEmpty ? [:] : ["topic": topic]
        return try await JSONDecoder().decode(MemoryPage.self, from: perform(request("memory", query: query)))
    }

    func remember(_ fact: String, topic: String = "") async throws {
        _ = try await perform(request("memory", method: "POST", json: ["fact": fact, "topic": topic]))
    }

    func forget(_ fact: MemoryFact, topic: String) async throws {
        _ = try await perform(request("memory/forget", method: "POST", json: ["line": fact.line, "topic": topic]))
    }

    // MARK: - Notifications

    /// Ask for permission (once; iOS remembers the answer) and for the device token. AppDelegate hands
    /// the token to `registerPush`, on every launch and every time the machine is reached again.
    private func enablePush() {
        #if DEBUG
        if CommandLine.arguments.contains("--no-push") { return }   // simulator screenshots: no permission alert on top
        #endif
        Task {
            let center = UNUserNotificationCenter.current()
            guard (try? await center.requestAuthorization(options: [.alert, .sound])) == true else { return }
            UIApplication.shared.registerForRemoteNotifications()
        }
    }

    /// Tell the machine where reminders and scheduled replies go: this phone's token and the app's bundle id.
    func registerPush(token hex: String) {
        guard isConfigured else { return }
        // The key goes to the machine over the paired connection and nowhere else; notifications come back sealed with it.
        var body: [String: Any] = ["token": hex, "key": PushKey.base64(PushKey.loadOrCreate()), "name": UIDevice.current.name]
        #if DEBUG
        body["sandbox"] = true   // a development build's token; the machine tries the other host if this is wrong
        #endif
        UserDefaults.standard.set(hex, forKey: "pushToken")
        Task {
            if let req = try? request("push/register", method: "POST", json: body) { _ = try? await perform(req) }
        }
    }

    /// Take this phone off a machine's notifications as the app leaves it (unpaired, deleted, or another Vision
    /// in use), so a phone once paired with someone else's computer stops getting their reminders.
    private func unregisterPush(from base: URL?, bearer: String) {
        guard let hex = UserDefaults.standard.string(forKey: "pushToken"), let base,
              let req = try? request("push/register/\(hex)", method: "DELETE", base: base, bearer: bearer) else { return }
        Task { _ = try? await perform(req) }
    }

    /// The threads' Lock Screen card as the machine sends it (vision/liveactivity.py summary); nil when absent.
    static func threadsCard(_ value: Any?) -> VisionThreadsAttributes.ContentState? {
        guard let dict = value as? [String: Any], let data = try? JSONSerialization.data(withJSONObject: dict) else { return nil }
        return try? JSONDecoder().decode(VisionThreadsAttributes.ContentState.self, from: data)
    }

    /// Hand the machine a token for the threads' card: "start" lets it start one, "card" changes and ends one.
    /// The push key rides along, so the names on the card come sealed for this phone.
    func registerActivity(kind: String, token: String) {
        guard isConfigured else { return }
        var body: [String: Any] = ["kind": kind, "token": token, "key": PushKey.base64(PushKey.loadOrCreate())]
        #if DEBUG
        body["sandbox"] = true
        #endif
        Task {
            if let req = try? request("push/activity", method: "POST", json: body) { _ = try? await perform(req) }
        }
    }

    /// A tapped notification names the chat it came from. On a cold launch the chat list is not here
    /// yet: `applyHello` keeps the id as long as the machine still lists it.
    func open(notifiedChat id: String) {
        if chats.contains(where: { $0.id == id }) {
            select(chat: id)
            Task { await fetchHistory(chat: id) }   // the reminder landed while the app was shut
        } else {
            activeChatID = id
        }
    }

    /// A project thread's notification tapped: the project's chat comes up and the thread opens over it
    /// (ThreadStrip takes `shownThread` once that chat is on screen).
    func open(notifiedProject project: String, thread: String, chat: String, sheet: Bool = false) {
        // `sheet`: the scout's own sheet even with no thread to open (a request to approve, say).
        shownThread = thread.isEmpty && !sheet ? nil : ShownThread(project: project, thread: thread)
        if let p = projects.first(where: { $0.id == project }) {
            openProject(p)
        } else if !chat.isEmpty {
            open(notifiedChat: chat)   // cold start: the projects arrive with hello, the chat is picked from it
        }
    }

    /// While connected: refresh when the report is missing or older than `usageInterval`, then wait out
    /// the rest of the interval. Coming back from the background only fetches if it has gone stale.
    private func keepUsageFresh() {
        usageTask?.cancel()
        usageTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                let age = self.usageFetchedAt.map { -$0.timeIntervalSinceNow } ?? .infinity
                if age >= Self.usageInterval {
                    await self.refreshUsage()
                }
                let wait = Self.usageInterval - (self.usageFetchedAt.map { -$0.timeIntervalSinceNow } ?? 0)
                try? await Task.sleep(for: .seconds(max(wait, 30)))
            }
        }
    }
}

/// Calls back once a screen frame (at most 60 a second) with the time since the last one, until stopped.
@MainActor
private final class FrameTicker: NSObject {
    private var link: CADisplayLink?
    private let tick: (Double) -> Void

    init(_ tick: @escaping (Double) -> Void) {
        self.tick = tick
        super.init()
        let link = CADisplayLink(target: self, selector: #selector(step))
        link.preferredFrameRateRange = CAFrameRateRange(minimum: 30, maximum: 60, preferred: 60)
        link.add(to: .main, forMode: .common)
        self.link = link
    }

    @objc private func step(_ link: CADisplayLink) {
        tick(link.targetTimestamp - link.timestamp)
    }

    func stop() {
        link?.invalidate()   // the link holds this ticker; letting go of it ends the loop
        link = nil
    }
}
