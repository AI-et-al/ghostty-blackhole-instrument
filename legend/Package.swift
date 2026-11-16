// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "BlackholeLegend",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "BlackholeLegend", path: "Sources/BlackholeLegend")
    ]
)
