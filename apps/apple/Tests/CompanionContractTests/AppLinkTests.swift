import XCTest
@testable import CompanionContract

final class AppLinkTests: XCTestCase {
    func testSpaReferenceWireAdmissionUsesTheNavigateEnvelope() throws {
        let generation = UUID().uuidString
        func message(_ path: String) throws -> BridgeMessage {
            try BridgeMessage.decode(["version": 1, "generation": generation, "type": "open-reference",
                "payload": ["path": path]], generation: generation, trustedMainFrame: true)
        }
        for path in ["/settings?tab=appearance", "/chat?search=release_notes"] {
            guard case .openReference(let decoded) = try message(path) else { return XCTFail("Expected reference") }
            XCTAssertEqual(decoded, path)
        }
        for path in ["/chat?search=release+notes", "/chat?search=release.notes",
            "/chat?search=src%2Fmain", "/chat?search=src%5cmain", "/chat?search=" + String(repeating: "a", count: 512)] {
            XCTAssertThrowsError(try message(path), path)
        }
    }
    func testCanonicalNavigationAndStandaloneLinks() throws {
        let profile = try ServerProfile("https://cognis.test")
        let cases: [(String, AppLink.Destination)] = [
            ("/", .main("/")), ("/chat/new", .main("/chat/new")), ("/tasks", .main("/tasks")),
            ("/notifications", .main("/notifications")), ("/chat/conv_a", .main("/chat/conv_a")),
            ("/tasks/task_a", .main("/tasks/task_a")), ("/projects/project_a", .main("/projects/project_a")),
            ("/library/artifact/art_a", .main("/library/artifact/art_a")),
            ("/library/deliverable/dlv_a", .main("/library/deliverable/dlv_a")),
            ("/chat?scope=agent_direct&agent_id=agent_a", .main("/chat?scope=agent_direct&agent_id=agent_a")),
            ("/chat/conv_a?window=1", .window(StandaloneTarget(kind: .conversation, id: "conv_a"))),
            ("/projects/project_a?window=1", .window(StandaloneTarget(kind: .project, id: "project_a"))),
            ("/library/deliverable/dlv_a?window=1", .window(StandaloneTarget(kind: .deliverable, id: "dlv_a"))),
            ("/native-pinned", .panel(agent: nil, conversation: nil, direct: false)),
            ("/native-pinned?agent=agent_a", .panel(agent: "agent_a", conversation: nil, direct: false)),
            ("/native-pinned?agent=agent_a&conversation=conv_a", .panel(agent: "agent_a", conversation: "conv_a", direct: false)),
            ("/native-pinned?agent=agent_a&direct=1", .panel(agent: "agent_a", conversation: nil, direct: true)),
        ]
        for (route, expected) in cases {
            for prefix in ["cognis://", "cognis://COGNIS.test:443", "https://cognis.test"] {
                XCTAssertEqual(try AppLink(url: URL(string: prefix + route)!, profile: profile, flavor: .standard).destination,
                    expected, prefix + route)
            }
        }
        XCTAssertEqual(try AppLink(url: URL(string: "https://cognis.test/chat/conv_a")!,
            profile: profile, flavor: .standard, newWindow: true).destination,
            .window(StandaloneTarget(kind: .conversation, id: "conv_a")))
    }

    func testInvalidAndSideEffectingLinksHaveNoDestination() throws {
        let profile = try ServerProfile("https://cognis.test")
        for text in [
            "cognis:///chat/new?window=1", "cognis:///tasks/task_a?window=1",
            "cognis:///library/artifact/art_a?window=1", "cognis:///chat/conv_a?send=hello",
            "cognis:///tasks/task_a?approve=1", "cognis:///native-pinned?conversation=conv_a",
            "cognis:///native-pinned?direct=1", "cognis:///native-pinned?agent=agent_a&direct=0",
            "cognis:///native-pinned?agent=agent_a&direct=1&conversation=conv_a",
            "cognis:///native-pinned?agent=a&agent=b", "cognis:///chat/conv_a#fragment",
            "cognis://user:password@cognis.test/chat/conv_a", "cognis:///chat/%2fconv_a",
            "cognis:///chat/%252fconv_a", "cognis:///chat/../tasks", "cognis:////chat/conv_a",
            "cognis:///chat/conv.a", "cognis:///api/auth/logout", "cognis:///chat/conv_a?window",
            "cognis:///chat/conv_a?window=1&window=1", "file:///chat/conv_a",
            "cognis-preview:///chat/conv_a", "cognis:///chat/" + String(repeating: "x", count: 1024),
        ] {
            XCTAssertThrowsError(try AppLink(url: URL(string: text)!, profile: profile, flavor: .standard), text)
        }
        XCTAssertThrowsError(try AppLink(url: URL(string: "cognis:///chat/conv_a")!,
            profile: profile, flavor: .acceptance))
    }

    func testServerMismatchPortsAndPreviewAreExplicit() throws {
        let profile = try ServerProfile("http://127.0.0.1:18763")
        XCTAssertEqual(try AppLink(url: URL(string: "cognis://127.0.0.1:18763/native-pinned")!,
            profile: profile, flavor: .standard).destination, .panel(agent: nil, conversation: nil, direct: false))
        for text in ["cognis://127.0.0.1:18764/tasks", "cognis://other.test/tasks", "https://127.0.0.1:18763/tasks"] {
            XCTAssertThrowsError(try AppLink(url: URL(string: text)!, profile: profile, flavor: .standard)) {
                guard case AppLink.LinkError.serverMismatch = $0 else { return XCTFail("Expected server mismatch") }
            }
        }
        XCTAssertEqual(try AppLink(url: URL(string: "cognis-preview:///tasks")!,
            profile: profile, flavor: .preview).destination, .main("/tasks"))
    }
}
