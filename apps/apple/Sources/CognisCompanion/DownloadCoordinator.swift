import AppKit
import WebKit
import CompanionContract

/// Owns one window's chooser, staged download, cancellation and atomic publication.
@MainActor
final class DownloadCoordinator: NSObject, WKDownloadDelegate {
    typealias Destination = @MainActor (NSWindow, String, @escaping @MainActor (URL?) -> Void) -> (@MainActor () -> Void)

    static let chooseDestination: Destination = { window, filename, completion in
        let panel = NSSavePanel()
        panel.nameFieldStringValue = filename
        panel.canCreateDirectories = true
        panel.beginSheetModal(for: window) { result in completion(result == .OK ? panel.url : nil) }
        return { panel.cancel(nil) }
    }

    private let profile: ServerProfile
    private let destination: Destination
    private var download: WKDownload?
    private weak var window: NSWindow?
    private var isCurrent: (@MainActor () -> Bool)?
    private var interaction: ((Bool) -> Void)?
    private var cancelChooser: (@MainActor () -> Void)?
    private var reply: (@MainActor (URL?) -> Void)?
    private var stagingDirectory: URL?
    private var stagedFile: URL?
    private var selectedFile: URL?
    var isBusy: Bool { download != nil }

    init(profile: ServerProfile, destination: @escaping Destination = chooseDestination) {
        self.profile = profile
        self.destination = destination
    }

    func begin(_ download: WKDownload, window: NSWindow, isCurrent: @escaping @MainActor () -> Bool,
               interaction: ((Bool) -> Void)?) {
        guard !isBusy, window.isKeyWindow, isCurrent() else { download.cancel { _ in }; return }
        self.download = download
        self.window = window
        self.isCurrent = isCurrent
        self.interaction = interaction
        download.delegate = self
    }

    func invalidate() { finish(cancel: true) }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping @MainActor (URL?) -> Void) {
        guard self.download === download else { completionHandler(nil); return }
        guard isCurrent?() == true, let window else {
            completionHandler(nil); finish(cancel: true); return
        }
        if let response = response as? HTTPURLResponse, !(200...299).contains(response.statusCode) {
            completionHandler(nil); fail(); return
        }
        reply = completionHandler
        interaction?(true)
        let cancel = destination(window, downloadFilename(suggestedFilename)) { [weak self] url in
            guard let self, self.download === download, let reply = self.reply else { return }
            self.reply = nil
            self.cancelChooser = nil
            self.interaction?(false)
            guard self.isCurrent?() == true, let url, url.isFileURL else {
                reply(nil); self.finish(cancel: true); return
            }
            do {
                // WebKit never writes to an existing/chosen file. Publish only a
                // complete download, on the same volume, after the final fence.
                let directory = url.deletingLastPathComponent()
                    .appendingPathComponent(".cognis-download-" + UUID().uuidString, isDirectory: true)
                try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false,
                                                        attributes: [.posixPermissions: 0o700])
                self.stagingDirectory = directory
                self.stagedFile = directory.appendingPathComponent("content")
                self.selectedFile = url
                reply(self.stagedFile)
            } catch {
                reply(nil); self.fail()
            }
        }
        // Injected destinations may complete synchronously.
        if reply != nil { cancelChooser = cancel }
    }

    func download(_ download: WKDownload, willPerformHTTPRedirection response: HTTPURLResponse,
                  newRequest request: URLRequest,
                  decisionHandler: @escaping @MainActor (WKDownload.RedirectPolicy) -> Void) {
        let allowed = self.download === download && isCurrent?() == true
            && request.url.map { isDownloadURL($0, profile: profile) } == true
        decisionHandler(allowed ? .allow : .cancel)
        if !allowed, self.download === download { fail() }
    }

    func downloadDidFinish(_ download: WKDownload) {
        guard self.download === download else { return }
        guard isCurrent?() == true, let stagedFile, let selectedFile else { finish(cancel: true); return }
        do {
            if FileManager.default.fileExists(atPath: selectedFile.path) {
                _ = try FileManager.default.replaceItemAt(selectedFile, withItemAt: stagedFile)
            } else {
                try FileManager.default.moveItem(at: stagedFile, to: selectedFile)
            }
            finish(cancel: false)
        } catch { fail() }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        guard self.download === download else { return }
        fail()
    }

    private func fail() {
        let owner = isCurrent?() == true ? window : nil
        let interaction = interaction
        finish(cancel: true)
        guard let owner else { return }
        let alert = NSAlert()
        alert.messageText = "Could not save file"
        alert.informativeText = "The download failed or the selected location is unavailable. Please try again."
        interaction?(true)
        alert.beginSheetModal(for: owner) { _ in interaction?(false) }
    }

    private func finish(cancel: Bool) {
        let active = download
        download = nil
        let reply = reply
        self.reply = nil
        let cancelChooser = cancelChooser
        self.cancelChooser = nil
        if reply != nil { interaction?(false) }
        reply?(nil)
        cancelChooser?()
        if cancel {
            // Unlink synchronously, including during applicationWillTerminate.
            // Open file descriptors can drain safely; a late WebKit open fails
            // once its private parent directory is gone. Never depend on a
            // cancellation callback running before process exit.
            let directory = stagingDirectory
            if let directory { try? FileManager.default.removeItem(at: directory) }
            active?.cancel { _ in
                // Retry a transient filesystem failure after the writer stops.
                if let directory { try? FileManager.default.removeItem(at: directory) }
            }
        } else if let stagingDirectory {
            try? FileManager.default.removeItem(at: stagingDirectory)
        }
        stagingDirectory = nil; stagedFile = nil; selectedFile = nil
        window = nil; isCurrent = nil; interaction = nil
    }
}
