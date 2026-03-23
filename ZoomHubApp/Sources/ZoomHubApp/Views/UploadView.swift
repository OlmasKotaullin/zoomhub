import SwiftUI
import UniformTypeIdentifiers

struct UploadView: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(AppState.self) private var appState

    @State private var selectedFile: URL?
    @State private var title = ""
    @State private var isUploading = false
    @State private var isDragging = false
    @State private var error: String?

    private let audioTypes: [UTType] = [.mp3, .mpeg4Audio, .wav, .audio]
    private let maxFileSize: UInt64 = 2 * 1024 * 1024 * 1024 // 2 ГБ

    var body: some View {
        VStack(spacing: 24) {
            // Header
            HStack {
                Text("Загрузить запись")
                    .font(.title2.bold())
                Spacer()
                Button {
                    dismiss()
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.title3)
                        .foregroundStyle(.tertiary)
                }
                .buttonStyle(.plain)
            }

            // Drop Zone
            ZStack {
                RoundedRectangle(cornerRadius: 16)
                    .strokeBorder(
                        isDragging ? Color.blue : Color.gray.opacity(0.3),
                        style: StrokeStyle(lineWidth: 2, dash: [8])
                    )
                    .background(
                        RoundedRectangle(cornerRadius: 16)
                            .fill(isDragging ? Color.blue.opacity(0.05) : .clear)
                    )

                VStack(spacing: 12) {
                    if let file = selectedFile {
                        Image(systemName: "waveform.circle.fill")
                            .font(.system(size: 40))
                            .foregroundStyle(.green)
                        Text(file.lastPathComponent)
                            .font(.headline)
                        Text(fileSizeString(file))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Button("Выбрать другой файл") {
                            pickFile()
                        }
                        .buttonStyle(.link)
                    } else {
                        Image(systemName: "arrow.down.doc")
                            .font(.system(size: 40))
                            .foregroundStyle(.secondary)
                        Text("Перетащите аудио или видео сюда")
                            .font(.headline)
                        Text("MP4, MP3, WAV, M4A, WebM, OGG — до 2 ГБ")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Button("Выбрать файл...") {
                            pickFile()
                        }
                        .buttonStyle(.bordered)
                    }
                }
            }
            .frame(height: 180)
            .onDrop(of: [.fileURL], isTargeted: $isDragging) { providers in
                handleDrop(providers)
            }

            // Title
            TextField("Название встречи", text: $title)
                .textFieldStyle(.roundedBorder)

            // Error
            if let error {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            // Actions
            HStack {
                Button("Отмена") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Spacer()
                Button {
                    Task { await upload() }
                } label: {
                    if isUploading {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small)
                            Text("Загрузка...")
                        }
                    } else {
                        Text("Загрузить")
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(selectedFile == nil || isUploading)
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(24)
        .frame(width: 480)
    }

    private func pickFile() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = audioTypes + [.mpeg4Movie, .movie]
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false

        if panel.runModal() == .OK, let url = panel.url {
            selectedFile = url
            if title.isEmpty {
                title = url.deletingPathExtension().lastPathComponent
                    .replacingOccurrences(of: "_", with: " ")
                    .replacingOccurrences(of: "-", with: " ")
            }
        }
    }

    private func handleDrop(_ providers: [NSItemProvider]) -> Bool {
        guard let provider = providers.first else { return false }
        provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier) { data, _ in
            guard let data = data as? Data,
                  let url = URL(dataRepresentation: data, relativeTo: nil) else { return }
            Task { @MainActor in
                selectedFile = url
                if title.isEmpty {
                    title = url.deletingPathExtension().lastPathComponent
                }
            }
        }
        return true
    }

    private func upload() async {
        guard let file = selectedFile else { return }

        // Валидация размера файла
        if let attrs = try? FileManager.default.attributesOfItem(atPath: file.path),
           let size = attrs[.size] as? UInt64, size > maxFileSize {
            let formatter = ByteCountFormatter()
            formatter.countStyle = .file
            error = "Файл слишком большой (\(formatter.string(fromByteCount: Int64(size)))). Максимум 2 ГБ."
            return
        }

        isUploading = true
        error = nil

        let meetingTitle = title.isEmpty ? file.deletingPathExtension().lastPathComponent : title

        do {
            let meeting = try await APIClient.shared.uploadMeeting(
                fileURL: file,
                title: meetingTitle,
                folderId: nil
            )
            appState.selectedMeetingId = meeting.id
            appState.refreshTrigger += 1
            dismiss()
        } catch {
            self.error = "Ошибка загрузки: \(error.localizedDescription)"
        }

        isUploading = false
    }

    private func fileSizeString(_ url: URL) -> String {
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: url.path),
              let size = attrs[.size] as? UInt64 else { return "" }
        let formatter = ByteCountFormatter()
        formatter.countStyle = .file
        return formatter.string(fromByteCount: Int64(size))
    }
}
