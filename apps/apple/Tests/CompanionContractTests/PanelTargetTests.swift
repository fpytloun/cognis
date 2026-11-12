import XCTest
@testable import CompanionContract

final class PanelTargetTests: XCTestCase {
    func testExplicitAndHistoryLaunchWaitForHydrationAndNeverSelectAnotherTab() throws {
        func snapshot(_ target: String, pins: String = "[]") throws -> Snapshot {
            let data = Data("""
                {"epoch":1,"authenticated":true,"connection":"connected","pins":\(pins),
                 "agents":[{"agentId":"agent_a","name":"A"}],"panelTargets":\(target)}
                """.utf8)
            let payload = try JSONSerialization.jsonObject(with: data)
            let message: [String: Any] = ["version": 1, "generation": "test-generation",
                "type": "snapshot", "payload": payload]
            guard case .snapshot(let snapshot) = try BridgeMessage.decode(message,
                generation: "test-generation", trustedMainFrame: true) else {
                throw HostError.invalidMessage
            }
            return snapshot
        }
        let loading = try snapshot(#"{"ready":false,"pinnedAgents":[]}"#)
        for requested in [nil, "agent_a"] {
            XCTAssertEqual(PanelTarget.presentation(requested: requested, conversation: nil, direct: false,
                lastAgent: "agent_a", snapshot: loading), .pending)
        }
        // A pinned agent with no resolved Home still uses its pinned presentation.
        let pinned = try snapshot(#"{"ready":true,"pinnedAgents":["agent_a"]}"#)
        XCTAssertEqual(PanelTarget.presentation(requested: "agent_a", conversation: nil, direct: false,
            lastAgent: nil, snapshot: pinned), .open(agent: "agent_a", conversation: nil, direct: false))
        let direct = try snapshot(#"{"ready":true,"pinnedAgents":[]}"#)
        XCTAssertEqual(PanelTarget.presentation(requested: nil, conversation: nil, direct: false,
            lastAgent: nil, snapshot: direct), .open(agent: "agent_a", conversation: nil, direct: true))
        XCTAssertEqual(PanelTarget.presentation(requested: "agent_a", conversation: "conv_missing", direct: false,
            lastAgent: nil, snapshot: pinned), .unretainedConversation)
        let retained = try snapshot(#"{"ready":true,"pinnedAgents":["agent_a"]}"#, pins:
            #"[{"conversationId":"conv_a","agentId":"agent_a","title":"A","agentName":"A","attention":"idle"}]"#)
        XCTAssertEqual(PanelTarget.presentation(requested: "agent_a", conversation: "conv_a", direct: false,
            lastAgent: nil, snapshot: retained), .open(agent: "agent_a", conversation: "conv_a", direct: false))
    }
    func testHistoryThenPinsThenCatalogueWithRemovedAgentFallback() throws {
        let pin = try JSONDecoder().decode(Pin.self, from: Data("""
            {"conversationId":"conv_a","agentId":"agent_a","title":"A","agentName":"A","attention":"idle"}
            """.utf8))
        let agents = try JSONDecoder().decode([TrayAgent].self, from: Data("""
            [{"agentId":"agent_b","name":"B"},{"agentId":"agent_c","name":"C"}]
            """.utf8))
        XCTAssertEqual(PanelTarget.resolve(lastAgent: "agent_c", pins: [pin], agents: agents), "agent_c")
        XCTAssertEqual(PanelTarget.resolve(lastAgent: "agent_a", pins: [pin], agents: agents), "agent_a")
        XCTAssertEqual(PanelTarget.resolve(lastAgent: "agent_deleted", pins: [pin], agents: agents), "agent_a")
        XCTAssertEqual(PanelTarget.resolve(lastAgent: nil, pins: [pin], agents: agents), "agent_a")
        XCTAssertEqual(PanelTarget.resolve(lastAgent: nil, pins: [], agents: agents), "agent_b")
        XCTAssertNil(PanelTarget.resolve(lastAgent: "agent_deleted", pins: [], agents: []))
    }
    func testProfileHistoryIsDeviceLocalAndSurvivesNewDefaultsInstance() throws {
        let suite = "cz.fpy.cognis.tests.history.\(UUID())"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let first = try ServerProfile("https://first.test"), second = try ServerProfile("https://second.test")
        defaults.set("agent_b", forKey: PanelTarget.historyKey(profile: first))
        XCTAssertEqual(UserDefaults(suiteName: suite)!.string(forKey: PanelTarget.historyKey(profile: first)), "agent_b")
        XCTAssertNil(defaults.string(forKey: PanelTarget.historyKey(profile: second)))
    }
    func testToggleProtectsOwnedInteractionsAndExplicitConversationSelection() {
        XCTAssertTrue(PanelTarget.shouldDismiss(visible: true, currentAgent: "a", targetAgent: "a",
            explicitConversation: nil, ownedInteraction: false))
        for (visible, current, explicit, owned) in [(false, "a", nil, false), (true, "b", nil, false),
            (true, "a", "conv_a", false), (true, "a", nil, true)] as [(Bool, String, String?, Bool)] {
            XCTAssertFalse(PanelTarget.shouldDismiss(visible: visible, currentAgent: current, targetAgent: "a",
                explicitConversation: explicit, ownedInteraction: owned))
        }
    }
    func testShortcutDefaultValidationPersistenceAndDisable() throws {
        let suite = "cz.fpy.cognis.tests.shortcut.\(UUID())"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        XCTAssertEqual(HotKeyPreference.load(from: defaults), HotKeyPreference(keyCode: 8, modifiers: 6400))
        XCTAssertFalse(HotKeyPreference(keyCode: 8, modifiers: 0).isValid)
        XCTAssertFalse(HotKeyPreference(keyCode: 53, modifiers: 256).isValid)
        let changed = HotKeyPreference(keyCode: 49, modifiers: HotKeyPreference.control)
        XCTAssertTrue(changed.isValid)
        try HotKeyPreference.save(changed, to: defaults)
        XCTAssertEqual(HotKeyPreference.load(from: defaults), changed)
        try HotKeyPreference.save(nil, to: defaults)
        XCTAssertNil(HotKeyPreference.load(from: defaults))
    }
}
