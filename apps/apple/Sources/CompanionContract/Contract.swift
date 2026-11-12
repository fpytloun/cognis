import Foundation
import CryptoKit

public enum HostError: Error { case invalidServer, invalidMessage, incompatibleProtocol }

public struct ServerProfile: Equatable, Sendable {
    public let origin: URL
    public let storageIdentifier: UUID
    public var isAcceptanceServer: Bool { origin.absoluteString == "http://127.0.0.1:18763" }

    public init(_ input: String, storageNamespace: String = "cz.fpy.cognis.companion") throws {
        guard let parts = URLComponents(string: input.trimmingCharacters(in: .whitespacesAndNewlines)),
              let scheme = parts.scheme?.lowercased(), let host = parts.host?.lowercased(),
              !host.isEmpty, parts.user == nil, parts.password == nil,
              parts.query == nil, parts.fragment == nil,
              parts.path.isEmpty || parts.path == "/",
              parts.port == nil || (1...65535).contains(parts.port!),
              scheme == "https" || (scheme == "http" && ["localhost", "127.0.0.1", "[::1]", "::1"].contains(host))
        else { throw HostError.invalidServer }
        var normalized = parts
        normalized.scheme = scheme
        normalized.host = host
        normalized.path = ""
        if (scheme == "https" && normalized.port == 443) || (scheme == "http" && normalized.port == 80) {
            normalized.port = nil
        }
        guard let url = normalized.url else { throw HostError.invalidServer }
        origin = url
        // Isolate cookies even across different server ports.
        // Preserve the standard app's existing profile mapping. Preview and
        // acceptance also separate storage by immutable native package identity.
        let seed = storageNamespace == "cz.fpy.cognis.companion"
            ? url.absoluteString : storageNamespace + "\0" + url.absoluteString
        let d = Array(SHA256.hash(data: Data(seed.utf8)))
        storageIdentifier = UUID(uuid: (
            d[0], d[1], d[2], d[3], d[4], d[5], d[6], d[7],
            d[8], d[9], d[10], d[11], d[12], d[13], d[14], d[15]
        ))
    }

    public func contains(_ url: URL) -> Bool {
        guard var components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              components.user == nil, components.password == nil else { return false }
        components.path = ""; components.query = nil; components.fragment = nil
        guard let candidate = try? ServerProfile(components.string ?? "") else { return false }
        return candidate.origin == origin
    }
}

public struct Pin: Codable, Equatable, Sendable {
    public let conversationId: String
    public let agentId: String
    public let title: String
    public let agentName: String
    public let attention: String
    public var path: String { "/chat/\(conversationId)?window=1" }
    public var isValid: Bool {
        validIdentifier(conversationId) && validIdentifier(agentId)
        && title.count <= 160 && agentName.count <= 80 && !agentName.isEmpty
        && ["error", "attention", "active", "unread", "idle"].contains(attention)
        && !title.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains)
        && !agentName.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains)
    }
}

public struct TrayAgent: Codable, Equatable, Sendable {
    public let agentId: String
    public let name: String
    public var isValid: Bool {
        validIdentifier(agentId) && !name.isEmpty && name.count <= 80
            && !name.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains)
    }
}

public struct PanelTargets: Codable, Sendable {
    public let ready: Bool
    public let pinnedAgents: [String]
    public var isValid: Bool {
        pinnedAgents.count <= 24 && pinnedAgents.allSatisfy(validIdentifier)
            && Set(pinnedAgents).count == pinnedAgents.count
    }
}

public struct Snapshot: Codable, Sendable {
    public let epoch: Int
    public let authenticated: Bool
    public let connection: String
    public let pins: [Pin]
    public let agents: [TrayAgent]
    // Older bridge-v1 projections can render the shell but cannot admit queued
    // panel URLs until target discovery is authoritative.
    public let panelTargets: PanelTargets?
    public var isValid: Bool {
        epoch >= 1 && epoch <= 1_000_000 && pins.count <= 24
        && ["idle", "connecting", "connected", "reconnecting", "stalled"].contains(connection)
        && pins.allSatisfy(\.isValid)
        && Set(pins.map(\.conversationId)).count == pins.count
        && agents.allSatisfy(\.isValid)
        && Set(agents.map(\.agentId)).count == agents.count
        && (panelTargets?.isValid ?? true)
        && (authenticated || (pins.isEmpty && agents.isEmpty))
    }
}

