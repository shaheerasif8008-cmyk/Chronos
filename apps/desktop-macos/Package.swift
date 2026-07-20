// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "ChronosDesktop",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "ChronosDesktop", targets: ["ChronosDesktop"]),
    ],
    targets: [
        .executableTarget(
            name: "ChronosDesktop",
            path: "Sources/ChronosDesktop"
        ),
    ],
    swiftLanguageModes: [.v5]
)
