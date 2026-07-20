import AppKit
import Foundation

struct LocalGrantMetadata: Codable, Identifiable {
    let id: String
    let serverGrantId: String
    let displayName: String

    enum CodingKeys: String, CodingKey {
        case id
        case serverGrantId = "server_grant_id"
        case displayName = "display_name"
    }
}

final class ScopedFolderAccess {
    let url: URL
    private var active = true

    init(url: URL) { self.url = url }

    func close() {
        guard active else { return }
        active = false
        url.stopAccessingSecurityScopedResource()
    }

    deinit { close() }
}

final class FolderGrantStore {
    static let shared = FolderGrantStore()

    private let defaultsKey = "chronos.folderGrantMetadata.v1"
    private let keychain = KeychainStore.shared
    private let lock = NSLock()

    private init() {}

    func metadata() -> [LocalGrantMetadata] {
        lock.lock()
        defer { lock.unlock() }
        guard let data = UserDefaults.standard.data(forKey: defaultsKey),
              let grants = try? JSONDecoder().decode([LocalGrantMetadata].self, from: data) else {
            return []
        }
        return grants
    }

    func authorizeFolder(url: URL, clientGrantId: String, serverGrantId: String) throws -> LocalGrantMetadata {
        let bookmark = try url.bookmarkData(
            options: [.withSecurityScope],
            includingResourceValuesForKeys: nil,
            relativeTo: nil
        )
        try keychain.save(bookmark, account: bookmarkAccount(clientGrantId))
        let item = LocalGrantMetadata(
            id: clientGrantId,
            serverGrantId: serverGrantId,
            displayName: url.lastPathComponent
        )
        var current = metadata().filter { $0.id != clientGrantId }
        current.append(item)
        try saveMetadata(current.sorted { $0.displayName.localizedCaseInsensitiveCompare($1.displayName) == .orderedAscending })
        return item
    }

    func revoke(clientGrantId: String) throws {
        keychain.delete(account: bookmarkAccount(clientGrantId))
        try saveMetadata(metadata().filter { $0.id != clientGrantId })
    }

    func removeAll() {
        metadata().forEach { keychain.delete(account: bookmarkAccount($0.id)) }
        UserDefaults.standard.removeObject(forKey: defaultsKey)
    }

    func withAuthorizedFolder<T>(clientGrantId: String, _ operation: (URL) throws -> T) throws -> T {
        let access = try accessAuthorizedFolder(clientGrantId: clientGrantId)
        defer { access.close() }
        return try operation(access.url)
    }

    func accessAuthorizedFolder(clientGrantId: String) throws -> ScopedFolderAccess {
        guard let bookmark = keychain.data(account: bookmarkAccount(clientGrantId)) else {
            throw BridgeError.missingGrant
        }
        var stale = false
        let url: URL
        do {
            url = try URL(
                resolvingBookmarkData: bookmark,
                options: [.withSecurityScope, .withoutUI],
                relativeTo: nil,
                bookmarkDataIsStale: &stale
            )
        } catch {
            throw BridgeError.missingGrant
        }
        guard !stale else { throw BridgeError.missingGrant }
        guard url.startAccessingSecurityScopedResource() else { throw BridgeError.missingGrant }
        return ScopedFolderAccess(url: url.resolvingSymlinksInPath().standardizedFileURL)
    }

    func jailedURL(root: URL, relativePath: String, mustExist: Bool = true) throws -> URL {
        guard !relativePath.contains("\0") else { throw BridgeError.pathEscape }
        let candidate = root.appendingPathComponent(relativePath.isEmpty ? "." : relativePath)
            .standardizedFileURL
            .resolvingSymlinksInPath()
        let rootPath = root.resolvingSymlinksInPath().standardizedFileURL.path
        let candidatePath = candidate.path
        guard candidatePath == rootPath || candidatePath.hasPrefix(rootPath + "/") else {
            throw BridgeError.pathEscape
        }
        if mustExist, !FileManager.default.fileExists(atPath: candidatePath) {
            throw BridgeError.commandFailed("The requested file no longer exists.")
        }
        return candidate
    }

    private func bookmarkAccount(_ id: String) -> String { "bookmark:\(id)" }

    private func saveMetadata(_ grants: [LocalGrantMetadata]) throws {
        let data = try JSONEncoder().encode(grants)
        lock.lock()
        UserDefaults.standard.set(data, forKey: defaultsKey)
        lock.unlock()
    }
}
