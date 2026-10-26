import CryptoKit
import Foundation
import Security

/// The key the machine seals notifications and the threads' card with (vision/push.py,
/// vision/liveactivity.py). The app makes it once, hands it to `vision serve` over the paired connection,
/// and keeps it in a keychain group the notification extension shares, so a push can be unsealed while the
/// app is shut. A Live Activity is drawn where there is no keychain at all, so the app also leaves a copy in
/// the app group's container for the card. Apple, and a relay if one is used, carry them but never have it.
enum PushKey {
    private static let service = "vision.push"
    private static let account = "key"
    /// `<team>.<bundle id>.push`, written into each target's Info.plist at build time.
    private static var group: String? { Bundle.main.object(forInfoDictionaryKey: "VisionKeychainGroup") as? String }
    /// `group.<bundle id>`: the app's and the card's shared container, which holds the copy.
    private static var shared: URL? {
        guard let id = Bundle.main.object(forInfoDictionaryKey: "VisionAppGroup") as? String, !id.isEmpty else { return nil }
        return FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: id)?.appendingPathComponent("push.key")
    }

    private static var query: [String: Any] {
        var q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        if let group, !group.isEmpty { q[kSecAttrAccessGroup as String] = group }
        return q
    }

    static func load() -> SymmetricKey? {
        var q = query
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        if SecItemCopyMatching(q as CFDictionary, &item) == errSecSuccess, let data = item as? Data, data.count == 32 {
            return SymmetricKey(data: data)
        }
        guard let shared, let data = try? Data(contentsOf: shared), data.count == 32 else { return nil }
        return SymmetricKey(data: data)
    }

    /// The key, made on first use. Readable after the first unlock, so a push that lands on a locked phone opens.
    static func loadOrCreate() -> SymmetricKey {
        if let key = load() {
            share(key)
            return key
        }
        let key = SymmetricKey(size: .bits256)
        var q = query
        q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        q[kSecValueData as String] = key.withUnsafeBytes { Data($0) }
        SecItemDelete(query as CFDictionary)
        SecItemAdd(q as CFDictionary, nil)
        share(key)
        return key
    }

    /// The copy for the card, rewritten only when it differs; readable after the first unlock, like the keychain's.
    private static func share(_ key: SymmetricKey) {
        guard let shared else { return }
        let data = key.withUnsafeBytes { Data($0) }
        guard (try? Data(contentsOf: shared)) != data else { return }
        try? data.write(to: shared, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    }

    static func base64(_ key: SymmetricKey) -> String {
        key.withUnsafeBytes { Data($0) }.base64EncodedString()
    }

    struct Message: Decodable {
        var title: String
        var body: String
        var chat: String
        /// A project thread's news: a tap opens that thread (absent from everything else, and from an older Vision).
        var project: String?
        var thread: String?
        /// Buttons on it: "approval" (Approve / Deny `approval`) or "land" (Land `thread`).
        var category: String?
        var approval: String?
    }

    /// A sealed notification opened.
    static func open(_ enc: String) -> Message? {
        guard let plain = unseal(enc) else { return nil }
        return try? JSONDecoder().decode(Message.self, from: plain)
    }

    /// What the machine sealed: base64 of nonce + ciphertext + tag (AES-256-GCM), holding JSON.
    static func unseal(_ enc: String) -> Data? {
        guard let key = load(), let raw = Data(base64Encoded: enc),
              let box = try? AES.GCM.SealedBox(combined: raw) else { return nil }
        return try? AES.GCM.open(box, using: key)
    }
}
