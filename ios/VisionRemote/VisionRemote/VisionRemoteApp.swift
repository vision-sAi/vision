import SwiftUI
import UserNotifications
import WidgetKit

/// Notifications from the machine (reminders, a scheduled task's reply): the device token goes to
/// `vision serve`, and a tapped notification opens the chat it names.
final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        UNUserNotificationCenter.current().setNotificationCategories(ScoutActions.categories)
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        let hex = deviceToken.map { String(format: "%02x", $0) }.joined()
        Task { @MainActor in VisionClient.shared?.registerPush(token: hex) }
    }

    // With the app open the machine's own toast already says it: no banner on top.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        []
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse) async {
        let info = response.notification.request.content.userInfo
        if [ScoutActions.approve, ScoutActions.deny, ScoutActions.landIt].contains(response.actionIdentifier) {
            // A button on a scout's notification: done from here, the app stays where it is. If it couldn't be
            // done, a notification says so (a tap on it opens the scout).
            if let problem = await ScoutActions.perform(response.actionIdentifier, info: info) {
                let note = UNMutableNotificationContent()
                note.title = response.actionIdentifier == ScoutActions.landIt ? "Couldn't land it" : "Couldn't pass that on"
                note.body = problem
                note.userInfo = info.filter { ["project", "thread", "chat"].contains($0.key as? String ?? "") }
                try? await center.add(UNNotificationRequest(identifier: UUID().uuidString, content: note, trigger: nil))
            }
            return
        }
        let project = info["project"] as? String ?? ""
        let thread = info["thread"] as? String ?? ""
        let chat = info["chat"] as? String ?? ""
        if !project.isEmpty {
            // A project thread's news: its chat, with that thread open on top.
            let sheet = info["approval"] != nil   // a scout's request: its sheet, where the request waits
            await MainActor.run { VisionClient.shared?.open(notifiedProject: project, thread: thread, chat: chat, sheet: sheet) }
            return
        }
        guard !chat.isEmpty else { return }
        await MainActor.run { VisionClient.shared?.open(notifiedChat: chat) }
    }
}

@main
struct VisionRemoteApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var client = VisionClient()
    @Environment(\.scenePhase) private var scenePhase

    init() {
        ThemeID.settle()
        CallLiveActivity.endLeftovers()
        #if DEBUG
        // Screenshot hook: `--theme hearth` opens in that theme.
        if let i = CommandLine.arguments.firstIndex(of: "--theme"), i + 1 < CommandLine.arguments.count {
            UserDefaults.standard.set(CommandLine.arguments[i + 1], forKey: ThemeID.key)
        }
        #endif
    }

    var body: some Scene {
        WindowGroup {
            ThemedRoot()
                .environmentObject(client)
        }
        .onChange(of: scenePhase) { _, phase in
            // Keep the socket for streamed speech and the live voice loop while their audio
            // session is active. An idle chat still disconnects cleanly before suspension.
            switch phase {
            case .active where client.isConfigured:
                client.connect()
            case .background:
                WidgetCenter.shared.reloadAllTimelines()   // whatever changed while it was open shows on the widgets
                if !client.inCall && !client.player.isPlaying { client.suspend() }
            default:
                break
            }
        }
    }
}

/// The app in the theme picked in Settings › Appearance. Read in a view, not the App: there it reached the chat
/// a pass after the Settings sheet took it, so the sheet turned a few frames before the screen behind it.
private struct ThemedRoot: View {
    @AppStorage(ThemeID.key) private var themeID = ThemeID.system.rawValue

    var body: some View {
        let theme = Theme.named(themeID)
        ChatView()
            .environment(\.theme, theme)
            .themeFont(.body)
            .tint(theme.accent)
            // Light or dark on the window (see Theme.applyScheme); a pick in Settings sets it itself first.
            .onAppear { theme.applyScheme() }
            .onChange(of: themeID) { theme.applyScheme() }
    }
}
