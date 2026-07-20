import Foundation

enum BridgeSelfTest {
    private enum Failure: Error { case failed(String) }

    static func run() -> Bool {
        do {
            try verify()
            print("Chronos Desktop self-test passed: HMAC, URL policy, and folder jail")
            return true
        } catch {
            FileHandle.standardError.write(Data("Chronos Desktop self-test failed: \(error)\n".utf8))
            return false
        }
    }

    private static func expect(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
        guard try condition() else { throw Failure.failed(message) }
    }

    private static func expectError(_ message: String, operation: () throws -> Void) throws {
        do {
            try operation()
        } catch {
            return
        }
        throw Failure.failed(message)
    }

    private static func verify() throws {
        let secret = Data(repeating: 0x2a, count: 32)
        let payload = try JSONSerialization.data(withJSONObject: ["path": "reports"], options: .bridgeOptions)
        let envelope = CommandEnvelope(
            commandId: "cmd-1",
            deviceId: "dev-1",
            nonce: "nonce-1",
            commandType: "list_files",
            payloadB64: payload.base64EncodedString(),
            expiresAt: "2026-07-12T20:00:00Z",
            signature: ""
        )
        let message = BridgeCrypto.commandMessage(envelope, payload: payload)
        let signature = BridgeCrypto.signature(message: message, secret: secret)
        try expect(signature.count == 64, "lowercase hexadecimal HMAC encoding")
        try expect(signature == signature.lowercased(), "lowercase HMAC encoding")
        try expect(BridgeCrypto.verify(signature: signature, message: message, secret: secret), "valid command HMAC")
        try expect(
            !BridgeCrypto.verify(
                signature: signature,
                message: BridgeCrypto.commandMessage(envelope, payload: Data("tampered".utf8)),
                secret: secret
            ),
            "payload tamper rejection"
        )

        let result = try JSONSerialization.data(withJSONObject: ["exit_code": 0], options: .bridgeOptions)
        let resultMessage = BridgeCrypto.resultMessage(
            commandId: "cmd-1",
            deviceId: "dev-1",
            nonce: "nonce-1",
            status: "succeeded",
            errorCode: nil,
            result: result
        )
        let resultSignature = BridgeCrypto.signature(message: resultMessage, secret: secret)
        try expect(BridgeCrypto.verify(signature: resultSignature, message: resultMessage, secret: secret), "valid result HMAC")
        let changedResult = BridgeCrypto.resultMessage(
            commandId: "cmd-1",
            deviceId: "dev-1",
            nonce: "nonce-1",
            status: "failed",
            errorCode: "execution_failed",
            result: result
        )
        try expect(!BridgeCrypto.verify(signature: resultSignature, message: changedResult, secret: secret), "status tamper rejection")

        let client = BridgeAPIClient()
        try expect(try client.validatedBaseURL("https://api.cognisiatech.com").scheme == "https", "HTTPS URL")
        try expect(try client.validatedBaseURL("http://localhost:8000").host == "localhost", "loopback HTTP")
        try expectError("external HTTP rejected") { _ = try client.validatedBaseURL("http://example.com") }
        try expectError("file URL rejected") { _ = try client.validatedBaseURL("file:///tmp/socket") }

        let base = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let root = base.appendingPathComponent("allowed")
        let sibling = base.appendingPathComponent("private")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: sibling, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: base) }
        try Data("safe".utf8).write(to: root.appendingPathComponent("safe.txt"))
        try FileManager.default.createSymbolicLink(
            at: root.appendingPathComponent("escape"),
            withDestinationURL: sibling
        )
        let store = FolderGrantStore.shared
        try expect(try store.jailedURL(root: root, relativePath: "safe.txt").lastPathComponent == "safe.txt", "in-grant file")
        try expectError("parent escape blocked") { _ = try store.jailedURL(root: root, relativePath: "../private") }
        try expectError("symlink escape blocked") { _ = try store.jailedURL(root: root, relativePath: "escape") }
    }
}