public enum BridgeMessage: Sendable {
    case openReference(String)
    case saveDiagnostic(DiagnosticExport)
    case openMain(String)
    case openWindow(String)
    case openResourceWindow(StandaloneTarget)
    case workspace(WorkspaceFrame)
    case rail(String, String?, Double?)
    case railSize(Double, Double)
    case panelState(String?, String?)
    case notificationSettings(String)
    case avatar(String, Data)
    case appearance(String, String)
    case snapshot(Snapshot)
    case route(String)
    case dismiss
    case notificationScope(String, String?)
    case notification(NativeNotificationIntent)
    case notificationReview
    public static func decode(_ body: Any, generation: String, trustedMainFrame: Bool) throws -> BridgeMessage {
        guard trustedMainFrame, JSONSerialization.isValidJSONObject(body),
              let value = body as? [String: Any],
              Set(value.keys) == ["version", "generation", "type", "payload"],
              let version = value["version"] as? Int else { throw HostError.invalidMessage }
        guard version == 1 else { throw HostError.incompatibleProtocol }
        guard value["generation"] as? String == generation,
              let type = value["type"] as? String,
              let payload = value["payload"] as? [String: Any],
              try JSONSerialization.data(withJSONObject: body).count <= (
                type == "save-diagnostic" ? DiagnosticExport.maximumEnvelopeBytes
                  : ["workspace", "snapshot"].contains(type) ? 524_288 : 32_768)
        else { throw HostError.invalidMessage }
        switch type {
        case "open-reference":
            guard Set(payload.keys) == ["path"], let path = payload["path"] as? String, validRoute(path)
            else { throw HostError.invalidMessage }
            return .openReference(path)
        case "save-diagnostic": return .saveDiagnostic(try DiagnosticExport.decode(payload))
        case "notification-settings":
            guard Set(payload.keys) == ["action"], let action = payload["action"] as? String,
                  ["status", "show"].contains(action) else { throw HostError.invalidMessage }
            return .notificationSettings(action)
        case "rail":
            guard let action = payload["action"] as? String else { throw HostError.invalidMessage }
            if action == "size" {
                guard Set(payload.keys) == ["action", "width", "height"],
                      let width = payload["width"] as? Double, let height = payload["height"] as? Double,
                      width.isFinite, height.isFinite, (32...128).contains(width), (32...2048).contains(height)
                else { throw HostError.invalidMessage }
                return .railSize(width, height)
            }
            if action == "open" {
                guard Set(payload.keys) == ["action", "agentId"],
                      let id = payload["agentId"] as? String, validIdentifier(id) else { throw HostError.invalidMessage }
                return .rail(action, id, nil)
            }
            if action == "step" {
                guard Set(payload.keys) == ["action", "delta"], let delta = payload["delta"] as? Double,
                      delta.isFinite, abs(delta) <= 20000 else { throw HostError.invalidMessage }
                return .rail(action, nil, delta)
            }
            guard Set(payload.keys) == ["action"], ["ready", "empty", "drag-start", "drag-move", "drag-end"].contains(action)
            else { throw HostError.invalidMessage }
            return .rail(action, nil, nil)
        case "panel-state":
            guard Set(payload.keys) == ["agentId", "conversationId"],
                  payload["agentId"] is NSNull || (payload["agentId"] as? String).map(validIdentifier) == true,
                  payload["conversationId"] is NSNull || (payload["conversationId"] as? String).map(validIdentifier) == true
            else { throw HostError.invalidMessage }
            return .panelState(payload["agentId"] as? String, payload["conversationId"] as? String)
        case "workspace": return .workspace(try WorkspaceFrame.decode(payload))
        case "notification-scope":
            guard Set(payload.keys) == ["scope", "account"],
                  let scope = payload["scope"] as? String, UUID(uuidString: scope) != nil,
                  payload["account"] is NSNull || payload["account"] is String else { throw HostError.invalidMessage }
            let account = payload["account"] as? String
            guard account == nil || (account!.utf8.count <= 254 && account!.contains("@")) else { throw HostError.invalidMessage }
            return .notificationScope(scope, account)
        case "notification":
            guard Set(payload.keys) == ["scope", "id", "kind", "target", "presentation"],
                  let scope = payload["scope"] as? String, UUID(uuidString: scope) != nil,
                  let id = payload["id"] as? String, id.utf8.count <= 400,
                  id.range(of: #"^(terminal|notification):[A-Za-z0-9_:.\-+]+$"#, options: .regularExpression) != nil,
                  let kind = payload["kind"] as? String, let target = payload["target"] as? String
            else { throw HostError.invalidMessage }
            let destination = NotificationTarget(kind: kind, target: target)
            guard destination.isValid, let presentation = payload["presentation"] as? [String: Any],
                  Set(presentation.keys) == ["title", "subtitle", "body", "sender", "approval"]
            else { throw HostError.invalidMessage }
            if let sender = presentation["sender"] as? [String: Any] {
                guard Set(sender.keys) == ["id", "name", "png"], kind == "chat", id.hasPrefix("terminal:")
                else { throw HostError.invalidMessage }
            } else if !(presentation["sender"] is NSNull) { throw HostError.invalidMessage }
            if let approval = presentation["approval"] as? [String: Any] {
                guard Set(approval.keys) == ["actionId", "revision", "submissionId"], id.hasPrefix("notification:")
                else { throw HostError.invalidMessage }
            } else if !(presentation["approval"] is NSNull) { throw HostError.invalidMessage }
            let content = try JSONDecoder().decode(NotificationPresentation.self, from: JSONSerialization.data(withJSONObject: presentation))
            guard content.isValid else { throw HostError.invalidMessage }
            return .notification(NativeNotificationIntent(scope: scope, id: id, target: destination, presentation: content))
        case "notification-review":
            guard payload.isEmpty else { throw HostError.invalidMessage }
            return .notificationReview
        case "avatar":
            guard Set(payload.keys) == ["agentId", "png"],
                  let id = payload["agentId"] as? String, validIdentifier(id),
                  let encoded = payload["png"] as? String, encoded.utf8.count <= 24000,
                  let data = Data(base64Encoded: encoded),
                  data.starts(with: [137, 80, 78, 71, 13, 10, 26, 10]) else { throw HostError.invalidMessage }
            return .avatar(id, data)
        case "appearance":
            guard Set(payload.keys) == ["theme", "background"],
                  let theme = payload["theme"] as? String, ["dark", "light"].contains(theme),
                  let background = payload["background"] as? String,
                  background.range(of: #"^#[0-9a-fA-F]{6}$"#, options: .regularExpression) != nil
            else { throw HostError.invalidMessage }
            return .appearance(theme, background)
        case "snapshot":
            let keys = Set(payload.keys)
            guard keys == ["epoch", "authenticated", "connection", "pins", "agents"]
                || keys == ["epoch", "authenticated", "connection", "pins", "agents", "panelTargets"]
            else { throw HostError.invalidMessage }
            if keys.contains("panelTargets") {
                guard let targets = payload["panelTargets"] as? [String: Any],
                      Set(targets.keys) == ["ready", "pinnedAgents"] else { throw HostError.invalidMessage }
            }
            let snapshot = try JSONDecoder().decode(Snapshot.self, from: JSONSerialization.data(withJSONObject: payload))
            guard let agents = payload["agents"] as? [[String: Any]],
                  agents.allSatisfy({ Set($0.keys) == ["agentId", "name"] }) else { throw HostError.invalidMessage }
            guard snapshot.isValid else { throw HostError.invalidMessage }
            return .snapshot(snapshot)
        case "open-main":
            guard Set(payload.keys) == ["path"], let path = payload["path"] as? String,
                  path.range(of: #"^/chat/[A-Za-z0-9_-]+$"#, options: .regularExpression) != nil
            else { throw HostError.invalidMessage }
            return .openMain(path)
        case "open-window":
            guard Set(payload.keys) == ["conversationId"],
                  let id = payload["conversationId"] as? String, validIdentifier(id)
            else { throw HostError.invalidMessage }
            return .openWindow(id)
        case "open-resource-window":
            guard Set(payload.keys) == ["kind", "id"],
                  let rawKind = payload["kind"] as? String,
                  let kind = StandaloneTarget.Kind(rawValue: rawKind), kind != .conversation,
                  let id = payload["id"] as? String, validIdentifier(id)
            else { throw HostError.invalidMessage }
            return .openResourceWindow(StandaloneTarget(kind: kind, id: id))
        case "route":
            guard Set(payload.keys) == ["path"], let path = payload["path"] as? String,
                  validRoute(path) else { throw HostError.invalidMessage }
            return .route(path)
        case "dismiss":
            guard payload.isEmpty else { throw HostError.invalidMessage }
            return .dismiss
        default: throw HostError.invalidMessage
        }
    }
}

public func validIdentifier(_ value: String) -> Bool {
    !value.isEmpty && value.utf8.count <= 160
    && value.range(of: "^[a-zA-Z0-9_-]+$", options: .regularExpression) != nil
}
public func validRoute(_ value: String) -> Bool {
    value.utf8.count <= 512
    && value.range(of: "^/(?:$|[a-zA-Z0-9/_%-]+(?:\\?[a-zA-Z0-9_=&%-]*)?)$", options: .regularExpression) != nil
    && !value.hasPrefix("//") && !value.lowercased().contains("%2f") && !value.lowercased().contains("%5c")
}
public func bundledAsset(_ url: URL, root: URL) -> URL? {
    guard url.scheme == "cognisbundle", url.host == "app", url.user == nil, url.password == nil,
          url.port == nil, url.query == nil, url.fragment == nil,
          !url.path.contains("\\"), !url.path.contains("\0") else { return nil }
    let base = root.resolvingSymlinksInPath().standardizedFileURL
    let file = base.appendingPathComponent(url.path).resolvingSymlinksInPath().standardizedFileURL
    guard file.path.hasPrefix(base.path + "/"), !url.path.split(separator: "/").contains("..") else { return nil }
    return file
}
