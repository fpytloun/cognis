import Foundation

/// A bounded, user-selected JSON export, never a URL download or destination path.
public struct DiagnosticExport: Sendable {
    public static let maximumFileBytes = 8 * 1024 * 1024
    public static let maximumEnvelopeBytes = 16 * 1024 * 1024
    public let filename: String
    public let data: Data

    public static func decode(_ payload: [String: Any]) throws -> DiagnosticExport {
        guard Set(payload.keys) == ["filename", "json"],
              let filename = payload["filename"] as? String,
              filename.utf8.count <= 160,
              let match = filename.range(of: #"^cognis-chat-diagnostic(?:-[A-Za-z0-9_-]{1,128})?\.json$"#,
                                         options: .regularExpression),
              match.lowerBound == filename.startIndex, match.upperBound == filename.endIndex,
              let json = payload["json"] as? String,
              json.utf8.count <= maximumFileBytes,
              let data = json.data(using: .utf8),
              let document = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              document["schema"] as? String == "cognis-chat-client-diagnostic-v1"
        else { throw HostError.invalidMessage }
        return DiagnosticExport(filename: filename, data: data)
    }
}
