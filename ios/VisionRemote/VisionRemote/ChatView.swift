import AVFoundation
import os
import PhotosUI
import SwiftUI

/// Signposts round the chat's heavy moments, so Instruments and the switching benchmark (FakeServerTests) can time them.
enum Perf {
    static let signposter = OSSignposter(subsystem: "VisionRemote", category: "perf")

    static func begin(_ name: StaticString) -> OSSignpostIntervalState {
        signposter.beginInterval(name, id: signposter.makeSignpostID())
    }

    static func end(_ name: StaticString, _ state: OSSignpostIntervalState) {
        signposter.endInterval(name, state)
    }
}

#if DEBUG
/// Performance hook: `--perf-drive <title>` switches into the chat whose title starts with that and back a few
/// times, then slides the chats drawer half out and back over it, as a finger would, frame by frame, and prints
/// how the frames kept up ("PERF …" lines on stdout). No test runner attached, so its accessibility snapshots
/// don't skew the numbers.
@MainActor final class PerfDrive: NSObject {
    /// Posted with the drawer's drag in points, or nil to let go.
    static let drag = Notification.Name("PerfDriveDrag")
    /// Posted with true or false: open or close the drawer as its button does.
    static let snap = Notification.Name("PerfDriveSnap")

    private var link: CADisplayLink?
    private var gaps: [Double] = []
    private var stamps: [Double] = []   // ms since reset, per gap: where in the switch the slow frames fell
    private var began: CFTimeInterval = 0
    private var last: CFTimeInterval = 0
    private var budget: Double = 0
    private var onTick: ((CFTimeInterval) -> Void)?

    static func run(_ client: VisionClient) async {
        if let i = CommandLine.arguments.firstIndex(of: "--swipe-drive") {   // PROFILE-TEMP
            try? await Task.sleep(for: .seconds(4))
            // `--swipe-drive <title>` measures on the chat whose title starts so: an idle one, or the sweep of a live
            // tool row (ScanRenderer) is in the figures too.
            if i + 1 < CommandLine.arguments.count, let chat = client.chats.first(where: { $0.title.hasPrefix(CommandLine.arguments[i + 1]) }) {
                client.select(chat: chat.id)
                try? await Task.sleep(for: .seconds(2))
            }
            print("PERF chat \(client.activeChat?.title.prefix(40) ?? "none"): \(client.messages.count) messages")
            let drive = PerfDrive()
            drive.start()
            for _ in 0..<3 { await drive.swipe(cycles: 4, width: 260); drive.report("swipe") }
            for _ in 0..<2 { await drive.snaps(4) }
            drive.stop()
            print("PERF done")
            return
        }
        guard let i = CommandLine.arguments.firstIndex(of: "--perf-drive"), i + 1 < CommandLine.arguments.count else { return }
        let title = CommandLine.arguments[i + 1]
        try? await Task.sleep(for: .seconds(4))
        guard let heavy = client.chats.first(where: { $0.title.hasPrefix(title) }),
              let light = client.chats.first(where: { $0.id != heavy.id }) else { print("PERF no chat titled \(title)"); return }
        let drive = PerfDrive()
        drive.start()
        func open(_ id: String) { client.select(chat: id) }   // as a tap in the drawer does (ChatsView.open)
        for pass in 0..<4 {
            open(light.id)
            try? await Task.sleep(for: .seconds(2))
            if pass == 0 { BubbleHeights.forget() }   // the first pass opens the heavy chat as if never seen
            drive.reset()
            let built = BubbleHeights.builtUnknown
            open(heavy.id)
            try? await Task.sleep(for: .seconds(2))
            drive.report(pass == 0 ? "switch in (cold)" : "switch in")
            print("PERF   \(BubbleHeights.builtUnknown - built) bubbles built with no height known, \(client.messages.count) messages")
        }
        for (name, id) in [("swipe heavy", heavy.id), ("swipe light", light.id), ("swipe heavy", heavy.id)] {
            open(id)
            try? await Task.sleep(for: .seconds(2))
            await drive.swipe(cycles: 4, width: 260)
            drive.report(name)
        }
        drive.stop()
        print("PERF done")
    }

    private func start() {
        let link = CADisplayLink(target: self, selector: #selector(tick))
        link.preferredFrameRateRange = CAFrameRateRange(minimum: 60, maximum: 120, preferred: 120)
        link.add(to: .main, forMode: .common)
        self.link = link
    }

    private func stop() { link?.invalidate() }

    private func reset() {
        gaps = []
        stamps = []
        began = 0
        last = 0
    }

    @objc private func tick(_ link: CADisplayLink) {
        if began == 0 { began = link.timestamp }
        if last > 0 { gaps.append((link.timestamp - last) * 1000); stamps.append((link.timestamp - began) * 1000) }
        last = link.timestamp
        budget = (link.targetTimestamp - link.timestamp) * 1000
        onTick?(link.targetTimestamp)
    }

    /// Out to `width` and back `cycles` times, a second each, set every frame as a drag gesture would.
    private func swipe(cycles: Int, width: CGFloat) async {
        reset()
        await withCheckedContinuation { (done: CheckedContinuation<Void, Never>) in
            var began: CFTimeInterval = 0
            onTick = { [weak self] now in
                if began == 0 { began = now }
                let t = now - began
                guard t < Double(cycles) else {
                    self?.onTick = nil
                    NotificationCenter.default.post(name: Self.drag, object: nil)
                    done.resume()
                    return
                }
                NotificationCenter.default.post(name: Self.drag, object: width * 0.5 * (1 - cos(2 * .pi * t)))
            }
        }
    }

    /// Open and close the drawer `count` times as its button would, letting each snap run its course, and prints how
    /// the frames kept up snap by snap (the median one and the worst), which a total over all of them blurred.
    private func snaps(_ count: Int) async {
        var dropped: [Int] = [], late: [Double] = []
        for _ in 0..<count {
            for open in [true, false] {
                reset()
                NotificationCenter.default.post(name: Self.snap, object: open)
                try? await Task.sleep(for: .seconds(0.6))
                let slow = gaps.filter { $0 > budget * 1.5 }
                dropped.append(slow.count)
                late.append(slow.reduce(0) { $0 + $1 - budget })
            }
        }
        dropped.sort(); late.sort()
        print(String(format: "PERF snap: %d snaps, dropped median %d worst %d, late median %.0f ms worst %.0f ms (budget %.1f ms)",
                     dropped.count, dropped[dropped.count / 2], dropped.last ?? 0, late[late.count / 2], late.last ?? 0, budget))
        reset()
    }

    private func report(_ name: String) {
        let slow = gaps.filter { $0 > budget * 1.5 }
        let worst = gaps.max() ?? 0
        let late = slow.reduce(0) { $0 + $1 - budget }
        print(String(format: "PERF %@: %d frames, %d dropped, worst %.0f ms, %.0f ms late in all (budget %.1f ms)",
                     name, gaps.count, slow.count, worst, late, budget))
        let when = zip(stamps, gaps).filter { $0.1 > budget * 1.5 }.map { String(format: "%.0fms +%.0f", $0.0, $0.1) }
        if !when.isEmpty { print("PERF   slow frames at: " + when.joined(separator: ", ")) }
        reset()
    }
}
#endif

struct ChatView: View {
    @EnvironmentObject private var client: VisionClient
    @Environment(\.theme) private var theme
    @State private var draft = ""
    @State private var attachments: [Attachment] = []
    @State private var pickedPhotos: [PhotosPickerItem] = []
    @State private var showPhotos = false
    @State private var showFiles = false
    @State private var attach: AttachMode?          // the popover above the "+", or the camera / photos card in the composer's slot

    enum AttachMode { case menu, camera, photos }
    @State private var uploading = false
    @State private var showSettings = false
    @State private var showWelcome = false            // nothing paired yet: the first-launch screen
    @State private var showFolders = false            // the folder picker, from an empty chat's folder line
    @State private var showSessions = false
    @State private var showScheduled = false   // a tap on the Next Up widget
    #if DEBUG
    @State private var showGlances = false     // `--open widgets`: the widgets' faces, with what the computer says
    #endif
    @State private var showUsage = false
    @State private var contextNote: String?           // the gauge's readout, shown just above the composer
    /// Live voice mode, inside the chat: the words stream into the transcript and the composer stays live.
    @StateObject private var call = VoiceCall()
    /// Distinguishes a newly started call from an old start task still waiting on a permission prompt.
    @State private var voiceSessionID: UUID?
    @State private var modelsExpanded = false
    @State private var effortsExpanded = false
    @StateObject private var picker = ModelPickerState()
    @State private var pillFrames: [PickerKind: CGRect] = [:]
    @State private var attachFrame: CGRect = .zero    // the "+" in global coordinates; the attach card grows out of it
    @State private var attachHole: CGRect = .zero     // the open menu or card in global coordinates; the lid leaves it live
    @State private var attachMenuSize: CGSize = .zero
    @State private var closingFromCard = false
    @State private var cardSize: CGSize = .zero     // the camera / photos card, so the menu's glass can morph to and from it
    /// The menu opened with the keyboard up: it and its cards live in the keyboard's window, over the keys, ChatGPT style.
    @State private var overKeyboard = false
    @State private var screenHeight: CGFloat = 0     // measured by the keyboard layer
    @State private var showChats = false
    /// The Projects screen is the main screen (the drawer's Projects row), a project's chat pushed on it in `projectPath`.
    @State private var projectsHome = false
    @State private var projectPath: [String] = []
    /// Project opens are pushed optimistically; remember the request so a late machine reply won't undo a swipe back.
    @State private var pendingProjectOpenID: String?
    /// Once the user swipes a project away, background chat updates must not push it back over the list.
    @State private var dismissedProjectIDs: Set<String> = []
    /// The chat screen is sliding for the drawer, until it comes to rest (see SideDrawer.away).
    @State private var drawerAway = false
    @State private var inputFocused = false
    @State private var composerFrame: CGRect = .zero
    /// Shell mode: `!` typed into the empty composer, the next line runs on the computer instead of going to Vision.
    @State private var shellMode = false
    @State private var composerCard: CGRect = .zero   // the composer card itself on the chat card; the attach menu lines up with it
    @State private var pageBox: CGRect = .zero        // the chat page on the chat card; the attach menu is placed in it
    /// The user message sent from this screen most recently; it gets pinned to the top so the reply reads downward.
    @State private var pinnedTurn: UUID?
    /// True for a moment after a chat is opened, while its transcript may still be loading.
    @State private var opening = false
    /// Bumped on every chat open, so a timer left over from an earlier open (fast switching) does nothing.
    @State private var openGeneration = 0
    /// Earliest messages not drawn yet while a chat comes on screen (see `stageIn`).
    @State private var heldBack = 0
    @State private var stagedChat: String?   // the chat `heldBack` was set for (see earlierMessages)
    /// The transcript as it last drew before a switch (TranscriptShotter), fading out over the next chat's.
    @State private var swapShot: UIImage?
    @State private var swapShotOpacity = 1.0
    /// The new chat's transcript waits out the old one's picture, then rises in (see fadeSwapShot).
    @State private var incomingOpacity = 1.0
    @State private var incomingOffset: CGFloat = 0
    @State private var shotter = TranscriptShotter.Box()
    /// The message a search hit opened the chat at, lit for a moment (see jumpToFound).
    @State private var found: Message.ID?
    /// A chat just opened is still filling in above the fold (its history landing, pictures arriving): the scroll
    /// holds its bottom still, in the same layout pass, until a drag, the keyboard or a new turn takes over.
    @State private var settling = false
    /// The latest line is out of sight under the composer: the jump arrow is offered.
    @State private var endHidden = false
    /// Where that line ends on screen, kept off the view's state so scrolling never redraws the chat.
    @State private var endMark = EndMark()
    /// After a tap on the jump arrow the view rides the reply down as it streams, until it ends or you drag.
    @State private var following = false
    /// A scripted scroll (the send glide or a jump) is under way; the arrow stays down meanwhile.
    @State private var gliding = false
    /// Bumped for every scripted scroll and every drag, so a glide's late follow-up knows it was overtaken.
    @State private var glideGeneration = 0
    /// Where the transcript sat when the finger now dragging it landed; gone once that drag ends. A swipe for the drawer
    /// scrolls the transcript by its vertical wobble before the drawer claims it, then freezes it there (scrolling is off
    /// while the drawer is out) until it snapped back on closing; the drawer's pull puts it back here instead. Only a
    /// drag still under way counts: an earlier scroll's start kept for a moment glided a swipe straight after it back
    /// to where that scroll began.
    @State private var dragFrom: CGFloat?
    @State private var transcriptScroll = ScrollPosition()

