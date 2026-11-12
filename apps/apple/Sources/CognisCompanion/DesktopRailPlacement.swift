import AppKit
import CoreGraphics

/// Server-local placement of the desktop rail on a physical display.
@MainActor
struct DesktopRailPlacement {
    struct Display {
        let id: String
        let legacyID: String
        let frame: NSRect
        let visibleFrame: NSRect
    }

    let defaults: UserDefaults
    let key: String

    static func display(_ screen: NSScreen) -> Display {
        let number = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as! NSNumber
        let uuid = CGDisplayCreateUUIDFromDisplayID(number.uint32Value).takeRetainedValue()
        return Display(id: CFUUIDCreateString(nil, uuid) as String, legacyID: number.stringValue,
            frame: screen.frame, visibleFrame: screen.visibleFrame)
    }

    func selected(in displays: [Display]) -> Display? {
        if let id = defaults.string(forKey: key + "-displayUUID") {
            return displays.first { $0.id == id } ?? displays.first
        }
        // Migrate installed clients' numeric display IDs while those defaults remain supported.
        if let legacy = defaults.string(forKey: key + "-display"),
           let display = displays.first(where: { $0.legacyID == legacy }) {
            select(display)
            return display
        }
        return displays.first
    }

    func select(_ display: Display) {
        defaults.set(display.id, forKey: key + "-displayUUID")
    }

    func frame(on display: Display, size: NSSize) -> NSRect {
        let visible = display.visibleFrame
        let height = min(size.height, max(48, visible.height - 16))
        let ratio = defaults.object(forKey: key + "-vertical") as? Double ?? 0.5
        let left = defaults.string(forKey: key + "-side") == "left"
        return NSRect(x: left ? visible.minX : visible.maxX - size.width,
            y: visible.minY + 8 + max(0, visible.height - height - 16) * min(max(ratio, 0), 1),
            width: size.width, height: height)
    }

    func move(y: CGFloat, on display: Display, size: NSSize) {
        let height = frame(on: display, size: size).height
        let range = max(1, display.visibleFrame.height - height - 16)
        defaults.set(min(max((y - display.visibleFrame.minY - 8) / range, 0), 1),
            forKey: key + "-vertical")
    }

    func drag(pointer: NSPoint, y: CGFloat, displays: [Display], size: NSSize) {
        guard let display = displays.first(where: { $0.frame.contains(pointer) }) else { return }
        select(display)
        move(y: y, on: display, size: size)
    }
}
