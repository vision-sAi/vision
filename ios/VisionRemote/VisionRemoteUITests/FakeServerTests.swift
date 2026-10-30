import XCTest

/// The app against `ios/fake_server.py` on the machine, which plays out a scene on cue and records what the app
/// sends back. Run with the fake's address for the test runner:
///     TEST_RUNNER_VISION_FAKE_URL=http://<machine>:8799 xcodebuild test … -only-testing:VisionRemoteUITests/FakeServerTests
/// Skipped when it isn't set.
final class FakeServerTests: XCTestCase {
    private var base: URL!

    override func setUpWithError() throws {
        guard let url = ProcessInfo.processInfo.environment["VISION_FAKE_URL"].flatMap(URL.init(string:)) else {
            throw XCTSkip("VISION_FAKE_URL isn't set: start ios/fake_server.py and pass its address")
        }
        base = url
        continueAfterFailure = false
    }

    // MARK: - the fake

    @discardableResult
    private func call(_ path: String, _ body: Any? = nil) throws -> Any? {
        var request = URLRequest(url: base.appendingPathComponent(path))
        if let body {
            request.httpMethod = "POST"
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let done = expectation(description: path)
        var data: Data?
        URLSession.shared.dataTask(with: request) { d, _, _ in data = d; done.fulfill() }.resume()
        wait(for: [done], timeout: 10)
        return try data.flatMap { try JSONSerialization.jsonObject(with: $0) }
    }

    /// The first frame the app sent that matches, waiting for it to arrive.
    private func frame(timeout: TimeInterval = 8, where match: ([String: Any]) -> Bool) throws -> [String: Any]? {
        let end = Date().addingTimeInterval(timeout)
        repeat {
            if let hit = (try call("_frames") as? [[String: Any]] ?? []).first(where: match) { return hit }
            Thread.sleep(forTimeInterval: 0.3)
        } while Date() < end
        return nil
    }

    private func sent(_ type: String) throws -> [[String: Any]] {
        (try call("_frames") as? [[String: Any]] ?? []).filter { $0["type"] as? String == type }
    }

    private func launch(_ arguments: [String] = []) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-serverURL", base.absoluteString] + arguments
        app.launchEnvironment["VISION_TOKEN"] = "fake"
        app.launch()
        return app
    }

    /// Settings scrolled down to the server's section, which sits below the fold and isn't drawn until then.
    private func launchServerSettings() -> XCUIApplication {
        let app = launch(["--open", "settings"])
        XCTAssertTrue(app.staticTexts["Settings"].waitForExistence(timeout: 10))
        Thread.sleep(forTimeInterval: 2)   // connected, the section loaded
        app.swipeUp()
        return app
    }

    private func element(_ app: XCUIApplication, label: String) -> XCUIElement {
        app.descendants(matching: .any).matching(NSPredicate(format: "label == %@", label)).firstMatch
    }

    /// The match that's on screen and can take a tap (a closed drawer keeps copies of some rows out of sight).
    private func onScreen(_ query: XCUIElementQuery, timeout: TimeInterval = 5) -> XCUIElement {
        let end = Date().addingTimeInterval(timeout)
        repeat {
            if let hit = query.allElementsBoundByIndex.first(where: { $0.exists && $0.isHittable }) { return hit }
            RunLoop.current.run(until: Date().addingTimeInterval(0.2))
        } while Date() < end
        return query.firstMatch
    }

    private func text(_ app: XCUIApplication, containing part: String) -> XCUIElement {
        app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", part)).firstMatch
    }

    private static let file = "/u/20260930-000000-aaaaaa-notes.md"
    private static let photo = "/u/20260930-000001-bbbbbb.jpg"
    private static let withAttachments = "Look at this\n\n[Attached file: \(file)]\n[Attached image: \(photo)]"

    private func busyChat(queued: [String]) -> [String: Any] {
        ["chat": "c1", "title": "Story", "busy": true, "user_text": "Tell me a story", "partial": "Once upon a time",
         "queued": queued, "history_id": "h1"]
    }

    // MARK: - tests

    /// A phone with nothing paired opens on the welcome screen. An address typed there (the simulator has no
    /// camera to scan with) is checked on the spot, and the chat takes over once the Vision answers.
    func testAFirstLaunchPairsFromTheWelcomeScreen() throws {
        try call("_scene", ["chats": []])
        let app = XCUIApplication()
        app.launchArguments = ["-serverURL", ""]
        app.launch()
        XCTAssertTrue(text(app, containing: "Vision runs on your computer").waitForExistence(timeout: 8))
        XCTAssertTrue(app.buttons["Not now"].exists)

        app.buttons["Enter address manually"].firstMatch.tap()
        let address = app.textFields["https://vision.example.com"]
        XCTAssertTrue(address.waitForExistence(timeout: 5))
        address.tap()
        address.typeText(base.absoluteString)
        let key = app.textFields["Access key"]
        key.tap()
        key.typeText("fake")
        app.buttons["Save"].tap()

        // The confirmation is up for a moment only, so look for it more often than waitForExistence does.
        let connected = text(app, containing: "Connected to")
        let deadline = Date().addingTimeInterval(10)
        var confirmed = false
        while !confirmed && Date() < deadline {
            confirmed = connected.exists
            if !confirmed { Thread.sleep(forTimeInterval: 0.05) }
        }
        XCTAssertTrue(confirmed, "the welcome screen should say it connected")
        XCTAssertTrue(app.buttons["Not now"].waitForNonExistence(timeout: 5), "the welcome screen should close itself once paired")
        XCTAssertTrue(app.buttons["Open on computer"].waitForExistence(timeout: 5), "the chat should be up behind it")
    }

    /// A reply says how long its turn took, as the terminal's footer does: from history, and from the
    /// `done` of a reply that finishes while the app is watching.
    func testARepliesFooterSaysHowLongItTook() throws {
        try call("_scene", ["chats": [["chat": "t1", "title": "Timed", "busy": false, "history_id": "h1"]],
                            "history": ["t1": [["role": "user", "text": "First question"],
                                               ["role": "assistant", "text": "First answer.", "duration_ms": 12300]]]])
        let app = launch()
        XCTAssertTrue(text(app, containing: "First answer.").waitForExistence(timeout: 10))
        XCTAssertTrue(element(app, label: "Took 12.3s").waitForExistence(timeout: 5), "a past reply's time, from history")

        try call("_push", ["type": "start", "chat": "t1", "text": "Second question"])
        XCTAssertTrue(element(app, label: "Working").waitForExistence(timeout: 5), "the timer runs while the reply does")
        try call("_push", ["type": "delta", "chat": "t1", "text": "Second answer."])
        // The app refetches history after `done`. Here the machine has the new turn without its time (as after
        // a restart), so the 4.2 s can only have come from the `done` itself, and must survive the refetch.
        try call("_scene", ["chats": [["chat": "t1", "title": "Timed", "busy": false, "history_id": "h1"]],
                            "history": ["t1": [["role": "user", "text": "First question"],
                                               ["role": "assistant", "text": "First answer.", "duration_ms": 12300],
                                               ["role": "user", "text": "Second question"],
                                               ["role": "assistant", "text": "Second answer."]]]])
        try call("_push", ["type": "done", "chat": "t1", "text": "Second answer.", "error": "", "busy": false,
                           "history_id": "h1", "duration_ms": 4200])
        XCTAssertTrue(element(app, label: "Took 4.2s").waitForExistence(timeout: 5), "the reply that just finished")
        XCTAssertTrue(element(app, label: "Took 12.3s").exists, "the earlier one keeps its own")
    }

    /// The machine's guess at the next message: grey in the empty composer, sent by the faded arrow, or put in
    /// the composer (selected, to type over or change) by tapping it.
    func testASuggestionIsSentFromTheArrowOrEditedFirst() throws {
        try call("_scene", ["chats": [["chat": "g1", "title": "Guess", "busy": false, "history_id": "h1", "suggestion": "yes, move it"]],
                            "history": ["g1": [["role": "user", "text": "Why does my loop run once?"],
                                               ["role": "assistant", "text": "The return is inside it. Want me to move it?"]]]])
        let app = launch()
        let arrow = app.buttons["Send suggestion: yes, move it"]
        XCTAssertTrue(arrow.waitForExistence(timeout: 10))
        shoot(app, "suggestion-shown")
        app.textViews["messageInput"].tap()
        Thread.sleep(forTimeInterval: 1.5)
        shoot(app, "suggestion-edit")
        XCTAssertEqual(app.textViews["messageInput"].value as? String, "yes, move it")
        app.textViews["messageInput"].typeText("no")
        XCTAssertEqual(app.textViews["messageInput"].value as? String, "no", "typing goes over the selected guess")

        app.textViews["messageInput"].clearText()
        XCTAssertTrue(arrow.waitForExistence(timeout: 3), "emptied, the guess is back")
        arrow.tap()
        XCTAssertNotNil(try frame { $0["type"] as? String == "message" && $0["text"] as? String == "yes, move it" })
    }

    /// The first key typed into the empty composer, and the draft cleared again, leave it where it was: one line
    /// of text is as tall as the empty field.
    func testTheComposerHoldsStillAsTheFirstKeyGoesIn() throws {
        try call("_scene", ["chats": [["chat": "s1", "title": "Still", "busy": false, "history_id": "h1"]],
                            "history": ["s1": [["role": "user", "text": "Hi"], ["role": "assistant", "text": "Hello."]]]])
        let app = launch()
        let input = app.textViews["messageInput"]
        XCTAssertTrue(input.waitForExistence(timeout: 10))
        input.tap()
        Thread.sleep(forTimeInterval: 1.5)   // the keyboard up and settled
        let empty = input.frame
        shoot(app, "composer-empty")
        input.typeText("W")
        Thread.sleep(forTimeInterval: 0.5)
        let typed = input.frame
        shoot(app, "composer-typed")
        input.typeText(XCUIKeyboardKey.delete.rawValue)
        Thread.sleep(forTimeInterval: 0.5)
        let cleared = input.frame
        shoot(app, "composer-cleared")
        XCTAssertEqual(typed, empty, "the first key moved the field: \(empty) → \(typed)")
        XCTAssertEqual(cleared, empty, "clearing the draft moved the field: \(empty) → \(cleared)")
    }

    /// After a model or effort switch the machine says so just before the next turn starts, after the phone has
    /// already put the message up: the message stays one bubble, under the note and above its reply.
    func testAMessageSentAfterAnEffortSwitchShowsOnce() throws {
        try call("_scene", ["chats": [["chat": "e1", "title": "Effort", "busy": false, "history_id": "h1"]],
                            "history": ["e1": [["role": "user", "text": "Hi"], ["role": "assistant", "text": "Hello."]]]])
        let app = launch()
        let input = app.textViews["messageInput"]
        XCTAssertTrue(input.waitForExistence(timeout: 10))
        input.tap()
        input.typeText("Make it louder")
        app.buttons["Send"].tap()
        XCTAssertNotNil(try frame { $0["type"] as? String == "message" && $0["text"] as? String == "Make it louder" })
        try call("_push", ["type": "note", "chat": "e1", "text": "Effort set to high", "switch": true])
        try call("_push", ["type": "start", "chat": "e1", "text": "Make it louder"])
        try call("_push", ["type": "delta", "chat": "e1", "text": "Turning it up."])
        XCTAssertTrue(text(app, containing: "Turning it up.").waitForExistence(timeout: 5))
        shoot(app, "effort-then-send")
        let bubbles = app.staticTexts.matching(NSPredicate(format: "label == %@", "Make it louder"))
        XCTAssertEqual(bubbles.count, 1, "one bubble for one message")
        let note = text(app, containing: "Effort set to high")
        XCTAssertTrue(note.exists)
        XCTAssertLessThan(note.frame.minY, bubbles.firstMatch.frame.minY, "the switch is said above the message it came before")
    }

