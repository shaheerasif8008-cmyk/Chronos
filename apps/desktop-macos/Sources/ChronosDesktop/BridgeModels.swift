import CryptoKit
import Foundation

enum BridgeStatus: String, Codable {
    case disconnected
    case pairing
    case online
    case degraded
    case revoked
}

struct PairDeviceRequest: Encodable {
    let code: String
    let displayName: String
    let platform: String
    let appVersion: String

    enum CodingKeys: String, CodingKey {
        case code
        case displayName = "display_name"
        case platform
        case appVersion = "app_version"
    }
}

struct PairDeviceResponse: Decodable {
    let deviceId: String
    let deviceToken: String
    let commandSecretB64: String

    enum CodingKeys: String, CodingKey {
        case deviceId = "device_id"
        case deviceToken = "device_token"
        case commandSecretB64 = "command_secret_b64"
    }
}

struct DeviceSummary: Decodable, Identifiable {
    let id: String
    let displayName: String
    let platform: String
    let status: String
    let lastSeenAt: String?

    enum CodingKeys: String, CodingKey {
        case id
        case displayName = "display_name"
        case platform
        case status
        case lastSeenAt = "last_seen_at"
    }
}

struct RegisterGrantRequest: Encodable {
    let clientGrantId: String
    let displayName: String

    enum CodingKeys: String, CodingKey {
        case clientGrantId = "client_grant_id"
        case displayName = "display_name"
    }
}

struct RegisteredGrant: Codable, Identifiable {
    let id: String
    let clientGrantId: String
    let displayName: String
    let status: String

    enum CodingKeys: String, CodingKey {
        case id
        case clientGrantId = "client_grant_id"
        case displayName = "display_name"
        case status
    }
}

struct CommandEnvelope: Decodable {
    let commandId: String
    let deviceId: String
    let nonce: String
    let commandType: String
    let payloadB64: String
    let expiresAt: String
    let signature: String

    enum CodingKeys: String, CodingKey {
        case commandId = "command_id"
        case deviceId = "device_id"
        case nonce
        case commandType = "command_type"
        case payloadB64 = "payload_b64"
        case expiresAt = "expires_at"
        case signature
    }
}

struct CommandResultSubmission: Codable {
    let nonce: String
    let status: String
    let errorCode: String?
    let resultB64: String
    let signature: String

    enum CodingKeys: String, CodingKey {
        case nonce
        case status
        case errorCode = "error_code"
        case resultB64 = "result_b64"
        case signature
    }
}

struct CommandExecutionResult {
    let status: String
    let errorCode: String?
    let body: [String: Any]
}

struct EmptyResponse: Decodable {}

enum BridgeError: LocalizedError {
    case invalidServerURL
    case insecureServerURL
    case invalidResponse
    case server(status: Int, message: String)
    case missingCredentials
    case invalidSecret
    case invalidSignature
    case expiredCommand
    case replayedCommand
    case malformedPayload
    case missingGrant
    case pathEscape
    case unsupportedCommand
    case commandFailed(String)

    var errorDescription: String? {
        switch self {
        case .invalidServerURL: return "Enter a valid Chronos API URL."
        case .insecureServerURL: return "Chronos requires HTTPS outside local development."
        case .invalidResponse: return "Chronos returned an invalid response."
        case let .server(_, message): return message
        case .missingCredentials: return "This Mac is not paired."
        case .invalidSecret: return "The stored device secret is invalid."
        case .invalidSignature: return "Chronos rejected an untrusted command signature."
        case .expiredCommand: return "The command expired before it reached this Mac."
        case .replayedCommand: return "Chronos blocked a replayed command."
        case .malformedPayload: return "The command payload is malformed."
        case .missingGrant: return "The requested folder is no longer authorized."
        case .pathEscape: return "The command tried to leave its authorized folder."
        case .unsupportedCommand: return "This version of Chronos Desktop does not support that command."
        case let .commandFailed(message): return message
        }
    }
}

enum BridgeCrypto {
    static func sha256Hex(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    static func commandMessage(_ envelope: CommandEnvelope, payload: Data) -> Data {
        Data(
            [
                "command:v1",
                envelope.commandId,
                envelope.deviceId,
                envelope.nonce,
                envelope.commandType,
                envelope.expiresAt,
                sha256Hex(payload),
            ].joined(separator: "\n").utf8
        )
    }

    static func resultMessage(
        commandId: String,
        deviceId: String,
        nonce: String,
        status: String,
        errorCode: String?,
        result: Data
    ) -> Data {
        Data(
            [
                "result:v1",
                commandId,
                deviceId,
                nonce,
                status,
                errorCode ?? "",
                sha256Hex(result),
            ].joined(separator: "\n").utf8
        )
    }

    static func signature(message: Data, secret: Data) -> String {
        let key = SymmetricKey(data: secret)
        let digest = HMAC<SHA256>.authenticationCode(for: message, using: key)
        return Data(digest).map { String(format: "%02x", $0) }.joined()
    }

    static func verify(signature: String, message: Data, secret: Data) -> Bool {
        guard signature.count == 64,
              signature.allSatisfy({ $0.isHexDigit }),
              let supplied = Data(hexadecimal: signature) else { return false }
        let key = SymmetricKey(data: secret)
        return HMAC<SHA256>.isValidAuthenticationCode(supplied, authenticating: message, using: key)
    }
}

private extension Data {
    init?(hexadecimal: String) {
        guard hexadecimal.count.isMultiple(of: 2) else { return nil }
        self.init(capacity: hexadecimal.count / 2)
        var index = hexadecimal.startIndex
        while index < hexadecimal.endIndex {
            let next = hexadecimal.index(index, offsetBy: 2)
            guard let byte = UInt8(hexadecimal[index..<next], radix: 16) else { return nil }
            append(byte)
            index = next
        }
    }
}

extension JSONSerialization.WritingOptions {
    static var bridgeOptions: JSONSerialization.WritingOptions { [.sortedKeys, .withoutEscapingSlashes] }
}
