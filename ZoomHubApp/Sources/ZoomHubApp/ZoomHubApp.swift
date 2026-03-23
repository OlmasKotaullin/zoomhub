import SwiftUI

@main
struct ZoomHubApp: App {
    @State private var backend = BackendManager()
    @State private var appState = AppState()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environment(backend)
                .environment(appState)
                .frame(minWidth: 900, minHeight: 600)
                .onAppear {
                    NotificationManager.requestPermission()
                    // backend.start() вызывается из OnboardingView или ContentView
                    if UserDefaults.standard.bool(forKey: "onboardingDone") {
                        backend.start()
                    }
                }
        }
        .windowStyle(.titleBar)
        .defaultSize(width: 1200, height: 800)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandMenu("Встречи") {
                Button("Загрузить запись...") {
                    appState.showUploadSheet = true
                }
                .keyboardShortcut("o", modifiers: .command)

                Button("Новая загрузка") {
                    appState.showUploadSheet = true
                }
                .keyboardShortcut("n", modifiers: .command)

                Divider()

                Button("Обновить список") {
                    appState.refreshTrigger += 1
                }
                .keyboardShortcut("r", modifiers: .command)

                Divider()

                Button("Назад к списку") {
                    appState.selectedMeetingId = nil
                }
                .keyboardShortcut(.escape, modifiers: [])
            }
        }

        MenuBarExtra("ZoomHub", systemImage: "waveform.circle.fill") {
            MenuBarView()
                .environment(backend)
        }
    }
}

@MainActor
@Observable
final class AppState {
    var showUploadSheet = false
    var refreshTrigger = 0
    var selectedMeetingId: Int?
}
