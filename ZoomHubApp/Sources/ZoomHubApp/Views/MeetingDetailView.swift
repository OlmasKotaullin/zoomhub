import SwiftUI

struct MeetingDetailView: View {
    let meetingId: Int

    @State private var detail: MeetingDetail?
    @State private var isLoading = true
    @State private var error: String?
    @State private var selectedTab: DetailTab = .summary
    @State private var pollingTimer: Timer?
    @State private var pollingStartTime: Date?
    @State private var isEditingTitle = false
    @State private var editTitle = ""
    private let pollingTimeout: TimeInterval = 15 * 60 // 15 минут

    enum DetailTab: String, CaseIterable {
        case summary = "Саммари"
        case transcript = "Транскрипт"
        case chat = "Чат"

        var icon: String {
            switch self {
            case .summary: "doc.text"
            case .transcript: "text.alignleft"
            case .chat: "bubble.left.and.bubble.right"
            }
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            if isLoading {
                ProgressView("Загрузка...")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let error {
                VStack(spacing: 12) {
                    Text(error).foregroundStyle(.secondary)
                    Button("Повторить") { Task { await load() } }
                        .buttonStyle(.bordered)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let detail {
                meetingContent(detail)
            }
        }
        .task { await load() }
        .onChange(of: meetingId) {
            Task { await load() }
        }
        .onDisappear { stopPolling() }
    }

    @ViewBuilder
    private func meetingContent(_ detail: MeetingDetail) -> some View {
        // Header
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    if isEditingTitle {
                        HStack(spacing: 8) {
                            TextField("Название", text: $editTitle)
                                .font(.title2.bold())
                                .textFieldStyle(.plain)
                                .onSubmit { saveTitle() }
                            Button("Сохранить") { saveTitle() }
                                .buttonStyle(.borderedProminent)
                                .controlSize(.small)
                            Button("Отмена") { isEditingTitle = false }
                                .buttonStyle(.bordered)
                                .controlSize(.small)
                        }
                    } else {
                        Text(detail.meeting.title)
                            .font(.title2.bold())
                            .onTapGesture(count: 2) {
                                editTitle = detail.meeting.title
                                isEditingTitle = true
                            }
                            .help("Двойной клик для переименования")
                    }
                    HStack(spacing: 8) {
                        Text(detail.meeting.formattedDate)
                        if !detail.meeting.formattedDuration.isEmpty {
                            Text("·")
                            Text(detail.meeting.formattedDuration)
                        }
                        StatusBadge(status: detail.meeting.status)
                    }
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                }
                Spacer()
                meetingActions(detail.meeting)
            }
            .padding(.horizontal, 24)
            .padding(.top, 16)
            .padding(.bottom, 8)

            // Tabs
            HStack(spacing: 0) {
                ForEach(DetailTab.allCases, id: \.self) { tab in
                    Button {
                        selectedTab = tab
                    } label: {
                        Label(tab.rawValue, systemImage: tab.icon)
                            .padding(.horizontal, 16)
                            .padding(.vertical, 8)
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(selectedTab == tab ? .primary : .secondary)
                    .background {
                        if selectedTab == tab {
                            RoundedRectangle(cornerRadius: 8)
                                .fill(.quaternary)
                        }
                    }
                }
            }
            .padding(.horizontal, 24)
            .padding(.bottom, 8)

            Divider()
        }

        // Content
        switch selectedTab {
        case .summary:
            SummaryTabView(summary: detail.summary, meeting: detail.meeting)
        case .transcript:
            TranscriptTabView(transcript: detail.transcript)
        case .chat:
            ChatTabView(meetingId: meetingId)
        }
    }

    private func meetingActions(_ meeting: Meeting) -> some View {
        HStack(spacing: 8) {
            if meeting.status == .ready, let detail {
                Menu {
                    Button("Markdown (.md)") {
                        ExportService.export(detail: detail, format: .markdown)
                    }
                    Button("Текст (.txt)") {
                        ExportService.export(detail: detail, format: .plainText)
                    }
                    Divider()
                    Button("Скопировать TLDR") {
                        ExportService.copyTLDR(summary: detail.summary)
                    }
                } label: {
                    Label("Экспорт", systemImage: "square.and.arrow.up")
                }
                .menuStyle(.borderlessButton)
                .frame(width: 100)
            }

            if meeting.status == .error {
                Button {
                    Task {
                        try? await APIClient.shared.retryMeeting(id: meetingId)
                        await load()
                    }
                } label: {
                    Label("Повторить", systemImage: "arrow.clockwise")
                }
                .buttonStyle(.bordered)
            }

            if meeting.status == .ready {
                Button {
                    Task {
                        try? await APIClient.shared.resummarize(id: meetingId)
                        await load()
                    }
                } label: {
                    Label("Пересобрать саммари", systemImage: "arrow.triangle.2.circlepath")
                }
                .buttonStyle(.bordered)
            }

            Button(role: .destructive) {
                Task {
                    try? await APIClient.shared.deleteMeeting(id: meetingId)
                }
            } label: {
                Image(systemName: "trash")
            }
            .buttonStyle(.bordered)
        }
    }

