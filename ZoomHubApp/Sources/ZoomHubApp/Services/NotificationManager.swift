import Foundation
import UserNotifications

enum NotificationManager {
    static func requestPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    static func sendMeetingReady(title: String, meetingId: Int) {
        let content = UNMutableNotificationContent()
        content.title = "Встреча обработана"
        content.body = title
        content.sound = .default
        content.userInfo = ["meetingId": meetingId]

        let request = UNNotificationRequest(
            identifier: "meeting-ready-\(meetingId)",
            content: content,
            trigger: nil
        )

        UNUserNotificationCenter.current().add(request)
    }

    static func sendError(title: String, meetingId: Int) {
        let content = UNMutableNotificationContent()
        content.title = "Ошибка обработки"
        content.body = title
        content.sound = .default
        content.userInfo = ["meetingId": meetingId]

        let request = UNNotificationRequest(
            identifier: "meeting-error-\(meetingId)",
            content: content,
            trigger: nil
        )

        UNUserNotificationCenter.current().add(request)
    }
}
