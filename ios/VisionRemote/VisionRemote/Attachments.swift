import AVFoundation
import CoreTransferable
import PhotosUI
import SwiftUI
import UIKit

/// A photo, video or file waiting in the composer: shown as a thumbnail (a chip for a file), uploaded when the
/// message is sent. For a video `image` is its first frame and `video` the H.264 copy that gets uploaded; for a
/// file `file` is the app's own copy of it and `image` just a placeholder. One taken back from a queued message
/// is already on the computer: `uploaded` is its path there, sent again as is.
struct Attachment: Identifiable, Equatable {
    let id = UUID()
    let image: UIImage
    var video: URL? = nil
    var duration: Double = 0
    var file: URL? = nil
    var uploaded: String? = nil

    static let maxFileBytes = 100 * 1024 * 1024   // the machine's limit too

    enum FileFailure: LocalizedError {
        case tooBig(String), unreadable(String)
        var errorDescription: String? {
            switch self {
            case .tooBig(let name): return "\(name) is over 100 MB."
            case .unreadable(let name): return "Couldn't open \(name)."
            }
        }
    }

    /// A file picked in the Files app, copied into the app's temporary folder under its own name (the picker's
    /// URL is only readable while its security scope is open).
    static func file(from picked: URL) throws -> Attachment {
        let name = picked.lastPathComponent
        let scoped = picked.startAccessingSecurityScopedResource()
        defer { if scoped { picked.stopAccessingSecurityScopedResource() } }
        let size = (try? picked.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
        guard size <= maxFileBytes else { throw FileFailure.tooBig(name) }
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("files-\(UUID().uuidString)")
        let copy = folder.appendingPathComponent(name)
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            try FileManager.default.copyItem(at: picked, to: copy)
        } catch {
            throw FileFailure.unreadable(name)
        }
        return Attachment(image: UIImage(), file: copy)
    }

    /// JPEG sized for a model to look at (long edge ≤ 1568 px), not the camera's full 12 MP frame.
    func jpegData() -> Data? {
        let longest = max(image.size.width, image.size.height)
        let scale = min(1, 1568 / longest)
        let size = CGSize(width: image.size.width * scale, height: image.size.height * scale)
        let format = UIGraphicsImageRendererFormat.default()
        format.scale = 1
        let resized = UIGraphicsImageRenderer(size: size, format: format).image { _ in image.draw(in: CGRect(origin: .zero, size: size)) }
        return resized.jpegData(compressionQuality: 0.85)
    }
}

/// Turns a picked video (screen recording or camera clip) into something the machine can digest:
/// H.264 (the machine's ffmpeg has no HEVC decoder), at most 1080p, 30 fps (smooth enough to play back
/// in the chat; a 60 fps screen recording's extra frames only cost upload), sound kept for Whisper.
enum VideoClip {
    static let maxDuration: Double = 300

    enum Failure: LocalizedError {
        case tooLong(Double), cannotExport

        var errorDescription: String? {
            switch self {
            case .tooLong(let s): return "That video is \(clock(s)) long; Vision takes up to \(clock(VideoClip.maxDuration))."
            case .cannotExport: return "That video couldn't be converted."
            }
        }
    }

    static func attachment(from asset: AVAsset) async throws -> Attachment {
        let duration = try await asset.load(.duration).seconds
        guard duration <= maxDuration else { throw Failure.tooLong(duration) }
        let generator = AVAssetImageGenerator(asset: asset)
        generator.appliesPreferredTrackTransform = true
        generator.maximumSize = CGSize(width: 800, height: 800)
        let poster = try await generator.image(at: .zero).image
        guard let export = AVAssetExportSession(asset: asset, presetName: AVAssetExportPreset1920x1080) else { throw Failure.cannotExport }
        if let composition = try? await AVMutableVideoComposition.videoComposition(withPropertiesOf: asset) {
            composition.frameDuration = CMTime(value: 1, timescale: 30)
            composition.sourceTrackIDForFrameTiming = kCMPersistentTrackID_Invalid  // else the source's frame rate wins
            export.videoComposition = composition
        }
        let out = FileManager.default.temporaryDirectory.appendingPathComponent("vision-\(UUID().uuidString).mp4")
        try await export.export(to: out, as: .mp4)
        return Attachment(image: UIImage(cgImage: poster), video: out, duration: duration)
    }

    static func clock(_ seconds: Double) -> String {
        let s = Int(seconds.rounded())
        return String(format: "%d:%02d", s / 60, s % 60)
    }
}