    @State private var wasProcessing = false

    private func load() async {
        isLoading = true
        error = nil
        do {
            let prev = detail?.meeting.status
            detail = try await APIClient.shared.getMeeting(id: meetingId)
            if let meeting = detail?.meeting {
                if meeting.status.isProcessing {
                    wasProcessing = true
                    startPolling()
                } else {
                    stopPolling()
                    // Нотификация при завершении обработки
                    if wasProcessing || prev?.isProcessing == true {
                        wasProcessing = false
                        if meeting.status == .ready {
                            NotificationManager.sendMeetingReady(title: meeting.title, meetingId: meetingId)
                        } else if meeting.status == .error {
                            NotificationManager.sendError(title: meeting.title, meetingId: meetingId)
                        }
                    }
                }
            }
        } catch {
            self.error = error.localizedDescription
        }
        isLoading = false
    }

    private func saveTitle() {
        let newTitle = editTitle.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !newTitle.isEmpty else { return }
        isEditingTitle = false
        Task {
            _ = try? await APIClient.shared.renameMeeting(id: meetingId, title: newTitle)
            await load()
        }
    }

    private func startPolling() {
        stopPolling()
        pollingStartTime = Date()
        pollingTimer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [self] _ in
            if let start = pollingStartTime, Date().timeIntervalSince(start) > pollingTimeout {
                stopPolling()
                return
            }
            Task { await load() }
        }
    }

    private func stopPolling() {
        pollingTimer?.invalidate()
        pollingTimer = nil
    }
}

// MARK: - Summary Tab

struct SummaryTabView: View {
    let summary: Summary?
    let meeting: Meeting
    @State private var copied = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                // Кнопка копирования
                if summary != nil {
                    HStack {
                        Spacer()
                        Button {
                            copyFullSummary()
                            copied = true
                            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { copied = false }
                        } label: {
                            Label(copied ? "Скопировано!" : "Копировать", systemImage: copied ? "checkmark" : "doc.on.doc")
                                .font(.caption)
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                    }
                }
                if let summary {
                    // TLDR
                    if let tldr = summary.tldr, !tldr.isEmpty {
                        summarySection("Краткое содержание", icon: "doc.text.fill") {
                            Text(tldr)
                                .font(.body)
                                .lineSpacing(4)
                        }
                    }

                    // Tasks
                    if let tasks = summary.tasks, !tasks.isEmpty {
                        summarySection("Задачи", icon: "checkmark.circle") {
                            ForEach(tasks) { task in
                                HStack(alignment: .top, spacing: 8) {
                                    Image(systemName: "circle")
                                        .font(.caption)
                                        .foregroundStyle(.blue)
                                        .padding(.top, 4)
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(task.task)
                                            .font(.body)
                                        HStack(spacing: 8) {
                                            if let assignee = task.assignee {
                                                Label(assignee, systemImage: "person")
                                                    .font(.caption)
                                                    .foregroundStyle(.secondary)
                                            }
                                            if let deadline = task.deadline {
                                                Label(deadline, systemImage: "calendar")
                                                    .font(.caption)
                                                    .foregroundStyle(.orange)
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }

                    // Topics
                    if let topics = summary.topics, !topics.isEmpty {
                        summarySection("Темы", icon: "tag") {
                            ForEach(topics) { topic in
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(topic.topic)
                                        .font(.body.bold())
                                    if let details = topic.details {
                                        Text(details)
                                            .font(.callout)
                                            .foregroundStyle(.secondary)
                                    }
                                }
                            }
                        }
                    }

                    // Insights
                    if let insights = summary.insights, !insights.isEmpty {
                        summarySection("Инсайты", icon: "lightbulb") {
                            ForEach(insights) { insight in
                                HStack(alignment: .top, spacing: 8) {
                                    Image(systemName: "sparkle")
                                        .foregroundStyle(.yellow)
                                        .font(.caption)
                                        .padding(.top, 3)
                                    Text(insight.insight)
                                        .font(.body)
                                }
                            }
                        }
                    }
                } else if meeting.status.isProcessing {
                    VStack(spacing: 12) {
                        ProgressView()
                            .controlSize(.large)
                        Text("Обработка встречи...")
                            .font(.headline)
                        Text(meeting.status.label)
                            .foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.top, 60)
                } else {
                    Text("Саммари недоступно")
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity)
                        .padding(.top, 60)
                }
            }
            .padding(24)
        }
    }

    private func copyFullSummary() {
        guard let s = summary else { return }
        var lines: [String] = []
        if let tldr = s.tldr { lines.append("Резюме: \(tldr)") }
        if let tasks = s.tasks, !tasks.isEmpty {
            lines.append("\nЗадачи:")
            for t in tasks {
                var l = "- \(t.task)"
                if let a = t.assignee { l += " → \(a)" }
                lines.append(l)
            }
        }
        if let topics = s.topics, !topics.isEmpty {
            lines.append("\nТемы:")
            for t in topics { lines.append("- \(t.topic)") }
        }
        if let insights = s.insights, !insights.isEmpty {
            lines.append("\nИнсайты:")
            for i in insights { lines.append("- \(i.insight)") }
        }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(lines.joined(separator: "\n"), forType: .string)
    }

    private func summarySection<Content: View>(_ title: String, icon: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Label(title, systemImage: icon)
                .font(.headline)
                .foregroundStyle(.primary)
            content()
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.controlBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
    }
}

// MARK: - Transcript Tab

struct TranscriptTabView: View {
    let transcript: Transcript?
    @State private var searchText = ""

