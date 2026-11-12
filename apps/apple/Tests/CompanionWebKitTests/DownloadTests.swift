import XCTest
import AppKit
import WebKit
import CompanionContract
@testable import CognisCompanion

@MainActor
final class DownloadTests: XCTestCase {
    private func makeHost(
        surface: String = "main",
        compiled: Bool = false,
        destination: @escaping DownloadCoordinator.Destination = DownloadCoordinator.chooseDestination
    ) async throws -> (BundleWebView, NSWindow, URL) {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.regular)
        NSApp.unhide(nil)
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("cognis-download-test-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        var origin = "https://downloads.cognis.test"
        var html = "<html><head></head><body><!-- __sveltekit_test.app.start( --></body></html>"
        if compiled {
            guard let fixture = ProcessInfo.processInfo.environment["COGNIS_DOWNLOAD_FIXTURE"],
                  let server = ProcessInfo.processInfo.environment["COGNIS_DOWNLOAD_ORIGIN"] else {
                throw XCTSkip("Set COGNIS_DOWNLOAD_FIXTURE and COGNIS_DOWNLOAD_ORIGIN to the compiled loopback fixture.")
            }
            origin = server
            html = try String(contentsOfFile: fixture + "/index.html", encoding: .utf8)
        }
        try html.write(to: root.appendingPathComponent("index.html"), atomically: true, encoding: .utf8)
        let profile = try ServerProfile(origin,
            storageNamespace: "cz.fpy.cognis.tests.downloads." + UUID().uuidString)
        let host = try BundleWebView(profile: profile, root: root, surface: surface, downloadDestination: destination)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 700, height: 500),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = host.web
        window.makeKeyAndOrderFront(nil)
        // XCTest starts in the background; preserve real key-window assertions.
        NSApp.activate(ignoringOtherApps: true)
        try await wait { window.isKeyWindow }
        let ready = expectation(description: "Trusted bundled document loaded")
        host.onReady = { ready.fulfill() }
        host.start(path: "/login")
        await fulfillment(of: [ready], timeout: 5)
        return (host, window, root)
    }

    private func clean(_ host: BundleWebView, _ window: NSWindow, _ root: URL) {
        host.invalidate()
        window.orderOut(nil)
        try? FileManager.default.removeItem(at: root)
    }

    private func wait(_ predicate: () -> Bool) async throws {
        for _ in 0..<100 {
            if predicate() { return }
            try await Task.sleep(for: .milliseconds(50))
        }
        XCTFail("Native download boundary did not complete")
        throw NSError(domain: "DownloadAcceptance", code: 1)
    }

    private func blob(_ host: BundleWebView, filename: String = "report.bin", delayed: Bool = false) async throws {
        _ = try await host.web.callAsyncJavaScript("""
            if (delayed) await new Promise(resolve => setTimeout(resolve, 20));
            const url = URL.createObjectURL(new Blob([new Uint8Array([0,1,127,128,255])]));
            const a = document.createElement('a'); a.href = url; a.download = filename;
            document.body.append(a); a.click(); a.remove();
            setTimeout(() => URL.revokeObjectURL(url),60000);
            return true;
            """, arguments: ["filename": filename, "delayed": delayed], in: nil, contentWorld: .page)
    }

    func testBlobSavePreservesBytesAndReplacesOnlyAfterCompletion() async throws {
        var picked: ((URL?) -> Void)?
        let (host, window, root) = try await makeHost { _, filename, completion in
            XCTAssertEqual(filename, "výsledek.bin"); picked = completion; return {}
        }
        defer { clean(host, window, root) }
        var interactions: [Bool] = []
        host.onOwnedInteraction = { interactions.append($0) }
        let output = root.appendingPathComponent("chosen.bin")
        let original = Data("existing file".utf8)
        try original.write(to: output)
        try await blob(host, filename: "výsledek.bin", delayed: true)
        try await wait { picked != nil }
        XCTAssertEqual(try Data(contentsOf: output), original)
        try XCTUnwrap(picked)(output)
        try await wait { (try? Data(contentsOf: output)) == Data([0,1,127,128,255]) }
        XCTAssertEqual(interactions, [true, false])
        XCTAssertFalse(try FileManager.default.contentsOfDirectory(atPath: root.path).contains {
            $0.hasPrefix(".cognis-download-")
        })
    }

    func testCancellationRepeatedClicksAndRetry() async throws {
        var picked: ((URL?) -> Void)?
        var picks = 0
        let (host, window, root) = try await makeHost { _, _, completion in
            picks += 1; picked = completion; return {}
        }
        defer { clean(host, window, root) }
        try await blob(host)
        try await wait { picks == 1 }
        try await blob(host)
        try XCTUnwrap(picked)(nil)
        XCTAssertEqual(picks, 1)
        try await blob(host)
        try await wait { picks == 2 }
        picked?(nil)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: root.path), ["index.html"])
    }

    func testInvalidationRevokesAndClosesPendingChooser() async throws {
        var picked: ((URL?) -> Void)?
        var cancelled = false
        let (host, window, root) = try await makeHost { _, _, completion in
            picked = completion; return { cancelled = true }
        }
        defer { clean(host, window, root) }
        var interactions: [Bool] = []
        host.onOwnedInteraction = { interactions.append($0) }
        try await blob(host)
        try await wait { picked != nil }
        host.invalidate()
        let output = root.appendingPathComponent("revoked.bin")
        picked?(output)
        XCTAssertTrue(cancelled)
        XCTAssertEqual(interactions, [true, false])
        XCTAssertFalse(FileManager.default.fileExists(atPath: output.path))
    }

    func testAccountResetRevokesPendingChooser() async throws {
        var picked: ((URL?) -> Void)?
        let (host, window, root) = try await makeHost { _, _, completion in picked = completion; return {} }
        defer { clean(host, window, root) }
        try await blob(host)
        try await wait { picked != nil }
        let barrier = expectation(description: "Account reset consumed")
        host.onMessage = { if case .snapshot = $0 { barrier.fulfill() } }
        _ = try await host.web.callAsyncJavaScript("""
            window.webkit.messageHandlers.cognis.postMessage({
              version:1,generation:__cognisNative.generation,type:'snapshot',
              payload:{epoch:2,authenticated:false,connection:'idle',pins:[],agents:[]}
            });
            return true;
            """, arguments: [:], in: nil, contentWorld: .page)
        await fulfillment(of: [barrier], timeout: 5)
        let output = root.appendingPathComponent("revoked.bin")
        picked?(output)
        XCTAssertFalse(FileManager.default.fileExists(atPath: output.path))
    }

    func testRailAndInactiveWindowsCannotStartDownloads() async throws {
        var picks = 0
        let (rail, railWindow, railRoot) = try await makeHost(surface: "rail") { _, _, reply in
            picks += 1; reply(nil); return {}
        }
        defer { clean(rail, railWindow, railRoot) }
        try await blob(rail)
        let (host, window, root) = try await makeHost { _, _, reply in picks += 1; reply(nil); return {} }
        defer { clean(host, window, root) }
        window.orderOut(nil)
        try await blob(host)
        // Barrier: await a later message from each originating web process.
        for item in [rail, host] { _ = try await item.web.evaluateJavaScript("document.body.childElementCount") }
        XCTAssertEqual(picks, 0)
    }

    func testDefaultSavePanelReallyOpensAndCancels() async throws {
        let (host, window, root) = try await makeHost()
        defer { clean(host, window, root) }
        try await blob(host, filename: "native-panel.bin")
        try await wait { window.attachedSheet is NSSavePanel }
        let panel = try XCTUnwrap(window.attachedSheet as? NSSavePanel)
        XCTAssertEqual(panel.nameFieldStringValue, "native-panel.bin")
        panel.cancel(nil)
        try await wait { window.attachedSheet == nil }
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: root.path), ["index.html"])
    }

    func testChildFrameCannotStartDownloads() async throws {
        var names: [String] = []
        let (host, window, root) = try await makeHost { _, name, reply in names.append(name); reply(nil); return {} }
        defer { clean(host, window, root) }
        _ = try await host.web.callAsyncJavaScript("""
            const file = URL.createObjectURL(new Blob(['untrusted']));
            const frame = document.createElement('iframe');
            frame.sandbox = 'allow-scripts allow-downloads';
            const attempted = new Promise((resolve, reject) => {
              const timeout = setTimeout(() => reject(new Error('Child did not attempt download')),3000);
              const receive = event => {
                if (event.source === frame.contentWindow && event.data === 'download-attempted') {
                  clearTimeout(timeout); window.removeEventListener('message',receive); resolve(true);
                }
              };
              window.addEventListener('message',receive);
            });
            frame.src = URL.createObjectURL(new Blob([
              '<body><script>const a = document.createElement("a"); a.href = "' + file +
              '"; a.download = "child.txt"; document.body.append(a); a.click();' +
              'parent.postMessage("download-attempted","*");<\\/script>'
            ],{type:'text/html'}));
            document.body.append(frame);
            return await attempted;
            """, arguments: [:], in: nil, contentWorld: .page)
        // WebKit may refuse the sandboxed request before its delegate sees it.
        // A later successful main-frame request fences native processing and
        // proves that no child request acquired a chooser or blocked the owner.
        try await blob(host, filename: "parent-after-child.bin")
        try await wait { names.contains("parent-after-child.bin") }
        XCTAssertEqual(names, ["parent-after-child.bin"])
        XCTAssertNil(window.attachedSheet)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: root.path), ["index.html"])
    }

    func testCompiledControlsSaveAllFormatsAndExports() async throws {
        var outputDirectory: URL?
        var proposed = ""
        let (host, window, root) = try await makeHost(compiled: true) { _, name, reply in
            proposed = name; reply(outputDirectory?.appendingPathComponent(name)); return {}
        }
        defer { clean(host, window, root) }
        outputDirectory = root
        let formats = [
            ("pdf", "report.pdf", Data("%PDF-synthetic-download\n".utf8)),
            ("html", "report.html", Data("<h1>Downloaded, never executed</h1>".utf8)),
            ("md", "report.md", Data("# Downloaded report\n".utf8))
        ]
        for (ext, name, bytes) in formats {
            _ = try await host.web.evaluateJavaScript("document.querySelector('a[href=\"/download.\(ext)\"]').click()")
            try await wait { (try? Data(contentsOf: root.appendingPathComponent(name))) == bytes }
            XCTAssertEqual(proposed, name)
        }
        _ = try await host.web.evaluateJavaScript("document.querySelector('#attachment button').click()")
        try await wait {
            (try? Data(contentsOf: root.appendingPathComponent("attachment.bin"))) == Data([0,1,127,128,255])
        }
        for (kind, name, bytes) in [
            ("workflow", "workflow.yaml", Data("name: Download acceptance\n".utf8)),
            ("recovery", "cognis-recovery-codes.txt", Data("synthetic-recovery-code\n".utf8)),
            ("skill", "SKILL.md", Data("# Download acceptance\n".utf8)),
            ("package", "skill.zip", Data([0,1,127,128,255])),
            ("recording", "ws-recording.jsonl", Data("{\"type\":\"synthetic-frame\"}\n".utf8))
        ] {
            _ = try await host.web.evaluateJavaScript("downloadFixture.saveBlob('\(kind)')")
            try await wait { (try? Data(contentsOf: root.appendingPathComponent(name))) == bytes }
            XCTAssertEqual(proposed, name)
        }
    }

    func testHTTPRedirectAndWebKitCookieAuthentication() async throws {
        var output: URL?
        let (host, window, root) = try await makeHost(compiled: true) { _, _, reply in reply(output); return {} }
        defer { clean(host, window, root) }
        output = root.appendingPathComponent("redirect.bin")
        _ = try await host.web.evaluateJavaScript("downloadFixture.saveUrl('/redirect')")
        try await wait { (try? Data(contentsOf: output!)) == Data([0,1,127,128,255]) }
        let cookie = try XCTUnwrap(HTTPCookie(properties: [
            .domain: "127.0.0.1", .path: "/", .name: "cognis_session", .value: "synthetic-download-cookie"
        ]))
        await host.web.configuration.websiteDataStore.httpCookieStore.setCookie(cookie)
        output = root.appendingPathComponent("cookie.bin")
        _ = try await host.web.evaluateJavaScript("downloadFixture.saveUrl('/cookie.bin')")
        try await wait { (try? Data(contentsOf: output!)) == Data([0,1,127,128,255]) }
    }

    func testDocumentInvalidationSynchronouslyRemovesStagingBeforeTermination() async throws {
        var output: URL?
        let (host, window, root) = try await makeHost(compiled: true) { _, _, reply in reply(output); return {} }
        defer { clean(host, window, root) }
        output = root.appendingPathComponent("slow.bin")
        _ = try await host.web.evaluateJavaScript("downloadFixture.saveUrl('/slow.bin')")
        try await wait {
            (try? FileManager.default.contentsOfDirectory(atPath: root.path))?.contains {
                $0.hasPrefix(".cognis-download-")
            } == true
        }
        host.invalidate()
        // Application shutdown does not run another async turn after invalidation.
        XCTAssertFalse(try FileManager.default.contentsOfDirectory(atPath: root.path).contains {
            $0.hasPrefix(".cognis-download-")
        })
        XCTAssertFalse(FileManager.default.fileExists(atPath: output!.path))
    }

    func testNativeFailuresPreserveExistingFileCleanStagingAndPermitRetry() async throws {
        var output: URL?
        let (host, window, root) = try await makeHost(compiled: true) { _, _, reply in reply(output); return {} }
        defer { clean(host, window, root) }
        let destination = root.appendingPathComponent("preserved.bin")
        output = destination
        for path in ["/denied", "/rejected-redirect", "/interrupted.bin"] {
            let original = Data("Do not replace on failure".utf8)
            try original.write(to: destination)
            _ = try await host.web.evaluateJavaScript("downloadFixture.saveUrl('\(path)')")
            try await wait { window.attachedSheet != nil }
            XCTAssertFalse(window.attachedSheet is NSSavePanel)
            XCTAssertEqual(try Data(contentsOf: destination), original)
            XCTAssertFalse(try FileManager.default.contentsOfDirectory(atPath: root.path).contains {
                $0.hasPrefix(".cognis-download-")
            })
            let sheet = try XCTUnwrap(window.attachedSheet)
            NSApp.endSheet(sheet, returnCode: NSApplication.ModalResponse.OK.rawValue)
            sheet.orderOut(nil)
            try await wait { window.attachedSheet == nil }
            _ = try await host.web.evaluateJavaScript("downloadFixture.saveUrl('/binary.bin')")
            try await wait { (try? Data(contentsOf: destination)) == Data([0,1,127,128,255]) }
        }
    }
}
