import Foundation
import AppKit

@MainActor
@Observable
final class SetupManager {
    enum SetupState: Equatable {
        case idle
        case running
        case completed
        case failed(String)
    }

    struct SetupStep: Identifiable, Equatable {
        let id: Int
        let title: String
        var status: StepStatus = .pending
        var detail: String = ""

        enum StepStatus: Equatable {
            case pending, running, done, error(String)
        }
    }

    private(set) var state: SetupState = .idle
    private(set) var steps: [SetupStep] = []
    private var process: Process?

    static let supportDir: URL = {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
            .appendingPathComponent("ZoomHub")
    }()

    static var envFilePath: String { supportDir.appendingPathComponent(".env").path }
    static var venvPython: String { supportDir.appendingPathComponent("venv/bin/python3").path }
    static var dataDir: String { supportDir.appendingPathComponent("data").path }

    var isSetupNeeded: Bool {
        !FileManager.default.fileExists(atPath: Self.venvPython)
    }

    func runSetup() {
        guard state != .running else { return }
        state = .running

        steps = [
            SetupStep(id: 1, title: "Установка Ollama"),
            SetupStep(id: 2, title: "Запуск Ollama"),
            SetupStep(id: 3, title: "Настройка Python"),
            SetupStep(id: 4, title: "Установка зависимостей"),
            SetupStep(id: 5, title: "Скачивание AI-модели"),
            SetupStep(id: 6, title: "Финализация"),
        ]

        guard let scriptPath = findSetupScript() else {
            state = .failed("Скрипт установки не найден в бандле приложения")
            return
        }

        let backendPath = findBackendPath() ?? ""

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/bash")
        proc.arguments = [scriptPath]

        var env = ProcessInfo.processInfo.environment
        env["BACKEND_PATH"] = backendPath
        proc.environment = env

        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = pipe

        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let str = String(data: data, encoding: .utf8) else { return }
            for line in str.components(separatedBy: "\n") where !line.isEmpty {
                let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !trimmed.isEmpty else { continue }
                Task { @MainActor in
                    self?.parseLine(trimmed)
                }
            }
        }

        proc.terminationHandler = { [weak self] proc in
            Task { @MainActor in
                pipe.fileHandleForReading.readabilityHandler = nil
                if proc.terminationStatus == 0 {
                    self?.state = .completed
                } else if self?.state != .completed {
                    self?.state = .failed("Установка завершилась с ошибкой (код \(proc.terminationStatus))")
                }
            }
        }

        do {
            try proc.run()
            process = proc
        } catch {
            state = .failed("Не удалось запустить установку: \(error.localizedDescription)")
        }
    }

    func saveEnvValue(key: String, value: String) {
        let envPath = Self.envFilePath
        let dir = Self.supportDir.path
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)

        var content = ""
        if FileManager.default.fileExists(atPath: envPath),
           let existing = try? String(contentsOfFile: envPath, encoding: .utf8) {
            content = existing
        }

        let pattern = "\(key)=.*"
        if content.range(of: pattern, options: .regularExpression) != nil {
            content = content.replacingOccurrences(of: pattern, with: "\(key)=\(value)", options: .regularExpression)
        } else {
            if !content.hasSuffix("\n") && !content.isEmpty { content += "\n" }
            content += "\(key)=\(value)\n"
        }

        try? content.write(toFile: envPath, atomically: true, encoding: .utf8)
    }

    func loadEnvValue(key: String) -> String {
        guard let content = try? String(contentsOfFile: Self.envFilePath, encoding: .utf8) else { return "" }
        for line in content.components(separatedBy: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("\(key)=") {
                return String(trimmed.dropFirst(key.count + 1))
            }
        }
        return ""
    }

    private func parseLine(_ line: String) {
        if line == "SETUP_COMPLETE" {
            state = .completed
            return
        }

        let parts = line.split(separator: ":", maxSplits: 2).map(String.init)
        guard parts.count >= 2, let stepId = Int(parts[1]) else { return }
        guard let index = steps.firstIndex(where: { $0.id == stepId }) else { return }

        let message = parts.count > 2 ? parts[2] : ""

        switch parts[0] {
        case "STEP":
            steps[index].status = .running
            steps[index].detail = message
        case "PROGRESS":
            steps[index].detail = message
        case "DONE":
            steps[index].status = .done
            steps[index].detail = message
        case "ERROR":
            steps[index].status = .error(message)
        default:
            break
        }
    }

    private func findSetupScript() -> String? {
        if let path = Bundle.main.path(forResource: "setup", ofType: "sh") {
            return path
        }
        let resourcePath = (Bundle.main.resourcePath ?? "") + "/setup.sh"
        if FileManager.default.fileExists(atPath: resourcePath) {
            return resourcePath
        }
        // Development: look relative to the binary
        let devPath = (Bundle.main.bundlePath as NSString).deletingLastPathComponent + "/Resources/setup.sh"
        if FileManager.default.fileExists(atPath: devPath) {
            return devPath
        }
        return nil
    }

    private func findBackendPath() -> String? {
        let bundleBackend = (Bundle.main.resourcePath ?? "") + "/backend"
        if FileManager.default.fileExists(atPath: bundleBackend + "/requirements.txt") {
            return bundleBackend
        }
        let knownPath = NSString("~/Вайбкодинг 2025/projects/zoomhub").expandingTildeInPath
        if FileManager.default.fileExists(atPath: knownPath + "/requirements.txt") {
            return knownPath
        }
        return nil
    }
}
