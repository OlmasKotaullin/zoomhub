import SwiftUI

struct ContentView: View {
    @Environment(BackendManager.self) private var backend
    @Environment(AppState.self) private var appState

    enum Tab: Hashable {
        case dashboard
        case settings
        case folder(Int)
    }

    @State private var selectedTab: Tab = .dashboard
    @State private var showUpload = false
    @State private var folders: [Folder] = []
    @State private var showOnboarding = !UserDefaults.standard.bool(forKey: "onboardingDone")
    @State private var updateChecker = UpdateChecker()

    var body: some View {
        Group {
            if showOnboarding {
                OnboardingView()
                    .environment(backend)
                    .onChange(of: backend.state) {
                        if backend.state == .running {
                            UserDefaults.standard.set(true, forKey: "onboardingDone")
                            showOnboarding = false
                        }
                    }
            } else {
                appContent
            }
        }
        .animation(.easeInOut(duration: 0.3), value: showOnboarding)
        .animation(.easeInOut(duration: 0.3), value: backend.state == .running)
        .onChange(of: appState.showUploadSheet) {
            showUpload = appState.showUploadSheet
        }
        .onChange(of: showUpload) {
            appState.showUploadSheet = showUpload
        }
    }

    private var appContent: some View {
        Group {
            switch backend.state {
            case .stopped, .starting:
                StartingView()
            case .error(let msg):
                ErrorView(message: msg, onRetry: {
                    backend.start()
                }, onPickFolder: {
                    backend.pickProjectDir()
                })
            case .running:
                mainContent
            }
        }
        .animation(.easeInOut(duration: 0.3), value: backend.state == .running)
        .onChange(of: appState.showUploadSheet) {
            showUpload = appState.showUploadSheet
        }
        .onChange(of: showUpload) {
            appState.showUploadSheet = showUpload
        }
    }

    private var mainContent: some View {
        VStack(spacing: 0) {
            if updateChecker.showBanner, let update = updateChecker.availableUpdate {
                updateBanner(update)
            }

            NavigationSplitView {
                sidebar
            } detail: {
                detailView
            }
        }
        .sheet(isPresented: $showUpload) {
            UploadView()
                .environment(appState)
        }
        .task {
            updateChecker.checkIfNeeded()
        }
    }

    private func updateBanner(_ update: UpdateChecker.Release) -> some View {
        HStack(spacing: 12) {
            Image(systemName: "arrow.down.circle.fill")
                .foregroundStyle(.blue)
            Text("Доступна версия \(update.version)")
                .font(.callout)
            Spacer()
            Button("Скачать") {
                updateChecker.openDownload()
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.small)
            Button {
                withAnimation { updateChecker.dismiss() }
            } label: {
                Image(systemName: "xmark")
                    .font(.caption)
            }
            .buttonStyle(.borderless)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .background(Color.blue.opacity(0.1))
        .transition(.move(edge: .top).combined(with: .opacity))
    }

    private var sidebar: some View {
        VStack(spacing: 0) {
            List {
                Section("Навигация") {
                    sidebarItem(label: "Все встречи", icon: "list.bullet.rectangle", tab: .dashboard)
                    sidebarItem(label: "Настройки", icon: "gear", tab: .settings)
                }

                Section("Папки") {
                    ForEach(folders) { folder in
                        sidebarItem(
                            label: "\(folder.icon ?? "📁") \(folder.name)",
                            icon: "folder",
                            tab: .folder(folder.id)
                        )
                    }

                    if folders.isEmpty {
                        Text("Нет папок")
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                    }
                }
            }
            .listStyle(.sidebar)
        }
        .frame(minWidth: 200)
        .toolbar {
            ToolbarItem {
                Button {
                    showUpload = true
                } label: {
                    Image(systemName: "plus")
                }
                .help("Загрузить запись")
            }
        }
        .task { await loadFolders() }
        .onChange(of: appState.refreshTrigger) {
            Task { await loadFolders() }
        }
    }

    private func sidebarItem(label: String, icon: String, tab: Tab) -> some View {
        Button {
            selectedTab = tab
            appState.selectedMeetingId = nil
        } label: {
            Label(label, systemImage: icon)
        }
        .buttonStyle(.plain)
        .padding(.vertical, 4)
    }

    @ViewBuilder
    private var detailView: some View {
        if let meetingId = appState.selectedMeetingId {
            MeetingDetailView(meetingId: meetingId)
        } else {
            switch selectedTab {
            case .dashboard:
                DashboardView()
                    .environment(appState)
            case .settings:
                SettingsView()
            case .folder(let folderId):
                DashboardView(folderId: folderId)
                    .environment(appState)
            }
        }
    }

    private func loadFolders() async {
        folders = (try? await APIClient.shared.getFolders()) ?? []
    }
}

// MARK: - Starting View

struct StartingView: View {
    var body: some View {
        VStack(spacing: 16) {
            ProgressView()
                .controlSize(.large)
            Text("Запуск ZoomHub...")
                .font(.title3)
                .foregroundStyle(.secondary)
            Text("Подключение к бэкенду")
                .font(.caption)
                .foregroundStyle(.tertiary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: - Error View

struct ErrorView: View {
    let message: String
    let onRetry: () -> Void
    var onPickFolder: (() -> Void)?

    var body: some View {
        VStack(spacing: 20) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 48))
                .foregroundStyle(.orange)
            Text("Не удалось запустить")
                .font(.title2.bold())
            Text(message)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 400)
            HStack(spacing: 12) {
                Button("Попробовать снова", action: onRetry)
                    .buttonStyle(.borderedProminent)
                if let onPickFolder {
                    Button("Выбрать папку проекта...", action: onPickFolder)
                        .buttonStyle(.bordered)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