    /// The plan approval waiting on the user. It is answered from a bar above the composer, never the
    /// question card, which would cover the plan before it could be read.
    private var waitingPlan: Question? {
        guard let form = client.pendingQuestions, form.questions.count == 1,
              let q = form.questions.first, q.header == "Plan" else { return nil }
        return q
    }

    /// The question card: any form but a plan approval.
    private var shownQuestions: Binding<QuestionForm?> {
        Binding(
            get: { waitingPlan == nil ? client.pendingQuestions : nil },
            set: { if $0 == nil { client.pendingQuestions = nil } })
    }

    var body: some View {
        // On a project's chat pushed over the Projects screen, the edge swipe goes back rather than out to the drawer.
        SideDrawer(isOpen: $showChats, away: $drawerAway, swipeEnabled: !pickerExpanded && attach == nil && projectPath.isEmpty) {
            ChatsView(projectsShown: projectsHome, showProjects: showProjects, openScout: openFromProjects,
                      onClose: { withAnimation(SideDrawerMotion.animation) { showChats = false } })
                .equatable()
        } content: {
            chatScreen
        }
        #if DEBUG
        .task { await PerfDrive.run(client) }
        #endif
        // Read through a binding, here and for drawerAway below: read in this body, every open and close redrew the
        // whole chat screen (a long transcript's worth) at the start of the snap, and the snap stuttered. For the same
        // reason the frames below are measured on the chat card (SideDrawerMotion.card), which a slide doesn't move.
        .onDrawerChange($showChats) { open in
            if open { inputFocused = false }
        }
        .onChange(of: client.editDraft) { _, text in
            // A queued message taken back to edit (long-press → Edit): into the composer, keyboard up.
            guard let edit = text else { return }
            draft = edit.text
            attachments += edit.attachments
            client.editDraft = nil
            inputFocused = true
        }
        // Keep this outside the transcript, whose identity changes with the chat. A live call is
        // owned by the chat that started it, so switching chats must tear it down in the same UI update.
        // A call started on the empty screen opens its chat with the first thing said: that is the
        // call's own chat arriving, not a switch away from it.
        // Leaving a draft for the chat it was opened over keeps the same chat id: the picture taken of the empty
        // screen must still fade, or it sits over that chat for good.
        .onChange(of: client.draft == nil) { _, left in
            if left, swapShot != nil { fadeSwapShot() }
        }
        .onChange(of: client.activeChatID) { old, _ in
            shellMode = false
            if old != nil { endVoice(chat: old) }
            if swapShot != nil { fadeSwapShot() }
            // On the Projects screen: a project's chat arriving (tapped there, just made, or a notification's) is
            // pushed on it; any other chat takes the screen back.
            guard projectsHome, client.activeChatID != nil else { return }
            if let project = client.activeProject {
                if projectPath == [project.id] {
                    if pendingProjectOpenID == project.id { pendingProjectOpenID = nil }
                } else if pendingProjectOpenID == project.id {
                    pendingProjectOpenID = nil
                } else if projectPath.isEmpty && !dismissedProjectIDs.contains(project.id) {
                    projectPath = [project.id]
                }
            } else if (client.active?.project ?? "").isEmpty {
                // A chat of no project's. (One whose project hasn't arrived yet, as on a reconnect, isn't that.)
                showProjects(false)
            }
        }
    }

    private var pickerExpanded: Bool { modelsExpanded || effortsExpanded }
    private var threadsKey: String { "\(client.activeProject?.id ?? "")|\(client.connection == .online)" }
    /// How many more photos and videos the message can take: the cap counts what the composer already holds.
    private var mediaRoom: Int { max(0, RecentPhotosGrid.maxSelection - attachments.filter { $0.file == nil }.count) }
    /// The camera or photos card is filling the composer's slot.
    private var cardOpen: Bool { attach != nil && attach != .menu }

    /// The main screen: a chat, or the Projects screen in its place with a project's chat pushed on top of it
    /// (back returns to the list; the edge swipe pops it rather than pulling the drawer out).
    private var chatScreen: some View {
        Group {
            if projectsHome {
                // The Scouts screen is the drawer's like the chat (plain SwiftUI, see SideDrawer), with the navigation stack
                // over it only for the scouts' chats pushed on it: inside the stack UIKit hands a screen the safe area
                // itself, worked out from where the drawer has it on screen. Empty, the stack is see-through and lets
                // touches through.
                ZStack {
                    ProjectsHome(onOpen: openFromProjects)
                        .topBar { scoutsBar }
                        .overlay(alignment: .top) { toastView }
                    NavigationStack(path: $projectPath) {
                        Color.clear
                            .containerBackground(.clear, for: .navigation)
                            .navigationTitle("Scouts")
                            .toolbar(.hidden, for: .navigationBar)
                            .navigationDestination(for: String.self) { id in
                                if client.activeProject?.id == id {
                                    chatPage(pushed: true)
                                } else if let project = client.projects.first(where: { $0.id == id }) {
                                    ProjectOpeningView(title: project.title)
                                } else {
                                    ProjectOpeningView(title: "Project")
                                }
                            }
                    }
                    .allowsHitTesting(!projectPath.isEmpty)
                }
            } else {
                chatPage(pushed: false)
            }
        }
        // While the attach menu or a card is open a window-level lid covers everything but it: nav bar, composer and
        // keyboard look exactly as they do, but nothing highlights or types, a tap or swipe just closes the card.
        .background(TouchLid(active: attach != nil, hole: overKeyboard ? keyboardAttachHole : attachHole) { closeAttach() }.frame(width: 0, height: 0))
        .background(KeyboardLayer(active: inputFocused || (overKeyboard && attach != nil), hole: keyboardAttachHole,
                                  content: keyboardAttachLayer).frame(width: 0, height: 0))
        .onPreferenceChange(AttachHoleKey.self) { attachHole = $0 }
        .onChange(of: projectPath) { oldPath, newPath in
            if projectsHome, let id = oldPath.first, newPath.isEmpty {
                dismissedProjectIDs.insert(id)
            }
        }
        .onAppear {
            client.willSwitchChat = { swapShot = shotter.take(); swapShotOpacity = 1 }
            if client.isConfigured {
                client.connect()
            } else {
                showWelcome = true
            }
            #if DEBUG
            if CommandLine.arguments.contains("--compose") { ComposeRequest.pending = true }   // as Message Vision leaves it
            #endif
            if CommandLine.arguments.contains("--drawer-cycle") {   // PROFILE-TEMP
                Timer.scheduledTimer(withTimeInterval: 1.2, repeats: true) { _ in
                    Task { @MainActor in withAnimation(SideDrawerMotion.animation) { showChats.toggle() } }
                }
            }
            takeComposeRequest()
            // Development: `--open model|chats` on launch (simulator screenshots).
            if let i = CommandLine.arguments.firstIndex(of: "--open"), i + 1 < CommandLine.arguments.count {
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                    switch CommandLine.arguments[i + 1] {
                    case "chats": showChats = true
                    case "projects": showProjects(true)
                    case "scout":
                        // The first scout's chat with its sheet up (simulator screenshots).
                        if let first = client.projects.first { client.open(notifiedProject: first.id, thread: "", chat: first.chat, sheet: true) }
                    case "sessions": showSessions = true
                    case "usage": showUsage = true
                    #if DEBUG
                    case "widgets": showGlances = true
                    #endif
                    case "settings": showSettings = true
                    case "voice": startVoice()
                    case "question":
                        client.pendingQuestions = QuestionForm(questions: [
                            Question(question: "Which database should the sync layer use?", header: "Storage", multiSelect: false,
                                     options: [.init(label: "SQLite (Recommended)", description: "Local file, zero setup, fine for one user"),
                                               .init(label: "Postgres", description: "Needs a server, scales to many writers"),
                                               .init(label: "Keep JSON files", description: nil)]),
                            Question(question: "Which platforms should it ship on?", header: "Targets", multiSelect: true,
                                     options: [.init(label: "iPhone", description: nil), .init(label: "Mac", description: nil),
                                               .init(label: "Linux terminal", description: nil)]),
                        ])
                    case "switch":
                        // Motion hook: hop between chats as a drawer tap does, every three seconds.
                        Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { _ in
                            let ids = client.chats.map(\.id)
                            guard ids.count > 1 else { return }
                            let next = ids[((ids.firstIndex(of: client.activeChatID ?? "") ?? -1) + 1) % ids.count]
                            var still = Transaction()
                            still.disablesAnimations = true
                            withTransaction(still) { client.select(chat: next) }
                        }
                    case "project":
                        // Motion hook: the Projects screen, its first project pushed and popped every four seconds.
                        showProjects(true)
                        Timer.scheduledTimer(withTimeInterval: 4, repeats: true) { _ in
                            if !projectPath.isEmpty {
                                projectPath = []
                            } else if let project = client.projects.first {
                                openFromProjects(project)
                            }
                        }
                    case "attach": openAttach()
                    case "attachkb":
                        inputFocused = true
                        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { openAttach() }
                    case "attach-kb":
                        // Keyboard up first, then the menu over it.
                        inputFocused = true
                        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { openAttach() }
                    case "attach-toggle":
                        // Motion hook: open like a tap, hold, close like a tap outside.
                        openAttach()
                        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { closeAttach() }
                    case "attach-kb-toggle":
                        // The same over the keyboard, as ChatGPT's recording.
                        inputFocused = true
                        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { openAttach() }
                        DispatchQueue.main.asyncAfter(deadline: .now() + 3.5) { closeAttach() }
                    default: break
                    }
                }
            }
        }
    }

