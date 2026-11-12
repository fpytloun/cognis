import XCTest
@testable import CompanionContract

final class ContractTests: XCTestCase {
    let generation = "8D6FEAFB-2AAC-4B41-AC82-42B2314563BB"

    func testServerValidationAndIsolation() throws {
        for input in ["http://192.168.1.1", "http://localhost.evil.test", "file:///tmp/ui",
                      "https://user:password@host.test", "https://host.test/path",
                      "https://host.test?token=secret", "https://host.test/#fragment"] {
            XCTAssertThrowsError(try ServerProfile(input), input)
        }
        let first = try ServerProfile("http://127.0.0.1:18761")
        let second = try ServerProfile("http://127.0.0.1:18763")
        XCTAssertTrue(second.isAcceptanceServer)
        XCTAssertFalse(first.isAcceptanceServer)
        XCTAssertFalse(try ServerProfile("https://host.test").isAcceptanceServer)
        XCTAssertFalse(try ServerProfile("https://127.0.0.1:18762").isAcceptanceServer)
        XCTAssertFalse(try ServerProfile("http://localhost:18762").isAcceptanceServer)
        XCTAssertNotEqual(first.storageIdentifier, second.storageIdentifier)
        XCTAssertEqual(first.storageIdentifier, try ServerProfile("http://127.0.0.1:18761/").storageIdentifier)
        XCTAssertEqual(try ServerProfile("https://HOST.test:443").origin, try ServerProfile("https://host.test/").origin)
        XCTAssertTrue(first.contains(URL(string: "http://127.0.0.1:18761/api/ws")!))
        XCTAssertFalse(first.contains(URL(string: "http://127.0.0.1:18762/api/ws")!))
    }

    func testInvalidFrameGenerationVersionAndShape() throws {
        let value: [String: Any] = ["version": 1, "generation": generation, "type": "dismiss", "payload": [:]]
        XCTAssertThrowsError(try BridgeMessage.decode(value, generation: generation, trustedMainFrame: false))
        XCTAssertThrowsError(try BridgeMessage.decode(value, generation: UUID().uuidString, trustedMainFrame: true))
        var invalid = value
        invalid["version"] = 2
        XCTAssertThrowsError(try BridgeMessage.decode(invalid, generation: generation, trustedMainFrame: true))
        invalid = value
        invalid["command"] = "execute"
        XCTAssertThrowsError(try BridgeMessage.decode(invalid, generation: generation, trustedMainFrame: true))
        if case .dismiss = try BridgeMessage.decode(value, generation: generation, trustedMainFrame: true) {} else {
            XCTFail("Expected typed dismiss")
        }
    }

    func testSnapshotLimitsAndAccountReset() throws {
        let pin: [String: Any] = [
            "conversationId": "conv_m1", "agentId": "agent_m1", "title": "Chat", "agentName": "Agent", "attention": "idle"
        ]
        func message(authenticated: Bool, pins: [[String: Any]]) -> [String: Any] {
            ["version": 1, "generation": generation, "type": "snapshot",
             "payload": ["epoch": 1, "authenticated": authenticated, "connection": "connected", "pins": pins, "agents": []]]
        }
        XCTAssertNoThrow(try BridgeMessage.decode(message(authenticated: true, pins: [pin]),
                                                   generation: generation, trustedMainFrame: true))
        XCTAssertThrowsError(try BridgeMessage.decode(message(authenticated: false, pins: [pin]),
                                                       generation: generation, trustedMainFrame: true))
        XCTAssertThrowsError(try BridgeMessage.decode(message(authenticated: true, pins: [pin, pin]),
                                                       generation: generation, trustedMainFrame: true))
        XCTAssertNoThrow(try BridgeMessage.decode(message(authenticated: false, pins: []),
                                                   generation: generation, trustedMainFrame: true))
        var oversized = pin
        oversized["title"] = String(repeating: "x", count: 33_000)
        XCTAssertThrowsError(try BridgeMessage.decode(message(authenticated: true, pins: [oversized]),
                                                       generation: generation, trustedMainFrame: true))
    }

