import XCTest
import WebKit
import AppKit
import CompanionContract
@testable import CognisCompanion

@MainActor
final class ArtifactPreviewTests: XCTestCase {
    func testPdfZoomAndFitWithProductionStyles() async throws {
        guard let fixture = ProcessInfo.processInfo.environment["COGNIS_ARTIFACT_PREVIEW_FIXTURE"] else {
            throw XCTSkip("Set COGNIS_ARTIFACT_PREVIEW_FIXTURE to the compiled synthetic fixture.")
        }
        let profile = try ServerProfile("https://artifact-preview.invalid",
            storageNamespace: "cz.fpy.cognis.tests.pdf-zoom-\(UUID())")
        let host = try BundleWebView(profile: profile, root: URL(fileURLWithPath: fixture))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1200, height: 800),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = host.web
        window.orderFront(nil)
        defer { host.invalidate(); window.orderOut(nil) }
        host.start(path: "/login")
        try await wait(host.web, "!!window.artifactPreviewFixture")
        _ = try await host.web.callAsyncJavaScript("return await artifactPreviewFixture.open('a4-pdf')",
            arguments: [:], in: nil, contentWorld: .page)
        try await wait(host.web, """
            !document.querySelector('[aria-label="Actual size (100%)"]').disabled &&
            document.querySelector('canvas').style.width !== '' &&
            document.querySelector('canvas').parentElement.clientWidth > 100 &&
            document.querySelector('canvas').parentElement.clientHeight > 100
            """)
        try await wait(host.web, """
            (() => { const c = document.querySelector('canvas'); const v = c?.parentElement;
            return c?.width > 0 && c.getBoundingClientRect().height <= v.clientHeight &&
            c.getBoundingClientRect().width <= v.clientWidth &&
            Number(document.querySelector('[aria-label="Zoom percentage"]').value) <= 100; })()
            """)
        _ = try await host.web.evaluateJavaScript("document.querySelector('[aria-label=\"Actual size (100%)\"]').click()")
        try await wait(host.web, "Math.abs(document.querySelector('canvas').getBoundingClientRect().width - 595*96/72) < 1")
        _ = try await host.web.evaluateJavaScript("document.querySelector('[aria-label=\"Zoom out\"]').click()")
        try await wait(host.web, "Math.abs(document.querySelector('canvas').getBoundingClientRect().width - 595) < 1")
        _ = try await host.web.evaluateJavaScript("""
            const input = document.querySelector('[aria-label="Zoom percentage"]');
            input.value = '25'; input.dispatchEvent(new Event('change', {bubbles:true}));
            """)
        try await wait(host.web, "Math.abs(document.querySelector('canvas').getBoundingClientRect().width - 595/3) < 1")
        _ = try await host.web.evaluateJavaScript("document.getElementById('fixture').style.width = '400px'")
        _ = try await host.web.evaluateJavaScript("[...document.querySelectorAll('button')].find(b => b.textContent === 'Next').click()")
        try await wait(host.web, """
            document.body.textContent.includes('Page 2 / 2') &&
            Math.abs(document.querySelector('canvas').getBoundingClientRect().width - 595/3) < 1
            """)
        _ = try await host.web.evaluateJavaScript("document.querySelector('[aria-label=\"Actual size (100%)\"]').click()")
        try await wait(host.web, """
            (() => { const c = document.querySelector('canvas'); return (
            Math.abs(c.getBoundingClientRect().width - 595*96/72) < 1 &&
            c.parentElement.scrollWidth > c.parentElement.clientWidth); })()
            """)
        _ = try await host.web.evaluateJavaScript("[...document.querySelectorAll('button')].find(b => b.textContent === 'Fit page').click()")
        try await wait(host.web, """
            (() => { const c = document.querySelector('canvas'); return (
            c.getBoundingClientRect().width <= c.parentElement.clientWidth &&
            c.getBoundingClientRect().height <= c.parentElement.clientHeight); })()
            """)
        XCTAssertTrue(host.assets.failed.isEmpty)
        print("ARTIFACT_PREVIEW_ZOOM_PASS actual-size zoom-out numeric resize overflow fit-page")
    }

    func testSupportedRenderersUnderNativePolicy() async throws {
        guard let fixture = ProcessInfo.processInfo.environment["COGNIS_ARTIFACT_PREVIEW_FIXTURE"] else {
            throw XCTSkip("Set COGNIS_ARTIFACT_PREVIEW_FIXTURE to the compiled synthetic fixture.")
        }
        let profile = try ServerProfile("https://artifact-preview.invalid",
            storageNamespace: "cz.fpy.cognis.tests.artifact-preview-\(UUID())")
        let host = try BundleWebView(profile: profile, root: URL(fileURLWithPath: fixture))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 900, height: 700),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = host.web
        window.orderFront(nil)
        defer { host.invalidate(); window.orderOut(nil) }
        host.start(path: "/login")
        try await wait(host.web, "!!window.artifactPreviewFixture")
        let resources = try await host.web.callAsyncJavaScript("""
            try { return await artifactPreviewFixture.validateResources(); }
            catch (e) { return String(e); }
            """,
            arguments: [:], in: nil, contentWorld: .page)
        print("ARTIFACT_PREVIEW_RESOURCES \(resources ?? "unavailable")")
        XCTAssertEqual(resources as? Bool, true)
        print("ARTIFACT_PREVIEW_BINARY_RESOURCES_PASS")
        let checks = [
            ("pdf", """
             document.body.textContent.includes('Page 1 / 2') &&
             (() => { const c = document.querySelector('canvas'); if (!c || !c.width) return false;
             const p = c.getContext('2d').getImageData(Math.floor(c.width/6), Math.floor(c.height*5/6), 1, 1).data;
             return p[0] > 200 && p[1] < 50; })()
             """),
            ("jpx-pdf", """
             document.body.textContent.includes('Page 1 / 2') &&
             (() => { const c = document.querySelector('canvas'); if (!c || !c.width) return false;
             const p = c.getContext('2d').getImageData(Math.floor(c.width/6), Math.floor(c.height*5/6), 1, 1).data;
             return p[0] > 200 && p[1] < 50; })()
             """),
            ("image", "document.querySelector('img')?.naturalWidth === 10"),
            ("markdown", "[...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].some(h => h.textContent.includes('Native Markdown')) && document.querySelector('strong')?.textContent === 'Rendered content'"),
            ("csv", "document.querySelector('table')?.textContent.includes('Native CSV')"),
            ("mermaid", "!!document.querySelector('svg')"),
            ("text", "document.querySelector('pre')?.textContent.includes('\"native\"')"),
            ("audio", "document.querySelector('audio')?.readyState >= 2"),
            ("video", "document.querySelector('video')?.videoWidth === 64"),
            ("invalid-pdf", "document.querySelector('[role=alert]')?.textContent.includes('The PDF could not be loaded')")
        ]
        for (kind, expression) in checks {
            do {
                _ = try await host.web.callAsyncJavaScript("return await artifactPreviewFixture.open(kind)",
                    arguments: ["kind": kind], in: nil, contentWorld: .page)
            } catch {
                print("ARTIFACT_PREVIEW_OPEN_ERROR \(kind) \((error as NSError).userInfo)")
                throw error
            }
            try await wait(host.web, expression)
            if kind == "pdf" {
                _ = try await host.web.evaluateJavaScript("""
                    [...document.querySelectorAll('button')].find(b => b.textContent === 'Next').click()
                    """)
                try await wait(host.web, "document.body.textContent.includes('Page 2 / 2')")
            }
            XCTAssertTrue(host.assets.failed.isEmpty, "Missing bundle assets: \(host.assets.failed)")
            print("ARTIFACT_PREVIEW_PASS \(kind)")
        }
        _ = try await host.web.callAsyncJavaScript("return await artifactPreviewFixture.close()",
            arguments: [:], in: nil, contentWorld: .page)
        let closed = try await host.web.evaluateJavaScript("document.querySelector('canvas') === null")
        XCTAssertEqual(closed as? Bool, true)
        try await wait(host.web, "artifactPreviewFixture.activeWorkers === 0")
        let violations = try await host.web.evaluateJavaScript("artifactPreviewFixture.diagnostics.filter(v => v.startsWith('CSP ') || v.startsWith('Worker:'))")
        XCTAssertEqual((violations as? [String]) ?? ["Missing diagnostic report"], [])
    }

    private func wait(_ web: WKWebView, _ expression: String) async throws {
        for _ in 0..<200 {
            if (try? await web.evaluateJavaScript(expression)) as? Bool == true { return }
            try await Task.sleep(for: .milliseconds(50))
        }
        let diagnostic = try? await web.evaluateJavaScript("""
            JSON.stringify({text:document.querySelector('#fixture').textContent.slice(0,1000),
            canvas:document.querySelector('canvas')?.getAttribute('width'),
            geometry:(() => { const c=document.querySelector('canvas'); return c ? {
                css:c.style.cssText,rect:c.getBoundingClientRect().toJSON(),
                viewport:c.parentElement.getBoundingClientRect().toJSON(),
                display:getComputedStyle(c.parentElement.parentElement).display,
                percent:document.querySelector('[aria-label="Zoom percentage"]').value,
                actualDisabled:document.querySelector('[aria-label="Actual size (100%)"]').disabled
            } : null; })(),
            pixel:(() => { const c=document.querySelector('canvas'); return c?.width ?
                [...c.getContext('2d').getImageData(Math.floor(c.width/6),Math.floor(c.height*5/6),1,1).data] : null; })(),
            errors:window.__cognisNativeStartupErrors, diagnostics:window.artifactPreviewFixture?.diagnostics})
            """)
        print("ARTIFACT_PREVIEW_DIAGNOSTIC \(diagnostic ?? "unavailable")")
        XCTFail("Timed out: \(expression)")
        throw NSError(domain: "ArtifactPreviewAcceptance", code: 1)
    }
}