    /// The chat itself. `pushed`: on top of the Projects screen, where the system's back button takes the drawer
    /// button's place.
    private func chatPage(pushed: Bool) -> some View {
        // The composer floats over the transcript so messages scroll under its translucent card.
        transcript
            .opacity(incomingOpacity).offset(y: incomingOffset)
            // Switching chats: the last chat's transcript, as last drawn, goes before the new one comes in.
            .overlay {
                if let swapShot {
                    Image(uiImage: swapShot).resizable().aspectRatio(contentMode: .fill)
                        .scaleEffect(0.97 + 0.03 * swapShotOpacity)
                        .opacity(swapShotOpacity).clipped().allowsHitTesting(false)
                }
            }
            .scrollDisabled(cardOpen)
            .overlay {
                // While the camera or photos card is up the transcript is frozen; a tap on it just closes the card.
                if cardOpen {
                    Color.black.opacity(0.001).ignoresSafeArea().onTapGesture { closeAttach() }.gesture(swipeToClose)
                }
            }
            .overlay(alignment: .bottom) { note(contextNote, edge: .bottom) { contextNote = nil } }
            .composerBar { inputBar.modifier(DrawerBottomRoom(on: !pushed)) }
            .overlay {
                // The attach menu grows out of the "+" and covers the composer; tap anywhere off it to close.  It
                // can't hang over the keyboard the way ChatGPT's does: the system draws the keys above every
                // window the app owns, so it sits flush with the composer card's left and bottom edges instead.
                // Always mounted, so its glass visibly grows out of the button and shrinks back into it. The
                // tap-catcher stays mounted too: without it the ZStack shrinks to the menu.
                let open = attach == .menu && !overKeyboard
                let menuLeft = composerCard.minX
                let menuBottom = composerCard.maxY
                let blobOffset = CGSize(width: attachFrame.minX - menuLeft, height: attachFrame.maxY - menuBottom)
                ZStack(alignment: .bottomLeading) {
                    Color.black.opacity(0.001).onTapGesture { closeAttach() }.gesture(swipeToClose).allowsHitTesting(open)
                    attachMenu(open: open, card: cardOpen && !overKeyboard, blobOffset: blobOffset)
                        .background(GeometryReader { g in
                            Color.clear.preference(key: AttachHoleKey.self, value: open ? g.frame(in: .global) : .zero)
                        })
                        .allowsHitTesting(open)
                        .padding(.leading, max(0, menuLeft - pageBox.minX))
                        .padding(.bottom, max(0, pageBox.maxY - menuBottom))
                }
                .ignoresSafeArea()
            }
            // The page's place, for the menu above: measured on the page itself, not on the overlay. A GeometryReader in
            // the overlay rebuilt the whole menu on every frame the chat slid for the drawer, and the overlay's own frame
            // (it ignores the safe area, which SwiftUI works out afresh under the shrink) jittered by fractions of a
            // point per frame, each one redrawing the whole chat screen. The two are the same box whenever the menu is
            // up: the chat lays out with no safe area (SideDrawer), and over the keyboard the menu lives elsewhere.
            .onCardFrame { pageBox = $0 }
        .background { ChatPageBackground().ignoresSafeArea() }
        .navigationTitle("Vision")
        .navigationBarTitleDisplayMode(.inline)
        // No bar: just the buttons floating over the chat, no title or background; a fade at the top
        // edge keeps messages scrolling up from clashing with the clock.
        // (A project's chat keeps the title slot: its name and threads sit there.)
        .toolbar(removing: client.activeProject == nil ? .title : nil)
        .toolbarBackground(.hidden, for: .navigationBar)
        // The main screen fades only the strip under the clock and battery, not down past its buttons.
        .softScrollEdges(top: pushed)
        .overlay(alignment: .top) { if !pushed { StatusBarFade() } }
        .onPreferenceChange(PillFrameKey.self) { if !$0.isEmpty { pillFrames = $0 } }
        .overlay {
            // The model and effort buttons each grow into a full-width slider over the dimmed chat,
            // laid across the composer, which blurs underneath it.
            ModelPickerOverlay(state: picker, isExpanded: $modelsExpanded, pillFrame: pillFrames[.model] ?? .zero, composer: composerCard)
            EffortPickerOverlay(state: picker, isExpanded: $effortsExpanded, pillFrame: pillFrames[.effort] ?? .zero, composer: composerCard)
        }
        // One slider at a time: opening the other pill swaps them.
        .onChange(of: modelsExpanded) { _, open in if open { effortsExpanded = false } }
        .onChange(of: effortsExpanded) { _, open in if open { modelsExpanded = false } }
        // On the main screen the buttons are a top bar of the chat's own (`chatBar`), not the navigation bar's, which
        // under the drawer's shrink laid itself out lower and snapped back as the chat landed. A project's chat pushed
        // over the Scouts screen keeps the navigation bar, for its back button.
        .toolbar {
            if pushed {
                // A project's chat: its name and threads as the title; a tap opens the project.
                ToolbarItem(placement: .principal) { ThreadStrip() }
                ToolbarItem(placement: .topBarTrailing) { terminalButton(glass: false) }
            }
        }
        .topBar(!pushed) { chatBar }
        .sheet(isPresented: $showSettings) { SettingsView() }
        .fullScreenCover(isPresented: $showWelcome) { WelcomeView() }
        .sheet(isPresented: $showFolders) {
            FolderPickerView { path in
                showFolders = false
                moveEmptyChat(to: path)
            }
            .presentationDetents([.fraction(0.88), .large])
            .presentationDragIndicator(.visible)
        }
        .sheet(isPresented: $showSessions) {
            SessionsView()
                .presentationDetents([.fraction(0.88), .large])
                .presentationDragIndicator(.visible)
        }
        .sheet(isPresented: $showUsage) {
            UsageView()
                .presentationDetents([.fraction(0.88), .large])
                .presentationDragIndicator(.visible)
        }
        .sheet(isPresented: $showScheduled) {
            ScheduledView()
                .presentationDetents([.fraction(0.88), .large])
                .presentationDragIndicator(.visible)
        }
        #if DEBUG
        .sheet(isPresented: $showGlances) { GlancesPreview() }
        #endif
        .onOpenURL { open(link: $0) }
        .onReceive(NotificationCenter.default.publisher(for: ComposeRequest.now)) { _ in takeComposeRequest() }
        .sheet(item: shownQuestions) { form in
            QuestionSheet(form: form) { client.answer($0) }
                .interactiveDismissDisabled()
        }
        .overlay(alignment: .top) { toastView }
        // A project's chat coming on screen (or the socket coming back to it) fetches its threads; frames keep them live.
        .task(id: threadsKey) {
            if let project = client.activeProject?.id, client.connection == .online {
                await client.refreshThreads(project: project)
            }
        }
    }

    /// A chat's top bar: the drawer, the project's name and threads, and the terminal.
    private var chatBar: some View {
        ChromeBar {
            drawerButton
        } title: {
            // A project's chat: its name and threads as the title; a tap opens the project.
            ThreadStrip()
        } trailing: {
            // Usage, earlier conversations and settings live in the chats drawer.
            terminalButton(glass: true)
        }
    }

    /// The Scouts screen's top bar: the drawer and its title.
    private var scoutsBar: some View {
        ChromeBar {
            drawerButton
        } title: {
            Text("Scouts").themeFont(.headline)
        } trailing: {
            EmptyView()
        }
    }

    /// ☰: opens the drawer, from a chat or from the Projects screen.
    private var drawerButton: some View {
        Button { withAnimation(SideDrawerMotion.animation) { showChats = true } } label: { ChromeGlyph { chatsButtonLabel }.chromeButtonLabel() }
            .chromeButton()
            .accessibilityLabel("Chats")
    }

    /// Opens the chat in the computer's terminal: the top bar's glass button, or a plain one in the navigation bar,
    /// which draws its own glass.
    @ViewBuilder private func terminalButton(glass: Bool) -> some View {
        let button = Button { client.openOnMachine() } label: {
            ChromeGlyph { Image(systemName: "apple.terminal") }.modifier(ChromeLabel(sized: glass))
        }
        .accessibilityLabel("Open on computer")
        .disabled(client.connection != .online || client.activeChat == nil || client.activeChat?.source == "terminal")
        if glass { button.chromeButton() } else { button }
    }

    /// The drawer's Projects row (true) or a chat picked there (false): the main screen swaps at once, still,
    /// while the drawer slides away.
    /// A widget tapped (`vision://…`, see GlanceLink): the screen it was about.
    private func open(link url: URL) {
        guard url.scheme == "vision" else { return }
        switch url.host() {
        case "compose": inputFocused = true
        case "usage": showUsage = true
        case "scheduled": showScheduled = true
        case "project":
            if let id = url.pathComponents.dropFirst().first, let project = client.projects.first(where: { $0.id == id }) {
                client.openProject(project)
            } else {
                showProjects(true)
            }
        case "projects": showProjects(true)
        case "chat":
            if let id = url.pathComponents.dropFirst().first { client.open(notifiedChat: id) }
        case "chats": showChats = true
        default: break
        }
    }

    /// Message Vision (Lock Screen, Control Center): keyboard up, once.
    private func takeComposeRequest() {
        guard ComposeRequest.pending else { return }
        ComposeRequest.pending = false
        inputFocused = true
    }

    private func showProjects(_ on: Bool) {
        var still = Transaction()
        still.disablesAnimations = true
        withTransaction(still) {
            projectsHome = on
            projectPath = []
            pendingProjectOpenID = nil
            dismissedProjectIDs.removeAll()
        }
    }

    /// A project tapped on the Projects screen: its chat, pushed. One already on screen is pushed straight away;
    /// otherwise the machine opens it and it's pushed once it arrives (see the `activeChatID` watch).
    private func openFromProjects(_ project: ProjectInfo) {
        pendingProjectOpenID = project.id
        dismissedProjectIDs.remove(project.id)
        // Its chat already open: the push's first frame draws just the latest bubbles (see `stageIn`), not the
        // whole thread before trimming it, which stalled the start of the slide.
        if client.activeProject?.id == project.id { heldBack = max(0, shown.count - 12) }
        projectPath = [project.id]
        client.openProject(project)
    }

    // MARK: transcript

    /// The transcript as drawn: queued messages wait in the strip above the composer instead, so the
    /// streaming reply never shoves them down, and land here when their turn starts.
    private var shown: [Message] { client.messages.filter { !$0.queued } }

    /// The folder picked from the empty screen: a draft just takes it. An open chat's folder is fixed when
    /// it opened, so a draft starts in the new one instead, and the empty chat it replaces is closed rather
    /// than left behind in the list. A chat in a terminal is left alone: closing it would quit that terminal.
    private func moveEmptyChat(to path: String) {
        let old = client.active
        let unused = client.messages.isEmpty
        client.startNewChat(in: path)
        if let old, unused, !old.busy, old.source != "terminal" {
            client.close(chat: old.id)
        }
    }