    /// A suggestion tapped in is all selected; its start handle, flush with the field's left edge, can be dragged
    /// right to take in less, as the end handle can be dragged left.
    func testTheStartHandleOfATappedInSuggestionMoves() throws {
        try call("_scene", ["chats": [["chat": "g1", "title": "Guess", "busy": false, "history_id": "h1",
                                       "suggestion": "yes, move it out of the loop"]],
                            "history": ["g1": [["role": "user", "text": "Why does my loop run once?"],
                                               ["role": "assistant", "text": "The return is inside it. Want me to move it?"]]]])
        let app = launch()
        let input = app.textViews["messageInput"]
        XCTAssertTrue(app.buttons["Send suggestion: yes, move it out of the loop"].waitForExistence(timeout: 10))
        input.tap()
        Thread.sleep(forTimeInterval: 1.5)   // the keyboard up, the guess in and selected, its handles drawn
        XCTAssertEqual(input.value as? String, "yes, move it out of the loop")
        shoot(app, "suggestion-selected")
        // Where a finger lands on the start handle: on its bar, a hair left of the first letter, which sits 14 pt in
        // from the field's edge (ComposerField.handleRoom).
        let handle = input.coordinate(withNormalizedOffset: CGVector(dx: 0, dy: 0.5)).withOffset(CGVector(dx: 12, dy: 0))
        handle.press(forDuration: 0.3, thenDragTo: handle.withOffset(CGVector(dx: 60, dy: 0)))
        Thread.sleep(forTimeInterval: 1)
        shoot(app, "suggestion-start-handle-dragged")
        input.typeText("Z")   // replaces whatever is still selected
        let value = input.value as? String ?? ""
        XCTAssertNotEqual(value, "Z", "the start handle didn't move: the whole guess was still selected")
        XCTAssertTrue(value.hasPrefix("y") && value.hasSuffix("Z"), "the start of the guess kept, the rest replaced: \(value)")
    }

    /// `!` in the empty composer turns it into a shell prompt; the command goes to the computer, and its row
    /// shows what it printed.
    func testBangRunsACommandOnTheComputer() throws {
        try call("_scene", ["chats": [["chat": "sh1", "title": "Shell", "busy": false, "history_id": "h1", "suggestion": "thanks"]],
                            "history": ["sh1": [["role": "user", "text": "Hi"], ["role": "assistant", "text": "Hello."]]],
                            "shell": true])
        let app = launch()
        let input = app.textViews["messageInput"]
        XCTAssertTrue(input.waitForExistence(timeout: 10))
        input.tap()   // the suggestion comes in, selected
        Thread.sleep(forTimeInterval: 1)
        XCTAssertEqual(input.value as? String, "thanks")
        input.typeText("!")   // over the selected suggestion: shell mode all the same
        XCTAssertEqual(input.value as? String ?? "", "", "the ! switches the mode; it isn't typed")
        input.typeText("git status")   // (the simulator's typing never auto-capitalises, so `Pwd` can't be caught here)
        shoot(app, "shell-typing")
        app.buttons["Run command"].tap()
        XCTAssertNotNil(try frame { $0["type"] as? String == "shell" && $0["text"] as? String == "git status" })
        XCTAssertTrue(app.buttons["Run command"].waitForNonExistence(timeout: 3), "back to a message box")

        try call("_push", ["type": "shell", "chat": "sh1", "text": "git status", "running": true])
        XCTAssertTrue(text(app, containing: "running…").waitForExistence(timeout: 5))
        // What it prints while it runs shows straight away (a login's code, printed before it waits).
        try call("_push", ["type": "shell_output", "chat": "sh1", "text": "git status", "output": "code: 7ADB-FA3A"])
        XCTAssertTrue(text(app, containing: "7ADB-FA3A").waitForExistence(timeout: 5))
        XCTAssertTrue(text(app, containing: "running…").exists, "still going")
        shoot(app, "shell-live")
        try call("_push", ["type": "shell", "chat": "sh1", "text": "git status",
                           "output": "On branch master\nnothing to commit, working tree clean", "code": 1])
        XCTAssertTrue(text(app, containing: "nothing to commit").waitForExistence(timeout: 5))
        XCTAssertTrue(text(app, containing: "exit 1").exists)
        shoot(app, "shell-row")

        input.tap()
        input.typeText("!")
        input.typeText(XCUIKeyboardKey.delete.rawValue)  // Backspace on the empty box leaves shell mode
        input.typeText("hello")
        XCTAssertTrue(app.buttons["Send"].waitForExistence(timeout: 3), "a message again, not a command")
    }

    /// The voice switch: off on the computer, the call button says so instead of starting a call; Settings'
    /// switch turns it back on there.
    func testTheVoiceSwitchTurnsTheComputersVoiceOnAndOff() throws {
        try call("_scene", ["chats": [["chat": "v1", "title": "Voice", "busy": false, "history_id": "h1"]],
                            "history": ["v1": [["role": "user", "text": "Hi"], ["role": "assistant", "text": "Hello."]]],
                            "voice_on": false,
                            // As a real Vision reports them: the voice row is labelled "Voice" too.
                            "status": ["rows": [["label": "Voice", "value": "jarvis · cuda", "state": "ready"],
                                                ["label": "Speech recognition", "value": "large-v3-turbo", "state": "ready"]],
                                       "problems": []]])
        let app = launch()
        let callButton = app.buttons["Live voice, off on the computer"]
        XCTAssertTrue(callButton.waitForExistence(timeout: 10))
        callButton.tap()
        XCTAssertTrue(text(app, containing: "voice is off on the computer").waitForExistence(timeout: 3))
        shoot(app, "voice-off-chat")

        let settings = launch(["--open", "settings"])
        let toggle = settings.switches["voiceSwitch"]
        XCTAssertTrue(toggle.waitForExistence(timeout: 10))
        Thread.sleep(forTimeInterval: 2)   // the status rows arrive
        shoot(settings, "voice-off-settings")
        XCTAssertFalse(text(settings, containing: "Speaks as").exists, "off: no speech rows under the switch")
        XCTAssertFalse(text(settings, containing: "Speech recognition").exists)
        toggle.switches.firstMatch.exists ? toggle.switches.firstMatch.tap() : toggle.tap()
        XCTAssertNotNil(try frame { $0["type"] as? String == "_voice_power" && $0["on"] as? Bool == true })
        XCTAssertTrue(text(settings, containing: "Loaded on the computer's GPU").waitForExistence(timeout: 5))
        XCTAssertTrue(text(settings, containing: "Speaks as").waitForExistence(timeout: 5), "on: the voice row, under its own name")
        shoot(settings, "voice-on-settings")
        // Counted on the sheet only: the chat underneath has its own.
        let named = settings.staticTexts.matching(NSPredicate(format: "label == %@", "Voice")).allElementsBoundByIndex.filter(\.isHittable)
        XCTAssertEqual(named.count, 1, "one row called Voice")
    }

