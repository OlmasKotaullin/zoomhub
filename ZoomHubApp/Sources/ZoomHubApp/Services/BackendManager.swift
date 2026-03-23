import Foundation
import AppKit

@MainActor
@Observable
final class BackendManager {
    enum State: Equatable {
        case stopped
        case starting
        case running
        case error(String)
    }

    private static let projectDirKey = "ZoomHubProjectDir"

    private(set) var state: State = .stopped
    private var process: Process?
    private var healthTimer: Timer?

    var isRunning: Bool { state == .running }

    var savedProjectDir: String? {
        UserDefaults.standard.string(forKey: Self.projectDirKey)
    }

    func start() {
        guard state == .stopped || state.isError else { return }
        state = .starting

        Task {
            let ready = await APIClient.shared.isBackendReady()
            if ready {
                await MainActor.run { self.state = .running }
                return
            }
            await MainActor.run { self.launchProcess() }
        }
    }

    private func launchProcess() {
        guard let pythonPath = resolvePython() else {
            state = .error("Python не найден. Пройдите настройку заново.")
            return
        }

        guard let backendDir = resolveBackendDir() else {
            state = .error("Код бэкенда не найден.")
            return
        }

        let dataDir = SetupManager.dataDir
        try? FileManager.default.createDirectory(atPath: dataDir + "/logs", withIntermediateDirectories: true)
        let logFile = dataDir + "/logs/backend.log"

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: pythonPath)
        proc.arguments = ["-m", "uvicorn", "app.main:app", "--port", "8002", "--host", "127.0.0.1"]
        proc.currentDirectoryURL = URL(fileURLWithPath: backendDir)

        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = backendDir
        env["ZOOMHUB_DATA_DIR"] = dataDir

        // Load .env from Application Support
        let envPath = SetupManager.envFilePath
        if let envContent = try? String(contentsOfFile: envPath, encoding: .utf8) {
            for line in envContent.components(separatedBy: "\n") {
                let trimmed = line.trimmingCharacters(in: .whitespaces)
                guard !trimmed.isEmpty, !trimmed.hasPrefix("#") else { continue }
                let parts = trimmed.split(separator: "=", maxSplits: 1)
                guard parts.count == 2 else { continue }
                env[String(parts[0])] = String(parts[1])
            }
        }

        proc.environment = env

        proc.standardOutput = FileHandle.nullDevice
        if FileManager.default.fileExists(atPath: logFile) || FileManager.default.createFile(atPath: logFile, contents: nil) {
            proc.standardError = FileHandle(forWritingAtPath: logFile) ?? FileHandle.nullDevice
        } else {
            proc.standardError = FileHandle.nullDevice
        }

        proc.terminationHandler = { [weak self] _ in
            Task { @MainActor in
                self?.state = .stopped
            }
        }

        do {
            try proc.run()
            process = proc
            waitForHealth()
        } catch {
            state = .error("Не удалось запустить бэкенд: \(error.localizedDescription)")
        }
    }

    func stop() {
        healthTimer?.invalidate()
        healthTimer = nil

        guard let proc = process, proc.isRunning else {
            state = .stopped
            return
        }

        proc.terminate()

        DispatchQueue.global().asyncAfter(deadline: .now() + 3) { [weak self] in
            if proc.isRunning {
                proc.interrupt()
            }
            Task { @MainActor in
                self?.process = nil
                self?.state = .stopped
            }
        }
    }

    func setProjectDir(_ path: String) {
        UserDefaults.standard.set(path, forKey: Self.projectDirKey)
    }

    func pickProjectDir() {
        let panel = NSOpenPanel()
        panel.title = "Выберите папку проекта ZoomHub"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.message = "Выберите папку, содержащую app/main.py"

        if panel.runModal() == .OK, let url = panel.url {
            let path = url.path
            if FileManager.default.fileExists(atPath: path + "/app/main.py") {
                setProjectDir(path)
                start()
            }
        }
    }

    private func waitForHealth() {
        var attempts = 0
        let maxAttempts = 30

        healthTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] timer in
            guard let self else { timer.invalidate(); return }
            attempts += 1

            Task {
                let ready = await APIClient.shared.isBackendReady()
                await MainActor.run {
                    if ready {
                        timer.invalidate()
                        self.state = .running
                    } else if attempts >= maxAttempts {
                        timer.invalidate()
                        self.state = .error("Бэкенд не отвечает после \(maxAttempts) попыток")
                    }
                }
            }
        }
    }

    private func resolvePython() -> String? {
        // 1. Venv in Application Support (installed by setup)
        let setupPython = SetupManager.venvPython
        if FileManager.default.isExecutableFile(atPath: setupPython) {
            return setupPython
        }

        // 2. Venv in project directory
        if let projectDir = resolveProjectDir() {
            let venvPython = projectDir + "/venv/bin/python3"
            if FileManager.default.isExecutableFile(atPath: venvPython) {
                return venvPython
            }
        }

        // 3. System Python
        let candidates = [
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3",
        ]
        for path in candidates {
            if FileManager.default.isExecutableFile(atPath: path) {
                return path
            }
        }
        return nil
    }

    private func resolveBackendDir() -> String? {
        // 1. Embedded in .app bundle
        let bundleBackend = (Bundle.main.resourcePath ?? "") + "/backend"
        if FileManager.default.fileExists(atPath: bundleBackend + "/app/main.py") {
            return bundleBackend
        }

        // 2. Saved project dir
        if let dir = resolveProjectDir() {
            return dir
        }

        return nil
    }

    private func resolveProjectDir() -> String? {
        if let saved = savedProjectDir,
           FileManager.default.fileExists(atPath: saved + "/app/main.py") {
            return saved
        }

        if let bundlePath = Bundle.main.bundlePath as NSString? {
            let parent = bundlePath.deletingLastPathComponent
            let sibling = (parent as NSString).appendingPathComponent("zoomhub")
            if FileManager.default.fileExists(atPath: sibling + "/app/main.py") {
                setProjectDir(sibling)
                return sibling
            }
        }

        let knownPath = NSString("~/Вайбкодинг 2025/projects/zoomhub").expandingTildeInPath
        if FileManager.default.fileExists(atPath: knownPath + "/app/main.py") {
            setProjectDir(knownPath)
            return knownPath
        }

        return nil
    }
}

private extension BackendManager.State {
    var isError: Bool {
        if case .error = self { return true }
        return false
    }
}
