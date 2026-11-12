import AppKit
import CompanionContract

/// Client-local controls. Callbacks retain the existing management owners.
@MainActor
final class NativeSettingsWindow: NSWindowController {
    private let shortcutStatus = NSTextField(wrappingLabelWithString: "")
    private var displayChanged: ((String) -> Void)?
    func updateShortcutStatus(_ status: String) { shortcutStatus.stringValue = status }

    init(railVisible: Bool, left: Bool,
         displays: [(id: String, name: String)], selectedDisplay: String?,
         displayChanged: @escaping (String) -> Void, previews: Bool,
         rail: @escaping () -> Void, side: @escaping (Bool) -> Void,
         tuck: @escaping () -> Void, preview: @escaping () -> Void,
         notifications: @escaping () -> Void, startup: NSMenu,
         shortcut: HotKeyPreference?, shortcutStatus: String,
         shortcutChanged: @escaping (HotKeyPreference?) -> Void,
         shortcutRecording: @escaping (Bool) -> Void) {
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 480, height: 590),
            styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "Settings"
        window.isReleasedWhenClosed = false
        super.init(window: window)
        self.displayChanged = displayChanged
        let stack = NSStackView()
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 12
        stack.edgeInsets = NSEdgeInsets(top: 24, left: 24, bottom: 24, right: 24)
        func heading(_ text: String) {
            let label = NSTextField(labelWithString: text)
            label.font = .boldSystemFont(ofSize: 13); stack.addArrangedSubview(label)
        }
        func button(_ title: String, checked: Bool? = nil, action: @escaping () -> Void) {
            let control = SettingsButton(title: title, checked: checked, action: action)
            stack.addArrangedSubview(control)
        }
        heading("Rail / Display")
        button("Show desktop chats", checked: railVisible, action: rail)
        let displayPicker = NSPopUpButton()
        for (index, display) in displays.enumerated() {
            displayPicker.addItem(withTitle: "\(index + 1): \(display.name)")
            displayPicker.lastItem?.representedObject = display.id
            if display.id == selectedDisplay { displayPicker.selectItem(at: index) }
        }
        displayPicker.target = self; displayPicker.action = #selector(selectDisplay(_:))
        displayPicker.setAccessibilityLabel("Avatar display")
        let displayRow = NSStackView(views: [NSTextField(labelWithString: "Display:"), displayPicker])
        displayRow.spacing = 8
        stack.addArrangedSubview(displayRow)
        button("Dock avatars on the left", checked: left) { side(true) }
        button("Dock avatars on the right", checked: !left) { side(false) }
        button("Toggle half-hide avatars", action: tuck)
        heading("Keyboard shortcut")
        stack.addArrangedSubview(ShortcutRecorder(value: shortcut, changed: shortcutChanged, recording: shortcutRecording))
        self.shortcutStatus.stringValue = shortcutStatus
        self.shortcutStatus.textColor = .secondaryLabelColor
        stack.addArrangedSubview(self.shortcutStatus)
        stack.addArrangedSubview(NSTextField(wrappingLabelWithString:
            "Opens the last agent's last selected panel tab. Cognis must be running; enable its login item below."))
        heading("Notifications")
        button("Show reply previews", checked: previews, action: preview)
        button("macOS notification controls…", action: notifications)
        heading("Startup")
        button("Startup options…") { startup.popUp(positioning: nil, at: NSEvent.mouseLocation, in: nil) }
        let note = NSTextField(wrappingLabelWithString:
            "These controls apply only to this Mac. Half-hide is saved by the main web workspace. macOS controls notification visibility.")
        note.textColor = .secondaryLabelColor; stack.addArrangedSubview(note)
        window.contentView = stack
        window.center()
    }
    @objc private func selectDisplay(_ sender: NSPopUpButton) {
        guard let id = sender.selectedItem?.representedObject as? String else { return }
        displayChanged?(id)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }
}

@MainActor
private final class SettingsButton: NSButton {
    private let run: () -> Void
    init(title: String, checked: Bool?, action: @escaping () -> Void) {
        run = action
        super.init(frame: .zero)
        self.title = title; target = self; self.action = #selector(invoke)
        if let checked { setButtonType(.switch); state = checked ? .on : .off }
        else { bezelStyle = .rounded }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }
    @objc private func invoke() { run() }
}
