import AppKit
import WebKit
import Carbon
import CompanionContract

@MainActor
final class DesktopPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

@MainActor
final class CompanionApp: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private var statusItem: NSStatusItem!
    private var mainWindow: NSWindow!
    private var compactWindow: DesktopPanel!
    private var railWindow: DesktopPanel?
    private var railWeb: BundleWebView?
    private var railSize = NSSize(width: 64, height: 64)
    private var railDragOrigin: (frameY: CGFloat, pointerY: CGFloat)?
    private var railPointerDown: (frameY: CGFloat, pointerY: CGFloat)?
    private var railPresentationRevision = 0
    private var railHasContent = false
    private var bundleWeb: BundleWebView?
    private var compactWeb: BundleWebView?
    private let standalone = StandaloneDocuments()
    private var conversationWindows: [String: NSWindow] { standalone.windows }
    private var conversationWeb: [String: BundleWebView] { standalone.hosts }
    private var workspaceRelay: WorkspaceRelay?
    private var failedDocuments = Set<String>()
    private var profile: ServerProfile?
    private var snapshot: Snapshot?
    private var epoch = 0
    private var currentPath = "/"
    private var mainPath = "/"
    private var compactConversation: String?
    private var compactAgent: String?
    private var compactDirect = false
    private var compactFocusRequest: String?
    private var previousApplication: NSRunningApplication?
    private var resident = false
    private var ready = false
    private var desktopVisible = true
    private var terminating = false
    private var stateLabel = "Not connected"
    private var visibilityObservers: [NSObjectProtocol] = []
    private var localClicks: Any?
    private var globalClicks: Any?
    private var interactionDepth = 0
    private var mainSurface: WindowSurface?
    private var compactSurface: WindowSurface?
    private var activationObserver: NSObjectProtocol?
    private var wakeObserver: NSObjectProtocol?
    private let defaults = UserDefaults.standard
    private var updates: UpdateCoordinator!
    private let executor = LocalExecutorAdapter()
    private var notifications: NativeNotificationCoordinator!
    private var settings: NativeSettingsWindow?
    private var pendingReview: NotificationReview?
    private var login: LoginCoordinator!
    private var launchPolicy = LaunchPolicy(loginItem: false, startInBackground: false)
    private var compatibilityTask: Task<Void, Never>?
    private var configuration: PackageConfiguration?
    private var servers: ServerSelectionCoordinator!
    private var launchURL: URL?
    private var pendingLink: AppLink.Destination?
    private lazy var hotKey = GlobalHotKey { [weak self] in self?.toggleLastPanel() }
    private var appName: String { configuration?.displayName ?? "Cognis (Invalid Package)" }

    func applicationWillFinishLaunching(_ notification: Notification) {
        NSAppleEventManager.shared().setEventHandler(self, andSelector: #selector(handleURL(_:withReplyEvent:)),
            forEventClass: AEEventClass(kInternetEventClass), andEventID: AEEventID(kAEGetURL))
    }

    @objc private func handleURL(_ event: NSAppleEventDescriptor, withReplyEvent reply: NSAppleEventDescriptor) {
        guard let text = event.paramDescriptor(forKeyword: AEKeyword(keyDirectObject))?.stringValue,
              text.utf8.count <= 1024, let url = URL(string: text) else { return }
        guard configuration != nil, profile != nil else { launchURL = url; return }
        receiveURL(url)
    }

    private func receiveURL(_ url: URL) {
        guard let profile, let configuration else { return }
        do {
            pendingLink = try AppLink(url: url, profile: profile, flavor: configuration.flavor).destination
            drainPendingLink()
            if snapshot?.authenticated == false { openMain() }
        } catch AppLink.LinkError.serverMismatch {
            showError("This link belongs to another server (\(url.host ?? "")). Connected to \(profile.origin.absoluteString). No server was switched.")
        } catch { showError("This Cognis link is not supported or is malformed.") }
    }

    private func drainPendingLink() {
        guard ready, snapshot?.authenticated == true, let destination = pendingLink else { return }
        // Authentication publishes before workspace restoration and agent discovery.
        if case .panel = destination, snapshot?.panelTargets?.ready != true { return }
        pendingLink = nil
        open(destination)
    }

    private func open(_ destination: AppLink.Destination) {
        guard snapshot?.authenticated == true else { return }
        switch destination {
        case .main(let route): openMain(); navigate(route)
        case .window(let target): openStandaloneWindow(target)
        case .panel(let requested, let conversation, let direct):
            guard let snapshot, let profile else { return }
            switch PanelTarget.presentation(requested: requested, conversation: conversation, direct: direct,
                lastAgent: defaults.string(forKey: PanelTarget.historyKey(profile: profile)), snapshot: snapshot) {
            case .pending: pendingLink = destination
            case .unavailable: showError("No available agent. Create or enable an agent in Cognis first.")
            case .unretainedConversation:
                showError("This conversation is not a retained tab for this agent. Open its /chat/ link, or pin the conversation first.")
            case .open(let agent, let conversation, let direct):
                openAgent(agent, conversation: conversation, direct: direct)
            }
        }
    }

    @objc private func toggleLastPanel() {
        guard profile != nil else { openMain(); return }
        pendingLink = .panel(agent: nil, conversation: nil, direct: false)
        drainPendingLink()
        if snapshot?.authenticated == false { openMain() }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        guard let configuration = try? PackageConfiguration(info: Bundle.main.infoDictionary ?? [:]) else {
            showError("Invalid package flavor or server restriction. No server connection was opened.")
            return
        }
        self.configuration = configuration
        hotKey.register(configuration.flavor == .acceptance ? nil : HotKeyPreference.load(from: defaults))
        servers = ServerSelectionCoordinator(configuration: configuration, defaults: defaults)
        updates = UpdateCoordinator()
        login = LoginCoordinator()
        launchPolicy = LaunchPolicy(loginItem: LoginCoordinator.launchedAtLogin, startInBackground: login.startInBackground)
        login.onChange = { [weak self] in self?.updateMenu() }
        notifications = NativeNotificationCoordinator.system()
        notifications.onReview = { [weak self] review in self?.reviewNotification(review) }
        notifications.onAction = { [weak self] review, decision in
            guard let self, let approval = review.approval, self.snapshot?.authenticated == true,
                  self.profile?.origin.absoluteString == review.profile, self.ready else {
                self?.reviewNotification(review); return
            }
            self.bundleWeb?.send(["type": "notification-action", "account": review.account,
                "scope": review.scope as Any? ?? NSNull(), "actionId": approval.actionId,
                "revision": approval.revision, "submissionId": approval.submissionId, "decision": decision])
        }
        Task { @MainActor in
            await notifications.refreshPermission() // Read-only; never requests authorization.
            NSLog("Cognis notification authorization status: %@", notifications.authorization)
        }
        notifications.isObserved = { [weak self] target in
            guard let self, target.kind == "chat" else { return false }
            @MainActor func observed(_ window: NSWindow?) -> Bool {
                window?.isKeyWindow == true && window?.isVisible == true
                    && window?.occlusionState.contains(.visible) == true
            }
            return observed(self.mainWindow) && self.currentPath == "/chat/" + target.target
                || observed(self.compactWindow) && self.compactConversation == target.target
                || observed(self.conversationWindows[target.target])
        }
        executor.onChange = { [weak self] in self?.updateMenu() }
        installApplicationMenu()
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        let template = NSImage(size: NSSize(width: 18, height: 18))
        for name in ["CognisMenuTemplate", "CognisMenuTemplate@2x"] {
            if let url = Bundle.main.url(forResource: name, withExtension: "png"),
               let data = try? Data(contentsOf: url),
               let representation = NSBitmapImageRep(data: data) {
                representation.size = NSSize(width: 18, height: 18)
                template.addRepresentation(representation)
            }
        }
        template.isTemplate = true
        template.accessibilityDescription = appName
        statusItem.button?.image = template
        statusItem.button?.toolTip = appName + " — " + configuration.flavor.rawValue
        desktopVisible = defaults.object(forKey: "desktopVisible") as? Bool ?? true
        createWindows()
        activationObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main
        ) { [weak self] notification in
            let pid = (notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication)?.processIdentifier
            MainActor.assumeIsolated {
                guard let pid, pid != ProcessInfo.processInfo.processIdentifier else { return }
                self?.previousApplication = nil
                self?.outsideClick(nil)
            }
        }
        wakeObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.bundleWeb?.send(["type": "resume"]) }
        }
        for (name, delta) in [(NSMenu.didBeginTrackingNotification, 1), (NSMenu.didEndTrackingNotification, -1)] {
            visibilityObservers.append(NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) {
                [weak self] _ in MainActor.assumeIsolated {
                    guard let self else { return }
                    self.interactionDepth = max(0, self.interactionDepth + delta)
                }
            })
        }
        localClicks = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) {
            [weak self] event in
            // AX/WebKit may synthesize a local action without an NSWindow.
            // It is not proof of a genuine click outside this application.
            if let self, let window = event.window {
                if window === self.railWindow, event.type == .leftMouseDown {
                    self.railPointerDown = (window.frame.minY, NSEvent.mouseLocation.y)
                }
                self.outsideClick(window)
            }
            return event
        }
        globalClicks = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) {
            [weak self] _ in
            MainActor.assumeIsolated { self?.outsideClick(nil) }
        }
        visibilityObservers.append(NotificationCenter.default.addObserver(
            forName: NSApplication.didResignActiveNotification, object: nil, queue: .main
        ) { [weak self] _ in MainActor.assumeIsolated { self?.outsideClick(nil) } })
        for name in [NSWindow.didBecomeKeyNotification, NSWindow.didResignKeyNotification,
                     NSWindow.didChangeOcclusionStateNotification, NSWindow.didMiniaturizeNotification,
                     NSWindow.didDeminiaturizeNotification, NSApplication.didBecomeActiveNotification,
                     NSApplication.didResignActiveNotification] {
            visibilityObservers.append(NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) {
                [weak self] _ in MainActor.assumeIsolated {
                    self?.publishVisibility(); self?.publishRailState()
                }
            })
        }
        visibilityObservers.append(NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main
        ) { [weak self] _ in MainActor.assumeIsolated { self?.clampAll() } })
        updateMenu()
        let panelLaunch = launchURL?.path == "/native-pinned"
        if let value = servers.initialSelection(background: launchPolicy.background || panelLaunch) {
            loadProfile(value, background: launchPolicy.background || panelLaunch)
        }
        if let url = launchURL, profile != nil { launchURL = nil; receiveURL(url) }
        updateMenu()
    }

    private func installApplicationMenu() {
        let app = NSMenu(title: appName)
        app.addItem(withTitle: "About " + appName, action: #selector(about), keyEquivalent: "")
        app.addItem(withTitle: "Open " + appName, action: #selector(openMain), keyEquivalent: "1")
        let updateItem = app.addItem(withTitle: "Updates", action: nil, keyEquivalent: "")
        updateItem.submenu = updates.menu()
        let executorItem = app.addItem(withTitle: "Local Executor", action: nil, keyEquivalent: "")
        executorItem.submenu = executor.menu()
        let startupItem = app.addItem(withTitle: "Startup", action: nil, keyEquivalent: "")
        startupItem.submenu = login.menu()
        app.addItem(withTitle: "Settings…", action: #selector(showSettings), keyEquivalent: ",")
        if let item = servers.menuItem(target: self, action: #selector(selectServer), key: "") { app.addItem(item) }
        app.addItem(.separator())
        app.addItem(withTitle: "Quit " + appName, action: #selector(quit), keyEquivalent: "q")
        NSApp.mainMenu = applicationMenu(application: app)
    }

    private func createWindows() {
        mainWindow = NSWindow(contentRect: NSRect(x: 100, y: 100, width: 1180, height: 820),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                              backing: .buffered, defer: false)
        mainWindow.title = appName
        mainWindow.titleVisibility = .hidden
        mainWindow.titlebarAppearsTransparent = true
        mainWindow.isMovableByWindowBackground = false
        mainWindow.minSize = NSSize(width: 680, height: 500)
        mainWindow.isReleasedWhenClosed = false
        mainWindow.setFrameAutosaveName("CognisMain")
        mainWindow.delegate = self
        compactWindow = DesktopPanel(contentRect: NSRect(x: 160, y: 160, width: 450, height: 640),
            styleMask: [.borderless, .resizable, .nonactivatingPanel], backing: .buffered, defer: false)
        compactWindow.title = "Cognis Chat"
        compactWindow.minSize = NSSize(width: 360, height: 400)
        compactWindow.level = .floating
        compactWindow.hidesOnDeactivate = false
        compactWindow.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        compactWindow.isReleasedWhenClosed = false
        compactWindow.isOpaque = false
        compactWindow.backgroundColor = .clear
        compactWindow.hasShadow = true
        compactWindow.delegate = self
    }

    @objc private func selectServer() {
        guard let next = servers?.select(current: profile), next != profile else { return }
        loadProfile(next)
    }

    private func loadProfile(_ value: ServerProfile, background: Bool = false, restoringPath: String? = nil) {
        guard let allowed = try? configuration?.profile(value.origin.absoluteString), allowed == value else { return }
        clearProjection()
        pendingLink = nil
        previousApplication = nil
        notifications.invalidate()
        compatibilityTask?.cancel()
        bundleWeb?.invalidate()
        invalidateSecondaryDocuments()
        bundleWeb = nil
        mainWindow.contentView = nil
        compactWindow.contentView = nil
        compactWindow.orderOut(nil)
        profile = value
        servers.remember(value)
        desktopVisible = defaults.object(forKey: railKey + "-enabled") as? Bool
            ?? defaults.object(forKey: "desktopVisible") as? Bool ?? true
        epoch = 0; currentPath = restoringPath.flatMap { validRoute($0) ? $0 : nil } ?? "/"
        mainPath = currentPath; resident = background; ready = false
        do {
            let root = Bundle.main.resourceURL!.appendingPathComponent("Web", isDirectory: true)
            let host = try BundleWebView(profile: value, root: root)
            bundleWeb = host
            workspaceRelay = WorkspaceRelay(profile: value.origin.absoluteString, main: host.generation)
            host.onMessage = { [weak self, weak host] message in
                guard let self, let host, self.bundleWeb === host else { return }
                self.receive(message)
            }
            host.onReady = { [weak self, weak host] in
                guard let self, let host, self.bundleWeb === host else { return }
                self.ready = true; self.publishVisibility()
                self.drainPendingLink()
            }
            host.onOpenLink = { [weak self] destination in self?.open(destination) }
            host.onReload = { [weak self, weak host] in
                guard let self, let host, self.bundleWeb === host else { return }
                self.reloadBundle()
            }
            host.onOwnedInteraction = { [weak self] owned in
                self?.interactionDepth += owned ? 1 : -1
            }
            host.onFailure = { [weak self, weak host] message in
                guard let self, let host, self.bundleWeb === host else { return }
                self.ready = false; self.workspaceRelay = nil
                self.invalidateSecondaryDocuments()
                host.invalidate()
                self.stateLabel = "App error — use Reload"; self.updateMenu()
                if !self.resident { self.showError(message) }
            }
            mainSurface = WindowSurface(web: host.web, main: true)
            mainSurface?.layer?.backgroundColor = mainWindow.backgroundColor.cgColor
            mainWindow.contentView = mainSurface
            compatibilityTask = Task { @MainActor [weak self, weak host] in
                let compatible = await CompatibilityChecker().check(value)
                guard !Task.isCancelled, let self, let host, self.bundleWeb === host else { return }
                // A network outage must not make existing offline drafts inaccessible.
                if compatible != false { host.start(path: self.currentPath) }
                else {
                    self.stateLabel = "Server unavailable or incompatible"
                    self.updateMenu()
                    if !self.resident {
                        self.showError("Cannot verify the server's Cognis client protocol. Check the connection and server compatibility, then reopen. Saved sign-in and drafts were not changed.")
                    }
                }
            }
            if !background { mainWindow.makeKeyAndOrderFront(nil); NSApp.activate() }
            else { mainWindow.orderOut(nil); NSApp.setActivationPolicy(.accessory) }
            stateLabel = "Connecting"
        } catch {
            stateLabel = "Bundled web UI missing"
            if !background { showError("The bundled web UI is missing. Build the complete .app using apps/apple/build.sh.") }
        }
        updateMenu()
    }

    private func reloadBundle() {
        guard let profile else { return }
        loadProfile(profile, background: resident, restoringPath: currentPath)
    }

    private func receive(_ message: BridgeMessage) {
        switch message {
        case .notificationSettings(let action):
            Task { @MainActor in
                await notifications.refreshPermission()
                publishNotificationStatus()
                if action == "show" { showNotificationOptions() }
            }
        case .workspace(let frame):
            if let host = bundleWeb { relayWorkspace(frame, from: host) }
        case .notificationScope(let scope, let account):
            notifications.setScope(scope, account: account, profile: profile?.origin.absoluteString)
        case .notification(let intent):
            Task { await notifications.receive(intent) }
        case .notificationReview:
            openMain(); navigate("/notifications")
        case .openMain(let path): openMain(); navigate(path)
        case .openWindow(let id): openConversationWindow(id)
        case .openResourceWindow(let target): openStandaloneWindow(target)
        case .saveDiagnostic, .openReference:
            break // File saves belong to the trusted originating WebView.
        case .avatar, .rail, .railSize, .panelState:
            break // Rail/panel capabilities are accepted only from their own documents.
        case .appearance(let theme, let background):
            let rgb = UInt32(background.dropFirst(), radix: 16) ?? 0
            let color = NSColor(srgbRed: CGFloat((rgb >> 16) & 255) / 255,
                                green: CGFloat((rgb >> 8) & 255) / 255,
                                blue: CGFloat(rgb & 255) / 255, alpha: 1)
            let appearance = NSAppearance(named: theme == "dark" ? .darkAqua : .aqua)
            mainWindow.appearance = appearance; compactWindow.appearance = appearance
            railWindow?.appearance = appearance
            for window in conversationWindows.values {
                window.appearance = appearance; window.backgroundColor = color
                window.contentView?.layer?.backgroundColor = color.cgColor
            }
            mainWindow.backgroundColor = color
            mainSurface?.layer?.backgroundColor = color.cgColor
            compactSurface?.layer?.backgroundColor = color.cgColor
        case .snapshot(let value):
            guard value.epoch >= epoch else { return }
            if value.epoch != epoch {
                if epoch != 0 {
                    clearProjection()
                    invalidateSecondaryDocuments()
                    if compactWindow.isVisible { dismissCompact() }
                }
                epoch = value.epoch
            }
            snapshot = value
            if !value.authenticated {
                invalidateSecondaryDocuments()
                if pendingLink != nil { openMain() }
            } else if compactDirect, !value.agents.contains(where: { $0.agentId == compactAgent }) {
                dismissCompact()
            } else if !compactDirect, let id = compactConversation, !value.pins.contains(where: { $0.conversationId == id }) {
                dismissCompact()
            }
            if let review = pendingReview {
                pendingReview = nil
                bundleWeb?.send(review.command)
            }
            stateLabel = value.authenticated ? value.connection.capitalized : "Sign in"
            rebuildIcons()
            updateMenu()
            drainPendingLink()
        case .route(let path):
            currentPath = path
            mainPath = path
            publishVisibility()
        case .dismiss:
            if compactConversation != nil { dismissCompact() }
        }
    }

    private func navigate(_ path: String) {
        guard validRoute(path) else { return }
        bundleWeb?.send(["type": "navigate", "path": path])
    }
    @objc private func openMain() {
        guard bundleWeb != nil else { selectServer(); return }
        NSApp.setActivationPolicy(.regular)
        if !ready {
            resident = false
            mainWindow.makeKeyAndOrderFront(nil); NSApp.activate()
            if stateLabel != "Connecting" { reloadBundle() }
            return
        }
        resident = false
        clamp(mainWindow)
        mainWindow.makeKeyAndOrderFront(nil)
        NSApp.activate()
        if currentPath == "/companion" { navigate("/") }
        publishVisibility()
    }
    @objc private func quickChat() {
        openMain()
        // Existing agent picker; creates nothing without explicit user action.
        navigate("/chat/new")
    }
    private func openPin(_ pin: Pin) {
        guard bundleWeb != nil, ready, snapshot?.authenticated == true,
               snapshot?.pins.contains(pin) == true else { return }
        openAgent(pin.agentId, conversation: pin.conversationId)
    }
    static func shouldTogglePinnedPopup(visible: Bool, currentAgent: String?, targetAgent: String,
                                       explicitConversation: String?) -> Bool {
        PanelTarget.shouldDismiss(visible: visible, currentAgent: currentAgent, targetAgent: targetAgent,
            explicitConversation: explicitConversation, ownedInteraction: false)
    }
    static func railDragY(frameY: CGFloat, pointerStartY: CGFloat, currentPointerY: CGFloat) -> CGFloat {
        frameY + currentPointerY - pointerStartY
    }
    private func openAgent(_ agentId: String, conversation: String? = nil, direct: Bool = false) {
        guard ready, snapshot?.authenticated == true, validIdentifier(agentId) else { return }
        if compactWindow.isVisible,
           interactionDepth > 0 || compactWindow.attachedSheet != nil || NSApp.modalWindow != nil {
            compactWindow.orderFrontRegardless()
            return
        }
        if PanelTarget.shouldDismiss(visible: compactWindow.isVisible, currentAgent: compactAgent,
            targetAgent: agentId, explicitConversation: conversation,
            ownedInteraction: interactionDepth > 0 || compactWindow.attachedSheet != nil || NSApp.modalWindow != nil) {
            dismissCompact()
            return
        }
        let path = "/native-pinned?agent=\(agentId)" + (direct ? "&direct=1" : (conversation.map { "&conversation=\($0)" } ?? ""))
        let sameDocument = compactAgent == agentId && compactDirect == direct && (direct || compactConversation == conversation)
        compactDirect = direct
        compactAgent = agentId
        if !sameDocument { compactConversation = conversation }
        if compactWeb == nil {
            guard let host = makeSecondaryDocument(surface: "pinned") else { return }
            compactWeb = host
            compactSurface = WindowSurface(web: host.web, main: false)
            compactSurface?.layer?.backgroundColor = mainWindow.backgroundColor.cgColor
            compactWindow.contentView = compactSurface
            host.start(path: path)
        } else if !sameDocument {
            compactWeb?.send(["type": "navigate", "path": path])
        }
        compactWindow.title = "Pinned conversation — " + appName
        if !NSApp.isActive { previousApplication = NSWorkspace.shared.frontmostApplication }
        if let icon = railWindow, let visible = icon.screen?.visibleFrame {
            compactWindow.setFrame(anchoredFrame(icon: icon.frame, size: compactWindow.frame.size,
                                                visible: visible), display: true)
        } else { clamp(compactWindow) }
        compactWindow.makeKeyAndOrderFront(nil)
        compactWindow.makeFirstResponder(compactWeb?.web)
        NSApp.activate()
        compactFocusRequest = UUID().uuidString
        if let profile { defaults.set(agentId, forKey: PanelTarget.historyKey(profile: profile)) }
        publishRailState()
        publishVisibility()
        focusCompactComposer()
    }
    private func focusCompactComposer() {
        guard compactWindow?.isVisible == true, interactionDepth == 0,
              let agent = compactAgent, let request = compactFocusRequest else { return }
        compactWeb?.send(["type": "focus-composer", "agentId": agent, "request": request])
    }
    private func dismissCompact() {
        let previous = previousApplication
        previousApplication = nil
        compactFocusRequest = nil
        compactWindow.orderOut(nil)
        publishVisibility()
        // The independent main document stays exactly where the user left it.
        // Canonical draft persistence outlives this transient timeline.
        compactWeb?.send(["type": "navigate", "path": "/companion"])
        compactConversation = nil
        compactAgent = nil
        compactDirect = false
        publishRailState()
        if NSApp.isActive, !mainWindow.isVisible, !conversationWindows.values.contains(where: \.isVisible),
           settings?.window?.isVisible != true { previous?.activate() }
    }
    private func outsideClick(_ clickedWindow: NSWindow?) {
        guard compactWindow.isVisible else { return }
        let owned = interactionDepth > 0 || compactWindow.attachedSheet != nil
            || NSApp.modalWindow != nil
        let inside = clickedWindow === compactWindow
            || clickedWindow === railWindow
            || clickedWindow?.parent === compactWindow
            || clickedWindow?.level == .popUpMenu
        if shouldDismissPanel(outside: !inside, ownedInteraction: owned) { dismissCompact() }
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if terminating { return true }
        if let id = conversationWindows.first(where: { $0.value === sender })?.key {
            standalone.retire(id: id)
            failedDocuments.remove(id)
            sender.contentView = nil; sender.delegate = nil
            if !mainWindow.isVisible && !conversationWindows.values.contains(where: \.isVisible) {
                NSApp.setActivationPolicy(.accessory)
            }
            return true
        }
        sender.orderOut(nil)
        if sender === compactWindow { dismissCompact() }
        if sender === mainWindow { resident = true }
        if !mainWindow.isVisible && !conversationWindows.values.contains(where: \.isVisible) {
            NSApp.setActivationPolicy(.accessory)
        }
        publishVisibility()
        return false
    }
    private func publishVisibility() {
        let hosts = [bundleWeb, compactWeb, railWeb] + conversationWeb.values.map(Optional.some)
        for host in hosts {
            guard let host else { continue }
            let window = host.web.window
            let visible = window?.isVisible == true && window?.isMiniaturized == false
                && window?.occlusionState.contains(.visible) == true
            let focused = host !== railWeb && visible && window?.isKeyWindow == true
            host.send(["type": "visibility", "visible": visible, "focused": focused])
        }
    }
    private func makeSecondaryDocument(surface: String) -> BundleWebView? {
        guard let profile, let root = Bundle.main.resourceURL?.appendingPathComponent("Web") else { return nil }
        guard let host = try? BundleWebView(profile: profile, root: root, surface: surface) else { return nil }
        host.onReady = { [weak self, weak host] in
            guard let self, let host, self.activeDocuments.contains(where: { $0 === host }) else { return }
            self.publishVisibility()
            if self.compactWeb === host { self.focusCompactComposer() }
        }
        host.onOpenLink = { [weak self] destination in self?.open(destination) }
        (host.web as? RailWebView)?.onPointerChange = { [weak self, weak host] in
            guard let self, let host, self.railWeb === host else { return }
            self.publishRailState()
        }
        host.onFailure = { [weak self, weak host] message in
            guard let self, let host, self.activeDocuments.contains(where: { $0 === host }) else { return }
            host.send(["type": "visibility", "visible": false, "focused": false])
            if self.compactWeb === host {
                self.dismissCompact(); host.invalidate(); self.compactWeb = nil; self.compactWindow.contentView = nil
            } else if self.railWeb === host {
                self.dismissCompact(); self.retireRail()
            } else if let id = self.conversationWeb.first(where: { $0.value === host })?.key {
                self.failedDocuments.insert(id)
                host.invalidate()
                let label = NSTextField(wrappingLabelWithString: message + " Close and reopen this window to recover. Saved drafts are retained.")
                self.conversationWindows[id]?.contentView = label
            }
        }
        host.onReload = { [weak self, weak host] in
            guard let self, let host, self.activeDocuments.contains(where: { $0 === host }) else { return }
            if self.compactWeb === host {
                let agent = self.compactAgent, conversation = self.compactConversation, direct = self.compactDirect
                self.dismissCompact(); host.invalidate(); self.compactWeb = nil; self.compactWindow.contentView = nil
                if let agent { self.openAgent(agent, conversation: conversation, direct: direct) }
            } else if self.railWeb === host {
                self.dismissCompact(); self.retireRail(); self.rebuildIcons()
            } else if let id = self.conversationWeb.first(where: { $0.value === host })?.key,
                      let window = self.conversationWindows[id], let target = self.standalone.target(id: id) {
                _ = self.windowShouldClose(window); window.close(); self.openStandaloneWindow(target)
            }
        }
        host.onMessage = { [weak self, weak host] message in
            guard let self, let host, self.profile == host.profile,
                  self.compactWeb === host || self.railWeb === host || self.conversationWeb.values.contains(where: { $0 === host })
            else { return }
            switch message {
            case .workspace(let frame): self.relayWorkspace(frame, from: host)
            case .openWindow(let id): self.openConversationWindow(id)
            case .openResourceWindow(let target): self.openStandaloneWindow(target)
            case .notificationSettings(let action): self.receive(.notificationSettings(action))
            case .openMain(let path): self.openMain(); self.navigate(path)
            case .rail(let action, let agentId, let delta):
                guard self.railWeb === host else { return }
                self.handleRail(action, agentId: agentId, delta: delta)
            case .railSize(let width, let height):
                guard self.railWeb === host else { return }
                self.railHasContent = true
                self.railSize = NSSize(width: width, height: height); self.layoutRail()
                if self.desktopVisible { self.railWindow?.orderFrontRegardless() }
            case .panelState(let agentId, let conversationId):
                guard self.compactWeb === host, self.compactWindow.isVisible else { return }
                guard agentId == self.compactAgent else { return }
                self.compactConversation = conversationId
                self.publishRailState()
                self.focusCompactComposer()
            case .dismiss:
                if self.compactWeb === host || self.railWeb === host { self.dismissCompact() }
                else { host.web.window?.performClose(nil) }
            default: break // Only the resident main document owns native projections/notifications.
            }
        }
        host.onOwnedInteraction = { [weak self] owned in
            guard let self else { return }
            self.interactionDepth = max(0, self.interactionDepth + (owned ? 1 : -1))
        }
        return host
    }
    private func openConversationWindow(_ id: String) {
        openStandaloneWindow(StandaloneTarget(kind: .conversation, id: id))
    }
    private func openStandaloneWindow(_ target: StandaloneTarget) {
        guard target.isValid, snapshot?.authenticated == true else { return }
        let id = target.key
        NSApp.setActivationPolicy(.regular)
        if let window = conversationWindows[id] {
            if failedDocuments.contains(id) {
                _ = windowShouldClose(window); window.close()
                openStandaloneWindow(target); return
            }
            window.deminiaturize(nil); window.makeKeyAndOrderFront(nil); NSApp.activate()
            publishVisibility(); return
        }
        guard conversationWindows.count < 10 else {
            showError("Close a window before opening another (limit 10)."); return
        }
        guard let host = makeSecondaryDocument(surface: target.surface) else { return }
        let window = NSWindow(contentRect: NSRect(x: 140, y: 140, width: 900, height: 780),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                              backing: .buffered, defer: false)
        window.title = target.title + " — " + appName
        window.titleVisibility = .hidden; window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = false
        window.appearance = mainWindow.appearance; window.backgroundColor = mainWindow.backgroundColor
        window.isReleasedWhenClosed = false; window.delegate = self
        guard standalone.insert(target: target, window: window, host: host) else { host.invalidate(); return }
        let surface = WindowSurface(web: host.web, main: true)
        surface.layer?.backgroundColor = mainWindow.backgroundColor.cgColor
        window.contentView = surface
        host.start(path: target.path)
        clamp(window); window.makeKeyAndOrderFront(nil); NSApp.activate()
        publishVisibility()
    }
    private func invalidateSecondaryDocuments() {
        retireRail()
        compactWeb?.invalidate(); compactWeb = nil
        compactWindow?.contentView = nil; compactWindow?.orderOut(nil)
        compactConversation = nil
        compactAgent = nil
        compactFocusRequest = nil
        standalone.retireAll()
        failedDocuments.removeAll()
    }
    private var activeDocuments: [BundleWebView] {
        [bundleWeb, compactWeb, railWeb].compactMap { $0 } + Array(conversationWeb.values)
    }
    private func relayWorkspace(_ frame: WorkspaceFrame, from host: BundleWebView) {
        guard let workspaceRelay, activeDocuments.contains(where: { $0 === host }) else { return }
        let documents = activeDocuments
        let targets = workspaceRelay.route(frame, source: host.generation,
            documents: Set(documents.map(\.generation))) {
                for replica in documents where replica !== bundleWeb {
                    replica.send(["type": "workspace-reset"])
                }
            }
        for target in documents where targets.contains(target.generation) {
            target.send(["type": "workspace", "frame": frame.value])
        }
    }
    private func clearProjection() {
        snapshot = nil
        retireRail()
        compactConversation = nil
    }
    private func rebuildIcons() {
        guard snapshot?.authenticated == true, desktopVisible else {
            railWindow?.orderOut(nil); return
        }
        if railWeb == nil {
            guard let host = makeSecondaryDocument(surface: "rail") else { return }
            let panel = DesktopPanel(contentRect: NSRect(origin: .zero, size: railSize),
                styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            panel.level = .floating; panel.hidesOnDeactivate = false
            panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            panel.backgroundColor = .clear; panel.isOpaque = false; panel.hasShadow = false
            panel.isReleasedWhenClosed = false
            railWeb = host; railWindow = panel
            panel.contentView = host.web
            layoutRail()
            host.start(path: "/native-rail")
        }
        layoutRail()
        if railHasContent { railWindow?.orderFrontRegardless() }
        publishRailState()
    }
    private func retireRail() {
        railDragOrigin = nil
        railPointerDown = nil
        railHasContent = false
        railWeb?.invalidate(); railWeb = nil
        railWindow?.contentView = nil; railWindow?.close(); railWindow = nil
    }
    private func publishRailState() {
        let mouse = NSEvent.mouseLocation
        let inside = railWindow?.isVisible == true && railWindow?.frame.contains(mouse) == true
        let local = railWindow.map { $0.convertPoint(fromScreen: mouse) } ?? .zero
        railPresentationRevision += 1
        railWeb?.send(["type": "rail-state",
            "revision": railPresentationRevision, "pointerInside": inside,
            "pointerX": local.x, "pointerY": (railWindow?.frame.height ?? 0) - local.y,
            "focused": railWindow?.isKeyWindow == true,
            "selectedAgentId": compactWindow?.isVisible == true ? compactAgent as Any? ?? NSNull() : NSNull(),
            "side": defaults.string(forKey: railKey + "-side") == "left" ? "left" : "right"])
    }
    private func handleRail(_ action: String, agentId: String?, delta: Double?) {
        guard snapshot?.authenticated == true else { return }
        switch action {
        case "open": if let agentId { openAgent(agentId) }
        case "ready": publishRailState()
        case "empty": railHasContent = false; railWindow?.orderOut(nil); dismissCompact()
        case "drag-start":
            dismissCompact()
            railDragOrigin = railPointerDown
        case "drag-move":
            guard let origin = railDragOrigin else { return }
            let pointer = NSEvent.mouseLocation
            railPlacement.drag(pointer: pointer,
                y: Self.railDragY(frameY: origin.frameY, pointerStartY: origin.pointerY, currentPointerY: pointer.y),
                displays: railDisplays, size: railSize)
            layoutRail()
        case "drag-end":
            if railDragOrigin != nil { handleRail("drag-move", agentId: nil, delta: nil) }
            railDragOrigin = nil; railPointerDown = nil
        case "step":
            dismissCompact()
            if let y = railWindow?.frame.minY, let delta { positionRail(y: y - delta) }
        default: break
        }
    }
    private var railKey: String { "CognisRail-\(profile?.storageIdentifier.uuidString ?? "")" }
    private var railPlacement: DesktopRailPlacement { DesktopRailPlacement(defaults: defaults, key: railKey) }
    private var railDisplays: [DesktopRailPlacement.Display] { NSScreen.screens.map(DesktopRailPlacement.display) }
    private func layoutRail() {
        guard let screen = railPlacement.selected(in: railDisplays), let railWindow else { return }
        let frame = railPlacement.frame(on: screen, size: railSize)
        if frame != railWindow.frame { railWindow.setFrame(frame, display: true) }
        publishRailState()
    }
    private func positionRail(y: CGFloat) {
        guard let screen = railPlacement.selected(in: railDisplays) else { return }
        railPlacement.move(y: y, on: screen, size: railSize)
        layoutRail()
    }
    @objc private func dockLeft() { dismissCompact(); defaults.set("left", forKey: railKey + "-side"); layoutRail() }
    @objc private func dockRight() { dismissCompact(); defaults.set("right", forKey: railKey + "-side"); layoutRail() }
    @objc private func toggleRailTuck() { railWeb?.send(["type": "rail-tuck"]) }
    private func pinMenuItem(_ pin: Pin) -> NSMenuItem {
        let item = NSMenuItem(title: pin.agentName + " — " + pin.title, action: #selector(pinSelected(_:)), keyEquivalent: "")
        item.representedObject = pin.conversationId
        item.target = self
        return item
    }
    @objc private func pinSelected(_ sender: NSMenuItem) {
        if let id = sender.representedObject as? String, let pin = snapshot?.pins.first(where: { $0.conversationId == id }) {
            openPin(pin)
        }
    }
    @objc private func toggleDesktop() {
        desktopVisible.toggle()
        defaults.set(desktopVisible, forKey: railKey + "-enabled")
        rebuildIcons()
        if !desktopVisible && compactWindow.isVisible { dismissCompact() }
        updateMenu()
    }
    @objc private func agentSelected(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String,
              snapshot?.agents.contains(where: { $0.agentId == id }) == true else { return }
        openAgent(id, direct: true)
    }
    @objc private func showSettings() {
        settings?.close()
        settings = NativeSettingsWindow(railVisible: desktopVisible,
            left: defaults.string(forKey: railKey + "-side") == "left",
            displays: NSScreen.screens.map { screen in
                (id: DesktopRailPlacement.display(screen).id, name: screen.localizedName)
            },
            selectedDisplay: railPlacement.selected(in: railDisplays)?.id,
            displayChanged: { [weak self] id in
                guard let self, let display = self.railDisplays.first(where: { $0.id == id }) else { return }
                self.dismissCompact()
                self.railPlacement.select(display)
                self.layoutRail()
            },
            previews: defaults.object(forKey: "notificationPreviews") as? Bool ?? true,
            rail: { [weak self] in self?.toggleDesktop() },
            side: { [weak self] left in
                if left { self?.dockLeft() } else { self?.dockRight() }
                self?.showSettings()
            },
            tuck: { [weak self] in self?.toggleRailTuck() },
            preview: { [weak self] in self?.toggleNotificationPreviews() },
            notifications: { [weak self] in self?.showNotificationOptions() }, startup: login.menu(),
            shortcut: HotKeyPreference.load(from: defaults), shortcutStatus: hotKey.status,
            shortcutChanged: { [weak self] value in
                guard let self else { return }
                do { try HotKeyPreference.save(value, to: self.defaults) }
                catch { self.showError("Could not save keyboard shortcut."); return }
            },
            shortcutRecording: { [weak self] recording in
                guard let self else { return }
                if recording { self.hotKey.unregister() }
                else { self.hotKey.register(HotKeyPreference.load(from: self.defaults)) }
                self.settings?.updateShortcutStatus(recording ? "Recording…" : self.hotKey.status)
            })
        settings?.showWindow(nil)
        NSApp.activate()
    }
    @objc private func notificationOptions() { showNotificationOptions() }
    @objc private func toggleNotificationPreviews() {
        defaults.set(!(defaults.object(forKey: "notificationPreviews") as? Bool ?? true), forKey: "notificationPreviews")
    }
    private func updateMenu() {
        let menu = NSMenu()
        let quick = menu.addItem(withTitle: "New conversation", action: #selector(quickChat), keyEquivalent: "")
        quick.isEnabled = snapshot?.authenticated == true
        let toggle = menu.addItem(withTitle: "Toggle panel — " + GlobalHotKey.label(HotKeyPreference.load(from: defaults)),
            action: #selector(toggleLastPanel), keyEquivalent: "")
        toggle.isEnabled = snapshot?.authenticated == true
        menu.addItem(.separator())
        for agent in snapshot?.agents ?? [] {
            let item = menu.addItem(withTitle: agent.name, action: #selector(agentSelected(_:)), keyEquivalent: "")
            item.representedObject = agent.agentId
        }
        menu.addItem(withTitle: "Settings…", action: #selector(showSettings), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "Open " + appName, action: #selector(openMain), keyEquivalent: "")
        let connection = menu.addItem(withTitle: "Connection: " + stateLabel, action: nil, keyEquivalent: "")
        connection.isEnabled = false
        if let item = servers.menuItem(target: self, action: #selector(selectServer), key: "") { menu.addItem(item) }
        menu.addItem(withTitle: "About / Diagnostics…", action: #selector(about), keyEquivalent: "")
        menu.addItem(withTitle: "Enable Notifications…", action: #selector(enableNotifications), keyEquivalent: "")
        let updateItem = menu.addItem(withTitle: "Updates", action: nil, keyEquivalent: "")
        updateItem.submenu = updates.menu()
        let executorItem = menu.addItem(withTitle: "Local Executor", action: nil, keyEquivalent: "")
        executorItem.submenu = executor.menu()
        let startupItem = menu.addItem(withTitle: "Startup", action: nil, keyEquivalent: "")
        startupItem.submenu = login.menu()
        menu.addItem(withTitle: "Quit " + appName, action: #selector(quit), keyEquivalent: "")
        menu.autoenablesItems = false
        for item in menu.items where item.action != nil { item.target = self }
        statusItem.menu = menu
    }
    private func clamp(_ window: NSWindow) {
        let frame = window.frame
        let display = NSScreen.screens.max {
            $0.visibleFrame.intersection(frame).width * $0.visibleFrame.intersection(frame).height
                < $1.visibleFrame.intersection(frame).width * $1.visibleFrame.intersection(frame).height
        } ?? NSScreen.main
        guard let visible = display?.visibleFrame else { return }
        let width = min(frame.width, visible.width), height = min(frame.height, visible.height)
        let next = NSRect(x: min(max(frame.minX, visible.minX), visible.maxX - width),
                          y: min(max(frame.minY, visible.minY), visible.maxY - height), width: width, height: height)
        if next != frame { window.setFrame(next, display: true) }
    }
    private func clampAll() {
        clamp(mainWindow); clamp(compactWindow)
        layoutRail()
    }
    func windowDidChangeBackingProperties(_ notification: Notification) { clampAll() }
    func windowDidResize(_ notification: Notification) {
        mainSurface?.needsLayout = true
        compactSurface?.needsLayout = true
        for window in conversationWindows.values { window.contentView?.needsLayout = true }
    }
    @objc private func about() {
        let info = Bundle.main.infoDictionary ?? [:]
        let alert = NSAlert()
        alert.messageText = appName + " — Diagnostics"
        alert.informativeText = "Package: \(info["CognisPackageFlavor"] ?? "unknown") • \(Bundle.main.bundleIdentifier ?? "unknown")\nNative \(info["CFBundleShortVersionString"] ?? "unknown") build \(info["CFBundleVersion"] ?? "unknown") • Web \(info["CognisWebVersion"] ?? "unknown")\nSource \(info["CognisSourceCommit"] ?? "unknown")\nHost bridge v1 • macOS 15+\nServer: \(profile?.origin.absoluteString ?? "Not selected")\nConnection: \(stateLabel)\n\(updates.status)\n\nDeveloper preview: M1 GUI acceptance pending. No APNs delivery while quit/asleep."
        alert.runModal()
    }
    private func showError(_ message: String) {
        let alert = NSAlert()
        alert.messageText = appName
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.runModal()
    }
    @objc private func quit() { NSApp.terminate(nil) }
    @objc private func enableNotifications() {
        // This selector is reachable only from the native menu. Never from web events.
        Task { @MainActor in
            await notifications.enableFromNativeMenu()
            publishNotificationStatus()
            showError(notifications.permissionStatus)
        }
    }
    private func publishNotificationStatus() {
        for host in [bundleWeb, compactWeb, railWeb].compactMap({ $0 }) + Array(conversationWeb.values) {
            host.send(notifications.status)
        }
    }
    private func showNotificationOptions() {
        let alert = NSAlert()
        alert.messageText = "macOS Notifications"
        alert.informativeText = notifications.permissionStatus
        let canRequest = notifications.authorization == "not-determined" && notifications.permissionError == nil
        alert.addButton(withTitle: canRequest ? "Enable Notifications" : "Open Notification Settings")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        if canRequest {
            enableNotifications() // Only the explicit native button can request authorization.
        } else if let url = URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension") {
            NSWorkspace.shared.open(url)
        }
    }
    private func reviewNotification(_ review: NotificationReview) {
        guard review.isValid, let value = try? configuration?.profile(review.profile) else { return }
        if profile != value {
            pendingReview = review
            loadProfile(value)
        } else {
            openMain()
            bundleWeb?.send(review.command)
        }
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { LaunchPolicy.terminateOnLastWindowClose }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openMain(); return true
    }
    func applicationWillTerminate(_ notification: Notification) {
        hotKey.unregister()
        NSAppleEventManager.shared().removeEventHandler(forEventClass: AEEventClass(kInternetEventClass),
                                                       andEventID: AEEventID(kAEGetURL))
        guard statusItem != nil else { return }
        terminating = true
        notifications.invalidate()
        compatibilityTask?.cancel()
        visibilityObservers.forEach(NotificationCenter.default.removeObserver)
        if let activationObserver { NSWorkspace.shared.notificationCenter.removeObserver(activationObserver) }
        if let wakeObserver { NSWorkspace.shared.notificationCenter.removeObserver(wakeObserver) }
        if let localClicks { NSEvent.removeMonitor(localClicks) }
        if let globalClicks { NSEvent.removeMonitor(globalClicks) }
        bundleWeb?.invalidate(); bundleWeb = nil
        invalidateSecondaryDocuments()
        clearProjection()
        mainWindow.close(); compactWindow.close()
        NSStatusBar.system.removeStatusItem(statusItem)
    }
}

@main
struct CognisCompanion {
    @MainActor static func main() {
        let app = NSApplication.shared
        if CommandLine.arguments.dropFirst().elementsEqual(["--notification-status"]) {
            Task { @MainActor in
                let evidence = await SystemNotificationDelivery().diagnostics()
                if let data = try? JSONSerialization.data(withJSONObject: evidence, options: [.sortedKeys]),
                   let text = String(data: data, encoding: .utf8) { print(text) }
                app.terminate(nil)
            }
            app.run()
            return
        }
        let delegate = CompanionApp()
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
