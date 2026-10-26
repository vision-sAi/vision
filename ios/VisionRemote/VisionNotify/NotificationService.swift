import UserNotifications

/// Runs when a push arrives, before it is shown: unseals the machine's words (PushKey) and puts them
/// in place of the stand-in text the machine sends in the clear. If it cannot (no key, a phone since re-paired), the stand-in
/// "Open Vision to read this." is shown as it came.
final class NotificationService: UNNotificationServiceExtension {
    private var deliver: ((UNNotificationContent) -> Void)?
    private var content: UNMutableNotificationContent?

    override func didReceive(_ request: UNNotificationRequest,
                             withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void) {
        deliver = contentHandler
        guard let content = request.content.mutableCopy() as? UNMutableNotificationContent else {
            contentHandler(request.content)
            return
        }
        self.content = content
        if let enc = content.userInfo["enc"] as? String, let message = PushKey.open(enc) {
            content.title = message.title
            content.body = message.body
            if !message.chat.isEmpty {
                content.threadIdentifier = message.chat
                content.userInfo["chat"] = message.chat   // a tap opens it (AppDelegate)
            }
            if let project = message.project, !project.isEmpty {
                content.userInfo["project"] = project     // …and a project thread's, that thread
                if let thread = message.thread, !thread.isEmpty { content.userInfo["thread"] = thread }
                if let category = message.category, !category.isEmpty {
                    content.categoryIdentifier = category        // its buttons (ScoutActions, registered by the app)
                    if let approval = message.approval { content.userInfo["approval"] = approval }
                }
            }
        }
        contentHandler(content)
    }

    override func serviceExtensionTimeWillExpire() {
        if let deliver, let content { deliver(content) }
    }
}
