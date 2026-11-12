import Foundation

/// Download destinations never authorize local file reads or credentialed URLs.
public func isDownloadURL(_ url: URL, profile: ServerProfile) -> Bool {
    if url.scheme == "blob" { return isHTMLPreviewBlob(url, profile: profile) }
    guard url.user == nil, url.password == nil, let host = url.host, !host.isEmpty else { return false }
    return url.scheme == "https"
        || (url.scheme == "http" && ["localhost", "127.0.0.1", "::1", "[::1]"].contains(host))
}

/// A server-suggested name is only a save-panel suggestion, never a path.
public func downloadFilename(_ suggested: String) -> String {
    let basename = suggested.replacingOccurrences(of: "\\", with: "/").split(separator: "/").last.map(String.init) ?? ""
    let clean = String(String.UnicodeScalarView(basename.unicodeScalars.filter {
        !CharacterSet.controlCharacters.contains($0) && $0 != ":"
    })).trimmingCharacters(in: .whitespacesAndNewlines)
    guard !clean.isEmpty, clean != ".", clean != ".." else { return "download" }
    var name = clean
    while name.utf8.count > 240 { name.removeLast() }
    return name
}