    private var transcript: some View {
        ScrollViewReader { proxy in
            ScrollView {
                // A plain VStack: lazy rows have estimated heights off screen, which makes scrollTo lurch.
                VStack(alignment: .leading, spacing: 12) {
                    if shown.isEmpty {
                        EmptyChatView(onSetUp: { showWelcome = true },
                                      // The chip rides along under a drawer swipe, so the finger lifts on it: that's a swipe, not a tap.
                                      onFolder: client.connection == .online ? { if !drawerAway { showFolders = true } } : nil)
                    }
                    // A chat seen before: its bubbles stand in at their height until scrolled near (BubbleSlot), so
                    // switching back in builds a screen of bubbles, not the thread. The last few draw at once (on
                    // screen when there is no pinned turn to fill it); the rest within reach follow a frame later.
                    ForEach(Array(earlierMessages.enumerated()), id: \.element.id) { i, message in
                        BubbleSlot(message: message, client: client, drawn: earlierMessages.count - i <= 3).id(message.id)
                            .background { FoundFlash(on: found == message.id) }
                    }
                    // Claude-app style: the sent message scrolls to the top and the reply streams into the space
                    // under it.  The turn is a screen-tall frame rather than a measured spacer, so the reply growing
                    // or the status line vanishing never changes the content height and nothing on screen shifts.
                    ZStack(alignment: .top) {
                        if !pinnedMessages.isEmpty {
                            // Sized by layout from the scroll view's own height, so it shrinks in step with the
                            // keyboard: no extra scroll room appears and nothing jumps.
                            Color.clear.containerRelativeFrame(.vertical) { height, _ in max(0, height - 24) }
                        }
                        VStack(alignment: .leading, spacing: 12) {
                            ForEach(pinnedMessages) { message in
                                MessageBubble(message: message, client: client).id(message.id)
                                    .background { FoundFlash(on: found == message.id) }
                            }
                            // The end of what's written, not of the turn's screen-tall frame: the jump arrow aims here.
                            Color.clear.frame(height: 0).id("end")
                                .onGeometryChange(for: CGFloat.self) { $0.frame(in: .named(SideDrawerMotion.card)).maxY } action: { y in
                                    endMark.y = y
                                    endMoved(proxy)
                                }
                        }
                    }
                    Color.clear.frame(height: 1).id("bottom")
                }
                .padding(.horizontal, 12)
                // None under the bottom marker: scrolled to it is scrolled to the very end, where the settle anchor holds.
                .padding(.top, 8)
                // A chat coming on screen draws still: no inherited animation fades its bubbles in or slides the
                // earlier ones into place as they land. On the content, not the scroll view: there it also took
                // the animation off the scroll view's own cross-fade in. (Before `openAtBottom` has run, `opening`
                // is still the last chat's: the staging check covers the first frame.)
                .transaction { if opening || stagedChat != client.activeChatID { $0.animation = nil } }
                .background { TranscriptShotter(box: shotter) }
            }
            // Whatever room it's offered, decided here: asked for its size (the composer bar asks on every layout), a
            // scroll view measures everything in it, and a long transcript took most of a chat switch measuring.
            .frame(minWidth: 0, maxWidth: .infinity, minHeight: 0, maxHeight: .infinity)
            // Measures an edit's one-line rows, so edits far off screen can stand in at their height undrawn.
            .background { EditMetricsProbe().padding(.horizontal, 12) }
            // Open at the bottom, but once settled never re-anchor: the keyboard covers the transcript instead of
            // shifting it.
            .defaultScrollAnchor(.bottom, for: .initialOffset)
            .defaultScrollAnchor(settling ? .bottom : nil, for: .sizeChanges)
            // While a chat is opening its content grows above the fold (held-back bubbles, history): follow the
            // bottom from here, once the growth is laid out.  A scrollTo straight after the state change, even a
            // main-queue hop later, still runs before layout and aims at the old bottom.
            .onScrollGeometryChange(for: CGFloat.self) { $0.contentSize.height } action: { _, _ in
                if opening && pinnedTurn == nil { scrollToBottom(proxy, animated: false) }
            }
            // On a push the nav bar's and composer's insets arrive a few frames after the first layout, leaving the
            // end short of the composer; the settle anchor holds content growing, not the room shrinking.
            .onScrollGeometryChange(for: CGFloat.self) { $0.containerSize.height } action: { _, _ in
                if settling && pinnedTurn == nil { scrollToBottom(proxy, animated: false) }
            }
            .scrollDismissesKeyboard(.interactively)
            .simultaneousGesture(SpatialTapGesture(coordinateSpace: .global).onEnded { tap in
                // The safe-area composer can share this recogniser; only taps outside it dismiss typing.
                guard !composerFrame.contains(tap.location) else { return }
                ReplySelection.release(unlessAt: tap.location)
                inputFocused = false
                settling = false   // a row opened by the tap grows downwards, not shoving what's above it up
            })
            // A drag lets go of a followed reply, and of any glide still under way.
            .scrollPosition($transcriptScroll)
            .onDrawerChange($drawerAway) { away in
                // A pull that began as a drag on the transcript, claimed by the drawer while the finger is still down.
                guard away, let from = dragFrom else { return }
                dragFrom = nil
                withAnimation(SideDrawerMotion.animation) { transcriptScroll.scrollTo(y: from) }
            }
            .onScrollPhaseChange { _, phase, context in
                guard phase == .interacting else { dragFrom = nil; return }
                dragFrom = context.geometry.contentOffset.y
                glideGeneration += 1
                gliding = false
                following = false
                settling = false
            }
            .onChange(of: inputFocused) { _, focused in
                if focused { settling = false }
            }
            // Outside the tap recogniser above, so tapping the arrow leaves the keyboard alone.
            .overlay(alignment: .bottom) { jumpButton(proxy) }
            .onChange(of: composerFrame) { _, _ in endMoved(proxy) }
            .onChange(of: shown.count) { _, _ in
                guard !shown.isEmpty else { return }
                if opening {
                    // History landing for a chat we just opened: stay glued to the bottom, no send choreography.
                    stageIn()
                    scrollToBottom(proxy, animated: false)
                } else if let turn = newTurn {
                    // The bubble and its frame land below the fold unanimated (nothing on screen moves), then once
                    // laid out the scroll glides the bubble to the top.
                    pinnedTurn = turn
                    following = false
                    settling = false
                    let generation = beginGlide()
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                        withAnimation(Self.sendAnimation) { proxy.scrollTo(turn, anchor: .top) }
                    }
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.7) {
                        if glideGeneration == generation { gliding = false }
                    }
                } else if pinnedTurn == nil {
                    scrollToBottom(proxy)
                }
            }
            .onChange(of: shown.last?.id) { _, _ in
                if opening { scrollToBottom(proxy, animated: false) }
            }
            .onChange(of: client.activeChatID) { _, _ in openAtBottom(proxy) }
            // A hit in the chat already on screen: no opening to wait for.
            .onChange(of: client.findInChat) { _, words in if !words.isEmpty && !opening { jumpToFound(proxy) } }
            .onChange(of: client.busy) { _, busy in
                call.busyChanged(busy)
                if !busy { following = false }
            }
            .onAppear { openAtBottom(proxy) }
            // A fresh scroll view per chat: the old one's indicator and offset never animate across the swap. (A
            // picture of the old one fades out over the new, see `swapShot`: SwiftUI's own transition never ran here.)
            .id(client.activeChatID)
        }
    }

    /// Fade-through from the chat that was on screen: its picture (taken in `select`, before anything changed) is
    /// over the new transcript already. It goes first, quickly, and only then does the new one rise in, so two
    /// screens of text never sit on top of each other half-faded.
    private func fadeSwapShot() {
        swapShotOpacity = 1
        incomingOpacity = 0
        incomingOffset = 14
        DispatchQueue.main.async {
            withAnimation(.easeIn(duration: Self.swapOut)) { swapShotOpacity = 0 }
            withAnimation(.easeOut(duration: Self.swapIn).delay(Self.swapOut * 0.6)) {
                incomingOpacity = 1
                incomingOffset = 0
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + Self.swapOut + 0.1) { if swapShotOpacity == 0 { swapShot = nil } }
        }
    }
    static let swapOut: TimeInterval = 0.16
    static let swapIn: TimeInterval = 0.32

    /// A chat coming on screen always starts at its bottom: scroll now, again once the new transcript has laid
    /// out, keep snapping there while its history is still arriving from the machine, and hold the bottom still
    /// while its pictures arrive.
    private func openAtBottom(_ proxy: ScrollViewProxy) {
        stagedChat = client.activeChatID
        pinnedTurn = nil
        following = false
        // No restore point from the last chat's transcript for this one to land on.
        dragFrom = nil
        transcriptScroll = ScrollPosition()
        glideGeneration += 1
        gliding = false
        opening = true
        settling = true
        openGeneration += 1
        let generation = openGeneration
        let drawn = Perf.begin("Open chat")
        stageIn()
        scrollToBottom(proxy, animated: false)
        DispatchQueue.main.async { scrollToBottom(proxy, animated: false); Perf.end("Open chat", drawn) }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
            if openGeneration == generation { opening = false; jumpToFound(proxy) }
        }
        // Pictures fetched from the machine can land well after that, over a slow connection especially.
        DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) {
            if openGeneration == generation { settling = false }
        }
    }

    /// Opened from a search hit (`client.findInChat`): the newest message holding every word is scrolled to the
    /// middle and lit for a moment, once its chat has settled at the bottom. Not on the phone (it holds the last 60
    /// rows), the chat just stays at its bottom.
    private func jumpToFound(_ proxy: ScrollViewProxy) {
        let words = client.findInChat.lowercased().split(separator: " ").map(String.init)
        client.findInChat = ""
        guard !words.isEmpty, let hit = shown.last(where: { m in words.allSatisfy { m.text.lowercased().contains($0) } }) else { return }
        heldBack = 0
        settling = false
        following = false
        DispatchQueue.main.async {
            withAnimation(.easeInOut(duration: 0.35)) { proxy.scrollTo(hit.id, anchor: .center) }
            found = hit.id
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.8) { if found == hit.id { found = nil } }
        }
    }

    /// The last dozen bubbles draw at once and the rest follow once the chats drawer has settled, so laying out a
    /// long thread never shares frames with the drawer's slide.  The scroll holds its bottom while the earlier
    /// bubbles land (see `settling`), so nothing visible moves: a scrollTo here ran before their layout and left
    /// the old bottom's offset, near the top of the full thread, on screen for a few frames.
    private func stageIn() {
        heldBack = max(0, shown.count - 12)
        guard heldBack > 0 else { return }
        // The generation, not the chat id: hopping A → B → A quickly must not let A's first timer land early.
        let generation = openGeneration
        DispatchQueue.main.asyncAfter(deadline: .now() + SideDrawerMotion.duration + 0.1) { land(generation) }
    }

    /// The held-back bubbles, a batch a frame, nearest the bottom first: in one go a long thread stalled the
    /// screen for as long as all of them took to build. Bubbles seen before stand in (BubbleSlot), so a batch of
    /// those costs nothing much.
    private func land(_ generation: Int) {
        guard openGeneration == generation, heldBack > 0 else { return }
        let landed = Perf.begin("Land history")
        var still = Transaction()
        still.disablesAnimations = true
        withTransaction(still) { heldBack = max(0, heldBack - 16) }
        DispatchQueue.main.async { Perf.end("Land history", landed) }
        if heldBack > 0 { DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { land(generation) } }
    }

    /// A user turn that has just landed and is not pinned yet.  Sent from the phone it arrives alone; sent from the
    /// machine's terminal it arrives together with the reply's streaming bubble, so anything after it must still be
    /// streaming: a finished reply (or a history reload) behind it means it is old news.
    private var newTurn: Message.ID? {
        guard let i = shown.lastIndex(where: { $0.role == .user }) else { return nil }
        let turn = shown[i]
        guard turn.id != pinnedTurn, shown[(i + 1)...].allSatisfy({ $0.streaming }) else { return nil }
        return turn.id
    }

    /// Index of the pinned user message, if it is still the latest user message on screen.
    private var pinnedIndex: Int? {
        guard let pinned = pinnedTurn, let i = shown.lastIndex(where: { $0.role == .user }),
              shown[i].id == pinned else { return nil }
        return i
    }

    private var earlierMessages: ArraySlice<Message> {
        // The frame a chat comes on screen draws before `openAtBottom` has staged it (onChange runs after the
        // body): held back here too, else the whole thread was built, torn down and built again.
        let held = stagedChat == client.activeChatID ? heldBack : max(0, shown.count - 12)
        return shown[..<(pinnedIndex ?? shown.count)].dropFirst(held)
    }

    private var pinnedMessages: ArraySlice<Message> {
        pinnedIndex.map { shown[$0...] } ?? []
    }

    /// The send scroll's curve.
    private static let sendAnimation = Animation.spring(duration: 0.6, bounce: 0)
    /// The follow ride's curve: short enough that lines landing a few times a second blend into one motion.
    private static let followAnimation = Animation.spring(duration: 0.3, bounce: 0)

    private func scrollToBottom(_ proxy: ScrollViewProxy, animated: Bool = true) {
        if animated {
            withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo("bottom", anchor: .bottom) }
        } else {
            proxy.scrollTo("bottom", anchor: .bottom)
        }
    }

    // MARK: jump arrow

    /// Starts a scripted scroll; the returned generation tells its follow-up whether a drag or another glide took over.
    private func beginGlide() -> Int {
        glideGeneration += 1
        gliding = true
        return glideGeneration
    }

    /// The latest line moved, or the composer did: ride it down if following, otherwise offer the arrow once it
    /// sinks under the composer.  The marker sits a row's spacing below the text, hence the margin.
    private func endMoved(_ proxy: ScrollViewProxy) {
        guard composerFrame.height > 0 else { return }
        let below = endMark.y - composerFrame.minY
        if following && !gliding {
            // Each new line lands as a short glide, not a hop: a reply arrives a line at a time, so an instant
            // scroll made the ride stutter at every wrap.
            if below > 2 { withAnimation(Self.followAnimation) { proxy.scrollTo("end", anchor: .bottom) } }
            return
        }
        let hidden = below > 16
        if hidden != endHidden { endHidden = hidden }
    }

    /// Glides to the latest line, then follows the reply from there if it is still coming.
    private func jumpToEnd(_ proxy: ScrollViewProxy) {
        let generation = beginGlide()
        withAnimation(.spring(duration: 0.4, bounce: 0)) { proxy.scrollTo("end", anchor: .bottom) }
        // Following starts once the glide lands: a follow scroll mid-glide would cut it short.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.45) {
            guard glideGeneration == generation else { return }
            gliding = false
            following = client.busy
            endMoved(proxy)
        }
    }

    /// ChatGPT's arrow: a small glass button just above the composer while the latest line is out of sight below it.
    private func jumpButton(_ proxy: ScrollViewProxy) -> some View {
        let offered = endHidden && !following && !gliding && !opening && !cardOpen
        return ZStack {
            if offered {
                Button { jumpToEnd(proxy) } label: {
                    Image(systemName: "arrow.down")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(Color.primary)
                        .frame(width: 36, height: 36)
                        .contentShape(Circle())
                }
                .buttonStyle(.plain)
                .modifier(ComposerGlass(interactive: true, radius: 18))
                .accessibilityLabel("Jump to latest")
                .transition(.scale(scale: 0.85).combined(with: .opacity).combined(with: .offset(y: 8)))
            }
        }
        .animation(.spring(duration: 0.28, bounce: 0.2), value: offered)
        .padding(.bottom, 10)
    }

    // MARK: status

    /// The chat list button, with a badge while another chat is working or waiting on you.
    private var chatsButtonLabel: some View {
        let others = client.chats.filter { $0.id != client.active?.id }
        let waiting = others.contains { $0.waiting }
        let working = others.contains { $0.busy }
        return Image(systemName: "list.bullet")
            .overlay(alignment: .topTrailing) {
                if waiting || working {
                    Circle().fill(waiting ? Color.purple : Color.orange).frame(width: 8, height: 8).offset(x: 4, y: -4)
                }
            }
    }

    // MARK: input

    /// Interactive Liquid Glass composer with model and action controls underneath.
    private var inputBar: some View {
        VStack(spacing: 8) {
            // Messages waiting behind the reply, as faded bubbles just above the composer.
            if client.messages.contains(where: \.queued) {   // no empty slot (and its spacing) when nothing waits
                QueuedStrip(messages: client.messages.filter(\.queued), client: client)
            }
            if let plan = waitingPlan {
                planBar(plan)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
            Group {
                if let attach, attach != .menu, !overKeyboard {
                    // One full-width card in the composer's slot: the camera or the recent-photos grid.
                    attachCard(attach)
                } else {
                    composer
                }
            }
            .onCardFrame { composerCard = $0 }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        // Measured with its padding: the transcript's dismiss tap treats this whole strip as "not outside".  Read
        // straight off its geometry: as a bar, its preferences never reach the screen.  Measured on the chat card, which the
        // drawer's slide doesn't move (SideDrawerMotion.card).
        .onCardFrame { composerFrame = $0 }
        .photosPicker(isPresented: $showPhotos, selection: $pickedPhotos, maxSelectionCount: max(1, mediaRoom), selectionBehavior: .ordered, matching: .any(of: [.images, .videos]))
        .fileImporter(isPresented: $showFiles, allowedContentTypes: [.item], allowsMultipleSelection: true) { result in
            switch result {
            case .success(let urls):
                for url in urls {
                    do { attachments.append(try Attachment.file(from: url)) } catch { client.toast = error.localizedDescription }
                }
            case .failure(let error):
                client.toast = "Couldn't open that: \(error.localizedDescription)"
            }
        }
        .onChange(of: pickedPhotos) { _, items in
            guard !items.isEmpty else { return }
            pickedPhotos = []
            Task {
                for item in items {
                    guard mediaRoom > 0 else {
                        client.toast = "Up to \(RecentPhotosGrid.maxSelection) photos and videos in a message."
                        break
                    }
                    do {
                        if item.supportedContentTypes.contains(where: { $0.conforms(to: .movie) }) {
                            uploading = true  // converting: the send button shows its spinner
                            defer { uploading = false }
                            guard let movie = try await item.loadTransferable(type: PickedMovie.self) else {
                                client.toast = "Couldn't load that video. Please choose it again."
                                continue
                            }
                            defer { try? FileManager.default.removeItem(at: movie.url) }
                            attachments.append(try await VideoClip.attachment(from: AVURLAsset(url: movie.url)))
                            continue
                        }
                        guard let data = try await item.loadTransferable(type: Data.self),
                              let image = UIImage(data: data) else {
                            client.toast = "Couldn't load that photo. Please choose it again."
                            continue
                        }
                        attachments.append(Attachment(image: image))
                    } catch {
                        client.toast = "Couldn't load that: \(error.localizedDescription)"
                    }
                }
            }
        }

    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 14) {
            if !attachments.isEmpty {
                AttachmentStrip(attachments: $attachments)
                    .disabled(uploading)
                    .drawerSwipeGuard()   // it scrolls sideways itself
            }
            if !draft.isEmpty && !call.engine.interimTranscript.isEmpty {
                Text(call.engine.interimTranscript)
                    .themeFont(size: 18)
                    .foregroundStyle(Color.primary)
                    .lineLimit(6)
                    .padding(.horizontal, 8)
            }
            ComposerField(text: $draft, focused: $inputFocused,
                          placeholder: client.inCall ? call.prompt : shellMode ? "Run a command on the computer" : !suggestion.isEmpty ? suggestion
                              : planMode ? "Plan with Vision" : "Message Vision",
                          suggestion: suggestion,
                          liveText: draft.isEmpty ? call.engine.interimTranscript : "",
                          hidesCaret: client.inCall,
                          disabled: uploading,
                          inert: attach != nil || pickerExpanded,
                          onInertKey: { if attach != nil { closeAttach() } else { closePickers() } },
                          onGenmoji: { attachments.append(Attachment(image: $0)) },
                          onPictures: { pictures in
                              for picture in pictures {
                                  guard mediaRoom > 0 else {
                                      client.toast = "Up to \(RecentPhotosGrid.maxSelection) photos and videos in a message."
                                      break
                                  }
                                  attachments.append(Attachment(image: picture))
                              }
                          },
                          onBang: client.canShell && client.active != nil && !client.inCall ? { shellMode = true } : nil,
                          onDeleteEmpty: { shellMode = false },
                          plain: shellMode)
                // While typing, a drag on the text (a selection handle, the cursor) is the text's: the drawer
                // keeps its hands off, with room for the handles' knobs above and below the lines.
                .background {
                    if inputFocused { Color.clear.padding(.vertical, -24).drawerSwipeGuard() }
                }
                .padding(.horizontal, 8 - ComposerField.handleRoom)   // the field's own room for the handles, given back
                .padding(.top, 6)
            // Every control in this row is guarded: a press that slides never pulls the chats drawer out.
            HStack(alignment: .center, spacing: 8) {
                attachButton
                    .drawerSwipeGuard()
                ModelPickerButton(state: picker, isExpanded: $modelsExpanded)
                    .drawerSwipeGuard()
                    .layoutPriority(1)
                // With no chat open the pills show the defaults the next chat starts on, effort included.
                if client.connection == .online, !client.effort.isEmpty || (client.active == nil && !(picker.effort ?? "").isEmpty) {
                    EffortPickerButton(state: picker, isExpanded: $effortsExpanded)
                        .drawerSwipeGuard()
                        .layoutPriority(1)
                }
                Spacer(minLength: 0)
                if client.connection == .online, !client.inCall, let ctx = client.context {  // a call needs the room
                    ContextGauge(tokens: ctx.tokens, window: ctx.window)
                        .fixedSize()
                        .onTapGesture {
                            contextNote = "\(tokenText(ctx.tokens)) of \(tokenText(ctx.window)) tokens in context"
                        }
                        .drawerSwipeGuard()
                }
                if client.inCall {
                    CallControls(call: call, onEnd: { endVoice() })
                        .drawerSwipeGuard()
                } else if canStartVoice {
                    voiceButton
                        .drawerSwipeGuard()
                }
                trailingButton
                    .drawerSwipeGuard()
            }
            .padding(.leading, 2)
        }
        .padding(.horizontal, 12)
        .padding(.top, 12)
        .padding(.bottom, 10)
        // Sits behind the controls, so the buttons and the field still win a tap; anywhere else in the
        // card starts typing, and a tap while typing is a no-op rather than a dismiss.
        .background {
            Color.clear
                .contentShape(RoundedRectangle(cornerRadius: 30, style: .continuous))
                .onTapGesture { focusInput() }
        }
        .modifier(ComposerGlass(interactive: true, radius: theme.composerRadius ?? 30))
        .overlay {
            // Shell mode: a pink border, as the terminal's input box gets.
            RoundedRectangle(cornerRadius: theme.composerRadius ?? 30, style: .continuous)
                .strokeBorder(Color.shellPink, lineWidth: 1.5)
                .opacity(shellMode ? 1 : 0)
                .animation(.easeInOut(duration: 0.2), value: shellMode)
                .allowsHitTesting(false)
        }
        .overlay {
            PlanGlow(radius: theme.composerRadius ?? 30, active: planMode)
                .opacity(planMode ? 1 : 0)
                .animation(.easeInOut(duration: 0.45), value: planMode)
        }
        // An open model or effort slider lies over the composer, which blurs away beneath it.
        .blur(radius: pickerExpanded ? 10 : 0)
        .animation(PickerMotion.animation, value: pickerExpanded)
        .onChange(of: inputFocused) { _, focused in
            if focused { closePickers() }
        }
    }

    /// The model and effort sliders leave the keyboard up; a key press while one is open only shuts it.
    private func closePickers() {
        withAnimation(PickerMotion.animation) { modelsExpanded = false; effortsExpanded = false }
    }

    /// Plan mode, switched from the "+" menu. Auto is the norm and shows nothing; plan, the one that changes
    /// what a send does, turns the composer itself green: its edge glows and the send button follows.
    private var planMode: Bool { client.connection == .online && client.mode == "plan" }

    /// "+": pops the attach card up out of the button.
    private var attachButton: some View {
        Button {
            if attach != nil { closeAttach() } else { openAttach() }
        } label: {
            Image(systemName: "plus")
                .font(.system(size: 25, weight: .regular))
                .frame(width: 34, height: 40)
                .foregroundStyle(client.connection == .online ? Color.primary : Color.secondary)
                // The menu's glass takes the button's place, then hands it back once it has shrunk home.
                .opacity(attach == .menu ? 0 : 1)
                .blur(radius: attach == .menu ? 4 : 0)
                .animation(attach == .menu ? .easeOut(duration: 0.08) : .easeOut(duration: 0.12).delay(0.2), value: attach == .menu)
                .contentShape(Rectangle())
                .onCardFrame { attachFrame = $0 }
        }
        .buttonStyle(PressHapticButtonStyle())
        .disabled(client.connection != .online || uploading)
        .accessibilityLabel("Add attachment")
    }

    /// A call is offered whenever the composer is otherwise idle: with something to send, the
    /// trailing slot belongs to sending it.
    private var canStartVoice: Bool {
        client.connection == .online && !uploading
            && attachments.isEmpty && draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    /// Live voice mode: Vision's half of a spoken conversation, with the microphone left open.
    private var voiceButton: some View {
        Button {
            // The voice switched off on the computer (Settings, /voice off): nothing there would hear or answer.
            guard client.voiceOn else {
                client.toast = "The voice is off on the computer. Turn it on in Settings."
                return
            }
            startVoice()
        } label: {
            Image(systemName: "waveform")
                .font(.system(size: 18, weight: .semibold))
                .frame(width: 38, height: 38)
                .modifier(AccentDisc(style: theme.voiceButton))
                .contentShape(Circle())
                .opacity(client.voiceOn ? 1 : 0.35)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(client.voiceOn ? "Live voice" : "Live voice, off on the computer")
    }

    /// The call runs in this chat, ChatGPT style: what you say and what Vision says stream into the
    /// transcript, and the keyboard still works; anything typed meanwhile is part of the call.
    private func startVoice() {
        guard client.connection == .online, !client.inCall else { return }
        if attach != nil { closeAttach() }
        client.player.stop()   // the call brings its own engine, and it is about to take the speaker
        client.inCall = true
        let chatID = client.activeChatID
        let sessionID = UUID()
        voiceSessionID = sessionID
        CallRemote.end = { endVoice() }   // the island's hang-up
        Task {
            // The task can be scheduled after a chat switch. Do not let that old task acquire the mic.
            guard voiceSessionID == sessionID, client.activeChatID == chatID else { return }
            await call.start(client: client, sessionID: sessionID)
            // Permission prompts are asynchronous, so the chat may have changed while the call started.
            guard voiceSessionID == sessionID, client.activeChatID == chatID else {
                call.stop(sessionID: sessionID, chat: chatID)
                return
            }
        }
    }

    /// `chat`: the chat the call was speaking for when that is no longer the one on screen.
    private func endVoice(chat: String? = nil) {
        CallRemote.end = nil
        guard let sessionID = voiceSessionID else {
            // Every chat switch comes through here: setting it when it's already off would redraw all of the client's watchers.
            if client.inCall { client.inCall = false }
            return
        }
        voiceSessionID = nil
        call.stop(sessionID: sessionID, chat: chat)
        client.inCall = false
    }

    /// Any swipe off the attach menu or card closes it, same as a tap; the drawer's own swipe is off meanwhile.
    private var swipeToClose: some Gesture {
        DragGesture(minimumDistance: 8).onChanged { _ in if attach != nil { closeAttach() } }
    }

    private func openAttach() {
        overKeyboard = inputFocused
        closingFromCard = false
        withAnimation(.spring(duration: 0.32, bounce: 0.12)) { attach = .menu }
    }

    private func closeAttach() {
        closingFromCard = cardOpen   // a card shrinks away on its own; the menu's glass stays hidden behind it
        withAnimation(.spring(duration: 0.3, bounce: 0)) { attach = nil }
    }

    /// The back arrow on the camera or photos card drops back to the menu rather than closing everything.
    private func backToAttachMenu() {
        withAnimation(.spring(duration: 0.35, bounce: 0.25)) { attach = .menu }
    }

    private func expandAttach(_ mode: AttachMode) {
        // Over the keyboard the card lies on the keys, which stay up; in the composer's slot it needs the room.
        if !overKeyboard { inputFocused = false }
        // The card's size before it lays out, so the glass grows straight to it (camera 3:4, photos 340 tall).
        let width = composerCard.width
        cardSize = CGSize(width: width, height: mode == .camera || overKeyboard ? width * 4 / 3 : 340)
        withAnimation(.spring(duration: 0.4, bounce: 0.15)) { attach = mode }
    }

    /// The popover above the "+": a glass list of rows, icon in a circle then the label.  Shut, it is a
    /// "+"-sized blob of glass on the button; glass and rows then scale out of it together as one piece
    /// with a touch of overshoot, and squash back into it the same way (ChatGPT's, measured frame by frame).
    /// With a card open the (hidden) glass takes the card's size, so opening one grows the menu into it and
    /// the back arrow shrinks it back into the menu.
    private func attachMenu(open: Bool, card: Bool, blobOffset: CGSize, fromTop: Bool = false) -> some View {
        let menu = attachMenuSize == .zero ? CGSize(width: 266, height: 215) : attachMenuSize
        // The "+"'s centre, measured from the menu's top-left corner.
        let plus = CGPoint(x: blobOffset.width + attachFrame.width / 2,
                           y: fromTop ? blobOffset.height + attachFrame.height / 2
                                      : menu.height + blobOffset.height - attachFrame.height / 2)
        let cardRect = CGRect(x: 0, y: fromTop ? 0 : menu.height - cardSize.height, width: cardSize.width, height: cardSize.height)
        return VStack(spacing: 0) {
            attachRow("camera", "Camera", enabled: UIImagePickerController.isSourceTypeAvailable(.camera)) { expandAttach(.camera) }
            attachRow("photo", "Photos") { expandAttach(.photos) }
            attachRow("paperclip", "Files") { closeAttach(); showFiles = true }
            // Auto or plan lives here with the other things you pick before sending: only for a chat whose
            // Vision says which it is in.
            if client.connection == .online, !client.mode.isEmpty {
                Divider().padding(.vertical, 6).padding(.horizontal, 6)
                planModeRow
            }
        }
        .padding(.vertical, 10)
        .padding(.horizontal, 17)
        .frame(width: 266, alignment: .leading)
        .onGeometryChange(for: CGSize.self) { $0.size } action: { attachMenuSize = $0 }
        .modifier(AttachBloom(progress: open || card ? 1 : 0, cardMix: card ? 1 : 0, menu: menu, plus: plus, card: cardRect,
                              glassShown: !card && !closingFromCard, rowsShown: !card))
        // Out fast with a little overshoot, home again with none; this also beats the keyboard layer's own spring.
        .animation(card ? .spring(duration: 0.4, bounce: 0.15)
                   : open ? .spring(duration: 0.32, bounce: 0.12) : .spring(duration: 0.3, bounce: 0),
                   value: (open ? 1 : 0) + (card ? 2 : 0))
    }

    private func attachRow(_ symbol: String, _ title: String, enabled: Bool = true, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 16) {
                Image(systemName: symbol)
                    .font(.system(size: 19, weight: .medium))
                    .frame(width: 42, height: 42)
                    .background(Color(.tertiarySystemFill), in: Circle())
                Text(title).themeFont(size: 21)
                Spacer(minLength: 0)
            }
            .foregroundStyle(.primary)
            .padding(.horizontal, 6)
            .padding(.vertical, 11.5)
            .contentShape(Rectangle())
        }
        .buttonStyle(PressHapticButtonStyle())
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.4)
    }

    /// Plan mode as a switch in the "+" menu: a tick and a green icon when it is on. A tap flips it and
    /// shuts the menu. Plan is what the terminal flips with Shift-Tab: read-only until a plan is approved
    /// (the approval arrives as a question form), the safer way to hand over work from a phone.
    private var planModeRow: some View {
        let plan = client.mode == "plan"
        return Button {
            closeAttach()
            client.setMode(plan ? "auto" : "plan")
        } label: {
            HStack(spacing: 16) {
                Image(systemName: "pause.circle")
                    .font(.system(size: 19, weight: .medium))
                    .foregroundStyle(plan ? Color.green : Color.primary)
                    .frame(width: 42, height: 42)
                    .background(plan ? Color.green.opacity(0.18) : Color(.tertiarySystemFill), in: Circle())
                Text("Plan mode").themeFont(size: 21)
                Spacer(minLength: 0)
                Image(systemName: "checkmark")
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundStyle(Color.green)
                    .opacity(plan ? 1 : 0)
            }
            .foregroundStyle(.primary)
            .padding(.horizontal, 6)
            .padding(.vertical, 11.5)
            .contentShape(Rectangle())
        }
        .buttonStyle(PressHapticButtonStyle())
        .disabled(client.busy)   // the machine refuses a switch mid-reply
        .opacity(client.busy ? 0.4 : 1)
        .accessibilityLabel("Plan mode")
        .accessibilityValue(plan ? "On" : "Off")
        .accessibilityHint(plan ? "Double tap for auto mode, where Vision runs tools without asking."
                                : "Double tap and Vision proposes a plan and waits for your yes.")
    }

    /// A tap on the composer's dead space starts typing.  Ignored while a picker or card owns the slot,
    /// and while the keyboard is already up, so tapping the box again never closes it.
    private func focusInput() {
        if pickerExpanded { closePickers() }
        guard !inputFocused, attach == nil else { return }
        guard !uploading else { return }
        inputFocused = true
    }

    /// Where the menu and card sit over the keyboard: they share their top-left corner, the card is 3:4 across the
    /// composer's width and ends just above the bottom of the screen (ChatGPT's layout).
    private var keyboardAttachOrigin: CGPoint {
        CGPoint(x: composerCard.minX, y: screenHeight - 16 - composerCard.width * 4 / 3)
    }

    private var keyboardAttachHole: CGRect {
        guard overKeyboard, let attach else { return .zero }
        let card = CGSize(width: composerCard.width, height: composerCard.width * 4 / 3)
        return CGRect(origin: keyboardAttachOrigin, size: attach == .menu ? attachMenuSize : card)
    }

    /// The menu and its cards as drawn inside the keyboard's window (KeyboardLayer), in screen coordinates.
    private var keyboardAttachLayer: some View {
        let origin = keyboardAttachOrigin
        let blobOffset = CGSize(width: attachFrame.minX - origin.x, height: attachFrame.minY - origin.y)
        return ZStack(alignment: .topLeading) {
            if overKeyboard, let attach, attach != .menu {
                attachCard(attach, height: composerCard.width * 4 / 3, fromTop: true)
                    .frame(width: composerCard.width)
            }
            attachMenu(open: overKeyboard && attach == .menu, card: overKeyboard && cardOpen, blobOffset: blobOffset, fromTop: true)
                .allowsHitTesting(overKeyboard && attach == .menu)
        }
        // This view sits in another hosting controller, out of reach of withAnimation, so the card animates here.
        .animation(.spring(duration: 0.4, bounce: 0.15), value: attach)
        .offset(x: origin.x, y: origin.y)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .ignoresSafeArea()
        .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { screenHeight = $0 }   // the keyboard window's, i.e. the screen's
    }

    /// Camera or the recent-photos grid fills the composer's slot as one full-width card.
    private func attachCard(_ mode: AttachMode, height: CGFloat? = nil, fromTop: Bool = false) -> some View {
        ZStack {
            switch mode {
            case .menu:
                EmptyView()
            case .camera:
                CameraCard(onCapture: { attachments.append(Attachment(image: $0)) }, onClose: closeAttach, onBack: backToAttachMenu)
                    .transition(.opacity)
            case .photos:
                RecentPhotosGrid(gridHeight: height ?? 340, room: mediaRoom,
                                 onPick: { attachments.append(contentsOf: $0); closeAttach() },
                                 onBrowse: { closeAttach(); showPhotos = true },
                                 onBack: backToAttachMenu)
                    .transition(.opacity)
            }
        }
        .frame(maxWidth: .infinity)
        .clipShape(RoundedRectangle(cornerRadius: 30, style: .continuous))
        .onGeometryChange(for: CGSize.self) { $0.size } action: { cardSize = $0 }
        .background(GeometryReader { g in
            Color.clear.preference(key: AttachHoleKey.self, value: g.frame(in: .global))
        })
        // Grows out of, and shrinks back into, the menu's corner by the "+", under the menu's morphing glass.
        .transition(.scale(scale: 0.8, anchor: fromTop ? .topLeading : .bottomLeading).combined(with: .opacity))
    }

    /// How full the chat's context is: a thin ring and the percentage, orange from 70%, red from 90%.
    private struct ContextGauge: View {
        let tokens: Int
        let window: Int

        private var fraction: Double { min(1, Double(tokens) / Double(max(1, window))) }
        private var percent: Int { Int((fraction * 100).rounded()) }
        private var tint: Color { fraction >= 0.9 ? .red : fraction >= 0.7 ? .orange : .secondary }

        var body: some View {
            ring
                .animation(.snappy(duration: 0.3), value: fraction)
                .padding(.vertical, 6)
                .contentShape(Rectangle())
        }

        private var ring: some View {
            HStack(spacing: 5) {
                ZStack {
                    Circle().stroke(Color(.tertiarySystemFill), lineWidth: 2.5)
                    Circle()
                        .trim(from: 0, to: fraction)
                        .stroke(tint, style: StrokeStyle(lineWidth: 2.5, lineCap: .round))
                        .rotationEffect(.degrees(-90))
                }
                .frame(width: 17, height: 17)
                Text("\(percent)%")
                    .themeFont(.footnote, digits: true)
                    .foregroundStyle(fraction >= 0.7 ? tint : .secondary)
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Context \(percent) percent full, \(tokenText(tokens)) of \(tokenText(window)) tokens")
        }
    }

    /// What the machine guesses comes next, offered while the composer is empty and nothing is running.
    private var suggestion: String {
        guard !client.inCall, !client.busy, !uploading, !shellMode, draft.isEmpty, attachments.isEmpty else { return "" }
        // The chat on screen's own, never another's: `active` falls back to the last chat when none is chosen.
        return client.chats.first { $0.id == client.activeChatID }?.suggestion ?? ""
    }

    @ViewBuilder
    /// Every icon here is held to the voice disc's 38 points: a 38-point symbol's line runs a few points taller, which
    /// grew the row, and so the composer, the moment the send arrow took the disc's place.
    private var trailingButton: some View {
        if uploading {
            ProgressView().frame(width: 38, height: 38)
        } else if shellMode && !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            Button {
                client.runShell(draft)
                draft = ""
                shellMode = false
            } label: {
                Image(systemName: "arrow.up.circle.fill").font(.system(size: 38)).frame(width: 38, height: 38).foregroundStyle(Color.shellPink)
            }
            .accessibilityLabel("Run command")
        } else if !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !attachments.isEmpty {
            Button { sendDraft() } label: {
                Image(systemName: "arrow.up.circle.fill").font(.system(size: 38)).frame(width: 38, height: 38)
                    .foregroundStyle(planMode ? AnyShapeStyle(Color.green) : AnyShapeStyle(.tint))
            }
            .accessibilityLabel(client.busy ? "Send, queued until the reply ends" : "Send")
            .contextMenu {
                // Mid-reply a message queues; a long press sends it into the reply now (Ctrl-X in the terminal).
                if client.busy {
                    Button("Send now", systemImage: "arrow.turn.down.right") { sendDraft(now: true) }
                }
            }
        } else if client.shellRunning && !client.busy {
            Button(action: client.stopShell) {
                Image(systemName: "stop.circle.fill").font(.system(size: 38)).frame(width: 38, height: 38).foregroundStyle(Color.shellPink)
            }
            .accessibilityLabel("Stop command")
        } else if !suggestion.isEmpty {
            // The empty composer's grey text is the machine's guess at the next message: the arrow sends it
            // (tapping the text puts it in the composer to change, ComposerField).
            Button { client.send(suggestion) } label: {
                Image(systemName: "arrow.up.circle.fill").font(.system(size: 38)).frame(width: 38, height: 38)
                    .foregroundStyle(planMode ? AnyShapeStyle(Color.green) : AnyShapeStyle(.tint))
                    .opacity(0.45)
            }
            .accessibilityLabel("Send suggestion: \(suggestion)")
        } else if client.busy {
            Button(action: client.cancel) {
                Image(systemName: "stop.circle.fill").font(.system(size: 38)).frame(width: 38, height: 38)
            }
            .accessibilityLabel("Stop")
        }
    }

    /// "Go ahead with this plan?" with Change it / Start, answering the plan in one tap. Change it says no and
    /// opens the keyboard for what should change.
    private func planBar(_ plan: Question) -> some View {
        HStack(spacing: 8) {
            Text("Go ahead with this plan?")
                .themeFont(.subheadline, weight: .semibold)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button {
                client.answer([plan.question: "No"])
                inputFocused = true
            } label: {
                Text("Change it")
                    .padding(.horizontal, 14)
                    .frame(height: 36)
                    .background(Capsule().fill(Color.primary.opacity(0.08)))
            }
            Button {
                client.answer([plan.question: "Yes"])
            } label: {
                Text("Start")
                    .foregroundStyle(Color(.systemBackground))
                    .padding(.horizontal, 18)
                    .frame(height: 36)
                    .background(Capsule().fill(Color(uiColor: .label)))   // Color.primary goes grey on the material
            }
        }
        .themeFont(.subheadline, weight: .semibold)
        .buttonStyle(.plain)
        .padding(.leading, 16)
        .padding(.trailing, 6)
        .padding(.vertical, 6)
        .background(.regularMaterial, in: Capsule())
    }

    private var toastView: some View {
        note(client.toast, edge: .top) { client.toast = nil }
    }

    /// A capsule that slides in from `edge` and clears itself after three seconds.
    private func note(_ text: String?, edge: Edge, clear: @escaping () -> Void) -> some View {
        Group {
            if let text {
                Text(text)
                    .themeFont(.footnote)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                    .background(.regularMaterial, in: Capsule())
                    .padding(edge == .top ? .top : .bottom, 8)
                    .transition(.move(edge: edge).combined(with: .opacity))
                    .task(id: text) {
                        try? await Task.sleep(for: .seconds(3))
                        if !Task.isCancelled { clear() }
                    }
            }
        }
        .animation(.easeInOut, value: text)
    }

    // MARK: actions

    private func sendDraft(now: Bool = false) {
        guard !uploading, client.connection == .online else { return }
        inputFocused = false
        let text = draft
        let photos = attachments
        let chatID = client.activeChatID
        guard !photos.isEmpty else {
            draft = ""
            client.send(text, now: now)
            return
        }
        uploading = true
        Task {
            defer { uploading = false }
            do {
                var paths: [String] = []
                for photo in photos {
                    if let path = photo.uploaded {  // taken back from a queued message: already on the machine
                        paths.append(path)
                        continue
                    }
                    if let file = photo.file {
                        paths.append(try await client.uploadFile(file, name: file.lastPathComponent))
                        continue
                    }
                    if let video = photo.video {
                        paths.append(try await client.uploadVideo(video))
                        continue
                    }
                    guard let data = photo.jpegData() else {
                        client.toast = "Couldn't prepare that photo. Your message hasn't been sent."
                        return
                    }
                    paths.append(try await client.upload(data))
                }
                guard client.connection == .online, client.activeChatID == chatID else {
                    client.toast = "Chat changed or disconnected. Your attachments are still here; tap Send to retry."
                    return
                }
                client.send(text, images: paths, thumbnails: photos.filter { $0.file == nil }.map(\.image), now: now)
                draft = ""
                attachments = []
            } catch {
                client.toast = "Upload failed: \(error.localizedDescription). Your attachments are still here; tap Send to retry."
            }
        }
    }
}

