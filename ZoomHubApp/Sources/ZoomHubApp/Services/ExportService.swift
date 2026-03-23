import Foundation
import AppKit

enum ExportFormat: String, CaseIterable {
    case markdown = "Markdown"
    case plainText = "Текст"

    var fileExtension: String {
        switch self {
        case .markdown: "md"
        case .plainText: "txt"
        }
    }
}

enum ExportService {
    static func export(detail: MeetingDetail, format: ExportFormat) {
        let content: String
        switch format {
        case .markdown: content = buildMarkdown(detail)
        case .plainText: content = buildPlainText(detail)
        }

        let panel = NSSavePanel()
        panel.title = "Экспорт встречи"
        panel.nameFieldStringValue = sanitizeFilename(detail.meeting.title) + "." + format.fileExtension
        panel.allowedContentTypes = [.plainText]

        guard panel.runModal() == .OK, let url = panel.url else { return }

        do {
            try content.write(to: url, atomically: true, encoding: .utf8)
            NSWorkspace.shared.activateFileViewerSelecting([url])
        } catch {
            let alert = NSAlert()
            alert.messageText = "Ошибка экспорта"
            alert.informativeText = error.localizedDescription
            alert.runModal()
        }
    }

    static func copyTLDR(summary: Summary?) {
        guard let tldr = summary?.tldr, !tldr.isEmpty else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(tldr, forType: .string)
    }

    // MARK: - Markdown

    private static func buildMarkdown(_ d: MeetingDetail) -> String {
        var lines: [String] = []
        let m = d.meeting

        lines.append("# \(m.title)")
        lines.append("")
        lines.append("**Дата:** \(m.formattedDate)")
        if !m.formattedDuration.isEmpty {
            lines.append("**Длительность:** \(m.formattedDuration)")
        }
        lines.append("**Статус:** \(m.status.label)")
        lines.append("")

        if let s = d.summary {
            if let tldr = s.tldr, !tldr.isEmpty {
                lines.append("## Краткое содержание")
                lines.append("")
                lines.append(tldr)
                lines.append("")
            }

            if let tasks = s.tasks, !tasks.isEmpty {
                lines.append("## Задачи")
                lines.append("")
                for task in tasks {
                    var line = "- [ ] \(task.task)"
                    if let assignee = task.assignee { line += " — *\(assignee)*" }
                    if let deadline = task.deadline { line += " (до \(deadline))" }
                    lines.append(line)
                }
                lines.append("")
            }

            if let topics = s.topics, !topics.isEmpty {
                lines.append("## Темы")
                lines.append("")
                for topic in topics {
                    lines.append("### \(topic.topic)")
                    if let details = topic.details {
                        lines.append(details)
                    }
                    lines.append("")
                }
            }

            if let insights = s.insights, !insights.isEmpty {
                lines.append("## Инсайты")
                lines.append("")
                for insight in insights {
                    lines.append("- \(insight.insight)")
                }
                lines.append("")
            }
        }

        if let t = d.transcript, !t.fullText.isEmpty {
            lines.append("---")
            lines.append("")
            lines.append("## Транскрипт")
            lines.append("")
            lines.append(t.fullText)
        }

        return lines.joined(separator: "\n")
    }

    // MARK: - Plain Text

    private static func buildPlainText(_ d: MeetingDetail) -> String {
        var lines: [String] = []
        let m = d.meeting

        lines.append(m.title.uppercased())
        lines.append(String(repeating: "=", count: min(m.title.count, 60)))
        lines.append("")
        lines.append("Дата: \(m.formattedDate)")
        if !m.formattedDuration.isEmpty {
            lines.append("Длительность: \(m.formattedDuration)")
        }
        lines.append("")

        if let s = d.summary {
            if let tldr = s.tldr, !tldr.isEmpty {
                lines.append("КРАТКОЕ СОДЕРЖАНИЕ")
                lines.append(tldr)
                lines.append("")
            }

            if let tasks = s.tasks, !tasks.isEmpty {
                lines.append("ЗАДАЧИ")
                for (i, task) in tasks.enumerated() {
                    var line = "\(i + 1). \(task.task)"
                    if let assignee = task.assignee { line += " — \(assignee)" }
                    if let deadline = task.deadline { line += " (до \(deadline))" }
                    lines.append(line)
                }
                lines.append("")
            }

            if let topics = s.topics, !topics.isEmpty {
                lines.append("ТЕМЫ")
                for topic in topics {
                    lines.append("• \(topic.topic)")
                    if let details = topic.details {
                        lines.append("  \(details)")
                    }
                }
                lines.append("")
            }
        }

        if let t = d.transcript, !t.fullText.isEmpty {
            lines.append(String(repeating: "-", count: 40))
            lines.append("ТРАНСКРИПТ")
            lines.append("")
            lines.append(t.fullText)
        }

        return lines.joined(separator: "\n")
    }

    private static func sanitizeFilename(_ name: String) -> String {
        let illegal = CharacterSet(charactersIn: "/\\:*?\"<>|")
        return name.components(separatedBy: illegal).joined(separator: "_")
            .trimmingCharacters(in: .whitespaces)
    }
}
