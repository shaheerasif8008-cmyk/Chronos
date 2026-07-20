import AppKit
import Darwin
import Foundation
import UserNotifications

private final class BoundedPipeCollector {
    private let lock = NSLock()
    private let limit: Int
    private var storage = Data()
    private(set) var truncated = false

    init(limit: Int) { self.limit = limit }

    func append(_ data: Data) {
        guard !data.isEmpty else { return }
        lock.lock()
        defer { lock.unlock() }
        let remaining = max(0, limit - storage.count)
        if remaining > 0 { storage.append(data.prefix(remaining)) }
        if data.count > remaining { truncated = true }
    }

    func string() -> String {
        lock.lock()
        defer { lock.unlock() }
        return String(decoding: storage, as: UTF8.self)
    }
}

final class CommandExecutor {
    private let grants: FolderGrantStore
    private let maxReadBytes = 1_048_576
    private let maxOutputBytes = 1_048_576
    private let allowedExecutables: Set<String> = [
        "cat", "find", "git", "grep", "head", "ls", "node", "npm", "npx",
        "pwd", "python", "python3", "rg", "sed", "swift", "tail", "wc",
    ]

    init(grants: FolderGrantStore = .shared) {
        self.grants = grants
    }

    func execute(commandType: String, payload: Data) async -> CommandExecutionResult {
        do {
            guard payload.count <= 262_144,
                  let object = try JSONSerialization.jsonObject(with: payload) as? [String: Any] else {
                throw BridgeError.malformedPayload
            }
            let body: [String: Any]
            switch commandType {
            case "list_files": body = try listFiles(object)
            case "read_file": body = try readFile(object)
            case "exec": body = try await runCommand(object)
            case "open_app": body = try await openApplication(object)
            case "notify": body = try await postNotification(object)
            case "revoke_grant": body = try revokeGrant(object)
            default: throw BridgeError.unsupportedCommand
            }
            return CommandExecutionResult(status: "succeeded", errorCode: nil, body: body)
        } catch let error as BridgeError {
            return CommandExecutionResult(
                status: "failed",
                errorCode: errorCode(error),
                body: ["message": error.localizedDescription]
            )
        } catch {
            return CommandExecutionResult(
                status: "failed",
                errorCode: "execution_failed",
                body: ["message": "The local operation failed safely."]
            )
        }
    }

    private func listFiles(_ payload: [String: Any]) throws -> [String: Any] {
        let grantId = try clientGrantId(payload)
        let relativePath = (payload["path"] as? String) ?? "."
        return try grants.withAuthorizedFolder(clientGrantId: grantId) { root in
            let target = try grants.jailedURL(root: root, relativePath: relativePath)
            let keys: Set<URLResourceKey> = [.isDirectoryKey, .fileSizeKey, .contentModificationDateKey]
            let children = try FileManager.default.contentsOfDirectory(
                at: target,
                includingPropertiesForKeys: Array(keys),
                options: [.skipsHiddenFiles]
            )
            let entries: [[String: Any]] = try children.prefix(1_000).map { url in
                let values = try url.resourceValues(forKeys: keys)
                return [
                    "name": url.lastPathComponent,
                    "is_directory": values.isDirectory ?? false,
                    "size_bytes": values.fileSize ?? 0,
                    "modified_at": values.contentModificationDate.map(ISO8601DateFormatter().string) ?? NSNull(),
                ]
            }
            return ["path": relativePath, "entries": entries, "truncated": children.count > 1_000]
        }
    }

    private func readFile(_ payload: [String: Any]) throws -> [String: Any] {
        let grantId = try clientGrantId(payload)
        guard let relativePath = payload["path"] as? String, !relativePath.isEmpty else {
            throw BridgeError.malformedPayload
        }
        return try grants.withAuthorizedFolder(clientGrantId: grantId) { root in
            let target = try grants.jailedURL(root: root, relativePath: relativePath)
            let attributes = try FileManager.default.attributesOfItem(atPath: target.path)
            guard (attributes[.type] as? FileAttributeType) == .typeRegular else {
                throw BridgeError.commandFailed("Only regular files can be read.")
            }
            let size = (attributes[.size] as? NSNumber)?.intValue ?? 0
            guard size <= maxReadBytes else {
                throw BridgeError.commandFailed("The file exceeds the 1 MB local read limit.")
            }
            let data = try Data(contentsOf: target, options: [.mappedIfSafe])
            if let text = String(data: data, encoding: .utf8) {
                return ["path": relativePath, "encoding": "utf-8", "content": text, "size_bytes": data.count]
            }
            return [
                "path": relativePath,
                "encoding": "base64",
                "content": data.base64EncodedString(),
                "size_bytes": data.count,
            ]
        }
    }

