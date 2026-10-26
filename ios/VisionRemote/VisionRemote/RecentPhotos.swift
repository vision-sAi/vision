import AVFoundation
import Photos
import SwiftUI
import UIKit

/// The camera roll's latest shots and videos (screen recordings included) as a grid inside the attach
/// card; tap to select several (numbered in the order picked), Add to attach them, "All Photos" for the full picker.
struct RecentPhotosGrid: View {
    var gridHeight: CGFloat = 340
    /// How many more the message can take: `maxSelection` less what the composer already holds.
    var room = RecentPhotosGrid.maxSelection
    let onPick: ([Attachment]) -> Void
    let onBrowse: () -> Void
    let onBack: () -> Void
    @State private var assets: [PHAsset] = []
    @State private var denied = false
    @State private var selected: [String] = []   // localIdentifiers, in the order tapped
    @State private var loading = false
    @State private var loadError: String?
    @State private var progress = ""

    /// Photos and videos in one message, across the grid, All Photos and what was already attached.
    static let maxSelection = 10

    private let columns = Array(repeating: GridItem(.flexible(), spacing: 2), count: 3)

    var body: some View {
        ZStack(alignment: .top) {
            if denied {
                VStack(spacing: 8) {
                    Text("Photo access is off").themeFont(.subheadline).foregroundStyle(.secondary)
                    Button("Open Settings") {
                        if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
                    }
                }
                .frame(maxWidth: .infinity)
                .frame(height: gridHeight)
                .background(Color(.secondarySystemBackground))
            } else {
                // The grid fills the card edge to edge; the controls float over it.
                ScrollView {
                    LazyVGrid(columns: columns, spacing: 2) {
                        ForEach(assets, id: \.localIdentifier) { asset in
                            PhotoCell(asset: asset, order: selected.firstIndex(of: asset.localIdentifier).map { $0 + 1 },
                                      full: selected.count >= room) {
                                toggle(asset)
                            }
                        }
                    }
                    .padding(.top, 52)
                    .padding(.bottom, 52)
                }
                .frame(height: gridHeight)
                .background(Color(.secondarySystemBackground))
            }
            HStack {
                Spacer()
                Button(action: onBrowse) {
                    Text("All Photos")
                        .themeFont(.subheadline, weight: .semibold)
                        .padding(.horizontal, 14)
                        .frame(height: 36)
                        .background(.regularMaterial, in: Capsule())
                }
            }
            .foregroundStyle(.primary)
            .padding(8)
        }
        .overlay(alignment: .bottomLeading) {
            // Back arrow in the bottom-left corner, over the grid.
            Button(action: onBack) {
                Image(systemName: "chevron.left")
                    .font(.system(size: 15, weight: .semibold))
                    .frame(width: 36, height: 36)
                    .background(.regularMaterial, in: Circle())
            }
            .foregroundStyle(.primary)
            .accessibilityLabel("Back")
            .padding(8)
        }
        .overlay(alignment: .bottomTrailing) {
            if !selected.isEmpty {
                Button { Task { await addSelected() } } label: {
                    Text("Add \(selected.count)")
                        .themeFont(.subheadline, weight: .semibold)
                        .foregroundStyle(.white)
                        .contentTransition(.numericText())
                        .padding(.horizontal, 16)
                        .frame(height: 36)
                        .background(Color.accentColor, in: Capsule())
                }
                .buttonStyle(.plain)
                .disabled(loading)
                .padding(8)
                .transition(.scale(scale: 0.8).combined(with: .opacity))
            }
        }
        .animation(.snappy(duration: 0.2), value: selected)
        .overlay {
            if loading {
                ProgressView(progress)
                    .padding()
                    .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16))
            }
        }
        .alert("Couldn't load photo", isPresented: Binding(
            get: { loadError != nil }, set: { if !$0 { loadError = nil } }
        )) {
            Button("OK") { loadError = nil }
        } message: {
            Text(loadError ?? "Please choose it again.")
        }
        .task { await fetch() }
    }

    private func fetch() async {
        var status = PHPhotoLibrary.authorizationStatus(for: .readWrite)
        if status == .notDetermined {
            status = await PHPhotoLibrary.requestAuthorization(for: .readWrite)
        }
        guard status == .authorized || status == .limited else { denied = true; return }
        let options = PHFetchOptions()
        options.sortDescriptors = [NSSortDescriptor(key: "creationDate", ascending: false)]
        options.fetchLimit = 60
        options.predicate = NSPredicate(format: "mediaType == %d OR mediaType == %d", PHAssetMediaType.image.rawValue, PHAssetMediaType.video.rawValue)
        let result = PHAsset.fetchAssets(with: options)
        var list: [PHAsset] = []
        result.enumerateObjects { asset, _, _ in list.append(asset) }
        assets = list
    }

    private func toggle(_ asset: PHAsset) {
        guard !loading else { return }
        if let i = selected.firstIndex(of: asset.localIdentifier) {
            selected.remove(at: i)
        } else if selected.count < room {
            selected.append(asset.localIdentifier)
        }
    }

    /// Load every selected photo and clip in the order picked, then hand them over together. One that
    /// fails is unselected and nothing is added, so a second Add retries with the rest.
    private func addSelected() async {
        let byID = Dictionary(assets.map { ($0.localIdentifier, $0) }, uniquingKeysWith: { a, _ in a })
        let chosen = selected.compactMap { byID[$0] }
        loading = true
        defer { loading = false }
        var attachments: [Attachment] = []
        for (n, asset) in chosen.enumerated() {
            let kind = asset.mediaType == .video ? "Preparing video" : "Loading photo"
            progress = chosen.count > 1 ? "\(kind) \(n + 1) of \(chosen.count)…" : "\(kind)…"
            do {
                attachments.append(asset.mediaType == .video ? try await loadVideo(asset) : try await loadPhoto(asset))
            } catch {
                selected.removeAll { $0 == asset.localIdentifier }
                loadError = error.localizedDescription
                return
            }
        }
        selected = []
        onPick(attachments)
    }

    private struct LoadFailure: LocalizedError {
        let errorDescription: String?
    }

    private func loadPhoto(_ asset: PHAsset) async throws -> Attachment {
        let options = PHImageRequestOptions()
        options.deliveryMode = .highQualityFormat
        options.isNetworkAccessAllowed = true
        return try await withCheckedThrowingContinuation { continuation in
            PHImageManager.default().requestImage(for: asset, targetSize: CGSize(width: 1568, height: 1568), contentMode: .aspectFit, options: options) { image, info in
                let degraded = (info?[PHImageResultIsDegradedKey] as? Bool) ?? false
                let error = info?[PHImageErrorKey] as? Error
                let cancelled = (info?[PHImageCancelledKey] as? Bool) ?? false
                guard !degraded || error != nil || cancelled else { return }   // the full one follows
                if let error {
                    continuation.resume(throwing: error)
                } else if cancelled {
                    continuation.resume(throwing: LoadFailure(errorDescription: "Photo loading was cancelled. Please try again."))
                } else if let image {
                    continuation.resume(returning: Attachment(image: image))
                } else {
                    continuation.resume(throwing: LoadFailure(errorDescription: "This photo couldn't be downloaded. Try All Photos or choose it again."))
                }
            }
        }
    }

    private func loadVideo(_ asset: PHAsset) async throws -> Attachment {
        let options = PHVideoRequestOptions()
        options.isNetworkAccessAllowed = true
        options.deliveryMode = .highQualityFormat
        let video: AVAsset = try await withCheckedThrowingContinuation { continuation in
            PHImageManager.default().requestAVAsset(forVideo: asset, options: options) { video, _, info in
                if let video {
                    continuation.resume(returning: video)
                } else {
                    let error = info?[PHImageErrorKey] as? Error
                    continuation.resume(throwing: error ?? LoadFailure(errorDescription: "This video couldn't be downloaded. Try All Photos or choose it again."))
                }
            }
        }
        return try await VideoClip.attachment(from: video)
    }
}

