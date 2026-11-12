import XCTest
import AppKit
import WebKit
import CompanionContract
@testable import CognisCompanion

@MainActor
// Requires an unlocked macOS GUI session. Real key-window ownership is part of
// acceptance; a locked/headless runner must not bypass or skip that assertion.
final class DiagnosticSaveTests: XCTestCase {
    private let filename = "cognis-chat-diagnostic-conv_native.json"
    private let json = #"{"schema":"cognis-chat-client-diagnostic-v1","scope":{"conversation_id":"conv_native"}}"#

    private func makeHost(
        surface: String = "main",
        destination: @escaping @MainActor (NSWindow, String, @escaping @MainActor (URL?) -> Void) -> Void
    ) async throws -> (BundleWebView, NSWindow, URL) {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.regular)
        NSApp.unhide(nil)
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("cognis-diagnostic-save-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        try """
            <!doctype html><html><head></head><body><script>
            const __sveltekit_diagnostic = {app:{start:()=>{window.diagnosticReady=true}}};
            __sveltekit_diagnostic.app.start();
            </script></body></html>
            """.write(to: root.appendingPathComponent("index.html"), atomically: true, encoding: .utf8)
        let profile = try ServerProfile("https://diagnostic-save.invalid",
            storageNamespace: "cz.fpy.cognis.tests.diagnostic-save." + UUID().uuidString)
        let host = try BundleWebView(profile: profile, root: root, surface: surface,
                                     diagnosticDestination: destination)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 500, height: 400),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = host.web
        let keyReady = expectation(description: "Native test window became key")
        let token = NotificationCenter.default.addObserver(forName: NSWindow.didBecomeKeyNotification,
                                                           object: window, queue: .main) { _ in keyReady.fulfill() }
        window.makeKeyAndOrderFront(nil)
        // XCTest starts in the background; the cooperative API cannot acquire focus.
        NSApp.activate(ignoringOtherApps: true)
        await fulfillment(of: [keyReady], timeout: 5)
        NotificationCenter.default.removeObserver(token)
        let ready = expectation(description: "Trusted synthetic document loaded")
        host.onReady = { ready.fulfill() }
        host.start(path: "/login")
        await fulfillment(of: [ready], timeout: 5)
        XCTAssertTrue(window.isKeyWindow)
        return (host, window, root)
    }

    private func send(_ host: BundleWebView, type: String = "save-diagnostic", payload: [String: Any]? = nil) async throws {
        let barrier = expectation(description: "Bridge messages handled in order")
        host.onMessage = { if case .dismiss = $0 { barrier.fulfill() } }
        _ = try await host.web.callAsyncJavaScript("""
            const config = window.__cognisNative;
            const send = (type,payload) => window.webkit.messageHandlers.cognis.postMessage({
              version:1,generation:config.generation,type,payload
            });
            send(type,payload); send('dismiss',{}); return true;
            """, arguments: ["type": type, "payload": payload ?? ["filename": filename, "json": json]],
            in: nil, contentWorld: .page)
        await fulfillment(of: [barrier], timeout: 5)
    }

    private func clean(_ host: BundleWebView, _ window: NSWindow, _ root: URL) {
        host.invalidate()
        window.orderOut(nil)
        try? FileManager.default.removeItem(at: root)
    }

    func testTrustedSaveWritesOnlyTheChosenFileAndBalancesInteraction() async throws {
        var selected: ((URL?) -> Void)?
        var proposed = ""
        let (host, window, root) = try await makeHost { _, name, completion in
            proposed = name; selected = completion
        }
        defer { clean(host, window, root) }
        var interactions: [Bool] = []
        host.onOwnedInteraction = { interactions.append($0) }
        try await send(host)
        XCTAssertEqual(proposed, filename)
        XCTAssertEqual(interactions, [true])
        let output = root.appendingPathComponent(filename)
        XCTAssertFalse(FileManager.default.fileExists(atPath: output.path))
        try XCTUnwrap(selected)(output)
        XCTAssertEqual(try Data(contentsOf: output), json.data(using: .utf8))
        XCTAssertEqual(interactions, [true, false])
    }

    func testCancelAndRepeatedClicksDoNotWriteOrOpenDuplicateSheets() async throws {
        var selected: ((URL?) -> Void)?
        var picks = 0
        let (host, window, root) = try await makeHost { _, _, completion in
            picks += 1; selected = completion
        }
        defer { clean(host, window, root) }
        try await send(host)
        try await send(host)
        XCTAssertEqual(picks, 1)
        try XCTUnwrap(selected)(nil)
        XCTAssertFalse(FileManager.default.fileExists(atPath: root.appendingPathComponent(filename).path))
        try await send(host)
        XCTAssertEqual(picks, 2)
        selected?(nil)
    }

    func testInvalidatedDocumentCannotCompleteAnEarlierSave() async throws {
        var selected: ((URL?) -> Void)?
        let (host, window, root) = try await makeHost { _, _, completion in selected = completion }
        defer { clean(host, window, root) }
        var interactions: [Bool] = []
        host.onOwnedInteraction = { interactions.append($0) }
        try await send(host)
        host.invalidate()
        let output = root.appendingPathComponent(filename)
        try XCTUnwrap(selected)(output)
        XCTAssertFalse(FileManager.default.fileExists(atPath: output.path))
        XCTAssertEqual(interactions, [true, false])
    }

    func testAccountResetRevokesAnEarlierSave() async throws {
        var selected: ((URL?) -> Void)?
        let (host, window, root) = try await makeHost { _, _, completion in selected = completion }
        defer { clean(host, window, root) }
        func snapshot(_ epoch: Int, _ authenticated: Bool) -> [String: Any] {
            ["epoch": epoch, "authenticated": authenticated, "connection": "connected", "pins": [], "agents": []]
        }
        try await send(host, type: "snapshot", payload: snapshot(1, true))
        try await send(host)
        try await send(host, type: "snapshot", payload: snapshot(2, false))
        let output = root.appendingPathComponent(filename)
        try XCTUnwrap(selected)(output)
        XCTAssertFalse(FileManager.default.fileExists(atPath: output.path))
    }

    func testRailAndNonKeyWindowsCannotRequestSaveSheets() async throws {
        var picks = 0
        let (rail, railWindow, railRoot) = try await makeHost(surface: "rail") { _, _, _ in picks += 1 }
        defer { clean(rail, railWindow, railRoot) }
        try await send(rail)
        XCTAssertEqual(picks, 0)
        let (host, window, root) = try await makeHost { _, _, _ in picks += 1 }
        defer { clean(host, window, root) }
        window.orderOut(nil)
        try await send(host)
        XCTAssertEqual(picks, 0)
    }
}
