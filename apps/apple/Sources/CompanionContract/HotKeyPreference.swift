import Foundation

public struct HotKeyPreference: Codable, Equatable, Sendable {
    // Carbon's public modifier bits; keep the portable contract independent of AppKit.
    public static let command: UInt32 = 1 << 8
    public static let shift: UInt32 = 1 << 9
    public static let option: UInt32 = 1 << 11
    public static let control: UInt32 = 1 << 12
    public static let `default` = HotKeyPreference(keyCode: 8, modifiers: command | option | control)
    public let keyCode: UInt32
    public let modifiers: UInt32
    public init(keyCode: UInt32, modifiers: UInt32) {
        self.keyCode = keyCode; self.modifiers = modifiers
    }
    public var isValid: Bool {
        keyCode <= 126 && ![36, 48, 51, 53, 55, 56, 57, 58, 59, 60, 61, 62, 63, 117].contains(keyCode)
            && modifiers & (Self.command | Self.control | Self.option) != 0
            && modifiers & ~(Self.command | Self.shift | Self.option | Self.control) == 0
    }
    public static func load(from defaults: UserDefaults) -> Self? {
        guard let data = defaults.data(forKey: "panelHotKey") else { return .default }
        return (try? JSONDecoder().decode(Self.self, from: data)).flatMap { $0.isValid ? $0 : nil }
    }
    public static func save(_ value: Self?, to defaults: UserDefaults) throws {
        // JSON null is the explicit disabled state; absence retains the default.
        defaults.set(try JSONEncoder().encode(value), forKey: "panelHotKey")
    }
}