    private func runCommand(_ payload: [String: Any]) async throws -> [String: Any] {
        let grantId = try clientGrantId(payload)
        guard let argv = payload["argv"] as? [String],
              let executable = argv.first,
              !argv.isEmpty,
              argv.count <= 64,
              argv.reduce(0, { $0 + $1.utf8.count }) <= 32_768,
              allowedExecutables.contains(executable),
              !argv.contains(where: { $0.contains("\0") }) else {
            throw BridgeError.commandFailed("That executable is not allowed by the local bridge policy.")
        }
        let access = try grants.accessAuthorizedFolder(clientGrantId: grantId)
        defer { access.close() }
        let workingPath = (payload["working_directory"] as? String) ?? "."
        let workingURL = try grants.jailedURL(root: access.url, relativePath: workingPath)
        let timeout = min(max((payload["timeout_seconds"] as? Double) ?? 60, 1), 300)

        let stdout = Pipe()
        let stderr = Pipe()
        let stdoutCollector = BoundedPipeCollector(limit: maxOutputBytes)
        let stderrCollector = BoundedPipeCollector(limit: maxOutputBytes)
        stdout.fileHandleForReading.readabilityHandler = { handle in stdoutCollector.append(handle.availableData) }
        stderr.fileHandleForReading.readabilityHandler = { handle in stderrCollector.append(handle.availableData) }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = argv
        process.currentDirectoryURL = workingURL
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = stdout
        process.standardError = stderr
        process.environment = [
            "HOME": access.url.path,
            "LANG": "en_US.UTF-8",
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "TMPDIR": FileManager.default.temporaryDirectory.path,
        ]

        try process.run()
        let timedOut = await wait(process: process, timeout: timeout)
        stdout.fileHandleForReading.readabilityHandler = nil
        stderr.fileHandleForReading.readabilityHandler = nil
        stdoutCollector.append(stdout.fileHandleForReading.readDataToEndOfFile())
        stderrCollector.append(stderr.fileHandleForReading.readDataToEndOfFile())

        return [
            "argv": argv,
            "working_directory": workingPath,
            "exit_code": Int(process.terminationStatus),
            "stdout": stdoutCollector.string(),
            "stderr": stderrCollector.string(),
            "truncated": stdoutCollector.truncated || stderrCollector.truncated,
            "timed_out": timedOut,
        ]
    }

    private func wait(process: Process, timeout: Double) async -> Bool {
        await withTaskGroup(of: Bool.self) { group in
            group.addTask {
                process.waitUntilExit()
                return false
            }
            group.addTask {
                try? await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
                return true
            }
            let timedOut = await group.next() ?? true
            if timedOut, process.isRunning {
                process.terminate()
                try? await Task.sleep(nanoseconds: 1_000_000_000)
                if process.isRunning { kill(process.processIdentifier, SIGKILL) }
            }
            group.cancelAll()
            return timedOut
        }
    }

    private func openApplication(_ payload: [String: Any]) async throws -> [String: Any] {
        _ = try clientGrantId(payload)
        guard let bundleId = payload["bundle_id"] as? String,
              bundleId.range(of: #"^[A-Za-z0-9.-]{3,200}$"#, options: .regularExpression) != nil,
              let appURL = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleId) else {
            throw BridgeError.commandFailed("The approved application is not installed.")
        }
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.activates = true
            configuration.addsToRecentItems = false
            NSWorkspace.shared.openApplication(at: appURL, configuration: configuration) { _, error in
                if let error { continuation.resume(throwing: error) }
                else { continuation.resume() }
            }
        }
        return ["bundle_id": bundleId, "opened": true]
    }

    private func postNotification(_ payload: [String: Any]) async throws -> [String: Any] {
        guard Set(payload.keys) == ["title", "body", "category"],
              let title = payload["title"] as? String,
              !title.isEmpty,
              title.count <= 120,
              let body = payload["body"] as? String,
              body.count <= 500,
              let category = payload["category"] as? String,
              category.range(of: #"^[a-z][a-z0-9_-]{0,39}$"#, options: .regularExpression) != nil else {
            throw BridgeError.malformedPayload
        }

        let center = UNUserNotificationCenter.current()
        let settings = await center.notificationSettings()
        guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else {
            throw BridgeError.commandFailed("Desktop notifications are not enabled on this Mac.")
        }

        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.categoryIdentifier = category
        content.sound = .default
        let request = UNNotificationRequest(
            identifier: "chronos-\(UUID().uuidString.lowercased())",
            content: content,
            trigger: nil
        )
        try await center.add(request)
        return ["delivered": true, "category": category]
    }

    private func revokeGrant(_ payload: [String: Any]) throws -> [String: Any] {
        let id = try clientGrantId(payload)
        try grants.revoke(clientGrantId: id)
        return ["client_grant_id": id, "revoked": true]
    }

    private func clientGrantId(_ payload: [String: Any]) throws -> String {
        guard let id = payload["client_grant_id"] as? String,
              UUID(uuidString: id) != nil else {
            throw BridgeError.malformedPayload
        }
        return id
    }

    private func errorCode(_ error: BridgeError) -> String {
        switch error {
        case .missingGrant: return "grant_unavailable"
        case .pathEscape: return "path_escape_blocked"
        case .malformedPayload: return "invalid_payload"
        case .unsupportedCommand: return "unsupported_command"
        default: return "execution_failed"
        }
    }
}
