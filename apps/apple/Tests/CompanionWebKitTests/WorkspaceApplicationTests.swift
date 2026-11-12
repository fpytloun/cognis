import XCTest
import AppKit
import WebKit
import CompanionContract
@testable import CognisCompanion

@MainActor
final class WorkspaceApplicationTests: XCTestCase {
    private let fixture = """
    (() => {
      if (!location.hostname.endsWith('.invalid')) throw Error('Reserved HTTPS fixture required');
      const calls = window.fixtureCalls = {writes:0, summaries:0, details:{}};
      const set = Storage.prototype.setItem;
      Storage.prototype.setItem = function(key,value) {
        if (key.startsWith('cognis.conversation-workspace.')) calls.writes++;
        return set.call(this,key,value);
      };
      window.fetch = async input => {
        const path = new URL(typeof input === 'string' ? input : input.url, location.origin).pathname;
        window.fixtureTrace?.('fetch.response', {route:path,status:200});
        const user = {email:'owner@workspace.invalid',name:'Workspace Fixture',role:'user'};
        let body = {};
        if (path.endsWith('/me')) body = user;
        else if (path.endsWith('/refresh') || path.endsWith('/logout'))
          body = {status:'authenticated',user,expires_at:'2030-01-01T00:00:00Z'};
        else if (path.endsWith('/conversations/conv_forbidden'))
          return new Response(JSON.stringify({detail:'Synthetic forbidden'}),{status:403,headers:{'Content-Type':'application/json'}});
        else if (/\\/conversations\\/conv_(first|second|fresh|capacity_\\d+)$/.test(path)) {
          calls.details[path] = (calls.details[path] || 0) + 1;
          if (path.endsWith('/conv_fresh') && window.fixtureDeferDetail)
            await new Promise(resolve => { window.fixtureReleaseDetail = resolve; });
          const id = path.split('/').pop();
          body = {conversation_id:id,agent_id:'agent_first',title:id,status:'idle',context:{type:'web'}};
        } else if (path.includes('/agents')) { calls.summaries++; body = {items:[],has_more:false}; }
        else if (path.includes('/sidebar')) { calls.summaries++; body = {background_work:{items:[]},agent_direct_chats:[]}; }
        else if (path.includes('/inbox')) { calls.summaries++; body = {items:[],total:0,unread_count:0}; }
        return new Response(JSON.stringify(body),{status:200,headers:{'Content-Type':'application/json'}});
      };
      window.WebSocket = class extends EventTarget {
        static CONNECTING=0; static OPEN=1; static CLOSING=2; static CLOSED=3;
        readyState=0;
        constructor() { super(); queueMicrotask(() => {this.readyState=1;this.onopen?.(new Event('open'));}); }
        send() {}
        close() {this.readyState=3;}
      };
    })();
    """
    private func text(_ host: BundleWebView, _ id: String) async throws -> String {
        do {
            return try await host.web.callAsyncJavaScript("return document.getElementById(id)?.textContent || '';",
                arguments: ["id": id], in: nil, contentWorld: .page) as? String ?? ""
        } catch {
            let failure = error as NSError
            print("WORKSPACE_EVALUATION_FAILED doc=\(host.generation) output=\(id) type=\(String(reflecting: type(of: error))) domain=\(failure.domain) code=\(failure.code) loading=\(host.web.isLoading) delegate=\(host.web.navigationDelegate != nil)")
            throw error
        }
    }
    private func state(_ host: BundleWebView) async throws -> [String: Any] {
        let raw = try await text(host, "workspace-state")
        return (try? JSONSerialization.jsonObject(with: Data(raw.utf8))) as? [String: Any] ?? [:]
    }
    private func poll() async throws {
        do { try await Task.sleep(for: .milliseconds(50)) }
        catch {
            let failure = error as NSError
            print("WORKSPACE_POLL_FAILED type=\(String(reflecting: type(of: error))) domain=\(failure.domain) code=\(failure.code)")
            throw error
        }
    }
    private func awaitEntries(_ host: BundleWebView, _ count: Int) async throws {
        for _ in 0..<150 {
            if (try await state(host)["entries"] as? [Any])?.count == count { return }
            try await poll()
        }
        let value = try await text(host, "workspace-state")
        let auth = try await text(host, "auth-state")
        let trace = try await host.web.evaluateJavaScript("JSON.stringify(window.fixtureAuthTrace || [])") as? String ?? ""
        let events = (try? JSONSerialization.jsonObject(with: Data(trace.utf8))) as? [[String: Any]] ?? []
        for event in events {
            print("WORKSPACE_AUTH_TRACE doc=\(host.generation) n=\(event["n"] ?? "") kind=\(event["kind"] ?? "") value=\(event["value"] ?? "")")
            print(event["stack"] ?? "")
        }
        print("WORKSPACE_CONVERGENCE_FAILED doc=\(host.generation) expected=\(count) auth=\(auth) state=\(value) loading=\(host.web.isLoading) delegate=\(host.web.navigationDelegate != nil)")
        throw NSError(domain: "WorkspaceApplication", code: 1, userInfo: [
            NSLocalizedDescriptionKey: "doc \(host.generation) did not converge to \(count) entries; auth=\(auth); state=\(value)"
        ])
    }
    private func click(_ host: BundleWebView, _ id: String) async throws {
        _ = try await host.web.callAsyncJavaScript("document.getElementById(id).click(); return true;",
            arguments: ["id": id], in: nil, contentWorld: .page)
    }
    // Gate A uses main's real binding/local owner result. Gate B independently
    // uses replicas and Swift relay delivery; it is not main binding coverage.
    @MainActor
    private final class RestoreSession {
        var hosts: [BundleWebView] = []
        var windows: [NSWindow] = []
        var relay: WorkspaceRelay!
        var holdDelivery = false
        var held: [(BundleWebView, [String: Any])] = []
        var commands: [(String, Int)] = []
        var mainResults: [Int] = []
        let root: URL
        let profile: ServerProfile
        let fixture: String
        var main: BundleWebView { hosts[0] }
        init(root: URL, fixture: String) throws {
            self.root = root; self.fixture = fixture
            profile = try ServerProfile("https://restore.workspace.invalid",
                storageNamespace: "cz.fpy.cognis.tests.restore." + UUID().uuidString)
            _ = NSApplication.shared
            let main = try add("main")
            relay = WorkspaceRelay(profile: profile.origin.absoluteString, main: main.generation)
        }
        func add(_ surface: String) throws -> BundleWebView {
            let host = try BundleWebView(profile: profile, root: root, surface: surface)
            host.web.configuration.userContentController.addUserScript(WKUserScript(
                source: fixture, injectionTime: .atDocumentStart, forMainFrameOnly: true))
            host.onMessage = { [weak self, weak host] message in
                guard let self, let host, case .workspace(let frame) = message else { return }
                if frame.kind == "command" { self.commands.append((host.generation, frame.id)) }
                if host === self.main && frame.kind == "result" { self.mainResults.append(frame.id) }
                let recipients = self.relay?.route(frame, source: host.generation,
                    documents: Set(self.hosts.map(\.generation))) {
                        for secondary in self.hosts.dropFirst() { secondary.send(["type": "workspace-reset"]) }
                    } ?? []
                for target in self.hosts where recipients.contains(target.generation) {
                    let envelope: [String: Any] = ["type": "workspace", "frame": frame.value]
                    if self.holdDelivery && host === self.main {
                        self.held.append((target, envelope))
                    } else { target.send(envelope) }
                }
            }
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1440, height: 900),
                styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
            window.isReleasedWhenClosed = false; window.contentView = host.web
            windows.append(window); hosts.append(host)
            host.start(path: "/workspace-acceptance")
            return host
        }
        func release() {
            holdDelivery = false
            let pending = held; held.removeAll()
            // Use the same public host delivery as production, retaining original
            // document, command ID, owner epoch and monotonic revision.
            for (host, envelope) in pending { host.send(envelope) }
        }
        func hasHeldResult(for document: String) -> Bool {
            guard let command = commands.last(where: { $0.0 == document }) else { return false }
            return held.contains { host, envelope in
                guard host.generation == command.0, let frame = envelope["frame"] as? [String: Any] else { return false }
                return frame["kind"] as? String == "result"
                    && frame["target"] as? String == command.0 && frame["id"] as? Int == command.1
            }
        }
        func close() {
            hosts.forEach { $0.invalidate() }
            for window in windows { window.contentView = nil; window.close() }
        }
    }
    private func restoreRoot() throws -> URL {
        guard let path = ProcessInfo.processInfo.environment["COGNIS_WORKSPACE_WEB_ROOT"],
              FileManager.default.fileExists(atPath: path + "/WORKSPACE_ACCEPTANCE_ONLY") else {
            throw XCTSkip("Requires own canonical build-workspace-acceptance.mjs export")
        }
        return URL(fileURLWithPath: path)
    }
    private func awaitText(_ host: BundleWebView, _ id: String, containing expected: String,
                           readiness: Bool = false, exactIDs: Set<String>? = nil) async throws {
        for _ in 0..<(readiness ? 150 : 100) {
            let value = try await text(host, id)
            if let exactIDs {
                if let ids = try? JSONSerialization.jsonObject(with: Data(value.utf8)) as? [String],
                   ids.count == exactIDs.count, Set(ids) == exactIDs {
                    print("WORKSPACE_SUMMARIES_READY ids=\(ids.sorted())")
                    return
                }
            } else if value.contains(expected) { return }
            try await poll()
        }
        XCTFail("Expected \(id) to match \(exactIDs?.sorted().description ?? expected); actual \(try await text(host, id))")
    }
    private func bindingWindows(_ host: BundleWebView) async throws -> [[String: Any]] {
        let raw = try await text(host, "binding-windows")
        return try XCTUnwrap(JSONSerialization.jsonObject(with: Data(raw.utf8)) as? [[String: Any]])
    }
    func testMainBindingRetainedRestoreDelayedValidationAndScopeFences() async throws {
        let session = try RestoreSession(root: restoreRoot(), fixture: fixture)
        defer { session.close() }
        let main = session.main
        try await awaitText(main, "auth-state", containing: "authenticated", readiness: true)
        try await awaitText(main, "workspace-state", containing: "owner@workspace.invalid", readiness: true)
        // Public execute control confirms the runtime owner is installed first.
        try await click(main, "select-first")
        try await awaitText(main, "workspace-result", containing: "missing-selection", readiness: true)
        try await click(main, "binding-seed")
        try await awaitText(main, "binding-result", containing: "seeded", readiness: true)
        // Seeding commits membership before its scheduled summary hydration finishes.
        let seededIDs = Set((0..<9).map { "conv_capacity_\($0)" } + ["conv_second"])
        try await awaitText(main, "workspace-summaries", containing: "", readiness: true, exactIDs: seededIDs)
        let initial = try await bindingWindows(main)
        XCTAssertEqual(initial.count, 10)
        XCTAssertEqual(initial.filter { $0["minimized"] as? Bool == false }.count, 4)
        let details = try await main.web.evaluateJavaScript("JSON.stringify(window.fixtureCalls.details)") as? String
        session.mainResults.removeAll()
        try await click(main, "binding-hold")
        try await awaitText(main, "validation-gate", containing: "held")
        try await click(main, "binding-open"); try await click(main, "binding-open")
        let pending = try await text(main, "binding-result")
        XCTAssertEqual(pending, "pending")
        let blocked = try await bindingWindows(main)
        XCTAssertEqual(blocked.first { $0["key"] as? String == "agent:agent_first" }?["minimized"] as? Bool, true)
        try await click(main, "binding-release")
        try await awaitText(main, "binding-result", containing: "\"status\":\"focused\"")
        let opened = try await bindingWindows(main)
        XCTAssertEqual(opened.count, 10)
        XCTAssertEqual(opened.filter { $0["minimized"] as? Bool == false }.count, 5)
        XCTAssertEqual(opened.filter { $0["resolvedConversationId"] as? String == "conv_second" }.count, 1)
        let afterDetails = try await main.web.evaluateJavaScript("JSON.stringify(window.fixtureCalls.details)") as? String
        print("MAIN_BINDING_DETAIL_COUNTS baseline=\(details ?? "unavailable") after=\(afterDetails ?? "unavailable")")
        XCTAssertEqual(details, afterDetails, "Retained restore must skip allocation detail fetch")
        XCTAssertEqual(session.mainResults.count, 1, "Same-binding repeated clicks submit one owner command")
        try await click(main, "binding-new")
        try await awaitText(main, "binding-result", containing: "\"scope\":\"retained\"")
        try await click(main, "binding-forbidden")
        try await awaitText(main, "binding-result", containing: "\"status\":\"failed\"")
        for change in ["binding-dispose", "binding-project"] {
            try await click(main, "binding-minimize")
            let beforeFence = try await bindingWindows(main)
            let targetBeforeFence = beforeFence.first { $0["key"] as? String == "agent:agent_first" }
            try await click(main, "binding-hold")
            try await awaitText(main, "validation-gate", containing: "held")
            try await click(main, "binding-open")
            try await click(main, change)
            try await click(main, "binding-release")
            try await awaitText(main, "binding-result", containing: "scope-changed")
            let afterFence = try await bindingWindows(main)
            let targetAfterFence = afterFence.first { $0["key"] as? String == "agent:agent_first" }
            if change == "binding-project" { XCTAssertNil(targetAfterFence) }
            else {
                XCTAssertEqual(targetAfterFence?["minimized"] as? Bool, true)
                XCTAssertEqual(targetAfterFence?["focusRevision"] as? Int, targetBeforeFence?["focusRevision"] as? Int)
            }
            try await click(main, "binding-dashboard")
        }
        // Account invalidation while a genuinely new detail request awaits.
        _ = try await main.web.evaluateJavaScript("window.fixtureDeferDetail = true")
        try await click(main, "binding-new")
        for _ in 0..<100 {
            if try await main.web.evaluateJavaScript("typeof window.fixtureReleaseDetail === 'function'") as? Bool == true { break }
            try await poll()
        }
        let releaseReady = try await main.web.evaluateJavaScript("typeof window.fixtureReleaseDetail === 'function'") as? Bool
        XCTAssertEqual(releaseReady, true)
        try await click(main, "logout")
        try await awaitText(main, "auth-state", containing: "anonymous")
        _ = try await main.web.evaluateJavaScript("window.fixtureReleaseDetail()")
        try await awaitText(main, "binding-result", containing: "\"status\":\"failed\"")
        let finalWindows = try await bindingWindows(main)
        XCTAssertEqual(finalWindows.count, 0)
        print("MAIN_BINDING_RESTORE_PASS: local owner validation; not a Swift-relayed acknowledgement")
    }
    func testReplicaSwiftDelayedResultProjectionAndDocumentReplacement() async throws {
        let session = try RestoreSession(root: restoreRoot(), fixture: fixture)
        defer { session.close() }
        let main = session.main, replica = try session.add("pinned")
        for host in [main, replica] {
            try await awaitText(host, "workspace-state", containing: "owner@workspace.invalid", readiness: true)
        }
        try await click(main, "binding-seed")
        try await awaitText(main, "binding-result", containing: "seeded", readiness: true)
        try await awaitEntries(replica, 10)
        session.commands.removeAll(); session.holdDelivery = true
        try await click(replica, "pin-second")
        for _ in 0..<100 {
            if session.hasHeldResult(for: replica.generation) { break }
            try await poll()
        }
        XCTAssertEqual(session.commands.count, 1)
        XCTAssertTrue(session.hasHeldResult(for: replica.generation))
        let pending = try await text(replica, "workspace-result")
        XCTAssertEqual(pending, "pending")
        session.release()
        try await awaitText(replica, "workspace-result", containing: "\"key\":\"agent:agent_first\"")
        try await awaitEntries(replica, 10)
        XCTAssertEqual(session.commands.count, 1, "Delayed delivery must not replay mutation")
        session.holdDelivery = true
        try await click(replica, "pin-second")
        for _ in 0..<100 {
            if session.hasHeldResult(for: replica.generation) { break }
            try await poll()
        }
        XCTAssertTrue(session.hasHeldResult(for: replica.generation), "Retirement must fence an actual matching old result")
        replica.invalidate()
        session.hosts.removeAll { $0 === replica }
        let replacement = try session.add("pinned")
        session.release() // Held envelopes still target the retired document.
        try await awaitEntries(replacement, 10)
        let replacementResult = try await text(replacement, "workspace-result")
        XCTAssertEqual(replacementResult, "")
        XCTAssertNotEqual(replica.generation, replacement.generation)
        session.holdDelivery = true
        try await click(replacement, "pin-second")
        for _ in 0..<100 {
            if session.hasHeldResult(for: replacement.generation) { break }
            try await poll()
        }
        XCTAssertTrue(session.hasHeldResult(for: replacement.generation))
        try await click(main, "logout")
        try await awaitText(replacement, "auth-state", containing: "anonymous")
        session.release() // A late old-account frame cannot restore retired state.
        try await awaitEntries(replacement, 0)
        print("REPLICA_SWIFT_RESTORE_PASS: real relay command/result/projection; no replica binding")
    }
    func testPackagedHTTPSApplicationWorkspaceCommandsAndRestart() async throws {
        guard let path = ProcessInfo.processInfo.environment["COGNIS_WORKSPACE_WEB_ROOT"],
              FileManager.default.fileExists(atPath: path + "/WORKSPACE_ACCEPTANCE_ONLY") else {
            throw XCTSkip("Build the test-only packaged application export with build-workspace-acceptance.mjs")
        }
        _ = NSApplication.shared
        let profile = try ServerProfile("https://workspace.application.invalid",
            storageNamespace: "cz.fpy.cognis.tests.workspace." + UUID().uuidString)
        let root = URL(fileURLWithPath: path)
        var hosts: [BundleWebView] = []
        let attached = ProcessInfo.processInfo.environment["COGNIS_WORKSPACE_ATTACHED"] != "0"
        var windows: [NSWindow] = []
        print("WORKSPACE_ATTACHMENT_MODE \(attached ? "attached" : "detached") profile=\(profile.storageIdentifier)")
        var relay: WorkspaceRelay?
        var droppedAck = false
        func add(_ surface: String) throws -> BundleWebView {
            let host = try BundleWebView(profile: profile, root: root, surface: surface)
            print("WORKSPACE_HOST_ADD surface=\(surface) doc=\(host.generation)")
            host.onFailure = { [weak host] reason in
                print("WORKSPACE_HOST_FAILED surface=\(surface) doc=\(host?.generation ?? "released") reason=\(reason)")
            }
            host.onReady = { [weak host] in
                print("WORKSPACE_HOST_READY surface=\(surface) doc=\(host?.generation ?? "released")")
            }
            host.web.configuration.userContentController.addUserScript(WKUserScript(
                source: fixture, injectionTime: .atDocumentStart, forMainFrameOnly: true))
            host.onMessage = { [weak host] message in
                guard let host, case .workspace(let frame) = message else { return }
                print("WORKSPACE_FRAME \(surface) \(frame.kind) id=\(frame.id) revision=\(frame.revision) auth=\(frame.authEpoch) owner=\(frame.ownerEpoch ?? "nil") target=\(frame.target)")
                let recipients = relay?.route(frame, source: host.generation,
                    documents: Set(hosts.map(\.generation))) {
                        print("WORKSPACE_RESET")
                        for secondary in hosts where secondary !== hosts.first {
                            secondary.send(["type": "workspace-reset"])
                        }
                    } ?? []
                if frame.kind == "result" && !droppedAck { droppedAck = true; return }
                for target in hosts where recipients.contains(target.generation) {
                    target.send(["type": "workspace", "frame": frame.value])
                }
            }
            hosts.append(host)
            if attached {
                // Match App.swift: install the view in a retained, finite-sized
                // window before starting navigation. Never activate/show it.
                let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 900, height: 700),
                    styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
                window.isReleasedWhenClosed = false
                window.contentView = host.web
                windows.append(window)
                XCTAssertTrue(host.web.window === window)
                XCTAssertGreaterThan(host.web.bounds.width, 0)
                XCTAssertGreaterThan(host.web.bounds.height, 0)
                print("WORKSPACE_ATTACHED doc=\(host.generation) window=\(ObjectIdentifier(window)) size=\(host.web.bounds.size)")
            }
            host.start(path: "/workspace-acceptance")
            return host
        }
        let main = try add("main")
        relay = WorkspaceRelay(profile: profile.origin.absoluteString, main: main.generation)
        let first = try add("pinned"), second = try add("conversation")
        defer {
            hosts.forEach { print("WORKSPACE_HOST_RETIRE doc=\($0.generation)"); $0.invalidate() }
            for window in windows { window.contentView = nil; window.close() }
        }
        for host in hosts {
            for _ in 0..<150 {
                if try await state(host)["owner"] as? String == "owner@workspace.invalid" { break }
                try await poll()
            }
            let owner = try await state(host)["owner"] as? String
            XCTAssertEqual(owner, "owner@workspace.invalid")
        }
        try await click(first, "pin-first")
        try await click(second, "pin-second")
        for host in hosts { try await awaitEntries(host, 2) }
        print("WORKSPACE_PHASE concurrent-pins")
        // Lost acknowledgement must reconcile by ID, without duplicate writes.
        for _ in 0..<150 {
            if try await text(first, "workspace-result").contains("\"ok\":true") { break }
            try await poll()
        }
        let acknowledged = try await text(first, "workspace-result")
        XCTAssertTrue(acknowledged.contains("\"ok\":true"))
        print("WORKSPACE_PHASE reconciled-lost-ack")
        try await click(second, "unpin-first")
        print("WORKSPACE_PHASE unpin-click-complete")
        try await click(first, "select-first")
        print("WORKSPACE_PHASE selection-click-complete")
        for host in hosts { try await awaitEntries(host, 1) }
        print("WORKSPACE_PHASE unpin-selection")
        for host in [first, second] {
            let value = try await host.web.evaluateJavaScript("JSON.stringify(window.fixtureCalls)") as? String ?? ""
            XCTAssertTrue(value.contains("\"writes\":0"), value)
            XCTAssertTrue(value.contains("\"summaries\":0"), value)
        }
        // Replace the owner document; replicas cannot become writers in the gap.
        main.invalidate(); hosts.removeFirst()
        for replica in hosts { replica.send(["type": "workspace-reset"]) }
        let replacement = try add("main")
        hosts.removeLast(); hosts.insert(replacement, at: 0)
        relay = WorkspaceRelay(profile: profile.origin.absoluteString, main: replacement.generation)
        for host in hosts { try await awaitEntries(host, 1) }
        print("WORKSPACE_PHASE owner-restart")
        try await click(second, "pin-first")
        for host in hosts { try await awaitEntries(host, 2) }
        try await click(replacement, "logout")
        for host in hosts { try await awaitEntries(host, 0) }
        print("WORKSPACE_APPLICATION_PASS: packaged HTTPS app; actual web owner/replica, Swift relay, IDB auth; injected network")
    }
    func testStandaloneRegistryRetiresNativeResourcesAndBoundsEntries() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        try "<html><head></head><body>__sveltekit_registry.app.start(</body></html>".write(
            to: root.appendingPathComponent("index.html"), atomically: true, encoding: .utf8)
        let profile = try ServerProfile("https://registry.workspace.invalid",
            storageNamespace: "cz.fpy.cognis.tests.registry." + UUID().uuidString)
        let registry = StandaloneDocuments()
        for i in 0..<10 {
            let host = try BundleWebView(profile: profile, root: root, surface: "conversation")
            let window = NSWindow(contentRect: .zero, styleMask: [.titled, .closable], backing: .buffered, defer: false)
            window.isReleasedWhenClosed = false; window.contentView = host.web
            let kind: StandaloneTarget.Kind = i == 1 ? .project : i == 2 ? .deliverable : .conversation
            let target = StandaloneTarget(kind: kind, id: "conversation-\(i)")
            XCTAssertTrue(registry.insert(target: target, window: window, host: host))
            XCTAssertEqual(registry.target(id: target.key), target)
            XCTAssertFalse(registry.insert(target: target, window: window, host: host))
        }
        let host = try BundleWebView(profile: profile, root: root, surface: "conversation")
        let window = NSWindow(contentRect: .zero, styleMask: [], backing: .buffered, defer: false)
        XCTAssertFalse(registry.insert(target: StandaloneTarget(kind: .project, id: "overflow"), window: window, host: host))
        host.invalidate()
        let retired = registry.hosts["conversation-0"]!
        registry.retire(id: "conversation-0")?.close()
        XCTAssertNil(retired.web.superview); XCTAssertNil(retired.web.navigationDelegate)
        XCTAssertNil(retired.onMessage); XCTAssertEqual(registry.count, 9)
        registry.retireAll(); XCTAssertEqual(registry.count, 0)
        XCTAssertNil(registry.target(id: "project:conversation-1"))
        XCTAssertNil(registry.target(id: "deliverable:conversation-2"))
    }
}
