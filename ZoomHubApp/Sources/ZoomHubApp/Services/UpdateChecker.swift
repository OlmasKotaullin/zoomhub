import Foundation
import AppKit

@MainActor
@Observable
final class UpdateChecker {
    struct Release: Equatable {
        let version: String
        let url: String
        let notes: String
    }

    private(set) var availableUpdate: Release?
    var dismissed = false

    private static let repoKey = "ZoomHubGitHubRepo"
    private static let defaultRepo = "OlmasKotaullin/zoomhub"
    private static let lastCheckKey = "ZoomHubLastUpdateCheck"
    private static let checkInterval: TimeInterval = 4 * 3600 // 4 часа

    var showBanner: Bool {
        availableUpdate != nil && !dismissed
    }

    var githubRepo: String {
        get { UserDefaults.standard.string(forKey: Self.repoKey) ?? Self.defaultRepo }
        set { UserDefaults.standard.set(newValue, forKey: Self.repoKey) }
    }

    func checkIfNeeded() {
        let last = UserDefaults.standard.double(forKey: Self.lastCheckKey)
        let now = Date().timeIntervalSince1970
        guard now - last > Self.checkInterval else { return }
        Task { await check() }
    }

    func check() async {
        UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: Self.lastCheckKey)

        let repo = githubRepo
        guard let url = URL(string: "https://api.github.com/repos/\(repo)/releases/latest") else { return }

        var request = URLRequest(url: url)
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        request.timeoutInterval = 10

        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else { return }
            guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }

            guard let tagName = json["tag_name"] as? String else { return }
            let remoteVersion = tagName.trimmingCharacters(in: CharacterSet(charactersIn: "vV"))
            let currentVersion = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.0.0"

            guard isNewer(remote: remoteVersion, current: currentVersion) else { return }

            let downloadUrl = findDmgUrl(in: json) ?? "https://github.com/\(repo)/releases/latest"
            let notes = (json["body"] as? String) ?? ""

            await MainActor.run {
                self.availableUpdate = Release(version: remoteVersion, url: downloadUrl, notes: notes)
                self.dismissed = false
            }
        } catch {
            // Тихо игнорируем — не критично
        }
    }

    func openDownload() {
        guard let update = availableUpdate, let url = URL(string: update.url) else { return }
        NSWorkspace.shared.open(url)
    }

    func dismiss() {
        dismissed = true
    }

    private func isNewer(remote: String, current: String) -> Bool {
        let r = remote.split(separator: ".").compactMap { Int($0) }
        let c = current.split(separator: ".").compactMap { Int($0) }

        for i in 0..<max(r.count, c.count) {
            let rv = i < r.count ? r[i] : 0
            let cv = i < c.count ? c[i] : 0
            if rv > cv { return true }
            if rv < cv { return false }
        }
        return false
    }

    private func findDmgUrl(in json: [String: Any]) -> String? {
        guard let assets = json["assets"] as? [[String: Any]] else { return nil }
        for asset in assets {
            if let name = asset["name"] as? String, name.hasSuffix(".dmg"),
               let url = asset["browser_download_url"] as? String {
                return url
            }
        }
        return nil
    }
}
