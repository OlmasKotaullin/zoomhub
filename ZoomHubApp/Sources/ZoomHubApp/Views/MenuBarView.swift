import SwiftUI

struct MenuBarView: View {
    @Environment(BackendManager.self) private var backend
    @State private var recentMeetings: [Meeting] = []

    var body: some View {
        VStack(spacing: 4) {
            HStack {
                Circle()
                    .fill(statusColor)
                    .frame(width: 8, height: 8)
                Text(statusText)
                    .font(.callout)
            }

            Divider()

            if backend.isRunning {
                Button("Открыть ZoomHub") {
                    NSApp.activate(ignoringOtherApps: true)
                }

                if !recentMeetings.isEmpty {
                    Divider()
                    Text("Последние встречи")
                        .font(.caption)
                        .foregroundStyle(.secondary)

                    ForEach(recentMeetings.prefix(5)) { meeting in
                        Button {
                            NSApp.activate(ignoringOtherApps: true)
                        } label: {
                            HStack(spacing: 6) {
                                Image(systemName: meeting.status.icon)
                                    .font(.caption)
                                Text(meeting.title)
                                    .lineLimit(1)
                            }
                        }
                    }
                }

                Divider()
            }

            if !backend.isRunning {
                Button("Запустить бэкенд") {
                    backend.start()
                }
            } else {
                Button("Остановить бэкенд") {
                    backend.stop()
                }
            }

            Divider()

            Button("Выход") {
                backend.stop()
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                    NSApp.terminate(nil)
                }
            }
            .keyboardShortcut("q")
        }
        .padding(4)
        .task {
            if backend.isRunning { await loadRecent() }
        }
    }

    private var statusColor: Color {
        switch backend.state {
        case .running: .green
        case .starting: .orange
        case .stopped: .gray
        case .error: .red
        }
    }

    private var statusText: String {
        switch backend.state {
        case .running: "Работает"
        case .starting: "Запуск..."
        case .stopped: "Остановлен"
        case .error(let msg): "Ошибка: \(msg)"
        }
    }

    private func loadRecent() async {
        recentMeetings = (try? await APIClient.shared.getMeetings()) ?? []
    }
}
