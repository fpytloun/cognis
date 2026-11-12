import Foundation

/// Identifier-only window destinations. The host, not the web document, owns routes.
public struct StandaloneTarget: Equatable, Sendable {
    public enum Kind: String, Sendable { case conversation, project, deliverable }
    public let kind: Kind
    public let id: String

    public init(kind: Kind, id: String) { self.kind = kind; self.id = id }
    public var isValid: Bool { validIdentifier(id) }
    // Retain conversation keys for existing notification observation.
    public var key: String { kind == .conversation ? id : "\(kind.rawValue):\(id)" }
    public var path: String {
        switch kind {
        case .conversation: return "/chat/\(id)?window=1"
        case .project: return "/projects/\(id)?window=1"
        case .deliverable: return "/library/deliverable/\(id)?window=1"
        }
    }
    public var title: String {
        switch kind {
        case .conversation: return "Conversation"
        case .project: return "Project"
        case .deliverable: return "Deliverable"
        }
    }
    public var surface: String { kind == .conversation ? "conversation" : "resource" }
}
