import XCTest
import AppKit
import CompanionContract
@testable import CognisCompanion

@MainActor
final class PopupTargetTests: XCTestCase {
    func testSameServerReferencesUseNativeRouterAndOtherLinksKeepExternalBehavior() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        try Data("""
            <html><head></head><body>
            <a id="conversation" target="_blank" href="/chat/conv_link?window=1">Conversation</a>
            <a id="task" href="/tasks/task_link">Task</a>
            <a id="other" target="_blank" href="https://other.test/chat/conv_link">Other server</a>
            <a id="unsupported" href="/settings">Settings</a>
            <a id="scheme" href="cognis:///native-pinned">Custom scheme</a>
            <script>const __sveltekit_test = { app: { start() {} } }; __sveltekit_test.app.start();</script>
            </body></html>
            """.utf8).write(to: root.appendingPathComponent("index.html"))
        let profile = try ServerProfile("http://127.0.0.1:18763", storageNamespace: "cz.fpy.cognis.tests.links.\(UUID())")
        var external: [URL] = []
        let host = try BundleWebView(profile: profile, root: root, externalOpener: { external.append($0) })
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 720, height: 600),
            styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = host.web; window.orderFront(nil)
        defer { host.invalidate(); window.orderOut(nil) }
        var ready = false
        host.onReady = { ready = true }
        var destinations: [AppLink.Destination] = []
        host.onOpenLink = { destinations.append($0) }
        host.start(path: "/")
        for _ in 0..<500 where !ready { try await Task.sleep(for: .milliseconds(10)) }
        XCTAssertTrue(ready)
        for (id, count) in [("conversation", 1), ("task", 2), ("other", 3), ("unsupported", 4)] {
            _ = try await host.web.callAsyncJavaScript("document.getElementById(id).click()",
                arguments: ["id": id], in: nil, contentWorld: .page)
            for _ in 0..<200 where destinations.count + external.count < count {
                try await Task.sleep(for: .milliseconds(10))
            }
            XCTAssertEqual(destinations.count + external.count, count)
        }
        _ = try await host.web.evaluateJavaScript("document.getElementById('scheme').click()")
        _ = try await host.web.evaluateJavaScript("document.title")
        XCTAssertEqual(destinations, [
            .window(StandaloneTarget(kind: .conversation, id: "conv_link")), .main("/tasks/task_link")
        ])
        XCTAssertEqual(external.map(\.absoluteString), ["https://other.test/chat/conv_link", "http://127.0.0.1:18763/settings"])
        // Real SvelteKit consumes relative anchors before WKNavigationDelegate.
        // beforeNavigate forwards those through the same validated bridge instead.
        _ = try await host.web.evaluateJavaScript("""
            window.__cognisNativeReceive = command => { window.navigationReply = command; };
            window.webkit.messageHandlers.cognis.postMessage({version:1,generation:window.__cognisNative.generation,
                type:'open-reference',payload:{path:'/chat/conv_spa'}});
            true;
            """)
        for _ in 0..<200 where destinations.count < 3 { try await Task.sleep(for: .milliseconds(10)) }
        XCTAssertEqual(destinations.last, .main("/chat/conv_spa"))
        _ = try await host.web.evaluateJavaScript("""
            window.webkit.messageHandlers.cognis.postMessage({version:1,generation:window.__cognisNative.generation,
                type:'open-reference',payload:{path:'/settings?tab=appearance'}});
            true;
            """)
        for _ in 0..<200 {
            if try await host.web.evaluateJavaScript("window.navigationReply?.path ?? ''") as? String == "/settings?tab=appearance" { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        let repliedPath = try await host.web.evaluateJavaScript("window.navigationReply?.path ?? ''") as? String
        XCTAssertEqual(repliedPath, "/settings?tab=appearance")
        XCTAssertEqual(destinations.count, 3)
    }
    func testTypedResourceWindowsCrossTheTrustedWebKitBridgeWithoutBrowserPopups() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        try Data("<html><head></head><body><script>const __sveltekit_test = { app: { start() {} } }; __sveltekit_test.app.start();</script></body></html>".utf8)
            .write(to: root.appendingPathComponent("index.html"))
        let profile = try ServerProfile("http://127.0.0.1:18763", storageNamespace: "cz.fpy.cognis.tests.windows.\(UUID())")
        let host = try BundleWebView(profile: profile, root: root, surface: "resource")
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 720, height: 600),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = host.web
        window.orderFront(nil)
        defer { host.invalidate(); window.orderOut(nil) }
        var ready = false
        host.onReady = { ready = true }
        host.start(path: "/library/deliverable/shared_window?window=1")
        for _ in 0..<500 where !ready { try await Task.sleep(for: .milliseconds(10)) }
        XCTAssertTrue(ready)
        var targets: [StandaloneTarget] = []
        host.onMessage = { message in
            if case .openResourceWindow(let target) = message { targets.append(target) }
        }
        _ = try await host.web.callAsyncJavaScript("""
            for (const kind of ['project', 'deliverable']) {
              window.webkit.messageHandlers.cognis.postMessage({
                version: 1, generation, type: 'open-resource-window',
                payload: { kind, id: 'shared_window' }
              });
            }
            """, arguments: ["generation": host.generation], in: nil, contentWorld: .page)
        for _ in 0..<200 where targets.count < 2 { try await Task.sleep(for: .milliseconds(10)) }
        XCTAssertEqual(targets, [
            StandaloneTarget(kind: .project, id: "shared_window"),
            StandaloneTarget(kind: .deliverable, id: "shared_window")
        ])
    }
    func testOnlyAvatarClickTogglesAnExistingPopup() {
        XCTAssertTrue(CompanionApp.shouldTogglePinnedPopup(visible: true, currentAgent: "agent-first",
            targetAgent: "agent-first", explicitConversation: nil))
        XCTAssertFalse(CompanionApp.shouldTogglePinnedPopup(visible: true, currentAgent: "agent-first",
            targetAgent: "agent-first", explicitConversation: "conversation-second"))
        XCTAssertFalse(CompanionApp.shouldTogglePinnedPopup(visible: true, currentAgent: "agent-first",
            targetAgent: "agent-second", explicitConversation: nil))
        XCTAssertFalse(CompanionApp.shouldTogglePinnedPopup(visible: false, currentAgent: "agent-first",
            targetAgent: "agent-first", explicitConversation: nil))
    }
}
