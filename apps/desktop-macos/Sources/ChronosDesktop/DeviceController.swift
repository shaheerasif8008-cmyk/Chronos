import AppKit
import Foundation
import UserNotifications

@MainActor
final class DeviceController: ObservableObject {
    @Published var status: BridgeStatus = .disconnected
    @Published var statusDetail = "Pair this Mac to run approved Chronos tasks locally."
    @Published var apiURLText: String
    @Published var pairingCode = ""
    @Published var deviceName: String
    @Published var grants: [LocalGrantMetadata] = []
    @Published var lastSeen: Date?
    @Published var isBusy = false

    private let api = BridgeAPIClient()
    private let keychain = KeychainStore.shared
    private let grantStore = FolderGrantStore.shared
    private let executor = CommandExecutor()
    private var pollTask: Task<Void, Never>?
    private var lastHeartbeat = Date.distantPast

    private let tokenAccount = "device-token"
    private let secretAccount = "command-secret" // gitleaks:allow -- Keychain account label, not a credential
    private let deviceIdKey = "chronos.deviceId.v1"
    private let apiURLKey = "chronos.apiURL.v1"
    private let nonceKey = "chronos.executedNonces.v1" // gitleaks:allow -- UserDefaults key name, not a credential
    private let cachedResultIdsKey = "chronos.cachedResultIds.v1"

    init() {
        apiURLText = UserDefaults.standard.string(forKey: apiURLKey) ?? "https://api.cognisiatech.com"
        deviceName = Host.current().localizedName ?? "This Mac"
        grants = grantStore.metadata()
        if credentials() != nil {
            status = .degraded
            statusDetail = "Ready to reconnect."
        }
    }

    var isPaired: Bool { credentials() != nil }

    func start() {
        guard pollTask == nil, credentials() != nil else { return }
        status = .degraded
        statusDetail = "Connecting securely…"
        pollTask = Task { [weak self] in await self?.pollLoop() }
    }

    func stop() {
        pollTask?.cancel()
        pollTask = nil
        if isPaired {
            status = .degraded
            statusDetail = "Local bridge paused."
        }
    }

    func pair() async {
        guard !isBusy else { return }
        isBusy = true
        status = .pairing
        statusDetail = "Pairing with Chronos…"
        defer { isBusy = false }
        do {
            let baseURL = try api.validatedBaseURL(apiURLText)
            let code = pairingCode
                .uppercased()
                .filter { $0.isLetter || $0.isNumber }
            guard code.count >= 6 else {
                throw BridgeError.commandFailed("Enter the one-time pairing code shown in Chronos.")
            }
            let response = try await api.pair(
                baseURL: baseURL,
                request: PairDeviceRequest(
                    code: code,
                    displayName: String(deviceName.prefix(120)),
                    platform: "macos",
                    appVersion: appVersion
                )
            )
            guard let secret = Data(base64Encoded: response.commandSecretB64), secret.count == 32 else {
                throw BridgeError.invalidSecret
            }
            try keychain.save(secret, account: secretAccount)
            try keychain.save(response.deviceToken, account: tokenAccount)
            UserDefaults.standard.set(response.deviceId, forKey: deviceIdKey)
            UserDefaults.standard.set(baseURL.absoluteString, forKey: apiURLKey)
            apiURLText = baseURL.absoluteString
            pairingCode = ""
            status = .online
            statusDetail = "Paired. Waiting for approved tasks."
            start()
        } catch {
            keychain.delete(account: tokenAccount)
            keychain.delete(account: secretAccount)
            status = .disconnected
            statusDetail = safeMessage(error)
        }
    }

    func disconnect() async {
        stop()
        let current = credentials()
        var serverRevoked = false
        if let current {
            do {
                try await api.disconnect(baseURL: current.baseURL, deviceId: current.deviceId, token: current.token)
                serverRevoked = true
            } catch {
                serverRevoked = false
            }
        }
        clearCredentials()
        grantStore.removeAll()
        grants = []
        status = .disconnected
        statusDetail = serverRevoked
            ? "This Mac was disconnected and its device access was revoked."
            : "Local credentials were erased. Revoke this Mac in Chronos admin when you are online."
    }

