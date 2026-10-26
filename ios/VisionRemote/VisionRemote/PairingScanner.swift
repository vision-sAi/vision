import SwiftUI
import VisionKit

/// Live camera view that reads the pairing QR code `vision serve` prints. Not available in the simulator.
struct PairingScanner: UIViewControllerRepresentable {
    let onScan: (String) -> Void

    static var isSupported: Bool {
        DataScannerViewController.isSupported && DataScannerViewController.isAvailable
    }

    func makeUIViewController(context: Context) -> Host {
        Host(delegate: context.coordinator)
    }

    func updateUIViewController(_ uiViewController: Host, context: Context) {}

    func makeCoordinator() -> Coordinator { Coordinator(onScan: onScan) }

    /// Holds the scanner back until the sheet has finished sliding up: starting the camera
    /// mid-animation is what made it stutter. It fades in over black once it's running.
    final class Host: UIViewController {
        private weak var delegate: DataScannerViewControllerDelegate?
        private var scanner: DataScannerViewController?

        init(delegate: DataScannerViewControllerDelegate) {
            self.delegate = delegate
            super.init(nibName: nil, bundle: nil)
        }

        required init?(coder: NSCoder) { fatalError() }

        override func viewDidLoad() {
            super.viewDidLoad()
            view.backgroundColor = .black
        }

        override func viewDidAppear(_ animated: Bool) {
            super.viewDidAppear(animated)
            guard scanner == nil else { return }
            let scanner = DataScannerViewController(
                recognizedDataTypes: [.barcode(symbologies: [.qr])],
                qualityLevel: .balanced,
                isGuidanceEnabled: false,
                isHighlightingEnabled: false
            )
            scanner.delegate = delegate
            addChild(scanner)
            scanner.view.frame = view.bounds
            scanner.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
            scanner.view.alpha = 0
            view.addSubview(scanner.view)
            scanner.didMove(toParent: self)
            self.scanner = scanner
            try? scanner.startScanning()
            UIView.animate(withDuration: 0.25) { scanner.view.alpha = 1 }
        }

        override func viewWillDisappear(_ animated: Bool) {
            super.viewWillDisappear(animated)
            scanner?.stopScanning()
        }
    }

    final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        let onScan: (String) -> Void
        private var done = false

        init(onScan: @escaping (String) -> Void) { self.onScan = onScan }

        func dataScanner(_ dataScanner: DataScannerViewController, didAdd addedItems: [RecognizedItem], allItems: [RecognizedItem]) {
            guard !done else { return }
            for item in addedItems {
                if case .barcode(let code) = item, let payload = code.payloadStringValue {
                    done = true
                    dataScanner.stopScanning()
                    onScan(payload)
                    return
                }
            }
        }
    }
}
