import XCTest

/// A slide that starts on a button is a press, not a pull on the chats drawer; one from open chat still pulls.
final class DrawerSwipeTests: XCTestCase {
    private var app: XCUIApplication!

    override func setUp() {
        app = XCUIApplication()
        app.launchArguments = ["-serverURL", "http://127.0.0.1:9"]
        app.launchEnvironment["VISION_TOKEN"] = "ui-test-placeholder"
        app.launch()
        XCTAssertTrue(app.textViews["messageInput"].waitForExistence(timeout: 10))
    }

    /// The drawer waits at x = 0 under the chat even when shut (it fades in place), so its header is always
    /// there; it only takes touches once the drawer is out.
    private var drawerShowing: Bool {
        let header = app.staticTexts.matching(NSPredicate(format: "label == 'Vision'")).firstMatch
        return header.exists && header.isHittable
    }

    private func slideRight(from element: XCUIElement) {
        let start = element.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        start.press(forDuration: 0.15, thenDragTo: start.withOffset(CGVector(dx: 260, dy: 0)))
        sleep(1)
    }

    func testSlidingFromButtonsLeavesTheDrawerShut() {
        for label in ["Add attachment", "Chats", "Open on computer"] {
            let button = app.buttons[label].firstMatch
            XCTAssertTrue(button.waitForExistence(timeout: 5), label)
            slideRight(from: button)
            XCTAssertFalse(drawerShowing, "a slide from \(label) opened the drawer")
        }
    }

    /// While typing, a drag along the text (moving a selection handle to take in less) is the text's, not the drawer's.
    func testSlidingAlongTextBeingTypedLeavesTheDrawerShut() {
        let input = app.textViews["messageInput"]
        input.tap()
        input.typeText("select some of this text")
        slideRight(from: input)
        XCTAssertFalse(drawerShowing, "a slide along the text being typed opened the drawer")
        XCTAssertEqual(input.value as? String, "select some of this text")
    }

    /// With some of it selected, a drag from just under the text (where the end handle's knob hangs) moves the
    /// selection; the drawer stays shut.
    func testDraggingASelectionHandleLeavesTheDrawerShut() {
        let input = app.textViews["messageInput"]
        input.tap()
        input.typeText("select some of this text")
        input.doubleTap()   // a word selected: the handles are up
        sleep(1)
        let knob = input.coordinate(withNormalizedOffset: CGVector(dx: 0.3, dy: 1.0)).withOffset(CGVector(dx: 0, dy: 12))
        knob.press(forDuration: 0.15, thenDragTo: knob.withOffset(CGVector(dx: 160, dy: 0)))
        sleep(1)
        XCTAssertFalse(drawerShowing, "dragging a selection handle opened the drawer")
    }

    func testSlidingFromTheChatStillOpensTheDrawer() {
        let start = app.coordinate(withNormalizedOffset: CGVector(dx: 0.2, dy: 0.45))
        start.press(forDuration: 0.05, thenDragTo: start.withOffset(CGVector(dx: 260, dy: 0)))
        sleep(1)
        XCTAssertTrue(drawerShowing)
    }
}
