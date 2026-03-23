import SwiftUI

struct OnboardingView: View {
    @Environment(BackendManager.self) private var backend
    @State private var setup = SetupManager()
    @State private var page: Page = .setup
    @State private var apiKey: String = ""
    @State private var showKey = false

    enum Page {
        case setup
        case apiKeys
        case done
    }

    var body: some View {
        VStack(spacing: 0) {
            // Header
            VStack(spacing: 8) {
                Image(systemName: "waveform.circle.fill")
                    .font(.system(size: 48))
                    .foregroundStyle(.blue)
                Text("ZoomHub")
                    .font(.largeTitle.bold())
            }
            .padding(.top, 40)
            .padding(.bottom, 24)

            // Content
            Group {
                switch page {
                case .setup:
                    setupPage
                case .apiKeys:
                    apiKeysPage
                case .done:
                    donePage
                }
            }
            .frame(maxWidth: 500)
            .padding(.horizontal, 40)

            Spacer()

            // Progress dots
            HStack(spacing: 8) {
                dot(active: page == .setup)
                dot(active: page == .apiKeys)
                dot(active: page == .done)
            }
            .padding(.bottom, 32)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .onAppear {
            apiKey = setup.loadEnvValue(key: "ANTHROPIC_API_KEY")
            if setup.isSetupNeeded {
                setup.runSetup()
            } else {
                page = .apiKeys
            }
        }
        .onChange(of: setup.state) {
            if setup.state == .completed {
                withAnimation { page = .apiKeys }
            }
        }
    }

    // MARK: - Page 1: Setup

    private var setupPage: some View {
        VStack(spacing: 20) {
            Text("Установка компонентов")
                .font(.title2.bold())

            Text("Автоматически устанавливаю всё необходимое")
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)

            VStack(alignment: .leading, spacing: 10) {
                ForEach(setup.steps) { step in
                    HStack(spacing: 12) {
                        stepIcon(step.status)
                            .frame(width: 20)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(step.title)
                                .font(.body)
                            if !step.detail.isEmpty {
                                Text(step.detail)
                                    .font(.caption)
                                    .foregroundStyle(step.status.isError ? .red : .secondary)
                                    .lineLimit(1)
                            }
                        }
                        Spacer()
                    }
                }
            }
            .padding(20)
            .background(Color(.controlBackgroundColor), in: RoundedRectangle(cornerRadius: 12))

            if case .failed(let msg) = setup.state {
                Text(msg)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .multilineTextAlignment(.center)

                HStack(spacing: 12) {
                    Button("Попробовать снова") {
                        setup.runSetup()
                    }
                    .buttonStyle(.borderedProminent)

                    Button("Пропустить") {
                        withAnimation { page = .apiKeys }
                    }
                    .buttonStyle(.bordered)
                }
            }
        }
    }

    // MARK: - Page 2: API Keys

    private var apiKeysPage: some View {
        VStack(spacing: 20) {
            Text("Настройка API")
                .font(.title2.bold())

            Text("Для обработки длинных записей нужен ключ Claude API.\nКороткие записи обрабатываются локально (бесплатно).")
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .font(.callout)

            VStack(alignment: .leading, spacing: 8) {
                Text("Anthropic API Key")
                    .font(.headline)

                HStack {
                    Group {
                        if showKey {
                            TextField("sk-ant-...", text: $apiKey)
                        } else {
                            SecureField("sk-ant-...", text: $apiKey)
                        }
                    }
                    .textFieldStyle(.roundedBorder)

                    Button {
                        showKey.toggle()
                    } label: {
                        Image(systemName: showKey ? "eye.slash" : "eye")
                    }
                    .buttonStyle(.borderless)
                }

                Text("Получить ключ: console.anthropic.com")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(20)
            .background(Color(.controlBackgroundColor), in: RoundedRectangle(cornerRadius: 12))

            VStack(spacing: 8) {
                HStack(spacing: 6) {
                    Image(systemName: "info.circle")
                        .foregroundStyle(.blue)
                    Text("Без ключа приложение будет работать только с локальной моделью Ollama")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            HStack(spacing: 12) {
                Button("Продолжить") {
                    if !apiKey.isEmpty {
                        setup.saveEnvValue(key: "ANTHROPIC_API_KEY", value: apiKey)
                    }
                    withAnimation { page = .done }
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)

                if apiKey.isEmpty {
                    Button("Пропустить") {
                        withAnimation { page = .done }
                    }
                    .buttonStyle(.bordered)
                }
            }
        }
    }

    // MARK: - Page 3: Done

    private var donePage: some View {
        VStack(spacing: 20) {
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 56))
                .foregroundStyle(.green)

            Text("Всё готово!")
                .font(.title2.bold())

            Text("ZoomHub настроен и готов к работе.\nЗагрузите аудио/видео встречи — получите транскрипт, саммари и задачи.")
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)

            Button("Начать работу") {
                backend.start()
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
        }
        .onAppear {
            // Auto-start after 2 sec
            Task {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                if page == .done && backend.state == .stopped {
                    backend.start()
                }
            }
        }
    }

    // MARK: - Helpers

    @ViewBuilder
    private func stepIcon(_ status: SetupManager.SetupStep.StepStatus) -> some View {
        switch status {
        case .pending:
            Image(systemName: "circle")
                .foregroundStyle(.tertiary)
        case .running:
            ProgressView()
                .controlSize(.small)
        case .done:
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(.green)
        case .error:
            Image(systemName: "xmark.circle.fill")
                .foregroundStyle(.red)
        }
    }

    private func dot(active: Bool) -> some View {
        Circle()
            .fill(active ? Color.blue : Color.gray.opacity(0.3))
            .frame(width: 8, height: 8)
    }
}

private extension SetupManager.SetupStep.StepStatus {
    var isError: Bool {
        if case .error = self { return true }
        return false
    }
}
