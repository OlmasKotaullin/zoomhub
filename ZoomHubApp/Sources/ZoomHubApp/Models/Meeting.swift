import Foundation

enum MeetingStatus: String, Codable, CaseIterable {
    case downloading
    case transcribing
    case summarizing
    case ready
    case error

    var label: String {
        switch self {
        case .downloading: "Загрузка"
        case .transcribing: "Транскрипция"
        case .summarizing: "Саммари"
        case .ready: "Готово"
        case .error: "Ошибка"
        }
    }

    var icon: String {
        switch self {
        case .downloading: "arrow.down.circle"
        case .transcribing: "waveform"
        case .summarizing: "brain"
        case .ready: "checkmark.circle.fill"
        case .error: "exclamationmark.triangle.fill"
        }
    }

    var isProcessing: Bool {
        self == .downloading || self == .transcribing || self == .summarizing
    }
}

struct Meeting: Identifiable, Codable {
    let id: Int
    var title: String
    let date: String
    let durationSeconds: Int?
    let source: String
    var status: MeetingStatus
    let audioPath: String?
    let folderId: Int?
    let folderName: String?
    let createdAt: String

    enum CodingKeys: String, CodingKey {
        case id, title, date, source, status
        case durationSeconds = "duration_seconds"
        case audioPath = "audio_path"
        case folderId = "folder_id"
        case folderName = "folder_name"
        case createdAt = "created_at"
    }

    var formattedDuration: String {
        guard let secs = durationSeconds, secs > 0 else { return "" }
        let h = secs / 3600
        let m = (secs % 3600) / 60
        if h > 0 { return "\(h)ч \(m)м" }
        return "\(m) мин"
    }

    var formattedDate: String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withFullDate, .withDashSeparatorInDate]
        guard let d = formatter.date(from: String(date.prefix(10))) else { return date }
        let out = DateFormatter()
        out.locale = Locale(identifier: "ru_RU")
        out.dateFormat = "d MMMM yyyy"
        return out.string(from: d)
    }
}

struct Transcript: Codable {
    let id: Int
    let fullText: String
    let segments: [TranscriptSegment]?

    enum CodingKeys: String, CodingKey {
        case id
        case fullText = "full_text"
        case segments
    }
}

struct TranscriptSegment: Codable, Identifiable {
    var id: String { "\(start)-\(end)" }
    let start: Double
    let end: Double
    let speaker: String?
    let text: String
}

struct Summary: Codable {
    let id: Int
    let tldr: String?
    let tasks: [SummaryTask]?
    let topics: [SummaryTopic]?
    let insights: [SummaryInsight]?

    struct SummaryTask: Codable, Identifiable {
        var id: String { task }
        let task: String
        let assignee: String?
        let deadline: String?
    }

    struct SummaryTopic: Codable, Identifiable {
        var id: String { topic }
        let topic: String
        let details: String?
    }

    struct SummaryInsight: Codable, Identifiable {
        var id: String { insight }
        let insight: String
    }
}

struct MeetingDetail: Codable {
    let meeting: Meeting
    let transcript: Transcript?
    let summary: Summary?
}

struct Folder: Identifiable, Codable {
    let id: Int
    let name: String
    let icon: String?
    let keywords: String?
    let meetingCount: Int?

    enum CodingKeys: String, CodingKey {
        case id, name, icon, keywords
        case meetingCount = "meeting_count"
    }
}

struct ChatMessage: Identifiable, Codable {
    let id: Int?
    let role: String
    let content: String
    let createdAt: String?

    enum CodingKeys: String, CodingKey {
        case id, role, content
        case createdAt = "created_at"
    }

    var isUser: Bool { role == "user" }
}

struct HealthStatus: Codable {
    let status: String
    let llmProvider: String?
    let transcriptionProvider: String?

    enum CodingKeys: String, CodingKey {
        case status
        case llmProvider = "llm_provider"
        case transcriptionProvider = "transcription_provider"
    }
}

struct MeetingProgress: Codable {
    let status: MeetingStatus
    let progress: Int
    let message: String?
}

struct DashboardData: Codable {
    let meetings: [Meeting]
    let folders: [Folder]
}
