import SwiftUI
import UIKit

/// Hearth, our own typeface. Four weights ship in Fonts/ and are listed under
/// UIAppFonts. The Hearth theme (Settings › Appearance, `Theme.swift`) sets text in it; the other
/// themes never load it. These are the names and sizes that theme needs.
enum Hearth {
    static func name(_ weight: Font.Weight) -> String {
        switch weight {
        case .medium: "Hearth-Medium"
        case .semibold: "Hearth-SemiBold"
        case .bold, .heavy, .black: "Hearth-Bold"
        default: "Hearth-Regular"
        }
    }

    static func name(_ weight: UIFont.Weight) -> String {
        switch weight {
        case .medium: "Hearth-Medium"
        case .semibold: "Hearth-SemiBold"
        case .bold, .heavy, .black: "Hearth-Bold"
        default: "Hearth-Regular"
        }
    }

    /// Point sizes of Apple's text styles at the default Dynamic Type setting.
    static func size(_ style: Font.TextStyle) -> CGFloat {
        switch style {
        case .largeTitle: 34
        case .title: 28
        case .title2: 22
        case .title3: 20
        case .headline, .body: 17
        case .callout: 16
        case .subheadline: 15
        case .footnote: 13
        case .caption: 12
        case .caption2: 11
        default: 17
        }
    }
}
