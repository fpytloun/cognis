import AppKit
import Carbon
import CompanionContract

@MainActor
final class GlobalHotKey {
    private var handler: EventHandlerRef?
    private var registration: EventHotKeyRef?
    private let action: () -> Void
    private(set) var status = "Disabled"

    init(action: @escaping () -> Void) { self.action = action }

    func register(_ preference: HotKeyPreference?) {
        unregister()
        guard let preference, preference.isValid else { status = "Disabled"; return }
        var type = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        let installed = InstallEventHandler(GetApplicationEventTarget(), { _, event, context in
            guard let context, let event else { return OSStatus(eventNotHandledErr) }
            var id = EventHotKeyID()
            let result = GetEventParameter(event, EventParamName(kEventParamDirectObject),
                EventParamType(typeEventHotKeyID), nil, MemoryLayout<EventHotKeyID>.size, nil, &id)
            guard result == noErr, id.signature == 0x434F474E, id.id == 1 else {
                return OSStatus(eventNotHandledErr)
            }
            MainActor.assumeIsolated {
                Unmanaged<GlobalHotKey>.fromOpaque(context).takeUnretainedValue().action()
            }
            return noErr
        }, 1, &type, Unmanaged.passUnretained(self).toOpaque(), &handler)
        guard installed == noErr else { status = "Could not listen for shortcut (macOS \(installed))."; return }
        let result = RegisterEventHotKey(preference.keyCode, preference.modifiers,
            EventHotKeyID(signature: 0x434F474E, id: 1), GetApplicationEventTarget(), 0, &registration)
        status = result == noErr ? "Registered — \(Self.label(preference))"
            : "Shortcut unavailable (macOS \(result)). Another app may be using it."
    }

    func unregister() {
        if let registration { UnregisterEventHotKey(registration) }
        if let handler { RemoveEventHandler(handler) }
        registration = nil; handler = nil
    }

    static func label(_ value: HotKeyPreference?) -> String {
        guard let value else { return "Disabled" }
        var label = ""
        for (flag, symbol) in [(HotKeyPreference.control, "⌃"), (HotKeyPreference.option, "⌥"),
                               (HotKeyPreference.shift, "⇧"), (HotKeyPreference.command, "⌘")] {
            if value.modifiers & flag != 0 { label += symbol }
        }
        let source = TISCopyCurrentKeyboardLayoutInputSource().takeRetainedValue()
        if let pointer = TISGetInputSourceProperty(source, kTISPropertyUnicodeKeyLayoutData) {
            let data = Unmanaged<CFData>.fromOpaque(pointer).takeUnretainedValue()
            let layout = UnsafeRawPointer(CFDataGetBytePtr(data)!).assumingMemoryBound(to: UCKeyboardLayout.self)
            var dead: UInt32 = 0
            var count = 0
            var chars = [UniChar](repeating: 0, count: 8)
            if UCKeyTranslate(layout, UInt16(value.keyCode), UInt16(kUCKeyActionDisplay), 0,
                UInt32(LMGetKbdType()), OptionBits(kUCKeyTranslateNoDeadKeysBit), &dead,
                chars.count, &count, &chars) == noErr, count > 0 {
                let text = String(utf16CodeUnits: chars, count: count)
                if !text.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains) {
                    return label + (text == " " ? "Space" : text.uppercased())
                }
            }
        }
        return label + "Key \(value.keyCode)"
    }
}

/// The control owns key capture only while it is first responder.
@MainActor
final class ShortcutRecorder: NSButton {
    private var value: HotKeyPreference?
    private let changed: (HotKeyPreference?) -> Void
    private let recording: (Bool) -> Void
    private var capturing = false
    override var acceptsFirstResponder: Bool { true }

    init(value: HotKeyPreference?, changed: @escaping (HotKeyPreference?) -> Void,
         recording: @escaping (Bool) -> Void) {
        self.value = value; self.changed = changed; self.recording = recording
        super.init(frame: .zero)
        title = GlobalHotKey.label(value); bezelStyle = .rounded
        setAccessibilityLabel("Panel keyboard shortcut")
        target = self; action = #selector(begin)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }
    @objc private func begin() {
        guard !capturing else { return }
        capturing = true; recording(true)
        title = "Press shortcut (Esc cancels, Delete clears)"
        window?.makeFirstResponder(self)
    }
    override func resignFirstResponder() -> Bool {
        if capturing { finish(value, save: false) }
        return super.resignFirstResponder()
    }
    private func finish(_ next: HotKeyPreference?, save: Bool) {
        capturing = false; value = next; title = GlobalHotKey.label(next)
        if save { changed(next) }
        recording(false)
    }
    override func keyDown(with event: NSEvent) {
        guard capturing else { super.keyDown(with: event); return }
        if event.keyCode == 53 { finish(value, save: false); return }
        if event.keyCode == 51 || event.keyCode == 117 { finish(nil, save: true); return }
        var modifiers: UInt32 = 0
        for (flag, carbon) in [(NSEvent.ModifierFlags.command, HotKeyPreference.command),
                              (.control, HotKeyPreference.control), (.option, HotKeyPreference.option),
                              (.shift, HotKeyPreference.shift)] {
            if event.modifierFlags.contains(flag) { modifiers |= carbon }
        }
        let next = HotKeyPreference(keyCode: UInt32(event.keyCode), modifiers: modifiers)
        guard next.isValid else { NSSound.beep(); return }
        finish(next, save: true)
    }
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        guard capturing else { return super.performKeyEquivalent(with: event) }
        keyDown(with: event); return true
    }
}
