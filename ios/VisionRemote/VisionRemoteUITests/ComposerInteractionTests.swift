import XCTest

final class ComposerInteractionTests: XCTestCase {
    func testComposerPaddingOpensKeyboardAndRepeatedTapsKeepItOpen() {
        let app = XCUIApplication()
        app.launchArguments = ["-serverURL", "http://127.0.0.1:9"]
        app.launchEnvironment["VISION_TOKEN"] = "ui-test-placeholder"
        app.launch()

        let input = app.textViews["messageInput"]
        XCTAssertTrue(input.waitForExistence(timeout: 10))
        let keyboard = app.keyboards.firstMatch

        // These points lie in the glass card's padding, outside the UITextView.
        let paddingOffsets = [CGVector(dx: -8, dy: 10), CGVector(dx: 20, dy: -6)]
        for offset in paddingOffsets {
            input.coordinate(withNormalizedOffset: .zero).withOffset(offset).tap()
            XCTAssertTrue(keyboard.waitForExistence(timeout: 3), "Padding tap should begin typing")

            input.coordinate(withNormalizedOffset: .zero).withOffset(offset).tap()
            input.tap()
            input.tap()
            XCTAssertTrue(keyboard.exists, "Repeated composer taps should preserve the keyboard")
            input.typeText("Still typing")
            XCTAssertEqual(input.value as? String, "Still typing")
            input.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: 12))

            app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.25)).tap()
            XCTAssertTrue(keyboard.waitForNonExistence(timeout: 3), "Tapping the chat should dismiss typing")
        }
    }
}