private struct AttachHoleKey: PreferenceKey {
    static let defaultValue: CGRect = .zero
    static func reduce(value: inout CGRect, nextValue: () -> CGRect) {
        let next = nextValue()
        if next != .zero { value = next }
    }
}

/// The attach menu's grow and shrink, driven by one animated number so glass and rows always move together.
/// progress 0 is a round blob of glass on the "+", 1 the full menu (a spring may carry it a little past);
/// cardMix then stretches the glass to the camera or photos card.  The rows are the menu at full size scaled
/// by the glass's width and clipped to it, so a squat blob crops them rather than squashing them.
private struct AttachBloom: ViewModifier, Animatable {
    var progress: CGFloat
    var cardMix: CGFloat
    var menu: CGSize
    var plus: CGPoint          // the "+"'s centre from the menu's top-left
    var card: CGRect
    var glassShown: Bool
    var rowsShown: Bool

    var animatableData: AnimatablePair<CGFloat, CGFloat> {
        get { AnimatablePair(progress, cardMix) }
        set { progress = newValue.first; cardMix = newValue.second }
    }

    private static let blob: CGFloat = 44

    func body(content: Content) -> some View {
        let p = progress, d = Self.blob
        let clamped = min(max(p, 0), 1)
        let blob = CGRect(x: plus.x - d / 2, y: plus.y - d / 2, width: d, height: d)
        let rect = mix(mix(blob, CGRect(origin: .zero, size: menu), p), card, cardMix)
        let radius = min(d / 2 + (30 - d / 2) * clamped, min(rect.width, rect.height) / 2)
        let shape = RoundedRectangle(cornerRadius: radius, style: .continuous)
        content
            .scaleEffect(rect.width / max(menu.width, 1), anchor: .topLeading)
            .offset(x: rect.minX, y: rect.minY)
            .blur(radius: (1 - clamped) * 3)
            .opacity(min(max((p - 0.1) / 0.6, 0), 1))
            .opacity(rowsShown ? 1 : 0)
            .animation(rowsShown ? .easeOut(duration: 0.2).delay(0.06) : .easeIn(duration: 0.12), value: rowsShown)
            .mask(alignment: .topLeading) {
                shape.frame(width: rect.width, height: rect.height).offset(x: rect.minX, y: rect.minY)
            }
            .background(alignment: .topLeading) {
                Color.clear
                    .frame(width: rect.width, height: rect.height)
                    .modifier(ComposerGlass(radius: radius))
                    .offset(x: rect.minX, y: rect.minY)
                    .opacity(min(max(p / 0.1, 0), 1))   // the blob holds till it's home, then goes
                    .opacity(glassShown ? 1 : 0)
                    .animation(glassShown ? .easeOut(duration: 0.08) : .easeOut(duration: 0.2).delay(0.15), value: glassShown)
            }
    }