    func addFolder() async {
        guard let current = credentials(), !isBusy else {
            statusDetail = "Pair this Mac before authorizing a folder."
            return
        }
        let panel = NSOpenPanel()
        panel.title = "Authorize a folder for Chronos"
        panel.message = "Chronos will only access this folder after a governed task is approved. The full path stays on this Mac."
        panel.prompt = "Authorize Folder"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let folderURL = panel.url else { return }

        isBusy = true
        defer { isBusy = false }
        let clientId = UUID().uuidString.lowercased()
        do {
            let registered = try await api.registerGrant(
                baseURL: current.baseURL,
                deviceId: current.deviceId,
                token: current.token,
                clientGrantId: clientId,
                displayName: String(folderURL.lastPathComponent.prefix(120))
            )
            _ = try grantStore.authorizeFolder(
                url: folderURL,
                clientGrantId: clientId,
                serverGrantId: registered.id
            )
            grants = grantStore.metadata()
            statusDetail = "Authorized “\(folderURL.lastPathComponent)”."
        } catch {
            try? await api.revokeGrant(
                baseURL: current.baseURL,
                deviceId: current.deviceId,
                token: current.token,
                clientGrantId: clientId
            )
            statusDetail = safeMessage(error)
        }
    }

    func revokeFolder(_ grant: LocalGrantMetadata) async {
        try? grantStore.revoke(clientGrantId: grant.id)
        grants = grantStore.metadata()
        guard let current = credentials() else { return }
        do {
            try await api.revokeGrant(
                baseURL: current.baseURL,
                deviceId: current.deviceId,
                token: current.token,
                clientGrantId: grant.id
            )
            statusDetail = "Folder access revoked."
        } catch {
            statusDetail = "Local folder access was removed. Server revocation will need to be retried."
        }
    }