/// A video from the system photo picker, copied out of the picker's temporary file.
struct PickedMovie: Transferable {
    let url: URL

    static var transferRepresentation: some TransferRepresentation {
        FileRepresentation(contentType: .movie) { SentTransferredFile($0.url) } importing: { received in
            let copy = FileManager.default.temporaryDirectory
                .appendingPathComponent("picked-\(UUID().uuidString).\(received.file.pathExtension.isEmpty ? "mov" : received.file.pathExtension)")
            try FileManager.default.copyItem(at: received.file, to: copy)
            return PickedMovie(url: copy)
        }
    }
}

/// Play glyph and length over a video thumbnail.
struct VideoBadge: View {
    let duration: Double

    var body: some View {
        HStack(spacing: 3) {
            Image(systemName: "play.fill").font(.system(size: 9, weight: .bold))
            if duration > 0 { Text(VideoClip.clock(duration)).themeFont(.caption2, weight: .semibold).monospacedDigit() }
        }
        .foregroundStyle(.white)
        .padding(.horizontal, 6)
        .padding(.vertical, 3)
        .background(Color.black.opacity(0.55), in: Capsule())
        .padding(5)
    }
}

/// Big centred play button on dark glass, so a sent video reads as a video at a glance.
struct PlayOverlay: View {
    let size: CGFloat

    var body: some View {
        Image(systemName: "play.fill")
            .font(.system(size: size * 0.4, weight: .semibold))
            .foregroundStyle(.white)
            .offset(x: size * 0.04)  // the triangle's weight sits left of its box; nudge it to look centred
            .frame(width: size, height: size)
            .viewerBacking(in: Circle())
            .shadow(color: .black.opacity(0.25), radius: 6, y: 2)
    }
}

/// Thumbnails above the text field, each with a remove button.
struct AttachmentStrip: View {
    @Binding var attachments: [Attachment]
    @State private var preview: PreviewImage?

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 10) {
                ForEach(attachments) { att in
                    thumbnail(att)
                        .overlay(alignment: .topTrailing) {
                            Button {
                                attachments.removeAll { $0.id == att.id }
                            } label: {
                                Image(systemName: "xmark.circle.fill")
                                    .font(.system(size: 20))
                                    .symbolRenderingMode(.palette)
                                    .foregroundStyle(.white, Color.black.opacity(0.6))
                            }
                            .offset(x: 6, y: -6)
                            .accessibilityLabel(att.file != nil ? "Remove file" : att.video == nil ? "Remove photo" : "Remove video")
                        }
                }
            }
            .padding(.horizontal, 8)
            .padding(.top, 8)
        }
        .imagePreview($preview)
    }

    @ViewBuilder
    private func thumbnail(_ att: Attachment) -> some View {
        if let file = att.file {
            FileChip(name: att.uploaded.map(Message.fileName) ?? file.lastPathComponent)
                .frame(height: 72)
        } else {
            Image(uiImage: att.image)
                .resizable()
                .scaledToFill()
                .frame(width: 72, height: 72)
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                .contentShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                .onTapGesture { preview = PreviewImage(image: att.image, video: att.video.map { AVURLAsset(url: $0) }) }
                .overlay(alignment: .bottomLeading) {
                    if att.video != nil { VideoBadge(duration: att.duration) }
                }
        }
    }
}

/// A file as a small card: its type's icon, then the name (middle-truncated so the extension stays visible).
struct FileChip: View {
    let name: String

    private var symbol: String {
        switch (name as NSString).pathExtension.lowercased() {
        case "pdf": return "doc.richtext"
        case "zip", "gz", "tar", "tgz", "7z", "rar": return "doc.zipper"
        case "mp3", "m4a", "wav", "aac", "flac", "ogg": return "waveform"
        case "csv", "xls", "xlsx", "numbers": return "tablecells"
        case "swift", "py", "js", "ts", "json", "html", "css", "sh", "c", "cpp", "h", "rs", "go", "java", "kt", "rb", "yml", "yaml", "toml", "xml":
            return "chevron.left.forwardslash.chevron.right"
        default: return "doc.text"
        }
    }

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: symbol)
                .font(.system(size: 17, weight: .medium))
                .frame(width: 38, height: 38)
                .background(Color(.tertiarySystemFill), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            Text(name)
                .themeFont(.subheadline)
                .lineLimit(2)
                .truncationMode(.middle)
                .frame(maxWidth: 150, alignment: .leading)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .frame(maxHeight: .infinity)
        .background(Color(.secondarySystemFill), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .accessibilityElement(children: .combine)
        .accessibilityLabel("File \(name)")
    }
}
