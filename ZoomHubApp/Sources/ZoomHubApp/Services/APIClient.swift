import Foundation

actor APIClient {
    static let shared = APIClient()

    private let baseURL = "http://127.0.0.1:8002"
    private let session: URLSession
    private let decoder: JSONDecoder

    private init() {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 30
        session = URLSession(configuration: config)
        decoder = JSONDecoder()
    }

    // MARK: - Health

    func healthCheck() async throws -> HealthStatus {
        try await get("/health")
    }

    func isBackendReady() async -> Bool {
        do {
            let _: HealthStatus = try await get("/health")
            return true
        } catch {
            return false
        }
    }

    // MARK: - Meetings

    func getMeetings(search: String? = nil, status: String? = nil) async throws -> [Meeting] {
        var path = "/api/meetings?"
        if let search, !search.isEmpty {
            path += "q=\(search.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? "")&"
        }
        if let status {
            path += "status=\(status)&"
        }
        return try await get(path)
    }

    func getMeeting(id: Int) async throws -> MeetingDetail {
        try await get("/api/meetings/\(id)/detail")
    }

    func getMeetingProgress(id: Int) async throws -> MeetingProgress {
        try await get("/api/meetings/\(id)/progress")
    }

    func uploadMeeting(fileURL: URL, title: String, folderId: Int?) async throws -> Meeting {
        let url = URL(string: "\(baseURL)/api/meetings/upload")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"

        let boundary = UUID().uuidString
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")

        var body = Data()
        let fileData = try Data(contentsOf: fileURL)
        let filename = fileURL.lastPathComponent

        // Title field
        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append("Content-Disposition: form-data; name=\"title\"\r\n\r\n".data(using: .utf8)!)
        body.append("\(title)\r\n".data(using: .utf8)!)

        // Folder ID
        if let folderId {
            body.append("--\(boundary)\r\n".data(using: .utf8)!)
            body.append("Content-Disposition: form-data; name=\"folder_id\"\r\n\r\n".data(using: .utf8)!)
            body.append("\(folderId)\r\n".data(using: .utf8)!)
        }

        // File
        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append("Content-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\n".data(using: .utf8)!)
        body.append("Content-Type: application/octet-stream\r\n\r\n".data(using: .utf8)!)
        body.append(fileData)
        body.append("\r\n--\(boundary)--\r\n".data(using: .utf8)!)

        request.httpBody = body
        request.timeoutInterval = 300

        let (data, response) = try await session.data(for: request)
        try validateResponse(response)
        return try decoder.decode(Meeting.self, from: data)
    }

    func renameMeeting(id: Int, title: String) async throws -> Meeting {
        try await patch("/api/meetings/\(id)", body: ["title": title])
    }

    func deleteMeeting(id: Int) async throws {
        try await delete("/api/meetings/\(id)")
    }

    func retryMeeting(id: Int) async throws {
        try await post("/api/meetings/\(id)/retry")
    }

    func resummarize(id: Int) async throws {
        try await post("/api/meetings/\(id)/resummarize")
    }

    // MARK: - Chat

    func getChatHistory(meetingId: Int) async throws -> [ChatMessage] {
        try await get("/api/meetings/\(meetingId)/chat/history")
    }

    func sendChatMessage(meetingId: Int, message: String) async throws -> ChatMessage {
        try await post("/api/meetings/\(meetingId)/chat", body: ["message": message])
    }

    func clearChat(meetingId: Int) async throws {
        try await delete("/api/meetings/\(meetingId)/chat")
    }

    // MARK: - Folders

    func getFolders() async throws -> [Folder] {
        try await get("/api/folders")
    }

    func createFolder(name: String, icon: String?, keywords: String?) async throws -> Folder {
        var body: [String: String] = ["name": name]
        if let icon { body["icon"] = icon }
        if let keywords { body["keywords"] = keywords }
        return try await post("/api/folders", body: body)
    }

    func deleteFolder(id: Int) async throws {
        try await delete("/api/folders/\(id)")
    }

    // MARK: - Settings

    func setLLMProvider(_ provider: String) async throws {
        try await post("/api/settings/llm-provider", body: ["provider": provider])
    }

    func setTranscriptionProvider(_ provider: String) async throws {
        try await post("/api/settings/transcription-provider", body: ["provider": provider])
    }

    func providerHealth(type: String) async throws -> [String: Any] {
        let url = URL(string: "\(baseURL)/api/settings/health/\(type)")!
        let (data, response) = try await session.data(from: url)
        try validateResponse(response)
        guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw APIError.decodingFailed
        }
        return json
    }

    // MARK: - Private Helpers

    private func get<T: Decodable>(_ path: String) async throws -> T {
        try await withRetry {
            let url = URL(string: "\(self.baseURL)\(path)")!
            let (data, response) = try await self.session.data(from: url)
            try self.validateResponse(response)

            // Защита: если бэкенд вернул HTML вместо JSON
            if let contentType = (response as? HTTPURLResponse)?.value(forHTTPHeaderField: "content-type"),
               contentType.contains("text/html") {
                throw APIError.backendNotRunning
            }

            return try self.decoder.decode(T.self, from: data)
        }
    }

    @discardableResult
    private func post<T: Decodable>(_ path: String, body: [String: String]? = nil) async throws -> T {
        let url = URL(string: "\(baseURL)\(path)")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"

        if let body {
            request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
            let encoded = body.map { "\($0.key)=\($0.value.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? "")" }.joined(separator: "&")
            request.httpBody = encoded.data(using: .utf8)
        }

        let (data, response) = try await session.data(for: request)
        try validateResponse(response)
        return try decoder.decode(T.self, from: data)
    }

    @discardableResult
    private func post(_ path: String, body: [String: String]? = nil) async throws -> Data {
        let url = URL(string: "\(baseURL)\(path)")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"

        if let body {
            request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
            let encoded = body.map { "\($0.key)=\($0.value.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? "")" }.joined(separator: "&")
            request.httpBody = encoded.data(using: .utf8)
        }

        let (data, response) = try await session.data(for: request)
        try validateResponse(response)
        return data
    }

    @discardableResult
    private func patch<T: Decodable>(_ path: String, body: [String: String]) async throws -> T {
        let url = URL(string: "\(baseURL)\(path)")!
        var request = URLRequest(url: url)
        request.httpMethod = "PATCH"
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        let encoded = body.map { "\($0.key)=\($0.value.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? "")" }.joined(separator: "&")
        request.httpBody = encoded.data(using: .utf8)
        let (data, response) = try await session.data(for: request)
        try validateResponse(response)
        return try decoder.decode(T.self, from: data)
    }

    private func delete(_ path: String) async throws {
        let url = URL(string: "\(baseURL)\(path)")!
        var request = URLRequest(url: url)
        request.httpMethod = "DELETE"
        let (_, response) = try await session.data(for: request)
        try validateResponse(response)
    }

    private func validateResponse(_ response: URLResponse) throws {
        guard let http = response as? HTTPURLResponse else {
            throw APIError.invalidResponse
        }
        guard (200...299).contains(http.statusCode) else {
            throw APIError.httpError(http.statusCode)
        }
    }

    /// Выполняет запрос с retry для 5xx ошибок (exponential backoff)
    private func withRetry<T>(maxAttempts: Int = 3, _ operation: () async throws -> T) async throws -> T {
        var lastError: Error?
        for attempt in 0..<maxAttempts {
            do {
                return try await operation()
            } catch let error as APIError {
                lastError = error
                if case .httpError(let code) = error, (500...599).contains(code), attempt < maxAttempts - 1 {
                    let delay = UInt64(pow(2.0, Double(attempt))) * 1_000_000_000
                    try await Task.sleep(nanoseconds: delay)
                    continue
                }
                throw error
            } catch {
                lastError = error
                if attempt < maxAttempts - 1 {
                    let delay = UInt64(pow(2.0, Double(attempt))) * 1_000_000_000
                    try await Task.sleep(nanoseconds: delay)
                    continue
                }
                throw error
            }
        }
        throw lastError ?? APIError.invalidResponse
    }
}

enum APIError: LocalizedError {
    case invalidResponse
    case httpError(Int)
    case decodingFailed
    case backendNotRunning

    var errorDescription: String? {
        switch self {
        case .invalidResponse: "Некорректный ответ сервера"
        case .httpError(let code): "Ошибка HTTP \(code)"
        case .decodingFailed: "Ошибка декодирования"
        case .backendNotRunning: "Бэкенд не запущен"
        }
    }
}
