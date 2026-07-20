import AppKit
import SwiftUI
import UserNotifications

final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        UNUserNotificationCenter.current().delegate = self
        GlobalShortcutManager.shared.register()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }

    func applicationWillTerminate(_ notification: Notification) {
        GlobalShortcutManager.shared.unregister()
    }
}

@main
struct ChronosDesktopApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var controller = DeviceController()

    init() {
        if CommandLine.arguments.contains("--self-test") {
            exit(BridgeSelfTest.run() ? 0 : 1)
        }
    }

    var body: some Scene {
        WindowGroup("Chronos Desktop", id: "main") {
            DesktopSettingsView(controller: controller)
                .frame(minWidth: 640, idealWidth: 720, minHeight: 540, idealHeight: 620)
                .environment(\.colorScheme, .light)
                .onAppear { controller.start() }
        }
        .windowResizability(.contentMinSize)
        .commands {
            CommandGroup(after: .appInfo) {
                Button("Open Chronos Desktop") {
                    NSApp.activate(ignoringOtherApps: true)
                    NSApp.windows.first(where: { $0.canBecomeKey })?.makeKeyAndOrderFront(nil)
                }
                .keyboardShortcut("0", modifiers: [.command])
            }
        }

        MenuBarExtra("Chronos", systemImage: menuSymbol) {
            MenuBarPanel(controller: controller)
        }
        .menuBarExtraStyle(.window)
    }

    private var menuSymbol: String {
        switch controller.status {
        case .online: return "clock.arrow.circlepath"
        case .pairing: return "clock.badge.questionmark"
        case .degraded: return "clock.badge.exclamationmark"
        case .revoked: return "clock.badge.xmark"
        case .disconnected: return "clock"
        }
    }
}

private struct MenuBarPanel: View {
    @ObservedObject var controller: DeviceController
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                Image(systemName: "clock.arrow.circlepath")
                    .font(.system(size: 19, weight: .semibold))
                    .foregroundStyle(accent)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Chronos Desktop").font(.headline)
                    Label(statusLabel, systemImage: "circle.fill")
                        .font(.caption)
                        .foregroundStyle(statusColor)
                }
            }
            Text(controller.statusDetail)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Divider()
            Button("Open Chronos Desktop") {
                openWindow(id: "main")
                NSApp.activate(ignoringOtherApps: true)
            }
            .buttonStyle(.borderedProminent)
            .tint(accent)
            .frame(maxWidth: .infinity)
            if controller.isPaired {
                Button("Authorize a Folder…") { Task { await controller.addFolder() } }
                    .frame(maxWidth: .infinity)
                Button(controller.status == .online ? "Pause Local Bridge" : "Resume Local Bridge") {
                    if controller.status == .online { controller.stop() }
                    else { controller.start() }
                }
                .frame(maxWidth: .infinity)
            }
            Divider()
            HStack {
                Text("⌥ Space opens Chronos").font(.caption2).foregroundStyle(.secondary)
                Spacer()
                Button("Quit") { NSApp.terminate(nil) }.buttonStyle(.plain)
            }
        }
        .padding(16)
        .frame(width: 310)
    }

    private var accent: Color { Color(red: 0.78, green: 0.39, blue: 0.24) }
    private var statusLabel: String { controller.status.rawValue.capitalized }
    private var statusColor: Color {
        switch controller.status {
        case .online: return .green
        case .pairing: return .orange
        case .degraded: return .orange
        case .revoked: return .red
        case .disconnected: return .secondary
        }
    }
}

private struct DesktopSettingsView: View {
    @ObservedObject var controller: DeviceController
    @State private var showDisconnectConfirmation = false