    func testAssetConfinementAndRoutes() throws {
        let root = URL(fileURLWithPath: "/tmp/cognis-m1-test-assets")
        XCTAssertEqual(bundledAsset(URL(string: "cognisbundle://app/_app/start.js")!, root: root)?.path,
                       root.path + "/_app/start.js")
        for url in ["cognisbundle://app/../secret", "cognisbundle://remote/file", "file:///etc/passwd",
                    "cognisbundle://app/%2e%2e/secret", "cognisbundle://app/file?path=secret"] {
            XCTAssertNil(bundledAsset(URL(string: url)!, root: root), url)
        }
        XCTAssertTrue(validRoute("/chat/conv_m1?window=1"))
        for route in ["//host.test", "/%2fhost.test", "/%5csecret", "javascript:alert(1)", "/chat/id#fragment"] {
            XCTAssertFalse(validRoute(route), route)
        }
    }
    func testTrayCatalogueIsNotLimitedByPinOrWindowCapacity() throws {
        let agents = (0..<75).map { ["agentId": "agent_\($0)", "name": "Synthetic agent \($0)"] }
        func body(_ agents: [[String: String]], authenticated: Bool = true) -> [String: Any] {
            ["version": 1, "generation": generation, "type": "snapshot", "payload": [
                "epoch": 1, "authenticated": authenticated, "connection": "connected", "pins": [], "agents": agents
            ]]
        }
        XCTAssertNoThrow(try BridgeMessage.decode(body(agents), generation: generation, trustedMainFrame: true))
        XCTAssertThrowsError(try BridgeMessage.decode(body(agents, authenticated: false), generation: generation, trustedMainFrame: true))
        XCTAssertThrowsError(try BridgeMessage.decode(body([agents[0], agents[0]]), generation: generation, trustedMainFrame: true))
        XCTAssertThrowsError(try BridgeMessage.decode(body([["agentId": "agent_1", "name": "Agent", "url": "https://host.test"]]),
            generation: generation, trustedMainFrame: true))
    }
    func testNotificationPresentationRejectsURLsUnknownKeysOversizedAndInvalidActions() throws {
        func body(_ content: [String: Any], kind: String = "chat") -> [String: Any] {
            ["version": 1, "generation": generation, "type": "notification", "payload": [
                "scope": generation, "id": "notification:notice_1", "kind": kind, "target": "conv_1", "presentation": content
            ]]
        }
        let presentation: [String: Any] = ["title": "Synthetic agent", "subtitle": "Synthetic chat", "body": "Ready.",
            "sender": NSNull(), "approval": NSNull()]
        XCTAssertNoThrow(try BridgeMessage.decode(body(presentation), generation: generation, trustedMainFrame: true))
        for (key, value) in [("url", "https://host.test"), ("body", String(repeating: "x", count: 241)), ("title", "Agent\ninvalid")] {
            var content = presentation; content[key] = value
            XCTAssertThrowsError(try BridgeMessage.decode(body(content), generation: generation, trustedMainFrame: true))
        }
        var content = presentation
        content["approval"] = ["actionId": "action_1", "revision": 0, "submissionId": generation]
        XCTAssertThrowsError(try BridgeMessage.decode(body(content), generation: generation, trustedMainFrame: true))
        content["approval"] = ["actionId": "action_1", "revision": 2, "submissionId": generation]
        XCTAssertNoThrow(try BridgeMessage.decode(body(content), generation: generation, trustedMainFrame: true))
        content["sender"] = ["id": "agent_1", "name": "Agent", "png": "https://host.test/avatar"]
        XCTAssertThrowsError(try BridgeMessage.decode(body(content), generation: generation, trustedMainFrame: true))
    }
    func testStandaloneWindowCommandIsIdentifierOnlyAndGenerationScoped() throws {
        func message(_ payload: [String: Any]) -> [String: Any] {
            ["version": 1, "generation": generation, "type": "open-window", "payload": payload]
        }
        let value = message(["conversationId": "conv_window"])
        guard case .openWindow(let id) = try BridgeMessage.decode(value, generation: generation, trustedMainFrame: true)
        else { return XCTFail("Expected a typed single-conversation command") }
        XCTAssertEqual(id, "conv_window")
        XCTAssertThrowsError(try BridgeMessage.decode(value, generation: UUID().uuidString, trustedMainFrame: true))
        XCTAssertThrowsError(try BridgeMessage.decode(value, generation: generation, trustedMainFrame: false))
        for payload in [["path": "/chat/conv_window"], ["conversationId": "//untrusted.test"],
                        ["conversationId": "conv_window", "url": "https://untrusted.test"]] {
            XCTAssertThrowsError(try BridgeMessage.decode(message(payload), generation: generation, trustedMainFrame: true))
        }
    }
    func testResourceWindowCommandIsTypedScopedAndCannotChooseArbitraryRoutes() throws {
        func message(_ payload: [String: Any]) -> [String: Any] {
            ["version": 1, "generation": generation, "type": "open-resource-window", "payload": payload]
        }
        for kind in [StandaloneTarget.Kind.project, .deliverable] {
            let value = message(["kind": kind.rawValue, "id": "shared_id"])
            guard case .openResourceWindow(let target) = try BridgeMessage.decode(value,
                generation: generation, trustedMainFrame: true) else { return XCTFail("Expected resource window") }
            XCTAssertEqual(target, StandaloneTarget(kind: kind, id: "shared_id"))
            XCTAssertThrowsError(try BridgeMessage.decode(value, generation: UUID().uuidString, trustedMainFrame: true))
            XCTAssertThrowsError(try BridgeMessage.decode(value, generation: generation, trustedMainFrame: false))
        }
        for payload: [String: Any] in [
            ["kind": "conversation", "id": "shared_id"], ["kind": "artifact", "id": "shared_id"],
            ["kind": "project", "id": "../chat"], ["kind": "project", "id": "id?window=1"],
            ["kind": "deliverable", "id": "https://untrusted.test"],
            ["kind": "project", "id": "id", "path": "/settings"],
            ["kind": "project"], ["id": "id"], ["kind": "project", "id": NSNull()]
        ] {
            XCTAssertThrowsError(try BridgeMessage.decode(message(payload), generation: generation, trustedMainFrame: true))
        }
        let chat = StandaloneTarget(kind: .conversation, id: "shared_id")
        let project = StandaloneTarget(kind: .project, id: "shared_id")
        let deliverable = StandaloneTarget(kind: .deliverable, id: "shared_id")
        XCTAssertEqual(Set([chat.key, project.key, deliverable.key]).count, 3)
        XCTAssertEqual(chat.key, "shared_id")
        XCTAssertEqual(chat.path, "/chat/shared_id?window=1")
        XCTAssertEqual(project.path, "/projects/shared_id?window=1")
        XCTAssertEqual(deliverable.path, "/library/deliverable/shared_id?window=1")
        XCTAssertEqual(project.surface, "resource")
    }
}
