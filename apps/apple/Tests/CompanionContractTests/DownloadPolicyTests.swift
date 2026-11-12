import XCTest
@testable import CompanionContract

final class DownloadPolicyTests: XCTestCase {
    func testAllowsOnlyWebDownloadsAndSameOriginBlobs() throws {
        let profile = try ServerProfile("https://downloads.cognis.test")
        for value in [
            "https://downloads.cognis.test/file.pdf?signature=abc",
            "https://files.cognis.test/archive.zip",
            "http://127.0.0.1:18867/file",
            "blob:https://downloads.cognis.test/8d6feafb-2aac-4b41-ac82-42b2314563bb"
        ] {
            XCTAssertTrue(isDownloadURL(try XCTUnwrap(URL(string: value)), profile: profile), value)
        }
        for value in [
            "file:///etc/passwd", "data:text/html,hello", "javascript:alert(1)",
            "cognisbundle://app/index.html", "http://remote.cognis.test/file",
            "https://user:password@downloads.cognis.test/file",
            "blob:https://other.cognis.test/8d6feafb-2aac-4b41-ac82-42b2314563bb",
            "blob:https://downloads.cognis.test.evil/8d6feafb-2aac-4b41-ac82-42b2314563bb",
            "blob:null/8d6feafb-2aac-4b41-ac82-42b2314563bb",
            "blob:https://downloads.cognis.test/../file"
        ] {
            XCTAssertFalse(isDownloadURL(try XCTUnwrap(URL(string: value)), profile: profile), value)
        }
    }

    func testSuggestedNamesAreNotPathsAndRetainUnicode() {
        XCTAssertEqual(downloadFilename("../../report.pdf"), "report.pdf")
        XCTAssertEqual(downloadFilename("C:\\files\\report.pdf"), "report.pdf")
        XCTAssertEqual(downloadFilename("výsledek.json"), "výsledek.json")
        XCTAssertEqual(downloadFilename("bad:\nname.txt"), "badname.txt")
        XCTAssertEqual(downloadFilename(".."), "download")
        XCTAssertEqual(downloadFilename(""), "download")
        XCTAssertLessThanOrEqual(downloadFilename(String(repeating: "é", count: 200)).utf8.count, 240)
    }
}
