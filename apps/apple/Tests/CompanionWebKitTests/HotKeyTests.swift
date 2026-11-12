import XCTest
import AppKit
import CompanionContract
@testable import CognisCompanion

@MainActor
final class HotKeyTests: XCTestCase {
    func testRecorderCancelClearAndModifiedKeyThroughResponder() throws {
        _ = NSApplication.shared
        var saved: [HotKeyPreference?] = []
        var recording: [Bool] = []
        let recorder = ShortcutRecorder(value: .default, changed: { saved.append($0) },
                                        recording: { recording.append($0) })
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 480, height: 100),
            styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = recorder
        defer { window.orderOut(nil) }
        func press(_ code: UInt16, _ flags: NSEvent.ModifierFlags = []) throws {
            let event = try XCTUnwrap(NSEvent.keyEvent(with: .keyDown, location: .zero,
                modifierFlags: flags, timestamp: 0, windowNumber: window.windowNumber, context: nil,
                characters: "", charactersIgnoringModifiers: "", isARepeat: false, keyCode: code))
            XCTAssertTrue(recorder.performKeyEquivalent(with: event))
        }
        recorder.performClick(nil)
        try press(8) // Plain C cannot replace a global shortcut.
        XCTAssertTrue(saved.isEmpty)
        try press(53)
        XCTAssertTrue(saved.isEmpty)
        XCTAssertEqual(recording, [true, false])
        recorder.performClick(nil)
        try press(49, [.control, .option])
        XCTAssertEqual(saved.last!, HotKeyPreference(keyCode: 49, modifiers: 6144))
        XCTAssertTrue(recorder.title.contains("Space"))
        recorder.performClick(nil)
        try press(51)
        XCTAssertNil(saved.last!)
        XCTAssertEqual(recorder.title, "Disabled")
    }

    func testCarbonRegistrationConflictIsVisibleAndUnregistrationReleasesShortcut() {
        _ = NSApplication.shared
        let shortcut = HotKeyPreference(keyCode: 90, modifiers: 6400) // F20, not the user's default.
        let first = GlobalHotKey(action: {}), second = GlobalHotKey(action: {})
        defer { first.unregister(); second.unregister() }
        first.register(shortcut)
        XCTAssertTrue(first.status.hasPrefix("Registered"), first.status)
        second.register(shortcut)
        XCTAssertTrue(second.status.contains("unavailable"), second.status)
        first.unregister()
        second.register(shortcut)
        XCTAssertTrue(second.status.hasPrefix("Registered"), second.status)
    }
}
