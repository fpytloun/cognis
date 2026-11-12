import Foundation

/// Navigation only. Both OS URLs and ordinary web links enter the same router.
public struct AppLink: Equatable, Sendable {
    public enum Destination: Equatable, Sendable {
        case main(String)
        case window(StandaloneTarget)
        case panel(agent: String?, conversation: String?, direct: Bool)
    }
    public enum LinkError: Error { case invalid, serverMismatch }
    public let destination: Destination

    public static func scheme(for flavor: PackageFlavor) -> String? {
        switch flavor {
        case .standard: "cognis"
        case .preview: "cognis-preview"
        case .acceptance: nil
        }
    }

    public init(url: URL, profile: ServerProfile, flavor: PackageFlavor, newWindow: Bool = false) throws {
        guard url.absoluteString.utf8.count <= 1024,
              let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              parts.user == nil, parts.password == nil, parts.fragment == nil,
              !parts.percentEncodedPath.contains("%"), !parts.path.contains("\\"),
              !parts.path.split(separator: "/").contains(".."),
              !parts.path.hasPrefix("//") else { throw LinkError.invalid }
        if let scheme = Self.scheme(for: flavor), parts.scheme == scheme {
            // A custom URL supplies an authority, never an arbitrary origin.
            if let host = parts.host, !host.isEmpty {
                var origin = URLComponents()
                origin.scheme = profile.origin.scheme
                origin.host = host
                origin.port = parts.port
                guard let text = origin.string, let candidate = try? ServerProfile(text),
                      candidate.origin == profile.origin else { throw LinkError.serverMismatch }
            } else if parts.port != nil { throw LinkError.invalid }
        } else {
            guard ["https", "http"].contains(parts.scheme ?? "") else { throw LinkError.invalid }
            guard profile.contains(url) else { throw LinkError.serverMismatch }
        }
        var query: [String: String] = [:]
        for item in parts.queryItems ?? [] {
            guard let value = item.value, query[item.name] == nil else { throw LinkError.invalid }
            query[item.name] = value
        }
        let path = parts.path
        if path == "/native-pinned" {
            guard Set(query.keys).isSubset(of: ["agent", "conversation", "direct"]),
                  query["agent"].map(validIdentifier) ?? true,
                  query["conversation"].map(validIdentifier) ?? true,
                  query["direct"] == nil || query["direct"] == "1",
                  query["conversation"] == nil || query["direct"] == nil,
                  query["agent"] != nil || (query["conversation"] == nil && query["direct"] == nil)
            else { throw LinkError.invalid }
            destination = .panel(agent: query["agent"], conversation: query["conversation"], direct: query["direct"] == "1")
            return
        }
        if path == "/chat", Set(query.keys) == ["scope", "agent_id"],
           query["scope"] == "agent_direct", let agent = query["agent_id"], validIdentifier(agent) {
            destination = .main("/chat?scope=agent_direct&agent_id=\(agent)")
            return
        }
        guard query.isEmpty || query == ["window": "1"] else { throw LinkError.invalid }
        let segments = path.split(separator: "/", omittingEmptySubsequences: false)
        var standalone: StandaloneTarget?
        if ["/", "/chat/new", "/tasks", "/notifications"].contains(path) {
            guard query.isEmpty else { throw LinkError.invalid }
        } else if segments.count == 3, segments[0].isEmpty, validIdentifier(String(segments[2])) {
            switch segments[1] {
            case "chat" where segments[2] != "new":
                standalone = StandaloneTarget(kind: .conversation, id: String(segments[2]))
            case "projects": standalone = StandaloneTarget(kind: .project, id: String(segments[2]))
            case "tasks": break
            default: throw LinkError.invalid
            }
        } else if segments.count == 4, segments[0].isEmpty, segments[1] == "library",
                  ["artifact", "deliverable"].contains(segments[2]), validIdentifier(String(segments[3])) {
            if segments[2] == "deliverable" {
                standalone = StandaloneTarget(kind: .deliverable, id: String(segments[3]))
            }
        } else if !["/", "/chat/new", "/tasks", "/notifications"].contains(path) {
            throw LinkError.invalid
        }
        guard query.isEmpty || standalone != nil, validRoute(path) else { throw LinkError.invalid }
        destination = (newWindow || !query.isEmpty) && standalone != nil
            ? .window(standalone!) : .main(path)
    }
}
