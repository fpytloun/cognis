import AppKit
import CompanionContract

/// Owns live native resources only, never workspace membership or persistence.
@MainActor
final class StandaloneDocuments {
    private var entries: [String: (target: StandaloneTarget, window: NSWindow, host: BundleWebView)] = [:]
    var windows: [String: NSWindow] { entries.mapValues(\.window) }
    var hosts: [String: BundleWebView] { entries.mapValues(\.host) }
    var count: Int { entries.count }
    func target(id: String) -> StandaloneTarget? { entries[id]?.target }
    @discardableResult
    func insert(target: StandaloneTarget, window: NSWindow, host: BundleWebView) -> Bool {
        guard target.isValid, entries[target.key] == nil, entries.count < 10 else { return false }
        entries[target.key] = (target, window, host); return true
    }
    @discardableResult
    func retire(id: String) -> NSWindow? {
        guard let entry = entries.removeValue(forKey: id) else { return nil }
        entry.host.send(["type": "visibility", "visible": false, "focused": false])
        entry.host.invalidate()
        entry.window.contentView = nil
        entry.window.delegate = nil
        return entry.window
    }
    func retireAll() {
        for id in Array(entries.keys) { retire(id: id)?.close() }
    }
}
