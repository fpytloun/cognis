import AppKit
import XCTest
import Darwin

/// Runs unmodified XCTest cases inside a real foreground AppKit lifecycle.
@MainActor
final class GUIHost: NSObject, NSApplicationDelegate {
    private let configuration: [String: Any]
    private var window: NSWindow?

    init(configuration: [String: Any]) {
        self.configuration = configuration
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let anchor = NSWindow(contentRect: NSRect(x: 100, y: 100, width: 400, height: 160),
                              styleMask: [.titled], backing: .buffered, defer: false)
        anchor.title = "Cognis native GUI acceptance"
        anchor.isReleasedWhenClosed = false
        window = anchor
        anchor.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        var attempts = 0
        Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [self] timer in
            MainActor.assumeIsolated {
                attempts += 1
                if NSApp.isActive && anchor.isKeyWindow {
                    timer.invalidate()
                    runTests()
                } else if attempts >= 100 {
                    timer.invalidate()
                    finish(["error": "GUI host could not acquire a real key window"], success: false)
                }
            }
        }
    }

    private func runTests() {
        do {
            guard let path = configuration["bundle"] as? String,
                  let bundle = Bundle(path: path),
                  let pattern = configuration["pattern"] as? String else {
                throw NSError(domain: "GUIHost", code: 1)
            }
            for (key, value) in configuration["environment"] as? [String: String] ?? [:] {
                setenv(key, value, 1)
            }
            try bundle.loadAndReturnError()
            func leaves(_ test: XCTest) -> [XCTest] {
                if let suite = test as? XCTestSuite { return suite.tests.flatMap(leaves) }
                return [test]
            }
            let cases = leaves(XCTestSuite.default).filter {
                $0.name.range(of: pattern, options: .regularExpression) != nil
            }
            guard !cases.isEmpty else { throw NSError(domain: "GUIHost", code: 2) }
            let suite = XCTestSuite(name: "Cognis foreground GUI acceptance")
            cases.forEach(suite.addTest)
            print("GUI_HOST active=\(NSApp.isActive) key=\(window?.isKeyWindow == true) selected=\(cases.count)")
            suite.run()
            guard let run = suite.testRun else { throw NSError(domain: "GUIHost", code: 3) }
            let success = run.hasSucceeded && run.skipCount == 0 && run.executionCount == cases.count
            finish(["selected": cases.count, "executed": run.executionCount,
                    "failures": run.totalFailureCount, "skipped": run.skipCount], success: success)
        } catch {
            finish(["error": String(describing: error)], success: false)
        }
    }

    private func finish(_ result: [String: Any], success: Bool) -> Never {
        var result = result
        result["success"] = success
        if let path = configuration["result"] as? String,
           let data = try? JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: URL(fileURLWithPath: path), options: .atomic)
        }
        window?.orderOut(nil)
        fflush(stdout)
        fflush(stderr)
        exit(success ? 0 : 1)
    }
}

MainActor.assumeIsolated {
    guard let url = Bundle.main.url(forResource: "configuration", withExtension: "json"),
          let data = try? Data(contentsOf: url),
          let configuration = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        fatalError("Missing GUI test host configuration")
    }
    let application = NSApplication.shared
    _ = application.setActivationPolicy(.regular)
    let host = GUIHost(configuration: configuration)
    application.delegate = host
    application.run()
}
