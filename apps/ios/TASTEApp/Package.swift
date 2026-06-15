// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "TASTEApp",
    platforms: [
        .iOS(.v17),
        .macOS(.v14),
    ],
    products: [
        .library(name: "TASTEAppCore", targets: ["TASTEAppCore"]),
    ],
    targets: [
        .target(name: "TASTEAppCore"),
        .testTarget(name: "TASTEAppCoreTests", dependencies: ["TASTEAppCore"]),
    ]
)
