import AVFoundation
import SwiftUI
import UIKit

/// The camera as a card inside the composer, not a screen of its own: rounded viewfinder with the flash,
/// back, shutter and flip controls laid over it.  The shot attaches the moment it is taken.
struct CameraCard: View {
    let onCapture: (UIImage) -> Void
    let onClose: () -> Void
    let onBack: () -> Void
    @StateObject private var camera = CameraModel()

    var body: some View {
        ZStack {
            if camera.denied {
                VStack(spacing: 10) {
                    Image(systemName: "camera.fill").font(.system(size: 34)).foregroundStyle(.secondary)
                    Text("Camera access is off").themeFont(.headline)
                    Button("Open Settings") {
                        if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
                    }
                    .buttonStyle(.borderedProminent)
                }
                .foregroundStyle(.white)
            } else {
                CameraPreview(session: camera.session)
                    .opacity(camera.flashing ? 0 : 1)
            }
            VStack {
                HStack {
                    Spacer()
                    controlButton(camera.flash == .on ? "bolt.fill" : camera.flash == .auto ? "bolt.badge.automatic" : "bolt.slash") {
                        camera.cycleFlash()
                    }
                    .accessibilityLabel("Flash")
                }
                Spacer()
                ZStack {
                    Button {
                        camera.capture { image in
                            onCapture(image)
                            onClose()
                        }
                    } label: {
                        ZStack {
                            Circle().strokeBorder(.white, lineWidth: 3).frame(width: 66, height: 66)
                            Circle().fill(.white).frame(width: 54, height: 54)
                        }
                    }
                    .disabled(camera.capturing || camera.denied)
                    .accessibilityLabel("Take photo")
                    HStack {
                        controlButton("chevron.left", action: onBack).accessibilityLabel("Back")
                        Spacer()
                        controlButton("arrow.triangle.2.circlepath.camera") { camera.flip() }
                            .accessibilityLabel("Switch camera")
                    }
                }
            }
            .padding(12)
        }
        .aspectRatio(3 / 4, contentMode: .fit)
        .frame(maxWidth: .infinity)
        .background(Color(white: 0.1))
        .onAppear { camera.start() }
        .onDisappear { camera.stop() }
    }

    private func controlButton(_ symbol: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(.white)
                .frame(width: 40, height: 40)
                .background(Color.black.opacity(0.45), in: Circle())
        }
    }
}

/// AVCaptureSession plumbing behind the view; all session work happens off the main thread.
@MainActor
final class CameraModel: NSObject, ObservableObject {
    let session = AVCaptureSession()
    @Published var denied = false
    @Published var capturing = false
    @Published var flashing = false
    @Published var flash: AVCaptureDevice.FlashMode = .off

    private let queue = DispatchQueue(label: "vision.camera")
    private let output = AVCapturePhotoOutput()
    private var input: AVCaptureDeviceInput?
    private var position: AVCaptureDevice.Position = .back
    private var configured = false
    private var onCapture: ((UIImage) -> Void)?

    func start() {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            configureAndRun()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video) { ok in
                Task { @MainActor in ok ? self.configureAndRun() : (self.denied = true) }
            }
        default:
            denied = true
        }
    }

    func stop() {
        queue.async { [session] in if session.isRunning { session.stopRunning() } }
    }

    func cycleFlash() {
        switch flash {
        case .off: flash = .auto
        case .auto: flash = .on
        default: flash = .off
        }
    }

    func flip() {
        position = position == .back ? .front : .back
        let position = self.position
        queue.async { [self] in
            session.beginConfiguration()
            if let input { session.removeInput(input) }
            addInput(position)
            session.commitConfiguration()
        }
    }

    func capture(_ done: @escaping (UIImage) -> Void) {
        guard !capturing else { return }
        capturing = true
        onCapture = done
        let settings = AVCapturePhotoSettings()
        if output.supportedFlashModes.contains(flash) { settings.flashMode = flash }
        let mirrored = position == .front
        queue.async { [self] in
            if let connection = output.connection(with: .video), connection.isVideoMirroringSupported {
                connection.isVideoMirrored = mirrored
            }
            output.capturePhoto(with: settings, delegate: self)
        }
        withAnimation(.easeOut(duration: 0.08)) { flashing = true }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) { withAnimation { self.flashing = false } }
    }

    private func configureAndRun() {
        let position = self.position
        queue.async { [self] in
            if !configured {
                session.beginConfiguration()
                session.sessionPreset = .photo
                addInput(position)
                if session.canAddOutput(output) {
                    session.addOutput(output)
                    output.maxPhotoQualityPrioritization = .balanced
                }
                session.commitConfiguration()
                configured = true
            }
            if !session.isRunning { session.startRunning() }
        }
    }

    /// Called on `queue` inside a configuration block.
    nonisolated private func addInput(_ position: AVCaptureDevice.Position) {
        guard let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: position),
              let input = try? AVCaptureDeviceInput(device: device), session.canAddInput(input) else { return }
        session.addInput(input)
        Task { @MainActor in self.input = input }
    }
}

extension CameraModel: AVCapturePhotoCaptureDelegate {
    nonisolated func photoOutput(_ output: AVCapturePhotoOutput, didFinishProcessingPhoto photo: AVCapturePhoto, error: Error?) {
        let image = photo.fileDataRepresentation().flatMap(UIImage.init(data:))
        Task { @MainActor in
            capturing = false
            if let image { onCapture?(image) }
            onCapture = nil
        }
    }
}

/// The live viewfinder.
struct CameraPreview: UIViewRepresentable {
    let session: AVCaptureSession

    func makeUIView(context: Context) -> PreviewView {
        let view = PreviewView()
        view.previewLayer.session = session
        view.previewLayer.videoGravity = .resizeAspectFill
        return view
    }

    func updateUIView(_ uiView: PreviewView, context: Context) {}

    final class PreviewView: UIView {
        override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
        var previewLayer: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }
    }
}
