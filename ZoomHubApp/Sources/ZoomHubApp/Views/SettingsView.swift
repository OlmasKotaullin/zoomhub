import SwiftUI

struct SettingsView: View {
    @State private var llmProvider = "auto"
    @State private var transcriptionProvider = "bukvitsa"
    @State private var ollamaHealth: HealthInfo = .loading
    @State private var claudeHealth: HealthInfo = .loading
    @State private var bukvitsaHealth: HealthInfo = .loading
    @State private var whisperHealth: HealthInfo = .loading
    @State private var isSavingLLM = false
    @State private var isSavingTranscription = false

    enum HealthInfo {
        case loading
        case healthy(String)
        case unhealthy(String)

        var color: Color {
            switch self {
            case .loading: .gray
            case .healthy: .green
            case .unhealthy: .red
            }
        }

        var icon: String {
            switch self {
            case .loading: "circle.dashed"
            case .healthy: "checkmark.circle.fill"
            case .unhealthy: "xmark.circle.fill"
            }
        }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 32) {
                Text("Настройки")
                    .font(.title.bold())

                // LLM Provider
                GroupBox {
                    VStack(alignment: .leading, spacing: 16) {
                        Label("Языковая модель (LLM)", systemImage: "brain")
                            .font(.headline)

                        Text("Выберите провайдер для генерации саммари и чата")
                            .font(.callout)
                            .foregroundStyle(.secondary)

                        Picker("Провайдер", selection: $llmProvider) {
                            HStack {
                                Text("Авто (рекомендуется)")
                                healthBadge(ollamaHealth)
                            }
                            .tag("auto")

                            HStack {
                                Text("Только Ollama")
                                healthBadge(ollamaHealth)
                            }
                            .tag("ollama")

                            HStack {
                                Text("Только Claude")
                                healthBadge(claudeHealth)
                            }
                            .tag("claude")
                        }
                        .pickerStyle(.radioGroup)

                        HStack(spacing: 8) {
                            Image(systemName: "info.circle")
                                .foregroundStyle(.blue)
                            Group {
                                if llmProvider == "auto" {
                                    Text("Короткие встречи → Qwen 2.5:7b (бесплатно). Длинные → Claude API.")
                                } else if llmProvider == "ollama" {
                                    Text("Все саммари через Qwen 2.5:7b локально. Бесплатно, но ограниченный контекст.")
                                } else {
                                    Text("Все саммари через Claude API. Лучшее качество, платно.")
                                }
                            }
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        }
                        .padding(8)
                        .background(.blue.opacity(0.05), in: RoundedRectangle(cornerRadius: 8))

                        Button {
                            Task { await saveLLMProvider() }
                        } label: {
                            if isSavingLLM {
                                ProgressView().controlSize(.small)
                            } else {
                                Text("Сохранить")
                            }
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(isSavingLLM)
                    }
                    .padding(4)
                }

                // Transcription Provider
                GroupBox {
                    VStack(alignment: .leading, spacing: 16) {
                        Label("Транскрипция", systemImage: "waveform")
                            .font(.headline)

                        Text("Выберите сервис для расшифровки аудиозаписей")
                            .font(.callout)
                            .foregroundStyle(.secondary)

                        Picker("Провайдер", selection: $transcriptionProvider) {
                            HStack {
                                Text("Буквица (Telegram)")
                                healthBadge(bukvitsaHealth)
                            }
                            .tag("bukvitsa")

                            HStack {
                                Text("Whisper (локальная)")
                                healthBadge(whisperHealth)
                            }
                            .tag("whisper")
                        }
                        .pickerStyle(.radioGroup)

                        Button {
                            Task { await saveTranscriptionProvider() }
                        } label: {
                            if isSavingTranscription {
                                ProgressView().controlSize(.small)
                            } else {
                                Text("Сохранить")
                            }
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(isSavingTranscription)
                    }
                    .padding(4)
                }

                // Приложение
                GroupBox {
                    VStack(alignment: .leading, spacing: 16) {
                        Label("Приложение", systemImage: "app.badge")
                            .font(.headline)

                        Toggle("Показать Onboarding при следующем запуске", isOn: Binding(
                            get: { !UserDefaults.standard.bool(forKey: "onboardingDone") },
                            set: { UserDefaults.standard.set(!$0, forKey: "onboardingDone") }
                        ))

                        if let path = UserDefaults.standard.string(forKey: "ZoomHubProjectDir") {
                            HStack {
                                Text("Папка проекта:")
                                    .foregroundStyle(.secondary)
                                Text(path)
                                    .font(.caption)
                                    .foregroundStyle(.tertiary)
                                    .lineLimit(1)
                                    .truncationMode(.middle)
                            }
                        }
                    }
                    .padding(4)
                }

                // Backend Status
                GroupBox {
                    VStack(alignment: .leading, spacing: 12) {
                        Label("Статус системы", systemImage: "server.rack")
                            .font(.headline)

                        HStack {
                            healthIndicator("Ollama", health: ollamaHealth)
                            healthIndicator("Claude", health: claudeHealth)
                            healthIndicator("Буквица", health: bukvitsaHealth)
                            healthIndicator("Whisper", health: whisperHealth)
                        }

                        Button("Обновить статус") {
                            Task { await checkAllHealth() }
                        }
                        .buttonStyle(.bordered)
                    }
                    .padding(4)
                }
            }
            .padding(24)
        }
        .task { await checkAllHealth() }
    }

    @ViewBuilder
    private func healthBadge(_ health: HealthInfo) -> some View {
        Image(systemName: health.icon)
            .font(.caption)
            .foregroundStyle(health.color)
    }

    private func healthIndicator(_ name: String, health: HealthInfo) -> some View {
        VStack(spacing: 6) {
            Image(systemName: health.icon)
                .font(.title3)
                .foregroundStyle(health.color)
            Text(name)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(12)
        .background(Color(.controlBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
    }

    private func checkAllHealth() async {
        async let o = checkHealth("ollama")
        async let c = checkHealth("claude")
        async let b = checkHealth("bukvitsa")
        async let w = checkHealth("whisper")
        ollamaHealth = await o
        claudeHealth = await c
        bukvitsaHealth = await b
        whisperHealth = await w
    }

    private func checkHealth(_ type: String) async -> HealthInfo {
        do {
            let result = try await APIClient.shared.providerHealth(type: type)
            let healthy = result["healthy"] as? Bool ?? false
            let message = result["message"] as? String ?? (healthy ? "OK" : "Недоступен")
            return healthy ? .healthy(message) : .unhealthy(message)
        } catch {
            return .unhealthy(error.localizedDescription)
        }
    }

    private func saveLLMProvider() async {
        isSavingLLM = true
        try? await APIClient.shared.setLLMProvider(llmProvider)
        await checkAllHealth()
        isSavingLLM = false
    }

    private func saveTranscriptionProvider() async {
        isSavingTranscription = true
        try? await APIClient.shared.setTranscriptionProvider(transcriptionProvider)
        await checkAllHealth()
        isSavingTranscription = false
    }
}