    func requestNotificationPermission() async {
        do {
            let allowed = try await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .badge, .sound])
            statusDetail = allowed ? "Desktop notifications enabled." : "Desktop notifications remain disabled in System Settings."
        } catch {
            statusDetail = "Notification permission could not be updated."
        }
    }

    private func pollLoop() async {
        var delay: UInt64 = 2
        while !Task.isCancelled {
            guard let current = credentials() else { break }
            do {
                if Date().timeIntervalSince(lastHeartbeat) >= 60 {
                    try await api.heartbeat(
                        baseURL: current.baseURL,
                        deviceId: current.deviceId,
                        token: current.token,
                        appVersion: appVersion
                    )
                    lastHeartbeat = Date()
                }
                status = .online
                statusDetail = "Connected. Waiting for approved tasks."
                lastSeen = Date()
                if let envelope = try await api.nextCommand(
                    baseURL: current.baseURL,
                    deviceId: current.deviceId,
                    token: current.token
                ) {
                    try await handle(envelope: envelope, credentials: current)
                }
                delay = 2
                try? await Task.sleep(nanoseconds: 1_000_000_000)
            } catch let BridgeError.server(status: code, message: message) where code == 401 || code == 403 {
                status = .revoked
                statusDetail = message
                clearCredentials()
                break
            } catch {
                status = .degraded
                statusDetail = "Connection interrupted. Retrying safely."
                try? await Task.sleep(nanoseconds: delay * 1_000_000_000)
                delay = min(delay * 2, 60)
            }
        }
        pollTask = nil
    }

    private func handle(envelope: CommandEnvelope, credentials: Credentials) async throws {
        guard envelope.deviceId == credentials.deviceId,
              let payload = Data(base64Encoded: envelope.payloadB64) else {
            throw BridgeError.malformedPayload
        }
        guard BridgeCrypto.verify(
            signature: envelope.signature,
            message: BridgeCrypto.commandMessage(envelope, payload: payload),
            secret: credentials.secret
        ) else { throw BridgeError.invalidSignature }
        guard let expiry = parseDate(envelope.expiresAt), expiry > Date() else {
            throw BridgeError.expiredCommand
        }

        if hasExecuted(nonce: envelope.nonce) {
            if let cached = cachedSubmission(commandId: envelope.commandId) {
                try await api.submitResult(
                    baseURL: credentials.baseURL,
                    deviceId: credentials.deviceId,
                    commandId: envelope.commandId,
                    token: credentials.token,
                    submission: cached
                )
                clearCachedSubmission(commandId: envelope.commandId)
                return
            }
            throw BridgeError.replayedCommand
        }

        rememberExecuted(nonce: envelope.nonce)
        statusDetail = "Running one approved local operation…"
        let execution = await executor.execute(commandType: envelope.commandType, payload: payload)
        let resultData = try JSONSerialization.data(withJSONObject: execution.body, options: .bridgeOptions)
        let signature = BridgeCrypto.signature(
            message: BridgeCrypto.resultMessage(
                commandId: envelope.commandId,
                deviceId: credentials.deviceId,
                nonce: envelope.nonce,
                status: execution.status,
                errorCode: execution.errorCode,
                result: resultData
            ),
            secret: credentials.secret
        )
        let submission = CommandResultSubmission(
            nonce: envelope.nonce,
            status: execution.status,
            errorCode: execution.errorCode,
            resultB64: resultData.base64EncodedString(),
            signature: signature
        )
        try cache(submission: submission, commandId: envelope.commandId)
        try await api.submitResult(
            baseURL: credentials.baseURL,
            deviceId: credentials.deviceId,
            commandId: envelope.commandId,
            token: credentials.token,
            submission: submission
        )
        clearCachedSubmission(commandId: envelope.commandId)
        statusDetail = execution.status == "succeeded"
            ? "Approved local operation completed."
            : "The local operation failed safely."
    }

    private struct Credentials {
        let baseURL: URL
        let deviceId: String
        let token: String
        let secret: Data
    }

    private func credentials() -> Credentials? {
        guard let deviceId = UserDefaults.standard.string(forKey: deviceIdKey),
              let token = keychain.string(account: tokenAccount),
              let secret = keychain.data(account: secretAccount), secret.count == 32,
              let rawURL = UserDefaults.standard.string(forKey: apiURLKey),
              let baseURL = try? api.validatedBaseURL(rawURL) else { return nil }
        return Credentials(baseURL: baseURL, deviceId: deviceId, token: token, secret: secret)
    }

    private func clearCredentials() {
        for commandId in UserDefaults.standard.stringArray(forKey: cachedResultIdsKey) ?? [] {
            keychain.delete(account: "result:\(commandId)")
        }
        keychain.delete(account: tokenAccount)
        keychain.delete(account: secretAccount)
        UserDefaults.standard.removeObject(forKey: deviceIdKey)
        UserDefaults.standard.removeObject(forKey: nonceKey)
        UserDefaults.standard.removeObject(forKey: cachedResultIdsKey)
    }

    private func hasExecuted(nonce: String) -> Bool {
        (UserDefaults.standard.stringArray(forKey: nonceKey) ?? []).contains(nonce)
    }

    private func rememberExecuted(nonce: String) {
        var values = UserDefaults.standard.stringArray(forKey: nonceKey) ?? []
        values.append(nonce)
        if values.count > 1_024 { values.removeFirst(values.count - 1_024) }
        UserDefaults.standard.set(values, forKey: nonceKey)
    }

    private func cache(submission: CommandResultSubmission, commandId: String) throws {
        try keychain.save(try JSONEncoder().encode(submission), account: "result:\(commandId)")
        var ids = UserDefaults.standard.stringArray(forKey: cachedResultIdsKey) ?? []
        if !ids.contains(commandId) { ids.append(commandId) }
        if ids.count > 128 {
            let removed = ids.prefix(ids.count - 128)
            removed.forEach { keychain.delete(account: "result:\($0)") }
            ids.removeFirst(ids.count - 128)
        }
        UserDefaults.standard.set(ids, forKey: cachedResultIdsKey)
    }

    private func cachedSubmission(commandId: String) -> CommandResultSubmission? {
        guard let data = keychain.data(account: "result:\(commandId)") else { return nil }
        return try? JSONDecoder().decode(CommandResultSubmission.self, from: data)
    }

    private func clearCachedSubmission(commandId: String) {
        keychain.delete(account: "result:\(commandId)")
        let ids = (UserDefaults.standard.stringArray(forKey: cachedResultIdsKey) ?? []).filter { $0 != commandId }
        UserDefaults.standard.set(ids, forKey: cachedResultIdsKey)
    }

    private func parseDate(_ raw: String) -> Date? {
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return fractional.date(from: raw) ?? ISO8601DateFormatter().date(from: raw)
    }

    private var appVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.1.0"
    }

    private func safeMessage(_ error: Error) -> String {
        if let message = (error as? LocalizedError)?.errorDescription, !message.isEmpty {
            return String(message.prefix(500))
        }
        return "The operation could not be completed safely."
    }
}
