import AppKit
import XCTest
@testable import CognisCompanion

@MainActor
final class DesktopRailPlacementTests: XCTestCase {
    func testDragAcrossDisplaysAndRestoreAfterDisconnect() throws {
        let suite = "cz.fpy.cognis.rail-placement.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let primary = DesktopRailPlacement.Display(id: "primary", legacyID: "1",
            frame: NSRect(x: 0, y: 0, width: 1920, height: 1080),
            visibleFrame: NSRect(x: 0, y: 0, width: 1920, height: 1056))
        let secondary = DesktopRailPlacement.Display(id: "secondary", legacyID: "2",
            frame: NSRect(x: -1440, y: -200, width: 1440, height: 900),
            visibleFrame: NSRect(x: -1440, y: -200, width: 1440, height: 876))
        let size = NSSize(width: 64, height: 200)
        let placement = DesktopRailPlacement(defaults: defaults, key: suite)
        placement.select(primary)
        placement.drag(pointer: NSPoint(x: -300, y: 400), y: 300,
            displays: [primary, secondary], size: size)
        XCTAssertEqual(placement.selected(in: [primary, secondary])?.id, "secondary")
        XCTAssertEqual(placement.frame(on: secondary, size: size),
            NSRect(x: -64, y: 300, width: 64, height: 200))

        let restored = DesktopRailPlacement(defaults: defaults, key: suite)
        XCTAssertEqual(restored.frame(on: secondary, size: size).minY, 300)
        XCTAssertEqual(restored.selected(in: [primary])?.id, "primary")
        restored.move(y: 350, on: primary, size: size)
        XCTAssertEqual(restored.selected(in: [primary, secondary])?.id, "secondary")

        defaults.set("left", forKey: suite + "-side")
        let above = DesktopRailPlacement.Display(id: "above", legacyID: "3",
            frame: NSRect(x: 0, y: 1080, width: 1280, height: 800),
            visibleFrame: NSRect(x: 0, y: 1080, width: 1280, height: 776))
        restored.drag(pointer: NSPoint(x: 500, y: 1800), y: 1800,
            displays: [primary, above], size: size)
        XCTAssertEqual(restored.selected(in: [primary, above])?.id, "above")
        XCTAssertEqual(restored.frame(on: above, size: size),
            NSRect(x: 0, y: 1648, width: 64, height: 200))
    }

    func testSettingsDisplaySelectionPersistsUsingRealControl() async throws {
        let suite = "cz.fpy.cognis.rail-settings.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let displays = NSScreen.screens.map(DesktopRailPlacement.display)
        let target = try XCTUnwrap(displays.last)
        let placement = DesktopRailPlacement(defaults: defaults, key: suite)
        // Exercise the old installed-client preference before selecting a UUID.
        defaults.set(target.legacyID, forKey: suite + "-display")
        XCTAssertEqual(placement.selected(in: displays)?.id, target.id)
        let settings = NativeSettingsWindow(railVisible: true, left: false,
            displays: NSScreen.screens.enumerated().map { index, screen in
                (id: displays[index].id, name: screen.localizedName)
            }, selectedDisplay: displays.first?.id,
            displayChanged: { id in placement.select(displays.first { $0.id == id }!) },
            previews: true, rail: {}, side: { _ in }, tuck: {}, preview: {},
            notifications: {}, startup: NSMenu(), shortcut: nil, shortcutStatus: "Disabled",
            shortcutChanged: { _ in }, shortcutRecording: { _ in })
        defer { settings.close() }
        let stack = try XCTUnwrap(settings.window?.contentView as? NSStackView)
        let picker = try XCTUnwrap(stack.arrangedSubviews.compactMap { $0 as? NSStackView }
            .flatMap(\.arrangedSubviews).compactMap { $0 as? NSPopUpButton }.first)
        picker.selectItem(at: displays.count - 1)
        XCTAssertTrue(picker.sendAction(picker.action, to: picker.target))
        XCTAssertEqual(DesktopRailPlacement(defaults: defaults, key: suite).selected(in: displays)?.id, target.id)
        if let output = ProcessInfo.processInfo.environment["COGNIS_RAIL_SETTINGS_CAPTURE"] {
            let window = try XCTUnwrap(settings.window)
            window.orderFrontRegardless()
            window.contentView?.layoutSubtreeIfNeeded()
            let view = try XCTUnwrap(window.contentView)
            XCTAssertTrue(view.bounds.contains(picker.convert(picker.bounds, to: view)))
            try await Task.sleep(for: .milliseconds(250))
            let capture = Process()
            capture.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
            capture.arguments = ["-x", "-l", String(window.windowNumber), output]
            try capture.run()
            capture.waitUntilExit()
            XCTAssertEqual(capture.terminationStatus, 0)
        }
    }

    func testRailWindowMovesToAnotherConnectedPhysicalDisplay() throws {
        guard NSScreen.screens.count >= 2 else { throw XCTSkip("Requires two connected displays") }
        let suite = "cz.fpy.cognis.rail-monitors.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let screens = NSScreen.screens
        let displays = screens.map(DesktopRailPlacement.display)
        let placement = DesktopRailPlacement(defaults: defaults, key: suite)
        let size = NSSize(width: 64, height: 200)
        let window = DesktopPanel(contentRect: placement.frame(on: displays[0], size: size),
            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        defer { window.close() }
        let destination = displays[1]
        placement.drag(pointer: NSPoint(x: destination.frame.midX, y: destination.frame.midY),
            y: destination.visibleFrame.midY, displays: displays, size: size)
        let selected = try XCTUnwrap(placement.selected(in: displays))
        window.setFrame(placement.frame(on: selected, size: size), display: true)
        XCTAssertEqual(window.screen, screens[1])
        XCTAssertEqual(window.frame.maxX, destination.visibleFrame.maxX)
        XCTAssertTrue(destination.visibleFrame.contains(window.frame))
    }
}
