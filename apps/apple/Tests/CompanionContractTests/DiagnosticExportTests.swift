import XCTest
@testable import CompanionContract

final class DiagnosticExportTests: XCTestCase {
    let generation = "8D6FEAFB-2AAC-4B41-AC82-42B2314563BB"
    let filename = "cognis-chat-diagnostic-conv_native.json"
    let json = #"{"schema":"cognis-chat-client-diagnostic-v1","scope":{"conversation_id":"conv_native"}}"#

    func message(_ payload: [String: Any]) -> [String: Any] {
        ["version": 1, "generation": generation, "type": "save-diagnostic", "payload": payload]
    }

    func testTypedJSONSavePreservesBytesAndAllowsRealisticDiagnosticSizes() throws {
        let large = String(json.dropLast()) + #","metadata_padding":""# + String(repeating: "x", count: 100_000) + #""}"#
        let decoded = try BridgeMessage.decode(message(["filename": filename, "json": large]),
                                               generation: generation, trustedMainFrame: true)
        guard case .saveDiagnostic(let file) = decoded else { return XCTFail("Expected a diagnostic export") }
        XCTAssertEqual(file.filename, filename)
        XCTAssertEqual(file.data, large.data(using: .utf8))
        XCTAssertNoThrow(try DiagnosticExport.decode(["filename": "cognis-chat-diagnostic.json", "json": json]))
    }

    func testRejectsPathsURLsWrongTypesAndUnsafeFilenames() {
        for name in ["../" + filename, "/tmp/" + filename, "https://host.test/" + filename,
                     "diagnostic.html", filename + "\n", "cognis-chat-diagnostic-a/b.json",
                     "cognis-chat-diagnostic-" + String(repeating: "a", count: 129) + ".json"] {
            XCTAssertThrowsError(try DiagnosticExport.decode(["filename": name, "json": json]), name)
        }
        for payload: [String: Any] in [
            ["filename": filename, "json": json, "destination": "/tmp/output.json"],
            ["filename": filename, "url": "blob:https://host.test/data"],
            ["filename": filename, "json": ["schema": "cognis-chat-client-diagnostic-v1"]],
            ["filename": filename, "json": "{}"],
            ["filename": filename, "json": "not JSON"],
            ["filename": filename, "json": #"{"schema":"another-format"}"#],
        ] {
            XCTAssertThrowsError(try DiagnosticExport.decode(payload))
        }
    }

    func testRejectsOversizedUnicodeContentAndUntrustedBridgeContexts() {
        let oversized = String(repeating: "é", count: DiagnosticExport.maximumFileBytes / 2 + 1)
        XCTAssertThrowsError(try DiagnosticExport.decode(["filename": filename, "json": oversized]))
        let body = message(["filename": filename, "json": json])
        XCTAssertThrowsError(try BridgeMessage.decode(body, generation: generation, trustedMainFrame: false))
        XCTAssertThrowsError(try BridgeMessage.decode(body, generation: UUID().uuidString, trustedMainFrame: true))
        var invalid = body
        invalid["version"] = 2
        XCTAssertThrowsError(try BridgeMessage.decode(invalid, generation: generation, trustedMainFrame: true))
        invalid = body
        invalid["destination"] = "/tmp/implicit.json"
        XCTAssertThrowsError(try BridgeMessage.decode(invalid, generation: generation, trustedMainFrame: true))
    }
}