    private func mix(_ a: CGRect, _ b: CGRect, _ t: CGFloat) -> CGRect {
        CGRect(x: a.minX + (b.minX - a.minX) * t, y: a.minY + (b.minY - a.minY) * t,
               width: a.width + (b.width - a.width) * t, height: a.height + (b.height - a.height) * t)
    }
}

/// The composer's surface, and the attach menu's and jump arrow's: Liquid Glass under the system theme, a
/// flat card under the others (`Theme.composer`).
struct ComposerGlass: ViewModifier {
    var interactive = false
    var radius: CGFloat = 24
    @Environment(\.theme) private var theme

    func body(content: Content) -> some View {
        content.themeSurface(theme.composer, radius: radius, interactive: interactive)
    }
}

/// The chats and terminal glyphs over the transcript: the tint under the system theme (a toolbar's
/// default), the text colour under the skins.
private struct ChromeGlyph<Label: View>: View {
    @ViewBuilder let label: () -> Label
    @Environment(\.theme) private var theme

    var body: some View {
        if theme.chromeMonochrome {
            label().foregroundStyle(.primary)
        } else {
            label()
        }
    }
}

/// The main screen's top bar: a button at each end and a title in the middle, where the navigation bar put them.
private struct ChromeBar<Leading: View, Title: View, Trailing: View>: View {
    @ViewBuilder let leading: () -> Leading
    @ViewBuilder let title: () -> Title
    @ViewBuilder let trailing: () -> Trailing