private struct PhotoCell: View {
    let asset: PHAsset
    let order: Int?   // its place in the selection, nil when not selected
    let full: Bool    // the message can take no more: the ones not picked fade, so a tap on them isn't a mystery
    let onTap: () -> Void
    @State private var thumb: UIImage?

    var body: some View {
        Button(action: onTap) {
            // A fixed square decides the cell's size; the thumbnail fills it and is cropped, so every
            // cell is identical whatever the photo's shape.
            Color(.tertiarySystemFill)
                .aspectRatio(1, contentMode: .fit)
                .overlay {
                    if let thumb {
                        Image(uiImage: thumb).resizable().scaledToFill()
                    }
                }
                .clipped()
                .overlay(alignment: .bottomLeading) {
                    if asset.mediaType == .video {
                        VideoBadge(duration: asset.duration)
                    }
                }
                .overlay {
                    if order != nil { Color.white.opacity(0.2) }
                    if full && order == nil { Color.black.opacity(0.45) }
                }
                .overlay(alignment: .topTrailing) {
                    ZStack {
                        if let order {
                            Circle().fill(Color.accentColor)
                            Text("\(order)").font(.system(size: 12, weight: .bold)).foregroundStyle(.white)
                        } else {
                            Circle().fill(.black.opacity(0.15))
                        }
                        Circle().strokeBorder(.white, lineWidth: 1.5)
                    }
                    .frame(width: 22, height: 22)
                    .shadow(color: .black.opacity(0.25), radius: 2)
                    .padding(6)
                }
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .task {
            let options = PHImageRequestOptions()
            options.deliveryMode = .opportunistic
            options.resizeMode = .fast
            PHImageManager.default().requestImage(for: asset, targetSize: CGSize(width: 300, height: 300), contentMode: .aspectFill, options: options) { image, _ in
                if let image { thumb = image }
            }
        }
    }
}