    var body: some View {
        VStack(spacing: 0) {
            if let transcript, !transcript.fullText.isEmpty {
                // Поиск
                HStack(spacing: 8) {
                    Image(systemName: "magnifyingglass")
                        .foregroundStyle(.secondary)
                    TextField("Поиск в транскрипте...", text: $searchText)
                        .textFieldStyle(.plain)
                    if !searchText.isEmpty {
                        Text("\(matchCount) совпадений")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Button {
                            searchText = ""
                        } label: {
                            Image(systemName: "xmark.circle.fill")
                                .foregroundStyle(.secondary)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 24)
                .padding(.vertical, 10)
                .background(Color(.controlBackgroundColor))

                Divider()

                ScrollView {
                    if searchText.isEmpty {
                        Text(transcript.fullText)
                            .font(.body)
                            .lineSpacing(6)
                            .textSelection(.enabled)
                            .padding(24)
                    } else {
                        highlightedText
                            .padding(24)
                    }
                }
            } else {
                Text("Транскрипт недоступен")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .padding(.top, 60)
            }
        }
    }

    private var matchCount: Int {
        guard !searchText.isEmpty, let text = transcript?.fullText else { return 0 }
        return text.lowercased().components(separatedBy: searchText.lowercased()).count - 1
    }

    private var highlightedText: some View {
        let text = transcript?.fullText ?? ""
        let parts = text.components(separatedBy: searchText, caseInsensitive: true)

        return VStack(alignment: .leading, spacing: 0) {
            Text(buildHighlighted(text: text, query: searchText))
                .font(.body)
                .lineSpacing(6)
                .textSelection(.enabled)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func buildHighlighted(text: String, query: String) -> AttributedString {
        var result = AttributedString(text)
        let lowered = text.lowercased()
        let queryLow = query.lowercased()
        var searchStart = lowered.startIndex

        while let range = lowered.range(of: queryLow, range: searchStart..<lowered.endIndex) {
            let attrStart = AttributedString.Index(range.lowerBound, within: result)
            let attrEnd = AttributedString.Index(range.upperBound, within: result)
            if let attrStart, let attrEnd {
                result[attrStart..<attrEnd].backgroundColor = .yellow.opacity(0.4)
                result[attrStart..<attrEnd].font = .body.bold()
            }
            searchStart = range.upperBound
        }

        return result
    }
}

private extension String {
    func components(separatedBy separator: String, caseInsensitive: Bool) -> [String] {
        if caseInsensitive {
            return self.lowercased().components(separatedBy: separator.lowercased())
        }
        return self.components(separatedBy: separator)
    }
}
