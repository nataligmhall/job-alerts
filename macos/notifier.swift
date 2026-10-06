import Cocoa
import UserNotifications

final class Delegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        let center = UNUserNotificationCenter.current()
        center.delegate = self

        let args = CommandLine.arguments
        if let index = args.firstIndex(of: "notify"), args.count >= index + 4 {
            let title = args[index + 1]
            let body = args[index + 2]
            let url = args[index + 3]
            NSApp.activate(ignoringOtherApps: true)
            center.requestAuthorization(options: [.alert, .sound]) { granted, error in
                if let error {
                    fputs("notification permission: \(error.localizedDescription)\n", stderr)
                }
                guard granted else { exit(2) }
                let content = UNMutableNotificationContent()
                content.title = title
                content.body = body
                content.sound = .default
                if !url.isEmpty {
                    content.userInfo = ["url": url]
                }
                let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
                center.add(request) { error in
                    exit(error == nil ? 0 : 1)
                }
            }
            return
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + 8) {
            NSApp.terminate(nil)
        }
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let info = response.notification.request.content.userInfo
        if let urlString = info["url"] as? String, let url = URL(string: urlString), !urlString.isEmpty {
            NSWorkspace.shared.open(url)
        }
        completionHandler()
        NSApp.terminate(nil)
    }
}

@main
struct Main {
    static func main() {
        let app = NSApplication.shared
        let delegate = Delegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        _ = delegate
        app.run()
    }
}