    private func shoot(_ app: XCUIApplication, _ name: String) {
        let shot = app.screenshot()
        let attachment = XCTAttachment(screenshot: shot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
        try? shot.pngRepresentation.write(to: URL(fileURLWithPath: "/tmp/\(name).png"))
    }

    /// A finished reply can be copied or shared whole from under it; one still being written cannot.
    func testAFinishedReplyCanBeCopiedAndShared() throws {
        try call("_scene", ["chats": [["chat": "c1", "title": "Copy", "busy": false, "history_id": "h1"]],
                            "history": ["c1": [["role": "user", "text": "First question"],
                                               ["role": "assistant", "text": "First answer.", "duration_ms": 2000]]]])
        let app = launch()
        XCTAssertTrue(text(app, containing: "First answer.").waitForExistence(timeout: 10))
        XCTAssertEqual(app.buttons.matching(identifier: "Copy reply").count, 1)

        try call("_push", ["type": "start", "chat": "c1", "text": "Second question"])
        try call("_push", ["type": "delta", "chat": "c1", "text": "Second answer, so far"])
        XCTAssertTrue(text(app, containing: "Second answer, so far").waitForExistence(timeout: 5))
        XCTAssertEqual(app.buttons.matching(identifier: "Copy reply").count, 1, "nothing to copy on a reply that is still coming")

        app.buttons["Copy reply"].firstMatch.tap()
        XCTAssertTrue(text(app, containing: "Copied").waitForExistence(timeout: 3))

        app.buttons["Share reply"].firstMatch.tap()
        let sheet = app.otherElements["ActivityListView"]
        let bar = app.navigationBars["UIActivityContentView"]
        XCTAssertTrue(sheet.waitForExistence(timeout: 6) || bar.exists, "the system share sheet")
    }

    private func readAloudScene(voiceOn: Bool, readAloud: Bool = true) throws {
        try call("_scene", ["chats": [["chat": "c1", "title": "Read", "busy": false, "history_id": "h1"]],
                            "history": ["c1": [["role": "user", "text": "Why does my loop run once?"],
                                               ["role": "assistant", "text": "The return is inside the loop. Move it one level out.",
                                                "duration_ms": 2000]]],
                            "voice_on": voiceOn, "read_aloud": readAloud])
    }

    /// The speaker under a finished reply reads it in the computer's voice, a piece at a time, and stops when
    /// tapped again.
    func testAFinishedReplyIsReadAloudInTheComputersVoice() throws {
        try readAloudScene(voiceOn: true)
        let app = launch()
        let button = app.buttons["Read aloud"].firstMatch
        XCTAssertTrue(button.waitForExistence(timeout: 10))
        button.tap()
        let cut = try frame { $0["type"] as? String == "_speak_pieces" }
        XCTAssertEqual(cut?["text"] as? String, "The return is inside the loop. Move it one level out.")
        XCTAssertNotNil(try frame { $0["type"] as? String == "_speak" && $0["text"] as? String == "The return is inside the loop." })
        XCTAssertNotNil(try frame { $0["type"] as? String == "_speak" && $0["text"] as? String == "Move it one level out." })
        XCTAssertEqual(try sent("_speak").first?["spoken"] as? Bool, true, "already speakable: not put through speechify again")
        let stop = app.buttons["Stop reading"].firstMatch
        XCTAssertTrue(stop.waitForExistence(timeout: 3))
        shoot(app, "read-aloud-playing")
        stop.tap()
        XCTAssertTrue(app.buttons["Read aloud"].firstMatch.waitForExistence(timeout: 3))
    }

    /// With the voice off on the computer, the phone reads the same pieces in its own voice and asks for no WAVs.
    func testWithTheComputersVoiceOffTheReplyIsReadInThePhonesVoice() throws {
        try readAloudScene(voiceOn: false)
        let app = launch()
        let button = app.buttons["Read aloud"].firstMatch
        XCTAssertTrue(button.waitForExistence(timeout: 10))
        button.tap()
        XCTAssertNotNil(try frame { $0["type"] as? String == "_speak_pieces" })
        XCTAssertTrue(app.buttons["Stop reading"].firstMatch.waitForExistence(timeout: 3))
        XCTAssertEqual(try sent("_speak").count, 0, "nothing asked of a voice that is off")
        // Said through, it comes back to the speaker by itself.
        XCTAssertTrue(app.buttons["Read aloud"].firstMatch.waitForExistence(timeout: 20))
    }

    /// A Vision from before read-aloud has nothing to cut the reply with: no speaker under it.
    func testNoReadAloudForAVisionThatCannotCutAReply() throws {
        try readAloudScene(voiceOn: true, readAloud: false)
        let app = launch()
        XCTAssertTrue(app.buttons["Copy reply"].firstMatch.waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["Read aloud"].exists)
    }

    private func widgetScene(_ widgets: [String: Any]?) throws {
        var scene: [String: Any] = ["chats": [["chat": "c1", "title": "Chat", "busy": false, "history_id": "h1"]],
                                    "history": ["c1": [["role": "user", "text": "Hi"], ["role": "assistant", "text": "Hello."]]]]
        if let widgets { scene["widgets"] = widgets }
        try call("_scene", scene)
    }

    /// The Home Screen widgets' faces show what the computer's GET /widgets says: threads by state and name,
    /// each provider's usage, the next reminder.
    func testTheWidgetsShowWhatTheComputerSays() throws {
        let inTwoHours = Date().addingTimeInterval(2 * 3600).timeIntervalSince1970
        try widgetScene([
            "threads": ["working": 2, "needsYou": 1, "ready": 0, "projects": 2, "rows": [
                ["title": "Pick a database", "state": "waiting_on_you", "project": "p2", "project_title": "Sync"],
                ["title": "Fix the flaky test", "state": "working", "project": "p1", "project_title": "Vision"]]],
            "usage": [["provider": "claude", "label": "Claude", "windows": [
                ["label": "Current session", "used_percent": 24.0, "resets_at": inTwoHours],
                ["label": "Current week", "used_percent": 41.0, "resets_at": inTwoHours + 86400 * 3]]]],
            "next": ["id": "t1", "title": "Call the dentist", "kind": "remind", "at": inTwoHours],
            "upcoming": [["id": "t1", "title": "Call the dentist", "kind": "remind", "at": inTwoHours],
                         ["id": "t2", "title": "Check the nightly build", "kind": "task", "at": inTwoHours + 3600]],
            "chats": [["chat": "c2", "title": "Trip plan", "busy": true, "last": "", "updated": 2],
                      ["chat": "c1", "title": "Loop bug", "busy": false, "last": "The return was inside the loop.", "updated": 1]],
            "computer": ["name": "spike", "cpu": 12, "memory": 48, "disk": 63, "uptime": 3 * 86400.0,
                         "voice_on": false, "update": "vision upgrade"],
        ])
        let app = launch(["--open", "widgets"])
        XCTAssertTrue(text(app, containing: "Pick a database").waitForExistence(timeout: 10))
        XCTAssertTrue(text(app, containing: "need you").exists)
        XCTAssertTrue(text(app, containing: "Sync").exists, "each thread says its project")
        XCTAssertTrue(text(app, containing: "Claude").exists)
        XCTAssertTrue(text(app, containing: "24%").exists)
        XCTAssertTrue(text(app, containing: "Session").exists, "\"Current session\" shortened to fit")
        XCTAssertTrue(text(app, containing: "Call the dentist").exists)
        XCTAssertTrue(text(app, containing: "Today").exists || text(app, containing: "Tomorrow").exists)
        XCTAssertTrue(text(app, containing: "Check the nightly build").exists, "the medium Next Up lists what's after it")
        XCTAssertTrue(text(app, containing: "Loop bug").exists)
        XCTAssertTrue(text(app, containing: "The return was inside the loop.").exists)
        XCTAssertTrue(text(app, containing: "Replying").exists, "a chat still replying says so")
        XCTAssertTrue(text(app, containing: "spike").exists)
        XCTAssertTrue(text(app, containing: "63%").exists, "disk in use")
        XCTAssertTrue(text(app, containing: "Update available").exists)
        XCTAssertTrue(text(app, containing: "1 needs you · 2 working").exists, "the Lock Screen line")
        shoot(app, "widgets")
        app.swipeUp()
        shoot(app, "widgets-more")
    }

    /// A Vision from before the widgets: they say to update it, not that the computer can't be reached.
    func testTheWidgetsAskForAnUpdateFromAnOldVision() throws {
        try widgetScene(nil)
        let app = launch(["--open", "widgets"])
        XCTAssertTrue(text(app, containing: "run vision upgrade").waitForExistence(timeout: 10))
    }

    /// Nothing running and nothing scheduled reads as such, not as an error.
    func testQuietWidgets() throws {
        try widgetScene(["threads": ["projects": 1]])
        let app = launch(["--open", "widgets"])
        XCTAssertTrue(text(app, containing: "All quiet").waitForExistence(timeout: 10))
        XCTAssertTrue(text(app, containing: "Nothing scheduled").exists)
    }

    /// A widget's tap opens its screen: Next Up the scheduled list, Message Vision the keyboard.
    func testWidgetTapsOpenTheirScreens() throws {
        try widgetScene(["threads": ["projects": 0]])
        let app = launch()
        XCTAssertTrue(app.textViews["messageInput"].waitForExistence(timeout: 10))
        app.open(URL(string: "vision://scheduled")!)
        XCTAssertTrue(app.navigationBars["Coming up"].waitForExistence(timeout: 5))
        app.buttons["Close"].firstMatch.tap()
        app.open(URL(string: "vision://compose")!)
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5))
    }

    /// The drawer's search finds what was said in a chat, not only titles, and a hit opens that chat.
    func testSearchFindsWhatWasSaidInAChat() throws {
        try call("_scene", ["chats": [["chat": "c1", "title": "Groceries", "busy": false, "history_id": "h1", "updated": 2],
                                      ["chat": "c2", "title": "Homelab backup plan", "busy": false, "history_id": "h2", "updated": 1]],
                            "history": ["c1": [["role": "user", "text": "milk and eggs"]],
                                        "c2": [["role": "user", "text": "what should the backup plan be?"],
                                               ["role": "assistant", "text": "A weekly copy to the offsite box with restic."]]]])
        let app = launch()
        app.buttons["Chats"].tap()
        let field = app.textFields["searchChats"]
        XCTAssertTrue(field.waitForExistence(timeout: 10))
        field.tap()
        field.typeText("restic")
        let hit = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "offsite box")).firstMatch
        XCTAssertTrue(hit.waitForExistence(timeout: 5), "In messages lists the chat with the line that matched")
        XCTAssertTrue(text(app, containing: "In messages").exists)
        XCTAssertFalse(app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Groceries")).firstMatch.exists, "titles filter too")
        XCTAssertNotNil(try frame { $0["type"] as? String == "_search" && $0["q"] as? String == "restic" })
        hit.tap()
        XCTAssertTrue(text(app, containing: "with restic").waitForExistence(timeout: 5), "the hit opens its chat")
    }

    /// A pinned chat sits under Pinned above the others, and a long press pins another (the machine keeps it).
    func testAChatIsPinnedToTheTopOfTheDrawer() throws {
        try call("_scene", ["chats": [["chat": "c1", "title": "Groceries", "busy": false, "history_id": "h1", "pinned": true, "updated": 1,
                                       "folder": "~/code/vision"],
                                      ["chat": "c2", "title": "Tax return", "busy": false, "history_id": "h2", "updated": 2]],
                            "history": ["c1": [], "c2": []]])
        let app = launch()
        app.buttons["Chats"].tap()
        let pinned = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Groceries")).firstMatch
        let other = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Tax return")).firstMatch
        XCTAssertTrue(pinned.waitForExistence(timeout: 10))
        XCTAssertTrue(text(app, containing: "Pinned").exists)
        XCTAssertTrue(pinned.label.contains("~/code/vision"), "its row says where it works: \(pinned.label)")
        XCTAssertLessThan(pinned.frame.minY, other.frame.minY, "pinned first, though the other moved last")
        other.press(forDuration: 1.0)
        let pin = app.buttons["Pin"]
        XCTAssertTrue(pin.waitForExistence(timeout: 5))
        pin.tap()
        let sent = try frame { $0["type"] as? String == "pin" }
        XCTAssertEqual(sent?["chat"] as? String, "c2")
        XCTAssertEqual(sent?["pinned"] as? Bool, true)
    }

    /// A chat tapped on Recent Chats opens that chat.
    func testARecentChatTappedOpensIt() throws {
        try call("_scene", ["chats": [["chat": "c1", "title": "First", "busy": false, "history_id": "h1"],
                                      ["chat": "c2", "title": "Second", "busy": false, "history_id": "h2"]],
                            "history": ["c1": [["role": "user", "text": "Hi"], ["role": "assistant", "text": "First chat's reply."]],
                                        "c2": [["role": "user", "text": "Yo"], ["role": "assistant", "text": "Second chat's reply."]]],
                            "widgets": ["threads": ["projects": 0]]])
        let app = launch()
        XCTAssertTrue(app.textViews["messageInput"].waitForExistence(timeout: 10))
        app.open(URL(string: "vision://chat/c1")!)
        XCTAssertTrue(text(app, containing: "First chat's reply.").waitForExistence(timeout: 5))
        app.open(URL(string: "vision://chat/c2")!)
        XCTAssertTrue(text(app, containing: "Second chat's reply.").waitForExistence(timeout: 5))
    }

    /// A code block in a reply says its language and copies just its code, ready to paste.
    func testACodeBlockCopiesJustItsCode() throws {
        let reply = "Move the return out:\n\n```python\nfor x in items:\n    total += x\nreturn total\n```\n\nThat's it."
        try call("_scene", ["chats": [["chat": "k1", "title": "Code", "busy": false, "history_id": "h1"]],
                            "history": ["k1": [["role": "user", "text": "Fix my loop"], ["role": "assistant", "text": reply]]]])
        let app = launch()
        let copy = app.buttons["Copy code"].firstMatch
        XCTAssertTrue(copy.waitForExistence(timeout: 10))
        XCTAssertTrue(text(app, containing: "python").exists, "the fence's language is shown over the code")
        shoot(app, "code-block")
        copy.tap()
        XCTAssertTrue(app.buttons["Copied"].waitForExistence(timeout: 3))
        shoot(app, "code-block-copied")
        XCTAssertTrue(app.buttons["Copy code"].waitForExistence(timeout: 4), "back to Copy after a moment")

        let input = app.textViews["messageInput"]
        input.tap()
        input.press(forDuration: 1.0)
        let paste = app.menuItems["Paste"]
        XCTAssertTrue(paste.waitForExistence(timeout: 5))
        paste.tap()
        let allow = XCUIApplication(bundleIdentifier: "com.apple.springboard").buttons["Allow Paste"]
        if allow.waitForExistence(timeout: 2) { allow.tap() }
        XCTAssertEqual(input.value as? String, "for x in items:\n    total += x\nreturn total", "the code alone, no fences")
    }

    /// A picture on the clipboard pastes into the message box as a photo to send, where a text view
    /// alone would offer no Paste for it.
    func testAPictureOnTheClipboardPastesAsAPhoto() throws {
        try call("_scene", ["chats": [["chat": "p1", "title": "Paste", "busy": false, "history_id": "h1"]], "history": ["p1": []]])
        let picture = UIGraphicsImageRenderer(size: CGSize(width: 40, height: 30)).image { context in
            UIColor.systemOrange.setFill()
            context.fill(CGRect(x: 0, y: 0, width: 40, height: 30))
        }
        UIPasteboard.general.image = picture
        let app = launch()
        let input = app.textViews["messageInput"]
        XCTAssertTrue(input.waitForExistence(timeout: 10))
        input.tap()
        input.press(forDuration: 1.0)
        let paste = app.menuItems["Paste"]
        XCTAssertTrue(paste.waitForExistence(timeout: 5), "Paste is offered for a picture")
        paste.tap()
        // Reading another app's clipboard can ask first.
        let allow = XCUIApplication(bundleIdentifier: "com.apple.springboard").buttons["Allow Paste"]
        if allow.waitForExistence(timeout: 2) { allow.tap() }
        XCTAssertTrue(app.buttons["Remove photo"].waitForExistence(timeout: 5), "the picture is attached")
        XCTAssertEqual(input.value as? String ?? "", "", "nothing was typed in its place")
    }

    /// A long press on a finished reply selects the word under the finger and brings up the system's menu
    /// for a selection; a Text only offered the whole block to copy. Its paragraphs select as one, and a
    /// slide that starts on it still pulls the chats drawer.
    func testAWordInAFinishedReplyCanBeSelected() throws {
        try call("_scene", ["chats": [["chat": "s1", "title": "Select", "busy": false, "history_id": "h1"]],
                            "history": ["s1": [["role": "user", "text": "First question"],
                                               ["role": "assistant", "text": "First answer, with words to choose from.\n\nAnd a second paragraph."]]]])
        let app = launch()
        let reply = text(app, containing: "words to choose from")
        XCTAssertTrue(reply.waitForExistence(timeout: 10))
        XCTAssertTrue(reply.label.contains("second paragraph"), "both paragraphs are one stretch of text")
        reply.coordinate(withNormalizedOffset: CGVector(dx: 0.2, dy: 0.2)).press(forDuration: 1.0)
        XCTAssertTrue(app.menuItems["Look Up"].waitForExistence(timeout: 5), "a word is selected, not the whole reply")
        XCTAssertTrue(app.menuItems["Copy"].exists)
        // A slide that starts on the selection drags its handle: the drawer stays shut.
        let held = reply.coordinate(withNormalizedOffset: CGVector(dx: 0.2, dy: 0.2))
        held.press(forDuration: 0.05, thenDragTo: held.withOffset(CGVector(dx: 200, dy: 0)))
        let shut = app.staticTexts.matching(NSPredicate(format: "label == 'Vision'")).firstMatch
        sleep(1)
        XCTAssertFalse(shut.exists && shut.isHittable, "the slide pulled the drawer out from under the selection")

        app.terminate()
        let again = launch()
        let text = text(again, containing: "words to choose from")
        XCTAssertTrue(text.waitForExistence(timeout: 10))
        let start = text.coordinate(withNormalizedOffset: CGVector(dx: 0.3, dy: 0.5))
        start.press(forDuration: 0.05, thenDragTo: start.withOffset(CGVector(dx: 260, dy: 0)))
        let drawer = again.staticTexts.matching(NSPredicate(format: "label == 'Vision'")).firstMatch
        XCTAssertTrue(drawer.waitForExistence(timeout: 3) && drawer.isHittable, "the drawer is out")
    }

    /// Holding one of your own messages brings up a menu, as ChatGPT's app does: copy it, share it, or
    /// make its words selectable in the bubble, until a tap elsewhere lets the selection go.
    func testHoldingYourOwnMessageOffersCopySelectAndShare() throws {
        try call("_scene", ["chats": [["chat": "o1", "title": "Own", "busy": false, "history_id": "h1"]],
                            "history": ["o1": [["role": "user", "text": "A question with several words in it"],
                                               ["role": "assistant", "text": "An answer."]]]])
        let app = launch()
        let mine = text(app, containing: "A question with several words")
        XCTAssertTrue(mine.waitForExistence(timeout: 10))
        mine.press(forDuration: 1.0)
        XCTAssertTrue(app.buttons["Copy"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["Share"].exists)
        app.buttons["Select Text"].tap()

        // No sheet: the bubble itself is text to select from now, so holding a word selects it.
        XCTAssertTrue(mine.waitForExistence(timeout: 5))
        XCTAssertFalse(app.navigationBars["Select Text"].exists)
        mine.press(forDuration: 1.0)
        XCTAssertTrue(app.menuItems["Look Up"].waitForExistence(timeout: 5), "a word of it is selected, in the bubble")
        XCTAssertFalse(app.buttons["Select Text"].exists, "the bubble's own menu stays away while it is being selected from")

        // A tap elsewhere lets the selection go, and the bubble is itself again.
        app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.6)).tap()
        XCTAssertTrue(app.menuItems["Look Up"].waitForNonExistence(timeout: 5))
        mine.press(forDuration: 1.0)
        XCTAssertTrue(app.buttons["Select Text"].waitForExistence(timeout: 5))
        app.buttons["Copy"].tap()
        XCTAssertTrue(text(app, containing: "Copied").waitForExistence(timeout: 3))
    }

    /// The drawer's folder button opens the machine's folders: the recent ones, and the rest to walk
    /// through. Starting a chat from it asks the machine for a new chat in that folder, and an empty
    /// chat says where it works.
    func testANewChatCanBeStartedInAChosenFolder() throws {
        let home: [String: Any] = ["path": "/home/me", "label": "~", "parent": "/home",
                                   "folders": [["name": "code", "path": "/home/me/code"], ["name": "notes", "path": "/home/me/notes"]],
                                   "recent": [["path": "/home/me/code/vision", "label": "~/code/vision"]], "more": 0]
        let code: [String: Any] = ["path": "/home/me/code", "label": "~/code", "parent": "/home/me",
                                   "folders": [["name": "vision", "path": "/home/me/code/vision"]], "more": 0]
        try call("_scene", ["chats": [["chat": "f1", "title": "Here", "busy": false, "history_id": "h1", "folder": "~/code/vision"]],
                            "history": ["f1": []], "folders": ["": home, "/home/me/code": code]])
        let app = launch()
        XCTAssertTrue(element(app, label: "Working in ~/code/vision").waitForExistence(timeout: 10), "an empty chat says where it works")

        app.buttons["Chats"].tap()
        let folderButton = app.buttons["New chat in a folder"]
        XCTAssertTrue(folderButton.waitForExistence(timeout: 5))
        folderButton.tap()
        XCTAssertTrue(app.buttons["~/code/vision"].waitForExistence(timeout: 5), "the recent folder")
        app.buttons["code"].tap()
        XCTAssertTrue(app.buttons["vision"].waitForExistence(timeout: 5), "inside the folder that was tapped")
        app.buttons["Start chat here"].tap()
        let sent = try frame { $0["type"] as? String == "new" }
        XCTAssertEqual(sent?["workdir"] as? String, "/home/me/code")
    }

    /// Scouts (every project is one) is a screen of its own, picked in the drawer, and a scout's chat isn't listed
    /// with the chats. A tap on one whose chat isn't open asks the machine for it; New scout makes one there and
    /// opens its chat.
    func testProjectsAreListedApartFromChatsAndCanBeStarted() throws {
        let projects: [[String: Any]] = [
            ["id": "p1", "title": "Flaky tests", "goal": "Fix the flaky tests", "folder": "~/code/vision", "chat": "pc1", "status": "active"],
            ["id": "p2", "title": "Widget", "goal": "Ship the widget", "folder": "~/code/app", "chat": "", "status": "active"],
        ]
        try call("_scene", ["chats": [["chat": "pc1", "title": "Flaky tests", "busy": false, "history_id": "h1", "project": "p1",
                                       "folder": "~/code/vision"],
                                      ["chat": "c2", "title": "Groceries", "busy": false, "history_id": "h2"]],
                            "history": ["pc1": [], "c2": []], "projects": projects,
                            "folders": ["": ["path": "/home/me", "label": "~", "parent": "/home", "recent": [],
                                             "folders": [["name": "code", "path": "/home/me/code"]], "more": 0],
                                        "/home/me/code": ["path": "/home/me/code", "label": "~/code", "parent": "/home/me", "folders": [], "more": 0]]])
        let app = launch()
        app.buttons["Chats"].tap()
        XCTAssertTrue(text(app, containing: "Groceries").waitForExistence(timeout: 10))
        // Scouts are listed under their row with their buddies, not among the chats; a scout's own chat isn't a chat row.
        XCTAssertTrue(app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", "Widget,")).firstMatch.exists, "the scout is in the drawer")
        XCTAssertEqual(app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Flaky tests")).count, 1,
                       "a project's chat isn't listed with the chats too")

        app.buttons["Scouts"].tap()
        XCTAssertTrue(text(app, containing: "Widget").waitForExistence(timeout: 5))
        // One whose chat is open: pushed over the Projects screen, and back returns to it. The drawer, away under the
        // screen, lists the scouts too: the one on screen is tapped.
        onScreen(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Flaky tests"))).tap()
        XCTAssertTrue(app.buttons["Threads"].waitForExistence(timeout: 5), "the project's chat is pushed")
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(text(app, containing: "Widget").waitForExistence(timeout: 5), "back on the Projects screen")

        // One whose chat isn't open: pushed at once, opening while the machine opens its chat. The fake never does,
        // and back returns to the Projects screen. The drawer lists it too, so the one on screen is tapped.
        onScreen(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Widget"))).tap()
        let opening = app.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS %@", "Opening project")).firstMatch
        XCTAssertTrue(opening.waitForExistence(timeout: 5), "pushed before the machine answers")
        let opened = try frame { $0["type"] as? String == "new" }
        XCTAssertEqual(opened?["project"] as? String, "p2")
        app.navigationBars.buttons.element(boundBy: 0).tap()

        let newScout = app.buttons["New scout"]
        XCTAssertTrue(newScout.waitForExistence(timeout: 5))
        newScout.tap()
        let goal = app.textFields["Goal"]
        XCTAssertTrue(goal.waitForExistence(timeout: 5))
        goal.tap()
        goal.typeText("Tidy the docs")
        XCTAssertTrue(app.buttons["Create"].isEnabled, "a goal is enough: the folder is optional")
        app.buttons["More options"].tap()
        let name = app.textFields["Scout's name"]
        XCTAssertEqual(name.value as? String, "Atlas", "a name none of the others has is suggested")
        name.tap()
        name.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: 8) + "Quill")
        app.buttons["sprout"].tap()
        app.buttons["Folder"].tap()
        XCTAssertTrue(app.buttons["code"].waitForExistence(timeout: 5))
        app.buttons["code"].tap()
        app.buttons["Use this folder"].tap()
        app.buttons["Create"].tap()
        let made = try frame { $0["type"] as? String == "_post_projects" }
        XCTAssertEqual(made?["goal"] as? String, "Tidy the docs")
        XCTAssertEqual(made?["workdir"] as? String, "/home/me/code")
        let scout = made?["scout"] as? [String: Any]
        XCTAssertEqual(scout?["name"] as? String, "Quill")
        XCTAssertEqual(scout?["trait"] as? String, "sprout")
        XCTAssertEqual((made?["settings"] as? [String: Any])?["leash"] as? String, "checkins")
        let scheduled = try frame { $0["type"] as? String == "_checkins" }
        XCTAssertEqual(scheduled?["repeat"] as? String, "daily", "a new scout checks in every morning unless told otherwise")
        let openedNew = try frame { $0["type"] as? String == "new" && $0["project"] as? String == "p3" }
        XCTAssertNotNil(openedNew, "the new project's chat is opened")
    }

    /// A scout: its buddy and what needs you in the list, a request to run something approved from
    /// its sheet as edited, a check-in shown as a card in its chat, and its leash and rules changed from settings.
    func testAScoutsRequestIsApprovedAndItsLeashChanged() throws {
        let rules: [[String: Any]] = [
            ["action": "land", "label": "Land a branch into the project folder", "rule": "asks", "set": false],
            ["action": "publish", "label": "Push or open a pull request", "rule": "asks", "set": false],
        ]
        let projects: [[String: Any]] = [
            ["id": "p1", "title": "Desktop app", "goal": "Keep the desktop app moving", "folder": "~/code/vision", "chat": "sc1",
             "status": "active", "scout": ["name": "Atlas", "hue": 205, "trait": "antenna"], "mood": "needs_you", "leash": "checkins",
             "settings": ["leash": "checkins", "autonomy": "balanced"], "rules": rules,
             "approvals": [["id": "ap1", "target": "t1", "title": "Tray fix", "tool": "Bash", "command": "git push origin tray",
                            "action": "publish", "state": "pending", "created": 1]],
             "checkin": ["schedule": ["id": "k1", "repeat": "daily", "time": "08:00", "enabled": true], "last": NSNull(), "running": NSNull()]],
        ]
        let history: [[String: Any]] = [
            ["role": "checkin", "label": "Morning check-in", "started": 1_790_000_000, "ended": 1_790_002_340, "needs_you": 1,
             "text": "Fixed the tray icon on its own branch. The push needs you."],
        ]
        try call("_scene", ["chats": [["chat": "sc1", "title": "Desktop app", "busy": false, "history_id": "h1", "project": "p1",
                                       "folder": "~/code/vision"],
                                      ["chat": "c9", "title": "Groceries", "busy": false, "history_id": "h9"]],
                            "history": ["sc1": history, "c9": []], "projects": projects])
        let app = launch()
        app.buttons["Chats"].tap()
        let scouts = app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", "Scouts")).firstMatch
        XCTAssertTrue(scouts.waitForExistence(timeout: 10))
        // The count arrives with the scout's threads, a moment after the row.
        XCTAssertTrue(app.buttons["Scouts, 1 needs you"].waitForExistence(timeout: 5), "the drawer counts what waits on you: \(scouts.label)")
        // The scout is right there in the drawer, its line leading with what needs you; a tap opens its page.
        let row = app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", "Atlas,")).firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        XCTAssertTrue(row.label.contains("1 needs you"), "its line leads with what needs you: \(row.label)")
        row.tap()

        XCTAssertTrue(text(app, containing: "Morning check-in").waitForExistence(timeout: 5), "a check-in is a card in its chat")
        XCTAssertTrue(text(app, containing: "The push needs you").exists)
        app.buttons["Threads"].tap()
        XCTAssertTrue(text(app, containing: "git push origin tray").waitForExistence(timeout: 5), "the request waits on its sheet")
        app.buttons["Edit"].tap()
        let field = app.alerts.textFields.firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        field.typeText(" --dry-run")
        app.alerts.buttons["Approve edited"].tap()
        let decided = try frame { $0["type"] as? String == "_decide" }
        XCTAssertEqual(decided?["approval"] as? String, "ap1")
        XCTAssertEqual(decided?["decision"] as? String, "approve")
        XCTAssertEqual(decided?["command"] as? String, "git push origin tray --dry-run")
        XCTAssertTrue(text(app, containing: "git push origin tray").waitForNonExistence(timeout: 5), "decided, the card goes")

        app.buttons["Project settings"].tap()
        let leash = app.buttons["Schedule"]
        XCTAssertTrue(leash.waitForExistence(timeout: 5))
        leash.tap()
        app.switches["Ask me before big changes"].firstMatch.switches.firstMatch.tap()
        let changed = try frame { $0["type"] as? String == "_post_projects" && ($0["settings"] as? [String: Any])?["leash"] as? String == "autonomous" }
        XCTAssertNotNil(changed, "the leash is saved as it's changed")
        app.buttons["Does it"].firstMatch.tap()
        let rule = try frame { $0["type"] as? String == "_post_projects" && ($0["settings"] as? [String: Any])?["rules"] != nil }
        XCTAssertEqual((rule?["settings"] as? [String: Any])?["rules"] as? [String: String], ["land": "does"])
        app.buttons["Status report now"].tap()
        XCTAssertNotNil(try frame { $0["type"] as? String == "_checkin" })
    }

    /// A project's chat with open threads shows one small pill saying so; a tap lists them, each with where
    /// it is, and the one waiting on you is answered from its own page.
    func testAProjectChatListsItsThreadsAndOneCanBeAnswered() throws {
        // Its own chat id: the app keeps the chat it last showed across launches, and a later test's scene
        // shouldn't open on this one.
        let projects: [[String: Any]] = [
            ["id": "p1", "title": "Flaky tests", "goal": "Fix the flaky tests", "folder": "~/code/vision", "chat": "tc1", "status": "active"],
        ]
        let threads: [[String: Any]] = [
            ["id": "t1", "title": "Find the flaky test", "objective": "Find which test is flaky", "state": "working",
             "activity": "running tests", "tools": 4, "created": 1, "updated": 2],
            ["id": "t2", "title": "Check the CI config", "objective": "Check the CI config", "state": "waiting_on_you",
             "question": "Should I pin the Python version?", "created": 1, "updated": 3,
             "runs": [["at": 3, "kind": "start", "status": "needs_input", "summary": "Read the workflow files", "findings": [],
                       "changes": [], "duration_s": 42]]],
        ]
        try call("_scene", ["chats": [["chat": "tc1", "title": "Flaky tests", "busy": false, "history_id": "h1", "project": "p1",
                                       "folder": "~/code/vision"]],
                            "history": ["tc1": []], "projects": projects, "threads": ["p1": threads]])
        let app = launch()
        let strip = app.buttons["Threads"]
        XCTAssertTrue(strip.waitForExistence(timeout: 10), "the project's chat shows its threads")
        XCTAssertEqual(strip.value as? String, "2 threads, 1 needs you")

        strip.tap()
        let working = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Find the flaky test")).firstMatch
        let waiting = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Check the CI config")).firstMatch
        XCTAssertTrue(working.waitForExistence(timeout: 5))
        XCTAssertTrue(working.label.contains("Running tests"), "a working thread says what it is doing")
        XCTAssertTrue(waiting.exists)
        XCTAssertTrue(waiting.label.contains("Should I pin the Python version?"), "a waiting thread shows its question")

        waiting.tap()
        let answer = app.textFields["Answer"]
        XCTAssertTrue(answer.waitForExistence(timeout: 5))
        XCTAssertTrue(text(app, containing: "Read the workflow files").exists, "its run log")
        answer.tap()
        answer.typeText("Yes, 3.12")
        app.buttons["Send"].tap()
        let told = try frame { $0["type"] as? String == "_thread_message" }
        XCTAssertEqual(told?["thread"] as? String, "t2")
        XCTAssertEqual(told?["text"] as? String, "Yes, 3.12")
        XCTAssertTrue(app.textFields["Message this thread"].waitForExistence(timeout: 5), "answered, it is working again")
    }

    /// A closed thread isn't reopened: a message to it starts a new thread carrying it on, and its page is what shows.
    func testAMessageToAClosedThreadOpensTheThreadThatCarriesItOn() throws {
        let projects: [[String: Any]] = [
            ["id": "p1", "title": "Docs", "goal": "Write the docs", "folder": "~/code/vision", "chat": "tc2", "status": "active"],
        ]
        let threads: [[String: Any]] = [
            ["id": "t1", "title": "Write the guide", "objective": "Write the guide", "state": "resolved", "created": 1, "updated": 2],
        ]
        try call("_scene", ["chats": [["chat": "tc2", "title": "Docs", "busy": false, "history_id": "h1", "project": "p1",
                                       "folder": "~/code/vision"]],
                            "history": ["tc2": []], "projects": projects, "threads": ["p1": threads]])
        let app = launch()
        let strip = app.buttons["Threads"]
        XCTAssertTrue(strip.waitForExistence(timeout: 10))
        strip.tap()
        let closed = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Write the guide")).firstMatch
        XCTAssertTrue(closed.waitForExistence(timeout: 5))
        closed.tap()

        let field = app.textFields["Message to carry it on"]
        XCTAssertTrue(field.waitForExistence(timeout: 5), "it is closed")
        field.tap()
        field.typeText("Add a section on projects")
        app.buttons["Send"].tap()
        let told = try frame { $0["type"] as? String == "_thread_message" }
        XCTAssertEqual(told?["thread"] as? String, "t1")
        XCTAssertTrue(app.textFields["Message this thread"].waitForExistence(timeout: 5), "the new thread's page, working")
        XCTAssertFalse(app.textFields["Message to carry it on"].exists, "not the closed one's")
    }

    /// The project screen: threads grouped by state, a finished one landed from its page, a report shown as a
    /// note in the chat rather than as the user's words, the memory tab, and pausing from its settings.
    func testTheProjectScreenGroupsThreadsLandsOneAndShowsReportsAsNotes() throws {
        let projects: [[String: Any]] = [
            ["id": "p1", "title": "Calendar", "goal": "Add a calendar", "folder": "~/code/cal", "chat": "pj1", "status": "active",
             "tokens_out": 12400],
        ]
        let threads: [[String: Any]] = [
            ["id": "t1", "title": "Storage", "objective": "Set up storage", "state": "ready_for_review", "branch": "vision/calendar/storage-t1",
             "last_result": ["status": "completed", "summary": "SQLite tables in place", "changes": ["db.py"], "checks": [], "findings": [],
                             "question": NSNull(), "decisions": ["Dates are stored in UTC"]],
             "created": 1, "updated": 5,
             "runs": [["at": 1, "kind": "start", "status": "completed", "summary": "SQLite tables in place", "findings": [], "changes": []],
                      ["at": 4, "kind": "continue", "status": "completed", "summary": "Added an index", "findings": [], "changes": []]]],
            ["id": "t2", "title": "Docs", "objective": "Write the docs", "state": "working", "activity": "reading the code",
             "created": 2, "updated": 6],
        ]
        try call("_scene", ["chats": [["chat": "pj1", "title": "Calendar", "busy": false, "history_id": "h1", "project": "p1",
                                       "folder": "~/code/cal"]],
                            "history": ["pj1": []], "projects": projects, "threads": ["p1": threads],
                            "memory": ["p1": ["- 2026-10-11: Dates are stored in UTC (thread Storage)"]]])
        let app = launch()
        let strip = app.buttons["Threads"]
        XCTAssertTrue(strip.waitForExistence(timeout: 10))

        // A report turn: a note, never a user bubble.
        try call("_push", ["type": "start", "chat": "pj1", "text": "[Thread report] Thread “Storage” is ready for review: SQLite tables in place\n<vision-report>[]</vision-report>",
                           "note": "Thread “Storage” is ready for review: SQLite tables in place", "speak": false])
        XCTAssertTrue(text(app, containing: "is ready for review: SQLite tables in place").waitForExistence(timeout: 5))
        XCTAssertFalse(text(app, containing: "<vision-report>").exists, "the report's data never shows")
        try call("_push", ["type": "done", "chat": "pj1", "text": "Storage is done.", "error": "", "busy": false])

        strip.tap()
        XCTAssertTrue(app.staticTexts["Ready for review"].waitForExistence(timeout: 5), "threads are grouped by state")
        XCTAssertTrue(app.staticTexts["Working"].exists)

        app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Storage")).firstMatch.tap()
        let decided = app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", "Decided")).firstMatch
        XCTAssertTrue(decided.waitForExistence(timeout: 5))
        decided.tap()   // folded until asked for
        XCTAssertTrue(text(app, containing: "Dates are stored in UTC").waitForExistence(timeout: 5), "its decisions")
        app.buttons["Land"].firstMatch.tap()
        app.sheets.buttons["Land"].firstMatch.tap()
        let landed = try frame { $0["type"] as? String == "_thread_land" }
        XCTAssertEqual(landed?["thread"] as? String, "t1")
        XCTAssertTrue(app.textFields["Message to carry it on"].waitForExistence(timeout: 5), "landed, it is closed")

        app.navigationBars.buttons["Calendar"].tap()   // back to the project
        let memory = app.buttons["project-tab-Memory"]
        XCTAssertTrue(memory.waitForExistence(timeout: 5))
        memory.tap()
        XCTAssertTrue(text(app, containing: "Dates are stored in UTC").waitForExistence(timeout: 5))
        XCTAssertTrue(text(app, containing: "thread Storage").exists, "who decided it, under the fact")

        app.buttons["Project settings"].tap()
        XCTAssertTrue(app.textFields["Goal"].waitForExistence(timeout: 5))
        XCTAssertEqual(app.textFields["Goal"].value as? String, "Add a calendar", "its goal, in its settings")
        app.buttons["Pause"].tap()
        XCTAssertNotNil(try frame { $0["type"] as? String == "_project_pause" })
        XCTAssertTrue(app.buttons["Resume"].waitForExistence(timeout: 5))
        app.buttons["Cancel"].tap()
        XCTAssertTrue(text(app, containing: "Paused").waitForExistence(timeout: 5))
    }

    /// The folder line on an empty chat opens the same picker. The chat picked there replaces the empty
    /// one, which is closed rather than left behind in the list.
    func testTheFolderOfAnEmptyChatCanBeChangedFromTheChatItself() throws {
        let home: [String: Any] = ["path": "/home/me", "label": "~", "parent": "/home",
                                   "folders": [["name": "code", "path": "/home/me/code"]],
                                   "recent": [["path": "/home/me/notes", "label": "~/notes"]], "more": 0]
        try call("_scene", ["chats": [["chat": "e1", "title": "", "busy": false, "history_id": "h1", "folder": "~"]],
                            "history": ["e1": []], "folders": ["": home]])
        let app = launch()
        let line = app.buttons["Working in ~"]
        XCTAssertTrue(line.waitForExistence(timeout: 10))
        line.tap()
        XCTAssertTrue(app.buttons["~/notes"].waitForExistence(timeout: 5))
        app.buttons["~/notes"].tap()
        let opened = try frame { $0["type"] as? String == "new" }
        XCTAssertEqual(opened?["workdir"] as? String, "/home/me/notes")
        let closed = try frame { $0["type"] as? String == "close" }
        XCTAssertEqual(closed?["chat"] as? String, "e1", "the empty chat it replaces is closed")
    }

    /// Plan mode is switched on and off from the "+" menu, follows what the machine then says, and shows as
    /// the composer's green glow (auto shows nothing).
    func testPlanModeIsSwitchedFromThePlusMenu() throws {
        let chat: [String: Any] = ["chat": "m1", "title": "Careful", "busy": false, "history_id": "h1", "mode": "auto"]
        try call("_scene", ["chats": [chat], "history": ["m1": []]])
        let app = launch()
        let plus = app.buttons["Add attachment"]
        let label = app.descendants(matching: .any)["planModeGlow"]
        XCTAssertTrue(plus.waitForExistence(timeout: 10))
        XCTAssertFalse(label.exists, "auto shows nothing")

        plus.tap()
        let row = app.buttons["Plan mode"]
        XCTAssertTrue(row.waitForExistence(timeout: 3))
        XCTAssertEqual(row.value as? String, "Off")
        row.tap()
        let sent = try frame { $0["type"] as? String == "mode" }
        XCTAssertEqual(sent?["mode"] as? String, "plan")
        XCTAssertEqual(sent?["chat"] as? String, "m1")

        try call("_push", chat.merging(["type": "chat", "mode": "plan"]) { _, new in new })
        XCTAssertTrue(label.waitForExistence(timeout: 5))
        plus.tap()
        XCTAssertTrue(row.waitForExistence(timeout: 3))
        XCTAssertEqual(row.value as? String, "On")
        row.tap()
        let back = try frame { $0["type"] as? String == "mode" && $0["mode"] as? String == "auto" }
        XCTAssertEqual(back?["chat"] as? String, "m1")
    }

    /// A Vision from before modes says nothing about one, and the menu has no plan row.
    func testNoPlanModeForAVisionThatDoesNotSayItsMode() throws {
        try call("_scene", ["chats": [["chat": "o1", "title": "Old", "busy": false, "history_id": "h1"]], "history": ["o1": []]])
        let app = launch()
        XCTAssertTrue(app.textViews["messageInput"].waitForExistence(timeout: 10))
        app.buttons["Add attachment"].tap()
        XCTAssertTrue(app.buttons["Files"].waitForExistence(timeout: 3))
        XCTAssertFalse(app.buttons["Plan mode"].exists)
    }

    /// A Vision older than the app needs (0 stands in for one the app has outgrown) says so in
    /// Settings, with both ways to update when it can't say which.
    func testAnOutOfDateVisionIsSaidInSettings() throws {
        try call("_scene", ["chats": [["chat": "v1", "title": "Old", "busy": false, "history_id": "h1"]],
                            "history": ["v1": []], "protocol": 0])
        let app = launch(["--open", "settings"])
        XCTAssertTrue(app.staticTexts["Vision on your computer is out of date"].waitForExistence(timeout: 10))
        XCTAssertTrue(text(app, containing: "run Vision's installer again").exists)
    }

    /// The other two: an app behind the computer's Vision, and a Vision that fits with an update out,
    /// saying what to run. One that fits and is current says nothing, and neither does one from before
    /// the number, which is protocol 1.
    func testSettingsSaysWhenTheAppIsBehindOrAnUpdateIsOut() throws {
        try call("_scene", ["chats": [], "protocol": 99])
        var app = launch(["--open", "settings"])
        XCTAssertTrue(app.staticTexts["This app is out of date"].waitForExistence(timeout: 10))
        app.terminate()

        try call("_scene", ["chats": [], "update": "git -C ~/vision pull"])
        app = launch(["--open", "settings"])
        XCTAssertTrue(app.staticTexts["An update is ready for your computer"].waitForExistence(timeout: 10))
        XCTAssertTrue(text(app, containing: "git -C ~/vision pull").exists)
        app.terminate()

        for scene: [String: Any] in [["chats": []], ["chats": [], "protocol": NSNull()]] {
            try call("_scene", scene)
            app = launch(["--open", "settings"])
            XCTAssertTrue(app.staticTexts["Settings"].waitForExistence(timeout: 10))
            Thread.sleep(forTimeInterval: 2)
            XCTAssertFalse(text(app, containing: "out of date").exists)
            XCTAssertFalse(text(app, containing: "update is ready").exists)
            app.terminate()
        }
    }

    /// An update that is out is said over the chat too, so it isn't only found by opening Settings.
    /// (The empty list stands in for a day on which it hasn't been said yet.)
    func testAnUpdateThatIsOutIsSaidOverTheChat() throws {
        try call("_scene", ["chats": [], "update": "vision upgrade"])
        let app = launch(["-stalenessToasted", "()"])
        XCTAssertTrue(text(app, containing: "An update is ready for your computer").waitForExistence(timeout: 10))
    }

    /// An update the machine finds while the app is connected shows up without reconnecting, and goes
    /// once the machine says it's gone.
    func testAnUpdateFoundWhileConnectedShowsInSettings() throws {
        try call("_scene", ["chats": []])
        let app = launch(["--open", "settings"])
        XCTAssertTrue(app.staticTexts["Settings"].waitForExistence(timeout: 10))
        try call("_push", ["type": "update", "update": "sh ~/install.sh"])
        XCTAssertTrue(app.staticTexts["An update is ready for your computer"].waitForExistence(timeout: 10))
        try call("_push", ["type": "update", "update": NSNull()])
        XCTAssertTrue(app.staticTexts["An update is ready for your computer"].waitForNonExistence(timeout: 10))
    }

    /// Settings shows what the Vision runs for speech in the server's own words, problems included.
    func testSettingsShowsTheServersSpeechStatus() throws {
        try call("_scene", ["chats": [], "status": [
            "rows": [["label": "Voice", "value": "phoenix · 1.7B 8-bit · 0.65 s per second of speech", "state": "ready"],
                     ["label": "Speech recognition", "value": "large-v3-turbo", "state": "ready"]],
            "problems": ["The voice model is not downloaded. Run: vision setup"],
        ]])
        let app = launchServerSettings()
        XCTAssertTrue(app.staticTexts["Speech recognition"].waitForExistence(timeout: 10))
        XCTAssertTrue(text(app, containing: "1.7B 8-bit").exists)
        XCTAssertTrue(text(app, containing: "large-v3-turbo").exists)
        XCTAssertTrue(text(app, containing: "Run: vision setup").exists)
    }

    /// Paired through Tailscale but at home with it off: the Tailscale address doesn't answer, so the app
    /// falls back to the Wi-Fi address the pairing code also carried, and connects there.
    func testWhenTheMainAddressIsDownTheWiFiAddressIsUsed() throws {
        try call("_scene", ["chats": []])
        // Its own address: a saved Vision keeps the Wi-Fi address it was paired with, and the
        // unreachable-address test must not find one.
        let code = #"{"url": "http://10.255.255.2:8765", "token": "fake", "local": "\#(base.absoluteString)"}"#
        let app = XCUIApplication()
        app.launchArguments = ["--pair", code, "--open", "settings"]
        app.launch()
        XCTAssertTrue(text(app, containing: "10.255.255.2").waitForExistence(timeout: 10))  // paired with the code
        XCTAssertTrue(element(app, label: "Connected").waitForExistence(timeout: 40))
        XCTAssertFalse(text(app, containing: "Nothing answered").exists)
    }

    /// A Vision from before the status report: no section, nothing broken.
    func testSettingsWithoutAStatusLeavesTheSectionOut() throws {
        try call("_scene", ["chats": []])
        let app = launch(["--open", "settings"])
        XCTAssertTrue(element(app, label: "Connected").waitForExistence(timeout: 10))
        XCTAssertFalse(app.staticTexts["Speech recognition"].waitForExistence(timeout: 3))
    }

    /// Settings lists the voices made on the machine. One can be heard before it is chosen, which changes
    /// nothing; a tap on it saves it there, and Settings shows what the machine saved.
    func testAVoiceIsHeardThenPickedInSettings() throws {
        try call("_scene", ["chats": [], "voices": [
            "voice": "jarvis",
            "voices": [["id": "jarvis", "note": "designed", "sample": true], ["id": "narrator", "note": "cloned", "sample": true]],
        ]])
        let app = launchServerSettings()
        let row = { (name: String) in
            app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Voice' AND label CONTAINS %@", name)).firstMatch
        }
        let said = { (key: String) in (try self.call("voice") as? [String: Any])?[key] as? String }
        XCTAssertTrue(row("Jarvis").waitForExistence(timeout: 10))
        row("Jarvis").tap()

        XCTAssertTrue(app.buttons["Hear Narrator"].waitForExistence(timeout: 5))
        app.buttons["Hear Narrator"].tap()
        XCTAssertTrue(app.buttons["Stop Narrator"].waitForExistence(timeout: 5), "its sample is playing")
        XCTAssertEqual(try said("sampled"), "narrator")
        XCTAssertEqual(try said("voice"), "jarvis", "hearing a voice doesn't choose it")
        app.buttons["Stop Narrator"].tap()
        XCTAssertTrue(app.buttons["Hear Narrator"].waitForExistence(timeout: 3))

        app.buttons["Narrator"].tap()
        let chosen = app.buttons.matching(NSPredicate(format: "label == 'Narrator' AND isSelected == true")).firstMatch
        XCTAssertTrue(chosen.waitForExistence(timeout: 5))
        XCTAssertEqual(try said("voice"), "narrator")
        app.navigationBars.buttons.firstMatch.tap()
        XCTAssertTrue(row("Narrator").waitForExistence(timeout: 5), "Settings shows the voice in use")
    }

    /// With the server reporting its voice, that row opens the list of voices, its words under the name
    /// picked there, and the speed under it in the same section.
    func testTheServersVoiceRowPicksTheVoice() throws {
        try call("_scene", ["chats": [], "status": [
            "rows": [["label": "Voice", "value": "jarvis · on the GPU", "state": "ready"]], "problems": [],
        ], "voices": [
            "voice": "jarvis",
            "voices": [["id": "jarvis", "note": "designed"], ["id": "narrator", "note": "cloned"]],
        ]])
        let app = launchServerSettings()
        let row = app.buttons.matching(NSPredicate(format: "label CONTAINS 'Jarvis' AND label CONTAINS 'on the GPU'")).firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["Speaking speed"].exists)
        row.tap()
        XCTAssertTrue(app.buttons["Narrator"].waitForExistence(timeout: 3))
        app.buttons["Narrator"].tap()
        XCTAssertTrue(app.buttons.matching(NSPredicate(format: "label == 'Narrator' AND isSelected == true")).firstMatch.waitForExistence(timeout: 5))
        XCTAssertEqual((try call("voice") as? [String: Any])?["voice"] as? String, "narrator")
        app.navigationBars.buttons.firstMatch.tap()
        XCTAssertTrue(app.buttons.matching(NSPredicate(format: "label CONTAINS 'Narrator' AND label CONTAINS 'on the GPU'")).firstMatch
            .waitForExistence(timeout: 5))
    }

    /// No chat open, then one appears (from a terminal, a scheduled run) and asks a question: the form can be
    /// answered, and the answer goes to that chat.
    func testAQuestionFromAChatThatAppearsWithNoneOpenCanBeAnswered() throws {
        try call("_scene", ["chats": []])
        let app = launch()
        XCTAssertTrue(app.textViews["messageInput"].waitForExistence(timeout: 10))
        Thread.sleep(forTimeInterval: 2)   // connected, with nothing open
        let questions: [[String: Any]] = [["question": "Which colour?", "header": "Colour", "multiSelect": false,
                                           "options": [["label": "Red", "description": "Warm"], ["label": "Blue", "description": "Cool"]]]]
        try call("_push", ["type": "chat", "chat": "q1", "source": "terminal", "title": "Asking", "busy": true,
                           "waiting": true, "questions": questions, "user_text": "ask me", "partial": "", "history_id": "h"])
        let blue = app.buttons.matching(NSPredicate(format: "label CONTAINS 'Blue'")).firstMatch
        XCTAssertTrue(blue.waitForExistence(timeout: 8), "the form should open")
        blue.tap()
        app.buttons["Send"].tap()
        let answer = try frame { $0["type"] as? String == "answer" }
        XCTAssertEqual(answer?["chat"] as? String, "q1")
        XCTAssertEqual((answer?["answers"] as? [String: String])?["Which colour?"], "Blue")
        XCTAssertTrue(text(app, containing: "Which colour?").waitForNonExistence(timeout: 5), "the form should close")
    }

    /// Send now on a queued message sends it into the running reply, once.
    func testSendNowOnAQueuedMessage() throws {
        try call("_scene", ["chats": [busyChat(queued: ["Hi there"])]])
        let app = launch()
        let bubble = element(app, label: "Queued: Hi there")
        XCTAssertTrue(bubble.waitForExistence(timeout: 10))
        bubble.press(forDuration: 1.0)
        app.buttons["Send now"].tap()
        let steer = try frame { $0["type"] as? String == "steer" }
        XCTAssertEqual(steer?["text"] as? String, "Hi there")
        Thread.sleep(forTimeInterval: 1)
        XCTAssertEqual(try sent("steer").count, 1)
    }

    /// Remove on a queued message with a file then a photo names them in the order they were sent, which is
    /// how the machine finds it in its queue.
    func testRemovingAQueuedMessageWithAFileAndAPhoto() throws {
        try call("_scene", ["chats": [busyChat(queued: [Self.withAttachments])]])
        let app = launch()
        let bubble = element(app, label: "Queued: Look at this")
        XCTAssertTrue(bubble.waitForExistence(timeout: 10))
        bubble.press(forDuration: 1.0)
        app.buttons["Remove"].tap()
        let unqueue = try frame { $0["type"] as? String == "unqueue" }
        XCTAssertEqual(unqueue?["text"] as? String, "Look at this")
        XCTAssertEqual(unqueue?["images"] as? [String], [Self.file, Self.photo])
        XCTAssertNil(unqueue?["keep"])
        XCTAssertTrue(bubble.waitForNonExistence(timeout: 3))
    }

    /// Edit on a queued message brings its text and its photo and file back into the composer.
    func testEditingAQueuedMessageBringsItsAttachmentsBack() throws {
        try call("_scene", ["chats": [busyChat(queued: [Self.withAttachments])]])
        let app = launch()
        let bubble = element(app, label: "Queued: Look at this")
        XCTAssertTrue(bubble.waitForExistence(timeout: 10))
        bubble.press(forDuration: 1.0)
        app.buttons["Edit"].tap()
        let unqueue = try frame { $0["type"] as? String == "unqueue" }
        XCTAssertEqual(unqueue?["keep"] as? Bool, true)
        XCTAssertEqual(app.textViews["messageInput"].value as? String, "Look at this")
        XCTAssertTrue(app.buttons["Remove photo"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["Remove file"].exists)
    }

    /// A reply's run of calls opens in a sheet, as the Claude app shows them: every call on its own line, a failed
    /// one marked, and a tap on one shows what it ran and what came back. A call from a machine that sends no input
    /// still has a page, its detail standing in. `SHOT_DIR` (TEST_RUNNER_SHOT_DIR) keeps a picture of each screen.
    func testAReplysCallsOpenInASheetAndEachCallHasItsPage() throws {
        let text1 = "I'll check the row class's fields, fix the test, and rerun the suite."
        let calls: [[String: Any]] = [
            ["id": "a", "name": "Bash", "detail": "Check MenuRow's field names", "done": true, "is_error": false, "at": text1.count,
             "output": "@dataclass\nclass MenuRow:\n    label: str\n    hint: str",
             "input": [["description", "Check MenuRow's field names"], ["command", "cd ~/vision && sed -n '1235,1242p' vision/ui.py"]]],
            ["id": "b", "name": "Bash", "detail": "Fix the menu test field name, rerun it, then run the full suite", "done": true,
             "is_error": true, "at": text1.count, "output": "FAILED (failures=1, skipped=1)",
             "input": [["description", "Fix the menu test field name, rerun it, then run the full suite"],
                       ["command", "sed -i 's/r.value/r.label/g' tests/test_menu.py && .venv/bin/python -m unittest discover -s tests 2>&1 | tail -4"]]],
            ["id": "c", "name": "Read", "detail": "~/vision/vision/ui.py", "done": true, "is_error": false, "at": text1.count,
             "output": "1235  @dataclass"],
        ]
        try call("_scene", ["chats": [["chat": "k1", "title": "Calls", "busy": false, "history_id": "h1"]],
                            "history": ["k1": [["role": "user", "text": "Fix the menu test"],
                                               ["role": "assistant", "text": text1 + "\n\nThe pacer test only fails in the full suite.",
                                                "tools": calls]]]])
        let app = launch()
        if !text(app, containing: "The pacer test").waitForExistence(timeout: 5) {
            app.buttons["Chats"].tap()
            let chat = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Calls'")).firstMatch
            XCTAssertTrue(chat.waitForExistence(timeout: 5))
            chat.tap()
        }
        let group = element(app, label: "Ran 2 commands, read 1 file, 1 failed")
        let folded = group.waitForExistence(timeout: 10)
        if !folded { print("TREE>>>\n" + app.debugDescription + "\n<<<TREE") }
        XCTAssertTrue(folded, "the run folds to its total")
        group.tap()

        let first = text(app, containing: "Check MenuRow's field names")
        XCTAssertTrue(first.waitForExistence(timeout: 5), "the sheet lists each call")
        XCTAssertTrue(text(app, containing: "Fix the menu test field name").exists)
        XCTAssertTrue(text(app, containing: "~/vision/vision/ui.py").exists)
        Thread.sleep(forTimeInterval: 0.8)
        shot(app, "tool-sheet")

        text(app, containing: "Fix the menu test field name").tap()
        XCTAssertTrue(text(app, containing: "Command").waitForExistence(timeout: 5), "the call's page shows its input")
        XCTAssertTrue(text(app, containing: "unittest discover").exists, "the command, whole")
        XCTAssertTrue(text(app, containing: "FAILED (failures=1").exists, "and what came back")
        Thread.sleep(forTimeInterval: 0.8)
        shot(app, "tool-page")

        // The page's own bar: the chat's bar is still there behind the sheet, and its Chats button comes first.
        app.navigationBars["Bash"].buttons.firstMatch.tap()
        XCTAssertTrue(first.waitForExistence(timeout: 5), "back to the list")
        text(app, containing: "~/vision/vision/ui.py").tap()
        XCTAssertTrue(text(app, containing: "Detail").waitForExistence(timeout: 5), "no input sent: the detail stands in")
        XCTAssertTrue(text(app, containing: "1235  @dataclass").exists)
    }

    private func shot(_ app: XCUIApplication, _ name: String) {
        let png = app.screenshot().pngRepresentation
        let attachment = XCTAttachment(data: png, uniformTypeIdentifier: "public.png")
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
        if let dir = ProcessInfo.processInfo.environment["SHOT_DIR"] {
            try? png.write(to: URL(fileURLWithPath: dir).appendingPathComponent("\(name).png"))
        }
    }

    /// Threads working on the machine put a card on the Lock Screen: the counts, and each thread by name with where
    /// it stands. Pulled down over the Home Screen, the card is there to read with the app in the background.
    func testWorkingThreadsPutACardOnTheLockScreen() throws {
        try call("_scene", ["chats": []])
        let app = launch()
        XCTAssertTrue(app.textViews["messageInput"].waitForExistence(timeout: 10))
        Thread.sleep(forTimeInterval: 2)   // connected
        try call("_push", ["type": "threads_live", "state": [
            "rows": [["title": "Storage", "state": "waiting_on_you"], ["title": "Write the guide", "state": "working"],
                     ["title": "Login page", "state": "ready_for_review"]],
            "working": 1, "needsYou": 1, "ready": 1,
        ]])
        Thread.sleep(forTimeInterval: 2)
        XCUIDevice.shared.press(.home)
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        let top = springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.2, dy: 0.001))
        top.press(forDuration: 0.1, thenDragTo: springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.2, dy: 0.7)))
        let card = springboard.staticTexts.matching(NSPredicate(format: "label CONTAINS 'Storage'")).firstMatch
        XCTAssertTrue(card.waitForExistence(timeout: 10), "the card names the thread waiting on you")
        XCTAssertTrue(springboard.staticTexts.matching(NSPredicate(format: "label CONTAINS 'Needs you'")).firstMatch.exists)
        XCTAssertTrue(springboard.staticTexts.matching(NSPredicate(format: "label BEGINSWITH '1 working · 1 needs you'")).firstMatch.exists)
        let png = XCUIScreen.main.screenshot().pngRepresentation
        if let dir = ProcessInfo.processInfo.environment["SHOT_DIR"] {
            try? png.write(to: URL(fileURLWithPath: dir).appendingPathComponent("threads-card.png"))
        }

        // None left running: the card ends (it lingers as "All done" on the Lock Screen for a while).
        springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.95))
            .press(forDuration: 0.1, thenDragTo: springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.2)))
        app.activate()
        Thread.sleep(forTimeInterval: 2)
        try call("_push", ["type": "threads_live", "state": [
            "rows": [["title": "Login page", "state": "ready_for_review"]], "working": 0, "needsYou": 0, "ready": 1,
        ]])
        Thread.sleep(forTimeInterval: 2)
        XCUIDevice.shared.press(.home)
        top.press(forDuration: 0.1, thenDragTo: springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.2, dy: 0.7)))
        XCTAssertTrue(springboard.staticTexts.matching(NSPredicate(format: "label CONTAINS 'All done'")).firstMatch.waitForExistence(timeout: 10))
    }

    /// Away while one reply ended and the next began: back in the app, each reply sits under its own message.
    func testComingBackAfterAReplyEndedAndAnotherBegan() throws {
        try call("_scene", ["chats": [["chat": "s1", "title": "Two", "busy": true, "user_text": "First question",
                                       "partial": "First answer so far", "history_id": "h1"]],
                            "history": ["s1": []]])
        let app = launch()
        XCTAssertTrue(text(app, containing: "First answer so far").waitForExistence(timeout: 10))
        XCUIDevice.shared.press(.home)
        Thread.sleep(forTimeInterval: 3)
        try call("_scene", ["chats": [["chat": "s1", "title": "Two", "busy": true, "user_text": "Second question",
                                       "partial": "Second answer so far", "history_id": "h2"]],
                            "history": ["s1": [["role": "user", "text": "First question"],
                                               ["role": "assistant", "text": "First answer, complete."]]]])
        app.activate()
        XCTAssertTrue(text(app, containing: "Second question").waitForExistence(timeout: 10), "the new turn's message")
        XCTAssertTrue(text(app, containing: "Second answer so far").waitForExistence(timeout: 5))
        let finished = text(app, containing: "First answer, complete.").waitForExistence(timeout: 5)
        if !finished { print("TREE>>>\n" + app.debugDescription + "\n<<<TREE") }
        XCTAssertTrue(finished, "the finished reply")
        XCTAssertFalse(text(app, containing: "First answer so far").exists, "the old live bubble is gone")
    }

    /// A reply that runs on under the composer offers the jump arrow. A tap brings the latest line up and the view
    /// rides the rest of the reply down; a drag back up lets go, and the arrow comes back.
    func testTheJumpArrowFollowsAReplyThatRunsUnderTheComposer() throws {
        try call("_scene", ["chats": [["chat": "j1", "title": "Long", "busy": false, "history_id": "h1"]],
                            "history": ["j1": [["role": "user", "text": "Hi"], ["role": "assistant", "text": "Hello."]]]])
        let app = launch()
        XCTAssertTrue(text(app, containing: "Hello.").waitForExistence(timeout: 10))
        // A fresh simulator asks about notifications, and the prompt would sit over the transcript.
        let dontAllow = XCUIApplication(bundleIdentifier: "com.apple.springboard").buttons["Don’t Allow"]
        if dontAllow.waitForExistence(timeout: 3) { dontAllow.tap() }
        let arrow = app.buttons["Jump to latest"]
        let line = "P is the class of problems a computer can solve quickly, and NP the class whose answers can be checked quickly. "
        func stream(_ count: Int, from start: Int) throws {
            for i in start..<(start + count) {
                try call("_push", ["type": "delta", "chat": "j1", "text": "Step \(i): " + line + (i % 3 == 2 ? "\n\n" : "")])
                Thread.sleep(forTimeInterval: 0.12)
            }
        }

        try call("_push", ["type": "start", "chat": "j1", "text": "Explain P vs NP"])
        try stream(4, from: 0)
        shot(app, "jump-1-short")
        XCTAssertFalse(arrow.exists, "a reply that fits on screen offers no arrow")

        try stream(14, from: 4)
        shot(app, "jump-2-under-composer")
        XCTAssertTrue(arrow.waitForExistence(timeout: 3), "the reply ran on under the composer")
        XCTAssertTrue(arrow.isHittable)

        arrow.tap()
        Thread.sleep(forTimeInterval: 0.8)
        try stream(6, from: 18)
        shot(app, "jump-3-following")
        XCTAssertFalse(arrow.exists, "following the reply, the latest line stays in sight")
        XCTAssertTrue(text(app, containing: "Step 23:").isHittable, "the newest line is on screen")

        app.swipeDown()
        Thread.sleep(forTimeInterval: 0.8)
        try stream(2, from: 24)
        shot(app, "jump-4-let-go")
        XCTAssertTrue(arrow.waitForExistence(timeout: 3), "a drag up lets go, and the arrow is back")
    }

    /// How long switching into a heavy chat takes, from the drawer tap until its whole transcript is drawn and the
    /// app is idle. A benchmark, not a check: it plays a scene saved from a real chat, so it runs only when given
    /// one (TEST_RUNNER_VISION_PERF_SCENE=<scene.json>: two chats, the heavy one second, and a `marker`, words from the
    /// heavy chat's opening rows that appear nowhere else).
    func testSwitchingIntoAHeavyChat() throws {
        guard let path = ProcessInfo.processInfo.environment["VISION_PERF_SCENE"],
              let data = FileManager.default.contents(atPath: path),
              let scene = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let chats = scene["chats"] as? [[String: Any]], chats.count == 2,
              let light = chats[0]["title"] as? String, let heavy = chats[1]["title"] as? String,
              let marker = scene["marker"] as? String else {
            throw XCTSkip("VISION_PERF_SCENE isn't set to a saved scene")
        }
        try call("_scene", scene)
        let app = launch()
        XCTAssertTrue(app.textViews["messageInput"].waitForExistence(timeout: 10))
        let dontAllow = XCUIApplication(bundleIdentifier: "com.apple.springboard").buttons["Don’t Allow"]
        if dontAllow.waitForExistence(timeout: 3) { dontAllow.tap() }
        func open(_ title: String) {
            app.buttons["Chats"].firstMatch.tap()
            let row = text(app, containing: String(title.prefix(20)))
            XCTAssertTrue(row.waitForExistence(timeout: 5), title)
            row.tap()
        }
        // The opening line is drawn only once the held-back part of the transcript has landed: the end of the switch.
        let opening = text(app, containing: marker)
        let options = XCTMeasureOptions()
        options.invocationOptions = [.manuallyStart, .manuallyStop]
        options.iterationCount = 6
        let signpost = { (name: String) in
            XCTOSSignpostMetric(subsystem: "VisionRemote", category: "perf", name: name)
        }
        measure(metrics: [XCTClockMetric(), signpost("Open chat"), signpost("Land history")], options: options) {
            open(light)
            XCTAssertTrue(opening.waitForNonExistence(timeout: 10))
            startMeasuring()
            open(heavy)
            XCTAssertTrue(opening.waitForExistence(timeout: 20))
            stopMeasuring()
        }
        // The chats drawer pulled out over the heavy chat and pushed back, for the profiler to watch the slide.
        guard ProcessInfo.processInfo.environment["VISION_PERF_SWIPES"] != nil else { return }
        for _ in 0..<8 {
            let start = app.coordinate(withNormalizedOffset: CGVector(dx: 0.2, dy: 0.45))
            start.press(forDuration: 0.05, thenDragTo: start.withOffset(CGVector(dx: 260, dy: 0)), withVelocity: 300, thenHoldForDuration: 0)
            let back = app.coordinate(withNormalizedOffset: CGVector(dx: 0.9, dy: 0.45))
            back.press(forDuration: 0.05, thenDragTo: back.withOffset(CGVector(dx: -260, dy: 0)), withVelocity: 300, thenHoldForDuration: 0)
        }
    }

}

private extension XCUIElement {
    func clearText() {
        guard let text = value as? String, !text.isEmpty else { return }
        tap()
        typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: text.count))
    }

}