    var body: some View {
        HStack(spacing: 0) {
            leading()
            Spacer(minLength: 8)
            trailing()
        }
        .overlay { title() }
        .padding(.horizontal, 20)
        .frame(height: ChromeBarHeight.value)
    }
}

private enum ChromeBarHeight {
    static let value: CGFloat = 44
}

/// A top bar button's glyph, sized as the navigation bar's were (`sized`), or left to the navigation bar.
private struct ChromeLabel: ViewModifier {
    let sized: Bool

    func body(content: Content) -> some View {
        if sized {
            content
                .font(.system(size: 20, weight: .medium))
                .frame(width: 30, height: 30)
                .contentShape(Circle())
                .drawerSwipeGuard()
        } else {
            content.drawerSwipeGuard()
        }
    }
}

extension View {
    fileprivate func chromeButtonLabel() -> some View {
        modifier(ChromeLabel(sized: true))
    }

    /// The top bar's round glass button.
    fileprivate func chromeButton() -> some View {
        drawerGlassButton()
    }

    /// The main screen's top bar, pinned over the top of the content; on iOS 26 as a bar, which earns the soft scroll
    /// edge under it.
    @ViewBuilder
    fileprivate func topBar<Bar: View>(_ shown: Bool = true, @ViewBuilder _ bar: () -> Bar) -> some View {
        if !shown {
            self
        } else if #available(iOS 26.0, *) {
            safeAreaBar(edge: .top, spacing: 0) { DrawerLifted(content: bar()) }
        } else {
            safeAreaInset(edge: .top, spacing: 0) { DrawerLifted(content: bar()) }
        }
    }
}

/// The top bar, kept clear of the status bar by padding (the drawer lays the screen out with no safe area, see
/// SideDrawer). Its buttons ease a little lower as the drawer shrinks the chat out, to sit as the navigation bar's did
/// on the shrunk card: drawn lower only, the bar keeps its place in the layout.
private struct DrawerLifted<Content: View>: View {
    let content: Content
    @Environment(\.drawerBars) private var bars

    var body: some View {
        content
            .visualEffect { content, geo in content.offset(y: 12 * SideDrawerMotion.shrink(of: geo)) }
            .padding(.top, bars?.top ?? 0)
    }
}

/// The drawer's state watched without ChatView's body reading it: read there, a change redrew the whole chat screen.
private struct OnDrawerChange: ViewModifier {
    @Binding var value: Bool
    let action: (Bool) -> Void

    func body(content: Content) -> some View {
        content.onChange(of: value) { _, now in action(now) }
    }
}

extension View {
    /// Reports this view's frame on the chat card (SideDrawerMotion.card), which the drawer's slide doesn't move: it only
    /// changes when the chat's own layout does. Measured on the screen, it changed every frame of a slide, and following
    /// it redrew the whole chat screen each time.
    fileprivate func onCardFrame(_ action: @escaping (CGRect) -> Void) -> some View {
        onGeometryChange(for: CGRect.self, of: { $0.frame(in: .named(SideDrawerMotion.card)) }, action: action)
    }

    fileprivate func onDrawerChange(_ value: Binding<Bool>, perform action: @escaping (Bool) -> Void) -> some View {
        modifier(OnDrawerChange(value: value, action: action))
    }
}