    private let accent = Color(red: 0.78, green: 0.39, blue: 0.24)
    private let warmBackground = Color(red: 0.974, green: 0.969, blue: 0.945)

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                header
                statusCard
                if controller.isPaired { pairedContent } else { pairingContent }
                securityCard
            }
            .padding(32)
            .frame(maxWidth: 760, alignment: .leading)
        }
        .background(warmBackground.ignoresSafeArea())
        .tint(accent)
        .alert("Disconnect this Mac?", isPresented: $showDisconnectConfirmation) {
            Button("Cancel", role: .cancel) {}
            Button("Disconnect", role: .destructive) { Task { await controller.disconnect() } }
        } message: {
            Text("Chronos will erase the device token and every local folder authorization from this Mac. In-flight commands will be revoked.")
        }
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 14) {
            Image(nsImage: NSApplication.shared.applicationIconImage)
                .resizable()
                .scaledToFit()
                .frame(width: 48, height: 48)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text("Chronos Desktop")
                    .font(.system(size: 25, weight: .semibold, design: .rounded))
                    .foregroundStyle(Color(red: 0.13, green: 0.13, blue: 0.14))
                Text("A secure bridge for approved work on this Mac.")
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Text("v\(Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.1.0")")
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
        }
    }

    private var statusCard: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: statusIcon).foregroundStyle(statusColor).font(.title3)
            VStack(alignment: .leading, spacing: 4) {
                Text(controller.status.rawValue.capitalized).font(.headline)
                Text(controller.statusDetail).font(.subheadline).foregroundStyle(.secondary)
            }
            Spacer()
            if controller.isPaired {
                Button(controller.status == .online ? "Pause" : "Reconnect") {
                    if controller.status == .online { controller.stop() }
                    else { controller.start() }
                }
                .buttonStyle(.bordered)
            }
        }
        .padding(16)
        .background(.white, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(.quaternary))
    }

    private var pairingContent: some View {
        section(title: "Pair this Mac", subtitle: "In Chronos, open Settings → Desktop devices and create a one-time pairing code.") {
            VStack(alignment: .leading, spacing: 14) {
                field("Chronos API URL") {
                    TextField("https://api.cognisiatech.com", text: $controller.apiURLText)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityIdentifier("api-url")
                }
                field("Device name") {
                    TextField("This Mac", text: $controller.deviceName)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityIdentifier("device-name")
                }
                field("One-time pairing code") {
                    TextField("ABCD1234", text: $controller.pairingCode)
                        .textFieldStyle(.roundedBorder)
                        .font(.system(.body, design: .monospaced))
                        .accessibilityIdentifier("pairing-code")
                }
                Button {
                    Task { await controller.pair() }
                } label: {
                    if controller.isBusy { ProgressView().controlSize(.small) }
                    else { Label("Pair securely", systemImage: "link.badge.plus") }
                }
                .buttonStyle(.borderedProminent)
                .disabled(controller.isBusy)
                .accessibilityIdentifier("pair-device")
            }
        }
    }

    private var pairedContent: some View {
        VStack(alignment: .leading, spacing: 24) {
            section(title: "Authorized folders", subtitle: "Full paths and security-scoped bookmarks never leave this Mac. Every command is still governed and audited in Chronos.") {
                VStack(alignment: .leading, spacing: 10) {
                    if controller.grants.isEmpty {
                        VStack(spacing: 8) {
                            Image(systemName: "folder.badge.plus")
                                .font(.system(size: 28))
                                .foregroundStyle(.secondary)
                            Text("No authorized folders").font(.headline)
                            Text("Authorize only the folders Chronos should be able to use.")
                                .font(.subheadline).foregroundStyle(.secondary)
                        }
                        .frame(maxWidth: .infinity, minHeight: 130)
                    } else {
                        ForEach(controller.grants) { grant in
                            HStack(spacing: 12) {
                                Image(systemName: "folder.fill").foregroundStyle(accent)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(grant.displayName).font(.body.weight(.medium))
                                    Text("Device-only authorization · \(grant.id.prefix(8))")
                                        .font(.caption.monospaced()).foregroundStyle(.secondary)
                                }
                                Spacer()
                                Button("Revoke", role: .destructive) {
                                    Task { await controller.revokeFolder(grant) }
                                }
                            }
                            .padding(12)
                            .background(.white, in: RoundedRectangle(cornerRadius: 10))
                            .overlay(RoundedRectangle(cornerRadius: 10).stroke(.quaternary))
                        }
                    }
                    HStack {
                        Button { Task { await controller.addFolder() } } label: {
                            Label("Authorize Folder…", systemImage: "folder.badge.plus")
                        }
                        .buttonStyle(.borderedProminent)
                        Button { Task { await controller.requestNotificationPermission() } } label: {
                            Label("Enable Notifications", systemImage: "bell.badge")
                        }
                        .buttonStyle(.bordered)
                    }
                }
            }
            section(title: "Device access", subtitle: "Disconnecting revokes queued commands and erases device credentials from Keychain.") {
                Button("Disconnect this Mac…", role: .destructive) { showDisconnectConfirmation = true }
                    .buttonStyle(.bordered)
            }
        }
    }

    private var securityCard: some View {
        section(title: "Security boundary", subtitle: "Chronos Desktop fails closed when any check is missing.") {
            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 10) {
                securityItem("Signed commands", "checkmark.shield")
                securityItem("Short-lived leases", "timer")
                securityItem("Replay protection", "arrow.triangle.2.circlepath")
                securityItem("Keychain secrets", "key.fill")
                securityItem("Folder sandbox", "folder.badge.gearshape")
                securityItem("No shell execution", "terminal.fill")
            }
        }
    }

    private func section<Content: View>(title: String, subtitle: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.title3.weight(.semibold))
                Text(subtitle).font(.subheadline).foregroundStyle(.secondary)
            }
            content()
        }
        .padding(18)
        .background(.white.opacity(0.72), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(.black.opacity(0.08)))
    }

    private func field<Content: View>(_ label: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            content()
        }
    }

    private func securityItem(_ title: String, _ icon: String) -> some View {
        Label(title, systemImage: icon)
            .font(.subheadline)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(10)
            .background(.white, in: RoundedRectangle(cornerRadius: 9))
    }

    private var statusIcon: String {
        switch controller.status {
        case .online: return "checkmark.circle.fill"
        case .pairing: return "arrow.triangle.2.circlepath"
        case .degraded: return "exclamationmark.triangle.fill"
        case .revoked: return "xmark.shield.fill"
        case .disconnected: return "link.slash"
        }
    }

    private var statusColor: Color {
        switch controller.status {
        case .online: return .green
        case .pairing, .degraded: return .orange
        case .revoked: return .red
        case .disconnected: return .secondary
        }
    }
}
