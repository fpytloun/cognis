import Foundation

public enum PanelTarget {
    public enum Presentation: Equatable, Sendable {
        case pending, unavailable, unretainedConversation
        case open(agent: String, conversation: String?, direct: Bool)
    }
    public static func presentation(requested: String?, conversation: String?, direct: Bool,
                                    lastAgent: String?, snapshot: Snapshot) -> Presentation {
        guard snapshot.authenticated, let targets = snapshot.panelTargets, targets.ready else { return .pending }
        guard let agent = requested ?? resolve(lastAgent: lastAgent, pins: snapshot.pins, agents: snapshot.agents)
        else { return .unavailable }
        if let conversation, !snapshot.pins.contains(where: { $0.agentId == agent && $0.conversationId == conversation }) {
            return .unretainedConversation
        }
        return .open(agent: agent, conversation: conversation,
            direct: direct || (conversation == nil && !targets.pinnedAgents.contains(agent)))
    }
    /// Device-local history is only a preference, never proof of current access.
    public static func resolve(lastAgent: String?, pins: [Pin], agents: [TrayAgent]) -> String? {
        if let lastAgent, validIdentifier(lastAgent),
           pins.contains(where: { $0.agentId == lastAgent }) || agents.contains(where: { $0.agentId == lastAgent }) {
            return lastAgent
        }
        return pins.first?.agentId ?? agents.first?.agentId
    }

    public static func shouldDismiss(visible: Bool, currentAgent: String?, targetAgent: String,
                                     explicitConversation: String?, ownedInteraction: Bool) -> Bool {
        visible && currentAgent == targetAgent && explicitConversation == nil && !ownedInteraction
    }

    public static func historyKey(profile: ServerProfile) -> String {
        "CognisPanel-\(profile.storageIdentifier.uuidString)-lastAgent"
    }
}