/// Room under the composer for the home indicator, as padding: in the drawer the screen lays out with no safe area
/// (see SideDrawer).
private struct DrawerBottomRoom: ViewModifier {
    let on: Bool
    @Environment(\.drawerBars) private var bars

    func body(content: Content) -> some View {
        content.padding(.bottom, on ? bars?.bottom ?? 0 : 0)
    }
}

/// A round accent button (live voice): the accent at 15% behind an accent glyph today, a flat solid disc
/// or a ring under the other themes.
struct AccentDisc: ViewModifier {
    let style: ThemeAccentButton

    func body(content: Content) -> some View {
        switch style {
        case .tinted:
            content
                .background(Color.accentColor.opacity(0.15), in: Circle())
                .foregroundStyle(Color.accentColor)
        case .solid:
            content
                .background(Color.accentColor, in: Circle())
                .foregroundStyle(.white)
        case .outlined:
            content
                .overlay(Circle().strokeBorder(Color.primary, lineWidth: 1))
                .foregroundStyle(.primary)
        }
    }
}

/// Plan mode worn by the composer: the terminal's green, as a glow round the card's edge with a brighter
/// stretch of light drifting slowly round it, so the box you type in says what a send will do without a
/// word or a pixel of room. Still under Reduce Motion, and paused whenever it is not showing.
///
/// Nothing here redraws per frame: the halo is a fixed blur SwiftUI renders once, and the light is a conic
/// gradient turned by Core Animation on the render server. (A TimelineView rebuilding the gradient and its
/// blur thirty times a second made the whole app lag while plan mode was on.)
struct PlanGlow: View {
    let radius: CGFloat
    let active: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ZStack {
            // A soft halo spilling just past the edge, then the crisp line with the light running round it.
            RoundedRectangle(cornerRadius: radius, style: .continuous)
                .stroke(Color.green.opacity(0.35), lineWidth: 6)
                .blur(radius: 8)
                .opacity(0.55)
            PlanGlowLine(radius: radius, lineWidth: 1.5, moving: active && !reduceMotion)
        }
        .allowsHitTesting(false)
        .accessibilityElement()
        .accessibilityLabel(active ? "Plan mode on" : "")
        .accessibilityIdentifier(active ? "planModeGlow" : "")
        .accessibilityHidden(!active)
    }
}

/// PlanGlow's edge line: a conic green gradient seen only through a stroke of the card's outline, the
/// gradient spinning once every eight seconds.
private struct PlanGlowLine: UIViewRepresentable {
    let radius: CGFloat
    let lineWidth: CGFloat
    let moving: Bool

    func makeUIView(context: Context) -> LineView {
        LineView()
    }

    func updateUIView(_ view: LineView, context: Context) {
        view.radius = radius
        view.lineWidth = lineWidth
        view.moving = moving
    }

    final class LineView: UIView {
        private let light = CAGradientLayer()
        private let outline = CAShapeLayer()
        private static let spin = "spin"

        var radius: CGFloat = 30 { didSet { if radius != oldValue { setNeedsLayout() } } }
        var lineWidth: CGFloat = 1.5 { didSet { if lineWidth != oldValue { setNeedsLayout() } } }
        var moving = false { didSet { if moving != oldValue { updateSpin() } } }

        override init(frame: CGRect) {
            super.init(frame: frame)
            isUserInteractionEnabled = false
            backgroundColor = .clear
            let green = UIColor.systemGreen
            // Base glow, a bright streak, a softer one opposite.
            light.type = .conic
            light.startPoint = CGPoint(x: 0.5, y: 0.5)
            light.endPoint = CGPoint(x: 0.5, y: 0)
            light.colors = [0.35, 1, 0.35, 0.35, 0.8, 0.35, 0.35].map { green.withAlphaComponent($0).cgColor }
            light.locations = [0, 0.12, 0.3, 0.55, 0.68, 0.85, 1]
            layer.addSublayer(light)
            outline.fillColor = nil
            outline.strokeColor = UIColor.black.cgColor
            layer.mask = outline
        }

        required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

        override func layoutSubviews() {
            super.layoutSubviews()
            CATransaction.begin()
            CATransaction.setDisableActions(true)
            // A square as wide as the card's diagonal, centred, so the turning gradient covers every corner.
            let side = hypot(bounds.width, bounds.height)
            light.bounds = CGRect(x: 0, y: 0, width: side, height: side)
            light.position = CGPoint(x: bounds.midX, y: bounds.midY)
            outline.frame = bounds
            outline.lineWidth = lineWidth
            let inset = lineWidth / 2
            outline.path = UIBezierPath(roundedRect: bounds.insetBy(dx: inset, dy: inset),
                                        cornerRadius: max(0, radius - inset)).cgPath
            CATransaction.commit()
        }

        // Core Animation drops animations when the view leaves the window (and on backgrounding), so re-add.
        override func didMoveToWindow() {
            super.didMoveToWindow()
            updateSpin()
        }

        private func updateSpin() {
            guard moving, window != nil else {
                light.removeAnimation(forKey: Self.spin)
                return
            }
            guard light.animation(forKey: Self.spin) == nil else { return }
            let spin = CABasicAnimation(keyPath: "transform.rotation.z")
            spin.fromValue = 0
            spin.toValue = 2 * Double.pi
            spin.duration = 8
            spin.repeatCount = .infinity
            spin.isRemovedOnCompletion = false
            light.add(spin, forKey: Self.spin)
        }
    }
}

/// The chat page's background: the theme's colour, and under Panes a soft violet wash at the top, as the desktop's
/// panes have. The strip under the clock (StatusBarFade) draws the same, so messages fade into it without a seam.
private struct ChatPageBackground: View {
    @Environment(\.theme) private var theme

    var body: some View {
        ZStack(alignment: .top) {
            theme.backgroundColor
            if theme.backdrop {
                LinearGradient(colors: [(theme.accent ?? .accentColor).opacity(0.13), .clear], startPoint: .top, endPoint: .bottom)
                    .frame(height: 260)
            }
        }
    }
}

/// A fade under the status bar alone: messages dissolve into the page's own background beneath the clock and
/// battery, and stay crisp behind the top bar's buttons. The page's background rather than a system material,
/// whose own tint turned the strip grey over Panes' violet. The drawer lays the chat out with no safe area, so the
/// height is its bars' top.
private struct StatusBarFade: View {
    @Environment(\.drawerBars) private var bars

    var body: some View {
        ChatPageBackground()
            .frame(height: bars?.top ?? 0, alignment: .top)
            .clipped()
            .mask(LinearGradient(stops: [.init(color: .black, location: 0.6), .init(color: .clear, location: 1)],
                                 startPoint: .top, endPoint: .bottom))
            .ignoresSafeArea(edges: .top)
            .allowsHitTesting(false)
    }
}

/// Where the transcript's latest line ends on screen.  A plain box, so writing it on every scroll frame redraws nothing.
final class EndMark {
    var y: CGFloat = 0
}

extension View {
    /// The soft scroll-edge blur iOS 26 draws under the bars: messages fade out under the clock and buttons at the
    /// top, and soften into the composer at the bottom instead of running crisp beneath it.
    @ViewBuilder
    func softScrollEdges(top: Bool = true) -> some View {
        if #available(iOS 26.0, *) {
            scrollEdgeEffectStyle(.soft, for: top ? [.top, .bottom] : .bottom)
                .scrollEdgeEffectHidden(!top, for: .top)
        } else {
            self
        }
    }

    /// The composer pinned under the transcript.  On iOS 26 it is a bar, which is what earns it the soft edge.
    @ViewBuilder
    func composerBar<Bar: View>(@ViewBuilder _ bar: () -> Bar) -> some View {
        if #available(iOS 26.0, *) {
            safeAreaBar(edge: .bottom, spacing: 0, content: bar)
        } else {
            safeAreaInset(edge: .bottom, spacing: 0, content: bar)
        }
    }
}



#if DEBUG
/// `--open widgets`: the Home Screen widgets' faces at their sizes, filled from the computer's GET /widgets the way
/// the widgets fetch it, so the UI tests (and screenshots) can check them; Siri-free, WidgetKit-free.
private struct GlancesPreview: View {
    @EnvironmentObject private var client: VisionClient
    @State private var glance: Glance?
    @State private var problem: GlanceProblem?

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                HStack(spacing: 16) {
                    tile { ProjectsGlanceView(glance: glance, problem: problem, size: .small) }
                    tile { UsageGlanceView(glance: glance, problem: problem, size: .small) }
                }
                tile(wide: true) { ProjectsGlanceView(glance: glance, problem: problem, size: .medium) }
                tile(wide: true) { UsageGlanceView(glance: glance, problem: problem, size: .medium) }
                HStack(spacing: 16) {
                    tile { NextUpGlanceView(glance: glance, problem: problem, size: .small) }
                    NextUpGlanceView(glance: glance, problem: problem, size: .lockScreen)
                        .padding(8).frame(width: 160, height: 72).background(.quaternary, in: RoundedRectangle(cornerRadius: 14))
                }
                tile(wide: true) { NextUpGlanceView(glance: glance, problem: problem, size: .medium) }
                tile(wide: true) { RecentChatsGlanceView(glance: glance, problem: problem, size: .medium) }
                HStack(spacing: 16) {
                    tile { RecentChatsGlanceView(glance: glance, problem: problem, size: .small) }
                    tile { ComputerGlanceView(glance: glance, problem: problem, size: .small) }
                }
                HStack(spacing: 16) {   // the Lock Screen's
                    UsageGlanceView(glance: glance, problem: problem, size: .round).frame(width: 72, height: 72)
                    ProjectsGlanceView(glance: glance, problem: problem, size: .lockScreen)
                        .padding(8).frame(width: 170, height: 72).background(.quaternary, in: RoundedRectangle(cornerRadius: 14))
                }
                ProjectsGlanceView(glance: glance, problem: problem, size: .inline)
            }
            .padding()
        }
        .task {
            do { glance = try await GlanceFetch.fetch(SharedPairing.load() ?? client.glancePairing) } catch GlanceError.notPaired {
                problem = .notPaired
            } catch GlanceError.outdated { problem = .outdated } catch { problem = .offline }
        }
    }

    private func tile<V: View>(wide: Bool = false, @ViewBuilder _ content: () -> V) -> some View {
        content().padding(16).frame(width: wide ? 338 : 161, height: 161)
            .background(.quaternary, in: RoundedRectangle(cornerRadius: 22))
    }
}
#endif


/// Behind a message a search hit opened the chat at: a soft accent glow that fades.
private struct FoundFlash: View {
    let on: Bool
    @Environment(\.theme) private var theme

    var body: some View {
        RoundedRectangle(cornerRadius: 14)
            .fill((theme.accent ?? Color.accentColor).opacity(on ? 0.18 : 0))
            .padding(-6)
            .animation(.easeOut(duration: on ? 0.2 : 1.2), value: on)
            .allowsHitTesting(false)
    }
}

/// A view of no size inside the transcript's content, which can picture the scroll view it sits in as it last drew
/// on screen: what a chat switch fades out from.
private struct TranscriptShotter: UIViewRepresentable {
    final class Box {
        /// Every transcript's marker, held weakly: across a switch the old chat's and the new one's are both alive
        /// for a moment and either may report in last, so the one on screen is picked when a picture is wanted.
        private let views = NSHashTable<UIView>.weakObjects()

        func add(_ view: UIView) { views.add(view) }

        func take() -> UIImage? {
            for view in views.allObjects where view.window != nil {
                var v = view.superview
                while let next = v, !(next is UIScrollView) { v = next.superview }
                guard let scroll = v as? UIScrollView, scroll.bounds.width > 0, scroll.bounds.height > 0 else { continue }
                let box = CGRect(origin: .zero, size: scroll.bounds.size)
                return UIGraphicsImageRenderer(bounds: box).image { _ in scroll.drawHierarchy(in: box, afterScreenUpdates: false) }
            }
            return nil
        }
    }

    let box: Box

    func makeUIView(context: Context) -> UIView {
        let view = UIView()
        view.isUserInteractionEnabled = false
        view.isAccessibilityElement = false
        box.add(view)
        return view
    }

    func updateUIView(_ view: UIView, context: Context) { box.add(view) }
}
