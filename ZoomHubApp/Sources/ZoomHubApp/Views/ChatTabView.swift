import SwiftUI

struct ChatTabView: View {
    let meetingId: Int

    @State private var messages: [ChatMessage] = []
    @State private var inputText = ""
    @State private var isLoading = false
    @State private var isSending = false

    var body: some View {
        VStack(spacing: 0) {
            // Messages
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 12) {
                        if messages.isEmpty && !isLoading {
                            emptyChat
                        }

                        ForEach(messages) { message in
                            ChatBubble(message: message)
                                .id(message.id)
                        }

                        if isSending {
                            HStack {
                                ProgressView()
                                    .controlSize(.small)
                                Text("Думаю...")
                                    .font(.callout)
                                    .foregroundStyle(.secondary)
                                Spacer()
                            }
                            .padding(.horizontal, 16)
                            .id("thinking")
                        }
                    }
                    .padding(24)
                }
                .onChange(of: messages.count) {
                    if let last = messages.last {
                        withAnimation {
                            proxy.scrollTo(last.id, anchor: .bottom)
                        }
                    }
                }
                .onChange(of: isSending) {
                    if isSending {
                        withAnimation {
                            proxy.scrollTo("thinking", anchor: .bottom)
                        }
                    }
                }
            }

            Divider()

            // Input
            HStack(spacing: 12) {
                TextField("Спросите о встрече...", text: $inputText, axis: .vertical)
                    .textFieldStyle(.plain)
                    .lineLimit(1...5)
                    .onSubmit { sendMessage() }

                Button {
                    sendMessage()
                } label: {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(.title2)
                }
                .buttonStyle(.plain)
                .foregroundStyle(inputText.isEmpty ? Color.gray : Color.blue)
                .disabled(inputText.isEmpty || isSending)
                .keyboardShortcut(.return, modifiers: .command)
            }
            .padding(16)

            // Clear chat button
            if !messages.isEmpty {
                HStack {
                    Spacer()
                    Button("Очистить чат") {
                        Task {
                            try? await APIClient.shared.clearChat(meetingId: meetingId)
                            messages = []
                        }
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .buttonStyle(.plain)
                    .padding(.bottom, 8)
                    .padding(.trailing, 16)
                }
            }
        }
        .task { await loadHistory() }
    }

    private var emptyChat: some View {
        VStack(spacing: 12) {
            Image(systemName: "bubble.left.and.bubble.right")
                .font(.system(size: 36))
                .foregroundStyle(.tertiary)
            Text("Задайте вопрос о встрече")
                .font(.headline)
                .foregroundStyle(.secondary)
            Text("AI проанализирует транскрипт и саммари, чтобы ответить")
                .font(.caption)
                .foregroundStyle(.tertiary)
                .multilineTextAlignment(.center)
        }
        .padding(.top, 60)
    }

    private func loadHistory() async {
        isLoading = true
        do {
            messages = try await APIClient.shared.getChatHistory(meetingId: meetingId)
        } catch {
            // Если истории нет — начинаем с чистого листа
        }
        isLoading = false
    }

    private func sendMessage() {
        let text = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !isSending else { return }

        inputText = ""
        isSending = true

        // Сразу добавляем пользовательское сообщение
        let userMessage = ChatMessage(id: -(messages.count + 1), role: "user", content: text, createdAt: nil)
        messages.append(userMessage)

        Task {
            do {
                _ = try await APIClient.shared.sendChatMessage(meetingId: meetingId, message: text)
                // Загружаем актуальную историю
                messages = try await APIClient.shared.getChatHistory(meetingId: meetingId)
            } catch {
                let errorMsg = ChatMessage(
                    id: -(messages.count + 1),
                    role: "assistant",
                    content: "Ошибка: \(error.localizedDescription)",
                    createdAt: nil
                )
                messages.append(errorMsg)
            }
            isSending = false
        }
    }
}

struct ChatBubble: View {
    let message: ChatMessage

    var body: some View {
        HStack {
            if message.isUser { Spacer(minLength: 60) }

            VStack(alignment: message.isUser ? .trailing : .leading, spacing: 4) {
                if message.isUser {
                    Text(message.content)
                        .font(.body)
                        .textSelection(.enabled)
                        .lineSpacing(3)
                } else {
                    Text(markdownContent)
                        .font(.body)
                        .textSelection(.enabled)
                        .lineSpacing(3)
                }
            }
            .padding(12)
            .background(
                message.isUser
                    ? Color.blue.opacity(0.15)
                    : Color(.controlBackgroundColor),
                in: RoundedRectangle(cornerRadius: 12)
            )

            if !message.isUser { Spacer(minLength: 60) }
        }
    }

    private var markdownContent: AttributedString {
        (try? AttributedString(markdown: message.content, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(message.content)
    }
}
