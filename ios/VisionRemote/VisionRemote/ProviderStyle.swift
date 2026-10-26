import SwiftUI

/// One provider as the machine describes it (`GET /providers`): how to draw it and whether it is usable.
struct ProviderInfo: Decodable, Identifiable {
    let name: String
    let label: String
    let icon: String        // an SF Symbol name
    let tint: String        // "#RRGGBB"
    let source: String      // builtin · config · plugin
    let enabled: Bool
    let ready: Bool?
    var id: String { name }
}

struct ProviderList: Decodable {
    let providers: [ProviderInfo]
}

/// How each brain's provider is drawn across the app: its name, symbol and tint. The machine is the
/// source of truth (`VisionClient.providers`, fetched on connect) so a provider added there, an Ollama or
/// a hosted API, gets its own look; the table below is the fallback for the built-in four before the
/// list arrives and for anything the machine did not describe.
enum ProviderStyle {
    static let order = ["claude", "codex", "grok", "local"]

    /// The machine's descriptions, by name; VisionClient fills this when it connects.
    nonisolated(unsafe) static var known: [String: ProviderInfo] = [:]

    static func title(_ p: String) -> String {
        if let k = known[p] { return k.label }
        switch p {
        case "claude": return "Claude"
        case "codex": return "Codex"
        case "grok": return "Grok"
        case "local": return "Local"
        default: return p.capitalized
        }
    }

    static func icon(_ p: String) -> String {
        if let k = known[p] { return k.icon }
        switch p {
        case "claude": return "sparkles"
        case "codex": return "terminal"
        case "grok": return "bolt.fill"
        case "local": return "desktopcomputer"
        default: return "cpu"
        }
    }

    static func tint(_ p: String) -> Color {
        if let k = known[p], let c = Color(hex: k.tint) { return c }
        switch p {
        case "claude": return Color(red: 0.85, green: 0.47, blue: 0.34)
        case "codex": return Color(red: 0.06, green: 0.60, blue: 0.48)
        case "grok": return Color(red: 0.36, green: 0.40, blue: 0.50)
        case "local": return Color(red: 0.38, green: 0.36, blue: 0.93)
        default: return .gray
        }
    }

    /// Known providers first in the machine's order (the house order before it arrives), anything else after, alphabetically.
    static func sorted(_ providers: some Sequence<String>) -> [String] {
        let present = Set(providers)
        let first = known.isEmpty ? order : Array(known.keys).sorted { (known[$0]?.position ?? 0) < (known[$1]?.position ?? 0) }
        return first.filter { present.contains($0) } + present.subtracting(first).sorted()
    }
}

private extension ProviderInfo {
    /// Where the machine listed it (the registry's order); stored on the side to keep the struct Decodable as-is.
    var position: Int { ProviderStyle.positions[name] ?? Int.max }
}

extension ProviderStyle {
    nonisolated(unsafe) static var positions: [String: Int] = [:]

    /// Take the machine's list: what to draw, and the order it likes.
    static func take(_ list: [ProviderInfo]) {
        known = Dictionary(uniqueKeysWithValues: list.map { ($0.name, $0) })
        positions = Dictionary(uniqueKeysWithValues: list.enumerated().map { ($1.name, $0) })
    }
}

extension Color {
    /// "#RRGGBB" → Color, nil for anything else.
    init?(hex: String) {
        var s = hex.trimmingCharacters(in: .whitespaces)
        if s.hasPrefix("#") { s.removeFirst() }
        guard s.count == 6, let v = UInt32(s, radix: 16) else { return nil }
        self.init(red: Double((v >> 16) & 0xFF) / 255, green: Double((v >> 8) & 0xFF) / 255, blue: Double(v & 0xFF) / 255)
    }
}

/// A provider's symbol on a rounded tile of its tint, like an app icon.
struct ProviderBadge: View {
    let provider: String
    var size: CGFloat = 32

    var body: some View {
        Image(systemName: ProviderStyle.icon(provider))
            .font(.system(size: size * 0.44, weight: .semibold))
            .foregroundStyle(.white)
            .frame(width: size, height: size)
            .background(ProviderStyle.tint(provider).gradient,
                        in: RoundedRectangle(cornerRadius: size * 0.3, style: .continuous))
            .accessibilityHidden(true)
    }
}
