import SwiftUI

struct DashboardView: View {
    var folderId: Int? = nil

    @Environment(AppState.self) private var appState
    @State private var meetings: [Meeting] = []
    @State private var folders: [Folder] = []
    @State private var searchText = ""
    @State private var selectedFilter: MeetingStatus? = nil
    @State private var isLoading = true
    @State private var error: String?
    @State private var autoRefreshTimer: Timer?

    var body: some View {
        VStack(spacing: 0) {
            headerBar

            // Статистика
            if !meetings.isEmpty {
                statsBar
            }

            Divider()
            if isLoading {
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let error {
                errorBanner(error)
            } else if filteredMeetings.isEmpty {
                emptyState
            } else {
                meetingsList
            }
        }
        .task { await loadData() }
        .onChange(of: appState.refreshTrigger) {
            Task { await loadData() }
        }
        .onDisappear { autoRefreshTimer?.invalidate() }
    }

    private var statsBar: some View {
        HStack(spacing: 16) {
            statChip(value: "\(meetings.count)", label: "всего", icon: "list.bullet", color: .secondary)
            statChip(value: "\(meetings.filter { $0.status == .ready }.count)", label: "готово", icon: "checkmark.circle", color: .green)

            let processing = meetings.filter { $0.status.isProcessing }.count
            if processing > 0 {
                statChip(value: "\(processing)", label: "в работе", icon: "clock", color: .blue)
            }

            let errors = meetings.filter { $0.status == .error }.count
            if errors > 0 {
                statChip(value: "\(errors)", label: "ошибок", icon: "exclamationmark.triangle", color: .red)
            }

            Spacer()
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 8)
    }

    private func statChip(value: String, label: String, icon: String, color: Color) -> some View {
        HStack(spacing: 4) {
            Image(systemName: icon)
                .font(.caption2)
                .foregroundStyle(color)
            Text(value)
                .font(.caption.bold())
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 4)
        .background(color.opacity(0.08), in: Capsule())
    }

    private var headerBar: some View {
        HStack(spacing: 12) {
            Text("Встречи")
                .font(.title.bold())

            Spacer()

            // Фильтр по статусу
            Picker("Статус", selection: $selectedFilter) {
                Text("Все").tag(Optional<MeetingStatus>.none)
                ForEach(MeetingStatus.allCases, id: \.self) { status in
                    Label(status.label, systemImage: status.icon)
                        .tag(Optional(status))
                }
            }
            .pickerStyle(.menu)
            .frame(width: 140)

            // Поиск
            HStack {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(.secondary)
                TextField("Поиск...", text: $searchText)
                    .textFieldStyle(.plain)
                if !searchText.isEmpty {
                    Button {
                        searchText = ""
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(8)
            .background(.quaternary, in: RoundedRectangle(cornerRadius: 8))
            .frame(width: 220)

            Button {
                appState.showUploadSheet = true
            } label: {
                Label("Загрузить", systemImage: "plus")
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.regular)
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 16)
    }

    private var filteredMeetings: [Meeting] {
        meetings.filter { m in
            if let folderId, m.folderId != folderId { return false }
            if let filter = selectedFilter, m.status != filter { return false }
            if !searchText.isEmpty {
                return m.title.localizedCaseInsensitiveContains(searchText)
            }
            return true
        }
    }

    private var meetingsList: some View {
        ScrollView {
            LazyVStack(spacing: 8) {
                ForEach(filteredMeetings) { meeting in
                    MeetingRow(meeting: meeting)
                        .onTapGesture {
                            appState.selectedMeetingId = meeting.id
                        }
                        .contextMenu {
                            Button("Открыть") {
                                appState.selectedMeetingId = meeting.id
                            }

                            if meeting.status == .error {
                                Button {
                                    Task { try? await APIClient.shared.retryMeeting(id: meeting.id) }
                                } label: {
                                    Label("Повторить обработку", systemImage: "arrow.clockwise")
                                }
                            }

                            if meeting.status == .ready {
                                Button {
                                    Task { try? await APIClient.shared.resummarize(id: meeting.id) }
                                } label: {
                                    Label("Пересобрать саммари", systemImage: "arrow.triangle.2.circlepath")
                                }
                            }

                            Divider()

                            Button(role: .destructive) {
                                Task {
                                    try? await APIClient.shared.deleteMeeting(id: meeting.id)
                                    await loadData()
                                }
                            } label: {
                                Label("Удалить", systemImage: "trash")
                            }
                        }
                }
            }
            .padding(24)
        }
    }

    private var emptyState: some View {
        VStack(spacing: 16) {
            Image(systemName: "waveform.slash")
                .font(.system(size: 48))
                .foregroundStyle(.tertiary)
            Text(searchText.isEmpty ? "Нет встреч" : "Ничего не найдено")
                .font(.title3)
                .foregroundStyle(.secondary)
            if searchText.isEmpty {
                Button("Загрузить запись") {
                    appState.showUploadSheet = true
                }
                .buttonStyle(.borderedProminent)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func errorBanner(_ msg: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: "wifi.slash")
                .font(.system(size: 36))
                .foregroundStyle(.orange)
            Text(msg)
                .foregroundStyle(.secondary)
            Button("Повторить") {
                Task { await loadData() }
            }
            .buttonStyle(.bordered)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func loadData() async {
        isLoading = true
        error = nil
        do {
            let m = try await APIClient.shared.getMeetings()
            let f = try await APIClient.shared.getFolders()
            meetings = m
            folders = f

            // Автообновление если есть встречи в обработке
            let hasProcessing = meetings.contains { $0.status.isProcessing }
            if hasProcessing {
                startAutoRefresh()
            } else {
                autoRefreshTimer?.invalidate()
            }
        } catch let decodingError as DecodingError {
            switch decodingError {
            case .typeMismatch(let type, let context):
                self.error = "Тип \(type) не совпадает: \(context.codingPath.map(\.stringValue).joined(separator: ".")) — \(context.debugDescription)"
            case .keyNotFound(let key, let context):
                self.error = "Ключ '\(key.stringValue)' не найден: \(context.codingPath.map(\.stringValue).joined(separator: "."))"
            case .valueNotFound(let type, let context):
                self.error = "Значение \(type) не найдено: \(context.codingPath.map(\.stringValue).joined(separator: "."))"
            case .dataCorrupted(let context):
                self.error = "Данные повреждены: \(context.debugDescription)"
            @unknown default:
                self.error = "Ошибка декодирования: \(decodingError.localizedDescription)"
            }
        } catch {
            self.error = "Ошибка: \(error)"
        }
        isLoading = false
    }

    private func startAutoRefresh() {
        guard autoRefreshTimer == nil else { return }
        autoRefreshTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { _ in
            Task {
                let updated = try? await APIClient.shared.getMeetings()
                if let updated {
                    await MainActor.run { meetings = updated }
                    if !updated.contains(where: { $0.status.isProcessing }) {
                        await MainActor.run {
                            autoRefreshTimer?.invalidate()
                            autoRefreshTimer = nil
                        }
                    }
                }
            }
        }
    }
}

// MARK: - Meeting Row

struct MeetingRow: View {
    let meeting: Meeting

    var body: some View {
        HStack(spacing: 16) {
            // Статус-иконка
            Image(systemName: meeting.status.icon)
                .font(.title3)
                .foregroundStyle(statusColor)
                .frame(width: 32)

            VStack(alignment: .leading, spacing: 4) {
                Text(meeting.title)
                    .font(.headline)
                    .lineLimit(1)

                HStack(spacing: 8) {
                    Text(meeting.formattedDate)
                        .font(.caption)
                        .foregroundStyle(.secondary)

                    if !meeting.formattedDuration.isEmpty {
                        Text("·")
                            .foregroundStyle(.tertiary)
                        Text(meeting.formattedDuration)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }

                    if let folder = meeting.folderName {
                        Text("·")
                            .foregroundStyle(.tertiary)
                        Text(folder)
                            .font(.caption)
                            .foregroundStyle(.blue)
                    }
                }
            }

            Spacer()

            StatusBadge(status: meeting.status)
        }
        .padding(12)
        .background(Color(.controlBackgroundColor), in: RoundedRectangle(cornerRadius: 10))
        .contentShape(Rectangle())
    }

    private var statusColor: Color {
        switch meeting.status {
        case .ready: .green
        case .error: .red
        case .downloading, .transcribing, .summarizing: .blue
        }
    }
}

struct StatusBadge: View {
    let status: MeetingStatus

    var body: some View {
        HStack(spacing: 4) {
            if status.isProcessing {
                ProgressView()
                    .controlSize(.mini)
            }
            Text(status.label)
                .font(.caption.bold())
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 4)
        .background(backgroundColor.opacity(0.15), in: Capsule())
        .foregroundStyle(backgroundColor)
    }

    private var backgroundColor: Color {
        switch status {
        case .ready: .green
        case .error: .red
        case .downloading: .blue
        case .transcribing: .purple
        case .summarizing: .indigo
        }
    }
}
