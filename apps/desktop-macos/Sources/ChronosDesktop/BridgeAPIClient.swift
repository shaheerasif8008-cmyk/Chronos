import Foundation

final class BridgeAPIClient {
    private let session: URLSession
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder

    init(session: URLSession = .shared) {
        self.session = session
        self.encoder = JSONEncoder()
        self.decoder = JSONDecoder()
    }

    func validatedBaseURL(_ raw: String) throws -> URL {
        guard var components = URLComponents(string: raw.trimmingCharacters(in: .whitespacesAndNewlines)),
              let scheme = components.scheme?.lowercased(),
              let host = components.host?.lowercased(),
              !host.isEmpty else {
            throw BridgeError.invalidServerURL
        }
        let localHosts = ["localhost", "127.0.0.1", "::1"]
        guard scheme == "https" || (scheme == "http" && localHosts.contains(host)) else {
            throw BridgeError.insecureServerURL
        }
        components.path = components.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = components.url else { throw BridgeError.invalidServerURL }
        return url
    }

    func pair(baseURL: URL, request body: PairDeviceRequest) async throws -> PairDeviceResponse {
        try await send(
            baseURL: baseURL,
            path: "/desktop-devices/pair",
            method: "POST",
            body: body,
            token: nil
        )
    }

    func heartbeat(
        baseURL: URL,
        deviceId: String,
        token: String,
        appVersion: String
    ) async throws {
        let body = ["app_version": appVersion, "platform": "macos"]
        let _: EmptyResponse = try await send(
            baseURL: baseURL,
            path: "/desktop-devices/\(encoded(deviceId))/heartbeat",
            method: "POST",
            body: body,
            token: token
        )
    }

    func nextCommand(baseURL: URL, deviceId: String, token: String) async throws -> CommandEnvelope? {
        let request = try makeRequest(
            baseURL: baseURL,
            path: "/desktop-devices/\(encoded(deviceId))/commands/poll",
            method: "GET",
            body: Optional<String>.none,
            token: token
        )
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw BridgeError.invalidResponse }
        if http.statusCode == 204 { return nil }
        try validate(http: http, data: data)
        do {
            return try decoder.decode(CommandEnvelope.self, from: data)
        } catch {
            throw BridgeError.invalidResponse
        }
    }

    func submitResult(
        baseURL: URL,
        deviceId: String,
        commandId: String,
        token: String,
        submission: CommandResultSubmission
    ) async throws {
        let _: EmptyResponse = try await send(
            baseURL: baseURL,
            path: "/desktop-devices/\(encoded(deviceId))/commands/\(encoded(commandId))/result",
            method: "POST",
            body: submission,
            token: token
        )
    }

    func registerGrant(
        baseURL: URL,
        deviceId: String,
        token: String,
        clientGrantId: String,
        displayName: String
    ) async throws -> RegisteredGrant {
        try await send(
            baseURL: baseURL,
            path: "/desktop-devices/\(encoded(deviceId))/grants",
            method: "POST",
            body: RegisterGrantRequest(clientGrantId: clientGrantId, displayName: displayName),
            token: token
        )
    }

    func revokeGrant(
        baseURL: URL,
        deviceId: String,
        token: String,
        clientGrantId: String
    ) async throws {
        let _: EmptyResponse = try await send(
            baseURL: baseURL,
            path: "/desktop-devices/\(encoded(deviceId))/grants/\(encoded(clientGrantId))/revoke",
            method: "POST",
            body: ["reason": "revoked from Chronos Desktop"],
            token: token
        )
    }

    func disconnect(baseURL: URL, deviceId: String, token: String) async throws {
        let _: EmptyResponse = try await send(
            baseURL: baseURL,
            path: "/desktop-devices/\(encoded(deviceId))/disconnect",
            method: "POST",
            body: ["reason": "disconnected from Chronos Desktop"],
            token: token
        )
    }

    private func send<Response: Decodable, Body: Encodable>(
        baseURL: URL,
        path: String,
        method: String,
        body: Body,
        token: String?
    ) async throws -> Response {
        let request = try makeRequest(baseURL: baseURL, path: path, method: method, body: body, token: token)
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw BridgeError.invalidResponse }
        try validate(http: http, data: data)
        if Response.self == EmptyResponse.self, data.isEmpty {
            return EmptyResponse() as! Response
        }
        do {
            return try decoder.decode(Response.self, from: data)
        } catch {
            if Response.self == EmptyResponse.self {
                return EmptyResponse() as! Response
            }
            throw BridgeError.invalidResponse
        }
    }

    private func makeRequest<Body: Encodable>(
        baseURL: URL,
        path: String,
        method: String,
        body: Body?,
        token: String?
    ) throws -> URLRequest {
        guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL else {
            throw BridgeError.invalidServerURL
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = method == "GET" ? 35 : 20
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("ChronosDesktop/1", forHTTPHeaderField: "User-Agent")
        if let token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            request.httpBody = try encoder.encode(body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return request
    }

    private func validate(http: HTTPURLResponse, data: Data) throws {
        guard (200..<300).contains(http.statusCode) else {
            var message = "Chronos request failed (\(http.statusCode))."
            if data.count <= 16_384,
               let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let detail = object["detail"] as? String,
               !detail.isEmpty {
                message = String(detail.prefix(500))
            }
            throw BridgeError.server(status: http.statusCode, message: message)
        }
    }

    private func encoded(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? value
    }
}
