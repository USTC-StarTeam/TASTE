import SwiftUI

@main
struct TASTEApp: App {
    @StateObject private var model = AppViewModel()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(model)
                .task {
                    await model.runAutoRefreshLoop()
                }
                .task {
                    if let url = MobileLaunchConnectionImport.connectionURL(from: ProcessInfo.processInfo.arguments) {
                        await model.handleConnectionDeepLink(url)
                    }
                }
                .onOpenURL { url in
                    Task { await model.handleConnectionDeepLink(url) }
                }
        }
    }
}
