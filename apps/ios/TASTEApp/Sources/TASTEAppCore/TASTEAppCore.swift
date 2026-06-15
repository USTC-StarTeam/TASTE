import Foundation

public enum WorkflowStage: String, CaseIterable, Codable, Hashable, Sendable {
    case find
    case read
    case idea
    case plan
    case environment
    case experiment
    case paper

    public var displayName: String {
        switch self {
        case .find: "Find"
        case .read: "Read"
        case .idea: "Idea"
        case .plan: "Plan"
        case .environment: "Environment"
        case .experiment: "Experiment"
        case .paper: "Paper"
        }
    }

    public var symbolName: String {
        switch self {
        case .find: "magnifyingglass"
        case .read: "book.pages"
        case .idea: "lightbulb"
        case .plan: "list.bullet.clipboard"
        case .environment: "shippingbox"
        case .experiment: "chart.xyaxis.line"
        case .paper: "doc.richtext"
        }
    }
}

public enum ConnectionSettingsError: Error, Equatable {
    case invalidURL(String)
    case unsupportedScheme(String)
}

public struct ConnectionSettings: Codable, Equatable, Sendable {
    public let serverURL: URL
    public let serverAccessToken: String
    public let llmProvider: String
    public let llmBaseURL: URL?
    public let llmModel: String
    public let apiKeyReference: String

    public init(
        serverURLText: String,
        serverAccessToken: String = "",
        llmProvider: String = "",
        llmBaseURLText: String = "",
        llmModel: String = "",
        apiKeyReference: String = ""
    ) throws {
        self.serverURL = try Self.normalizedHTTPURL(serverURLText)
        self.serverAccessToken = serverAccessToken.trimmingCharacters(in: .whitespacesAndNewlines)
        self.llmProvider = llmProvider.trimmingCharacters(in: .whitespacesAndNewlines)
        let llmText = llmBaseURLText.trimmingCharacters(in: .whitespacesAndNewlines)
        self.llmBaseURL = llmText.isEmpty ? nil : try Self.normalizedHTTPURL(llmText)
        self.llmModel = llmModel.trimmingCharacters(in: .whitespacesAndNewlines)
        self.apiKeyReference = apiKeyReference.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private enum CodingKeys: String, CodingKey {
        case serverURL
        case llmProvider
        case llmBaseURL
        case llmModel
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let server = try container.decode(String.self, forKey: .serverURL)
        let provider = try container.decodeIfPresent(String.self, forKey: .llmProvider) ?? ""
        let llmBase = try container.decodeIfPresent(String.self, forKey: .llmBaseURL) ?? ""
        let model = try container.decodeIfPresent(String.self, forKey: .llmModel) ?? ""
        try self.init(serverURLText: server, llmProvider: provider, llmBaseURLText: llmBase, llmModel: model)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(serverURL.absoluteString, forKey: .serverURL)
        try container.encode(llmProvider, forKey: .llmProvider)
        try container.encodeIfPresent(llmBaseURL?.absoluteString, forKey: .llmBaseURL)
        try container.encode(llmModel, forKey: .llmModel)
    }

    public func applyAuthentication(to request: inout URLRequest) {
        guard !serverAccessToken.isEmpty else { return }
        request.setValue("Bearer \(serverAccessToken)", forHTTPHeaderField: "Authorization")
    }

    public var usesDeviceLoopbackServer: Bool {
        guard let host = serverURL.host?.lowercased() else { return false }
        return host == "localhost"
            || host == "::1"
            || host.hasPrefix("127.")
    }

    private static func normalizedHTTPURL(_ value: String) throws -> URL {
        var text = value.trimmingCharacters(in: .whitespacesAndNewlines)
        while text.count > 1, text.hasSuffix("/") {
            text.removeLast()
        }
        guard let url = URL(string: text), let scheme = url.scheme?.lowercased(), let host = url.host, !host.isEmpty else {
            throw ConnectionSettingsError.invalidURL(value)
        }
        guard scheme == "http" || scheme == "https" else {
            throw ConnectionSettingsError.unsupportedScheme(scheme)
        }
        return url
    }
}

public enum ServerConnectionKind: String, Codable, CaseIterable, Hashable, Sendable {
    case computer
    case server
    case cloud

    public var displayName: String {
        switch self {
        case .computer:
            return "Computer"
        case .server:
            return "Server"
        case .cloud:
            return "Cloud"
        }
    }

    public var symbolName: String {
        switch self {
        case .computer:
            return "desktopcomputer"
        case .server:
            return "server.rack"
        case .cloud:
            return "icloud"
        }
    }
}

public struct ServerConnectionProfile: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let name: String
    public let serverURLText: String
    public let kind: ServerConnectionKind

    public init(id: String, name: String, serverURLText: String, kind: ServerConnectionKind) throws {
        let normalized = try ConnectionSettings(serverURLText: serverURLText).serverURL.absoluteString
        self.id = id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? Self.stableID(for: normalized) : id.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        self.name = trimmedName.isEmpty ? kind.displayName : trimmedName
        self.serverURLText = normalized
        self.kind = kind
    }

    public static func stableID(for serverURLText: String) -> String {
        let slug = serverURLText.lowercased().unicodeScalars.map { scalar in
            CharacterSet.alphanumerics.contains(scalar) ? Character(scalar) : "-"
        }
        let compact = String(slug).split(separator: "-").joined(separator: "-")
        return compact.isEmpty ? "server-profile" : "server-\(compact)"
    }
}

public struct ServerConnectionProfileCatalog: Codable, Equatable, Sendable {
    public private(set) var profiles: [ServerConnectionProfile]
    public private(set) var selectedProfileID: String

    public init(profiles: [ServerConnectionProfile], selectedProfileID: String = "") {
        var catalog = ServerConnectionProfileCatalog()
        for profile in profiles {
            catalog.upsert(profile, select: selectedProfileID.isEmpty || profile.id == selectedProfileID)
        }
        if catalog.selectedProfileID.isEmpty {
            catalog.selectedProfileID = catalog.profiles.first?.id ?? ""
        }
        self = catalog
    }

    public init() {
        profiles = []
        selectedProfileID = ""
    }

    public var selectedProfile: ServerConnectionProfile? {
        profiles.first { $0.id == selectedProfileID } ?? profiles.first
    }

    public mutating func select(_ profileID: String) {
        if profiles.contains(where: { $0.id == profileID }) {
            selectedProfileID = profileID
        }
    }

    public mutating func upsert(_ profile: ServerConnectionProfile, select: Bool = true) {
        if let existingIndex = profiles.firstIndex(where: { $0.id == profile.id || $0.serverURLText == profile.serverURLText }) {
            profiles[existingIndex] = profile
        } else {
            profiles.append(profile)
        }
        if select {
            selectedProfileID = profile.id
        }
    }

    @discardableResult
    public mutating func upsertProfile(
        id: String,
        name: String,
        serverURLText: String,
        kind: ServerConnectionKind
    ) throws -> ServerConnectionProfile {
        let profile = try ServerConnectionProfile(id: id, name: name, serverURLText: serverURLText, kind: kind)
        upsert(profile)
        return profile
    }
}

public enum MobileConnectionLinkError: Error, Equatable, Sendable {
    case unsupportedURL
    case missingServerURL
    case invalidConnectionKind(String)
}

public struct MobileConnectionLink: Equatable, Sendable {
    public let profile: ServerConnectionProfile
    public let serverAccessToken: String
    public let selectedProjectID: String

    public init(url: URL) throws {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              components.scheme?.lowercased() == "taste",
              Self.isConnectRoute(host: components.host, path: components.path) else {
            throw MobileConnectionLinkError.unsupportedURL
        }

        let parameters = Self.queryParameters(from: components)
        guard let serverURLText = Self.firstValue(in: parameters, keys: ["server_url", "server", "url"]) else {
            throw MobileConnectionLinkError.missingServerURL
        }

        let kindText = Self.firstValue(in: parameters, keys: ["kind", "target", "target_type"]) ?? ServerConnectionKind.server.rawValue
        guard let kind = ServerConnectionKind(rawValue: kindText.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()) else {
            throw MobileConnectionLinkError.invalidConnectionKind(kindText)
        }

        let profileName = Self.firstValue(in: parameters, keys: ["profile", "profile_name", "name"]) ?? kind.displayName
        profile = try ServerConnectionProfile(id: "", name: profileName, serverURLText: serverURLText, kind: kind)
        serverAccessToken = Self.firstValue(in: parameters, keys: ["token", "server_access_token", "access_token"]) ?? ""
        selectedProjectID = Self.firstValue(in: parameters, keys: ["project", "project_id", "selected_project"]) ?? ""
    }

    private static func isConnectRoute(host: String?, path: String) -> Bool {
        let normalizedHost = host?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let normalizedPath = path.trimmingCharacters(in: CharacterSet(charactersIn: "/")).lowercased()
        return normalizedHost == "connect" || normalizedPath == "connect"
    }

    private static func queryParameters(from components: URLComponents) -> [String: String] {
        var result: [String: String] = [:]
        for item in components.queryItems ?? [] {
            let name = item.name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            let value = (item.value ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if !name.isEmpty, !value.isEmpty, result[name] == nil {
                result[name] = value
            }
        }
        return result
    }

    private static func firstValue(in parameters: [String: String], keys: [String]) -> String? {
        keys.compactMap { parameters[$0] }.first
    }
}

public enum MobileLaunchConnectionImport {
    public static let argumentName = "--taste-connection-link"

    public static func connectionURL(from arguments: [String]) -> URL? {
        guard let markerIndex = arguments.firstIndex(of: argumentName) else { return nil }
        let linkIndex = arguments.index(after: markerIndex)
        guard arguments.indices.contains(linkIndex) else { return nil }
        let rawLink = arguments[linkIndex].trimmingCharacters(in: .whitespacesAndNewlines)
        guard rawLink.hasPrefix("taste://connect"), let url = URL(string: rawLink) else { return nil }
        return url
    }
}

public struct TASTEEndpointBuilder: Sendable {
    public let settings: ConnectionSettings

    public init(settings: ConnectionSettings) {
        self.settings = settings
    }

    public func projects() -> URL {
        url(path: "/api/projects")
    }

    public func jobs(project: String? = nil) -> URL {
        var items = [
            URLQueryItem(name: "compact", value: "1"),
            URLQueryItem(name: "limit", value: "12"),
            URLQueryItem(name: "include_history", value: "1"),
        ]
        if let project, !project.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            items.append(URLQueryItem(name: "project", value: project))
        }
        return url(path: "/api/jobs", queryItems: items)
    }

    public func projectAction() -> URL {
        url(path: "/api/jobs/project")
    }

    public func config() -> URL {
        url(path: "/api/config")
    }

    public func configMeta() -> URL {
        url(path: "/api/config/meta")
    }

    public func llmProbe() -> URL {
        url(path: "/api/config/llm-probe")
    }

    public func projectSummary(projectID: String) -> URL {
        url(path: "/api/projects/\(projectID)")
    }

    public func projectConfig(projectID: String) -> URL {
        url(path: "/api/projects/\(projectID)/config")
    }

    public func projectClaudeLatestResponse(projectID: String, stage: WorkflowStage? = nil, maxChars: Int = MobileClaudeResponse.mobileMaxCharacters) -> URL {
        var items = [
            URLQueryItem(name: "max_chars", value: "\(MobileClaudeResponse.clampedMaxCharacters(maxChars))")
        ]
        if let stage {
            items.append(URLQueryItem(name: "stage", value: stage.rawValue))
        }
        return url(path: "/api/projects/\(projectID)/claude/latest-response", queryItems: items)
    }

    public func projectRuntime(projectID: String) -> URL {
        url(path: "/api/projects/\(projectID)/runtime")
    }

    public func projectRuntimeDetect(projectID: String) -> URL {
        url(path: "/api/projects/\(projectID)/runtime/detect")
    }

    public func job(jobID: String, compact: Bool = false) -> URL {
        url(path: "/api/jobs/\(jobID)", queryItems: [URLQueryItem(name: "compact", value: compact ? "1" : "0")])
    }

    public func cancelJob(jobID: String) -> URL {
        url(path: "/api/jobs/\(jobID)/cancel")
    }

    private func url(path: String, queryItems: [URLQueryItem] = []) -> URL {
        var components = URLComponents(url: settings.serverURL, resolvingAgainstBaseURL: false)!
        components.path = path
        components.queryItems = queryItems.isEmpty ? nil : queryItems
        return components.url!
    }
}

public enum TASTEJobStatus: String, Codable, Sendable {
    case queued
    case running
    case stale
    case done
    case blocked
    case error
    case cancelling
    case cancelled
    case previewAvailable = "preview_available"
    case needsWriting = "needs_writing"
    case previewPDFBlocked = "preview_pdf_blocked"

    public var isLive: Bool {
        switch self {
        case .queued, .running, .cancelling: true
        default: false
        }
    }
}

public struct TASTEJobProgress: Codable, Equatable, Sendable {
    public let phase: String
    public let current: Int
    public let total: Int
    public let percent: Int
    public let message: String

    public init(phase: String, current: Int, total: Int, percent: Int, message: String) {
        self.phase = phase
        self.current = current
        self.total = total
        self.percent = max(0, min(100, percent))
        self.message = message
    }
}

public struct TASTEJob: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let stage: WorkflowStage
    public let status: TASTEJobStatus
    public let createdAt: String
    public let cancelRequested: Bool
    public let progress: TASTEJobProgress?
    public let logs: [String]

    public init(id: String, stage: WorkflowStage, status: TASTEJobStatus, createdAt: String, cancelRequested: Bool = false, progress: TASTEJobProgress? = nil, logs: [String] = []) {
        self.id = id
        self.stage = stage
        self.status = status
        self.createdAt = createdAt
        self.cancelRequested = cancelRequested
        self.progress = progress
        self.logs = logs
    }

    private enum CodingKeys: String, CodingKey {
        case id = "job_id"
        case stage
        case status
        case createdAt = "created_at"
        case cancelRequested = "cancel_requested"
        case progress
        case logs
    }

    public static func decodeList(from data: Data) throws -> [TASTEJob] {
        if let wrapped = try? JSONDecoder.taste.decode(JobListResponse.self, from: data) {
            return wrapped.jobs
        }
        return try JSONDecoder.taste.decode([TASTEJob].self, from: data)
    }
}

public struct TASTEProject: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let name: String
    public let topic: String
    public let path: String

    public init(id: String, name: String, topic: String, path: String) {
        self.id = id
        self.name = name
        self.topic = topic
        self.path = path
    }

    public static func decodeList(from data: Data) throws -> [TASTEProject] {
        if let wrapped = try? JSONDecoder.taste.decode(ProjectListResponse.self, from: data) {
            return wrapped.projects
        }
        return try JSONDecoder.taste.decode([TASTEProject].self, from: data)
    }

    public static func decodeOne(from data: Data) throws -> TASTEProject {
        if let direct = try? JSONDecoder.taste.decode(TASTEProject.self, from: data) {
            return direct
        }
        let summary = try JSONDecoder.taste.decode(ProjectSummaryResponse.self, from: data)
        return TASTEProject(
            id: summary.project,
            name: summary.config?.name ?? summary.project,
            topic: summary.config?.topic ?? "",
            path: summary.path
        )
    }
}

private struct ProjectListResponse: Codable {
    let projects: [TASTEProject]
}

private struct ProjectSummaryResponse: Codable {
    let project: String
    let path: String
    let config: ProjectSummaryConfig?
}

private struct ProjectSummaryConfig: Codable {
    let name: String?
    let topic: String?
}

private struct JobListResponse: Codable {
    let jobs: [TASTEJob]
}

public struct RemoteArtifact: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let title: String
    public let kind: ArtifactKind
    public let urlString: String

    public init(id: String, title: String, kind: ArtifactKind, urlString: String) {
        self.id = id
        self.title = title
        self.kind = kind
        self.urlString = urlString.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public func remoteURL(relativeTo serverURL: URL) -> URL? {
        guard let normalized = Self.normalizedRemoteURLString(urlString) else { return nil }
        if let absolute = URL(string: normalized), let scheme = absolute.scheme?.lowercased() {
            return (scheme == "http" || scheme == "https") ? absolute : nil
        }
        guard normalized.hasPrefix("/api/") else { return nil }
        return URL(string: normalized, relativeTo: serverURL)?.absoluteURL
    }

    public func openPlan(relativeTo settings: ConnectionSettings) -> RemoteArtifactOpenPlan {
        let url = remoteURL(relativeTo: settings.serverURL)
        let protectedServerFile = url.map { Self.requiresAuthenticatedPreview(url: $0, serverURL: settings.serverURL) } ?? false
        let requiresPreview = !settings.serverAccessToken.isEmpty && protectedServerFile
        return .init(
            artifact: self,
            url: url,
            mode: requiresPreview ? .authenticatedPreview : .externalLink,
            note: requiresPreview
                ? "Open inside TASTE so the saved server access token can be sent with the request."
                : "Open remote artifact link."
        )
    }

    static func normalizedRemoteURLString(_ value: String) -> String? {
        let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        if let absolute = URL(string: text), let scheme = absolute.scheme?.lowercased() {
            return (scheme == "http" || scheme == "https") ? text : nil
        }
        return text.hasPrefix("/api/") ? text : nil
    }

    private static func requiresAuthenticatedPreview(url: URL, serverURL: URL) -> Bool {
        guard url.scheme?.lowercased() == serverURL.scheme?.lowercased(),
              url.host?.lowercased() == serverURL.host?.lowercased(),
              url.port == serverURL.port
        else {
            return false
        }
        return url.path.hasPrefix("/api/projects/") && url.path.contains("/files/")
    }
}

public enum RemoteArtifactOpenMode: String, Equatable, Sendable {
    case externalLink
    case authenticatedPreview
}

public struct RemoteArtifactOpenPlan: Equatable, Sendable {
    public let artifact: RemoteArtifact
    public let url: URL?
    public let mode: RemoteArtifactOpenMode
    public let note: String

    public init(artifact: RemoteArtifact, url: URL?, mode: RemoteArtifactOpenMode, note: String) {
        self.artifact = artifact
        self.url = url
        self.mode = mode
        self.note = note
    }

    public var canOpenExternally: Bool {
        url != nil && mode == .externalLink
    }
}

public enum ProjectAttentionKind: String, Codable, Sendable {
    case blocker
    case nextAction = "next_action"
}

public struct ProjectAttentionItem: Equatable, Identifiable, Sendable {
    public let id: String
    public let kind: ProjectAttentionKind
    public let title: String
    public let detail: String

    public init(id: String, kind: ProjectAttentionKind, title: String, detail: String) {
        self.id = id
        self.kind = kind
        self.title = title
        self.detail = detail
    }
}

public struct TASTEProjectSummary: Equatable, Sendable {
    public let project: String
    public let path: String
    public let stageSnapshots: [WorkflowStage: StageSnapshot]
    public let remoteArtifacts: [RemoteArtifact]
    public let attentionItems: [ProjectAttentionItem]
    public let runtimeStatus: ProjectRuntimeStatus?
    public let runPreferences: ProjectResearchPreferences

    public init(
        project: String,
        path: String,
        stageSnapshots: [WorkflowStage: StageSnapshot],
        remoteArtifacts: [RemoteArtifact] = [],
        attentionItems: [ProjectAttentionItem] = [],
        runtimeStatus: ProjectRuntimeStatus? = nil,
        runPreferences: ProjectResearchPreferences = .init()
    ) {
        self.project = project
        self.path = path
        self.stageSnapshots = stageSnapshots
        self.remoteArtifacts = remoteArtifacts
        self.attentionItems = attentionItems
        self.runtimeStatus = runtimeStatus
        self.runPreferences = runPreferences
    }

    public static func decode(from data: Data) throws -> TASTEProjectSummary {
        try JSONDecoder.taste.decode(TASTEProjectSummary.self, from: data)
    }
}

extension TASTEProjectSummary: Decodable {
    private enum CodingKeys: String, CodingKey {
        case project
        case path
        case stages
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let dynamicContainer = try decoder.container(keyedBy: DynamicCodingKey.self)
        project = try container.decodeIfPresent(String.self, forKey: .project) ?? ""
        path = try container.decodeIfPresent(String.self, forKey: .path) ?? ""

        var snapshots: [WorkflowStage: StageSnapshot] = [:]
        var artifacts: [RemoteArtifact] = []
        var seenArtifactURLs: Set<String> = []
        let attention = Self.makeAttentionItems(from: dynamicContainer)

        try Self.appendArtifacts(from: dynamicContainer, candidates: Self.topLevelArtifactCandidates, into: &artifacts, seenURLs: &seenArtifactURLs)

        if container.contains(.stages) {
            let stagesContainer = try container.nestedContainer(keyedBy: DynamicCodingKey.self, forKey: .stages)
            for key in stagesContainer.allKeys {
                guard let stage = WorkflowStage(rawValue: key.stringValue) else { continue }
                let value = try stagesContainer.decode(ProjectStageSummaryPayload.self, forKey: key)
                snapshots[stage] = StageSnapshot(
                    stage: stage,
                    status: value.status,
                    summary: value.preferredSummary
                )
            }
            if stagesContainer.contains(.init("paper")) {
                let paperContainer = try stagesContainer.nestedContainer(keyedBy: DynamicCodingKey.self, forKey: .init("paper"))
                try Self.appendArtifacts(from: paperContainer, candidates: Self.paperStageArtifactCandidates, into: &artifacts, seenURLs: &seenArtifactURLs)
            }
        }
        stageSnapshots = snapshots
        remoteArtifacts = artifacts
        attentionItems = attention
        let nestedRuntimeStatus = try? dynamicContainer.decode(ProjectRuntimeStatus.self, forKey: .init("runtime"))
        let topLevelRuntimeStatus = try? ProjectRuntimeStatus(from: decoder)
        if nestedRuntimeStatus?.hasStatusPayload == true {
            runtimeStatus = nestedRuntimeStatus
        } else if topLevelRuntimeStatus?.hasStatusPayload == true {
            runtimeStatus = topLevelRuntimeStatus
        } else {
            runtimeStatus = nil
        }
        runPreferences =
            (try? dynamicContainer.decode(ProjectResearchPreferences.self, forKey: .init("run_preferences")))
            ?? (try? dynamicContainer.decode(ProjectResearchPreferences.self, forKey: .init("config")))
            ?? .init()
    }
}

private extension TASTEProjectSummary {
    struct ArtifactCandidate {
        let key: String
        let title: String
        let kind: ArtifactKind
    }

    static let topLevelArtifactCandidates: [ArtifactCandidate] = [
        .init(key: "latest_generated_pdf_url", title: "Paper PDF", kind: .paperPDF),
        .init(key: "pdf_url", title: "Paper PDF", kind: .paperPDF),
        .init(key: "blocked_pdf_url", title: "Blocked PDF Preview", kind: .paperPDF),
        .init(key: "latest_generated_tex_url", title: "TeX Source", kind: .texSource),
        .init(key: "tex_url", title: "TeX Source", kind: .texSource),
        .init(key: "blocked_tex_url", title: "Blocked TeX Source", kind: .texSource),
        .init(key: "raw_pdf_url", title: "Raw Paper PDF", kind: .paperPDF),
        .init(key: "raw_tex_url", title: "Raw TeX Source", kind: .texSource),
    ]

    static let paperStageArtifactCandidates: [ArtifactCandidate] = [
        .init(key: "pdf_url", title: "Paper PDF", kind: .paperPDF),
        .init(key: "tex_url", title: "TeX Source", kind: .texSource),
        .init(key: "latest_generated_pdf_url", title: "Paper PDF", kind: .paperPDF),
        .init(key: "latest_generated_tex_url", title: "TeX Source", kind: .texSource),
        .init(key: "blocked_pdf_url", title: "Blocked PDF Preview", kind: .paperPDF),
        .init(key: "blocked_tex_url", title: "Blocked TeX Source", kind: .texSource),
        .init(key: "raw_pdf_url", title: "Raw Paper PDF", kind: .paperPDF),
        .init(key: "raw_tex_url", title: "Raw TeX Source", kind: .texSource),
    ]

    static func appendArtifacts(
        from container: KeyedDecodingContainer<DynamicCodingKey>,
        candidates: [ArtifactCandidate],
        into artifacts: inout [RemoteArtifact],
        seenURLs: inout Set<String>
    ) throws {
        for candidate in candidates {
            let rawValue = try container.decodeLossyStringIfPresent(forKey: .init(candidate.key))
            guard let normalized = RemoteArtifact.normalizedRemoteURLString(rawValue), !seenURLs.contains(normalized) else {
                continue
            }
            seenURLs.insert(normalized)
            artifacts.append(RemoteArtifact(
                id: "\(candidate.kind.rawValue)-\(artifacts.count)",
                title: candidate.title,
                kind: candidate.kind,
                urlString: normalized
            ))
        }
    }

    static func makeAttentionItems(from container: KeyedDecodingContainer<DynamicCodingKey>) -> [ProjectAttentionItem] {
        var items: [ProjectAttentionItem] = []
        var seenKeys: Set<String> = []

        func append(_ payload: ProjectAttentionPayload, kind: ProjectAttentionKind, defaultTitle: String) {
            guard let resolved = payload.resolved(defaultTitle: defaultTitle) else { return }
            let uniqueKey = "\(kind.rawValue)|\(resolved.title)|\(resolved.detail)"
            guard seenKeys.insert(uniqueKey).inserted else { return }
            items.append(ProjectAttentionItem(
                id: "\(kind.rawValue)-\(items.count)",
                kind: kind,
                title: resolved.title,
                detail: resolved.detail
            ))
        }

        for payload in decodeAttentionPayloads(from: container, forKey: "current_blocker") {
            append(payload, kind: .blocker, defaultTitle: "Current blocker")
        }
        for payload in decodeAttentionPayloads(from: container, forKey: "blockers") {
            append(payload, kind: .blocker, defaultTitle: "Current blocker")
        }

        let nextActionCount = items.filter { $0.kind == .nextAction }.count
        for payload in decodeAttentionPayloads(from: container, forKey: "next_actions") {
            append(payload, kind: .nextAction, defaultTitle: "Next action")
        }
        if items.filter({ $0.kind == .nextAction }).count == nextActionCount {
            for payload in decodeAttentionPayloads(from: container, forKey: "next_action") {
                append(payload, kind: .nextAction, defaultTitle: "Next action")
            }
        }
        for payload in decodeAttentionPayloads(from: container, forKey: "blocker_action_plan_summary") {
            append(payload, kind: .nextAction, defaultTitle: "Blocker action plan")
        }

        return items
    }

    static func decodeAttentionPayloads(
        from container: KeyedDecodingContainer<DynamicCodingKey>,
        forKey key: String
    ) -> [ProjectAttentionPayload] {
        let codingKey = DynamicCodingKey(key)
        if let values = try? container.decode([ProjectAttentionPayload].self, forKey: codingKey) {
            return values
        }
        if let value = try? container.decode(ProjectAttentionPayload.self, forKey: codingKey) {
            return [value]
        }
        if let values = try? container.decode([String].self, forKey: codingKey) {
            return values.map { ProjectAttentionPayload(text: $0) }
        }
        if let value = try? container.decodeLossyStringIfPresent(forKey: codingKey),
           !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return [ProjectAttentionPayload(text: value)]
        }
        return []
    }
}

private struct ProjectStageSummaryPayload: Decodable {
    let status: String
    let summary: String
    let summaryZh: String
    let summaryEn: String
    let humanSummary: String
    let reason: String

    private enum CodingKeys: String, CodingKey {
        case status
        case summary
        case summaryZh = "summary_zh"
        case summaryEn = "summary_en"
        case humanSummary = "human_summary"
        case reason
    }

    var preferredSummary: String {
        [summary, summaryZh, summaryEn, humanSummary, reason, status]
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .first { !$0.isEmpty } ?? ""
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        status = try container.decodeLossyStringIfPresent(forKey: .status)
        summary = try container.decodeLossyStringIfPresent(forKey: .summary)
        summaryZh = try container.decodeLossyStringIfPresent(forKey: .summaryZh)
        summaryEn = try container.decodeLossyStringIfPresent(forKey: .summaryEn)
        humanSummary = try container.decodeLossyStringIfPresent(forKey: .humanSummary)
        reason = try container.decodeLossyStringIfPresent(forKey: .reason)
    }
}

private struct ProjectAttentionPayload: Decodable {
    let title: String
    let issue: String
    let action: String
    let nextAction: String
    let summary: String
    let reason: String
    let category: String
    let message: String
    let detail: String
    let description: String
    let recommendation: String

    init(text: String) {
        title = ""
        issue = ""
        action = ""
        nextAction = ""
        summary = ""
        reason = ""
        category = ""
        message = ""
        detail = text
        description = ""
        recommendation = ""
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: DynamicCodingKey.self)
        title = try container.decodeLossyStringIfPresent(forKey: .init("title"))
        issue = try container.decodeLossyStringIfPresent(forKey: .init("issue"))
        action = try container.decodeLossyStringIfPresent(forKey: .init("action"))
        nextAction = try container.decodeLossyStringIfPresent(forKey: .init("next_action"))
        summary = try container.decodeLossyStringIfPresent(forKey: .init("summary"))
        reason = try container.decodeLossyStringIfPresent(forKey: .init("reason"))
        category = try container.decodeLossyStringIfPresent(forKey: .init("category"))
        message = try container.decodeLossyStringIfPresent(forKey: .init("message"))
        detail = try container.decodeLossyStringIfPresent(forKey: .init("detail"))
        description = try container.decodeLossyStringIfPresent(forKey: .init("description"))
        recommendation = try container.decodeLossyStringIfPresent(forKey: .init("recommendation"))
    }

    func resolved(defaultTitle: String) -> (title: String, detail: String)? {
        let resolvedTitle = Self.firstNonEmpty([title, issue, message, action, category])
        let resolvedDetail = Self.firstNonEmpty([nextAction, detail, summary, reason, description, recommendation])

        if resolvedTitle.isEmpty && resolvedDetail.isEmpty {
            return nil
        }
        if resolvedTitle.isEmpty {
            return (defaultTitle, resolvedDetail)
        }
        return (resolvedTitle, resolvedDetail)
    }

    private static func firstNonEmpty(_ values: [String]) -> String {
        values
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .first { !$0.isEmpty } ?? ""
    }
}

public struct StageSnapshot: Codable, Equatable, Sendable {
    public let stage: WorkflowStage
    public let status: String
    public let summary: String

    public init(stage: WorkflowStage, status: String, summary: String) {
        self.stage = stage
        self.status = status
        self.summary = summary
    }

    public var mobileStatusLabel: String {
        let normalized = status
            .replacingOccurrences(of: "_", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        guard !normalized.isEmpty else { return "" }

        switch normalized {
        case "waiting for environment repair":
            return "waiting for repair"
        case "waiting for environment review":
            return "waiting for review"
        default:
            break
        }

        let stagePrefix = "\(stage.displayName.lowercased()) "
        let withoutStagePrefix = normalized.hasPrefix(stagePrefix)
            ? String(normalized.dropFirst(stagePrefix.count))
            : normalized
        return withoutStagePrefix
    }
}

public struct WorkflowProgressSummary: Equatable, Sendable {
    public let selectedProject: String
    public let activeStage: WorkflowStage?
    public let percentComplete: Int
    public let statusLine: String
    public let completedStageCount: Int

    public init(selectedProject: String, stageSnapshots: [WorkflowStage: StageSnapshot], jobs: [TASTEJob]) {
        self.selectedProject = selectedProject
        if let liveJob = jobs
            .filter({ $0.status.isLive })
            .sorted(by: { WorkflowStage.allCases.firstIndex(of: $0.stage)! < WorkflowStage.allCases.firstIndex(of: $1.stage)! })
            .last {
            self.activeStage = liveJob.stage
            self.percentComplete = liveJob.progress?.percent ?? Self.stageStartPercent(liveJob.stage)
            self.statusLine = liveJob.progress?.message.isEmpty == false ? liveJob.progress!.message : "\(liveJob.stage.displayName) \(liveJob.status.rawValue)"
            self.completedStageCount = WorkflowStage.allCases.firstIndex(of: liveJob.stage) ?? 0
            return
        }

        if let latestSnapshot = WorkflowStage.allCases
            .compactMap({ stageSnapshots[$0] })
            .last {
            self.activeStage = latestSnapshot.stage
            self.percentComplete = Self.percentComplete(for: latestSnapshot)
            self.statusLine = latestSnapshot.summary.isEmpty ? latestSnapshot.status : latestSnapshot.summary
            self.completedStageCount = Self.completedStageCount(for: latestSnapshot)
            return
        }

        self.activeStage = nil
        self.percentComplete = 0
        self.statusLine = "No active workflow"
        self.completedStageCount = 0
    }

    private static func percentComplete(for snapshot: StageSnapshot) -> Int {
        if isCompleteStatus(snapshot.status) {
            return stageCompletionPercent(snapshot.stage)
        }
        return stageStartPercent(snapshot.stage)
    }

    private static func completedStageCount(for snapshot: StageSnapshot) -> Int {
        guard let index = WorkflowStage.allCases.firstIndex(of: snapshot.stage) else { return 0 }
        return isCompleteStatus(snapshot.status) ? min(WorkflowStage.allCases.count, index + 1) : index
    }

    private static func stageStartPercent(_ stage: WorkflowStage) -> Int {
        guard let index = WorkflowStage.allCases.firstIndex(of: stage) else { return 0 }
        return Int(Double(index) / Double(max(1, WorkflowStage.allCases.count)) * 100)
    }

    private static func stageCompletionPercent(_ stage: WorkflowStage) -> Int {
        guard let index = WorkflowStage.allCases.firstIndex(of: stage) else { return 0 }
        return Int(Double(index + 1) / Double(max(1, WorkflowStage.allCases.count)) * 100)
    }

    private static func isCompleteStatus(_ status: String) -> Bool {
        let normalized = status.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return [
            "done",
            "completed",
            "complete",
            "ready",
            "selected",
            "preview_available",
            "submission_ready",
        ].contains(normalized)
    }
}

public struct MobileJobDetailSummary: Equatable, Identifiable, Sendable {
    public let id: String
    public let title: String
    public let statusLine: String
    public let progressLabel: String
    public let percentComplete: Int
    public let logTail: [String]

    public init(job: TASTEJob, maxLogLines: Int = 24) {
        id = job.id
        title = job.stage.displayName
        if let progress = job.progress {
            statusLine = progress.message.isEmpty ? progress.phase : progress.message
            progressLabel = progress.total > 0 ? "\(progress.current)/\(progress.total)" : ""
            percentComplete = progress.percent
        } else {
            statusLine = job.status.rawValue.replacingOccurrences(of: "_", with: " ")
            progressLabel = ""
            percentComplete = 0
        }
        logTail = Array(job.logs.suffix(max(0, maxLogLines)))
    }
}

public enum ArtifactKind: String, Codable, Hashable, Sendable {
    case markdownSummary
    case jobList
    case projectSummary
    case paperPDF
    case texSource
    case dataset
    case repositoryCheckout
}

public struct MobileStoragePolicy: Equatable, Sendable {
    public static let `default` = MobileStoragePolicy(
        maxCachedBytes: 20 * 1024 * 1024,
        cacheableArtifactKinds: [.markdownSummary, .jobList, .projectSummary]
    )

    public let maxCachedBytes: Int
    public let cacheableArtifactKinds: Set<ArtifactKind>

    public init(maxCachedBytes: Int, cacheableArtifactKinds: Set<ArtifactKind>) {
        self.maxCachedBytes = maxCachedBytes
        self.cacheableArtifactKinds = cacheableArtifactKinds
    }
}

public struct MobileNetworkPolicy: Equatable, Sendable {
    public static let `default` = MobileNetworkPolicy(
        memoryCacheBytes: 4 * 1024 * 1024,
        diskCacheBytes: 0,
        timeoutForRequest: 25,
        timeoutForResource: 60,
        requestCachePolicy: .reloadIgnoringLocalCacheData
    )

    public let memoryCacheBytes: Int
    public let diskCacheBytes: Int
    public let timeoutForRequest: TimeInterval
    public let timeoutForResource: TimeInterval
    public let requestCachePolicy: URLRequest.CachePolicy

    public init(
        memoryCacheBytes: Int,
        diskCacheBytes: Int,
        timeoutForRequest: TimeInterval,
        timeoutForResource: TimeInterval,
        requestCachePolicy: URLRequest.CachePolicy
    ) {
        self.memoryCacheBytes = max(0, memoryCacheBytes)
        self.diskCacheBytes = max(0, diskCacheBytes)
        self.timeoutForRequest = timeoutForRequest
        self.timeoutForResource = timeoutForResource
        self.requestCachePolicy = requestCachePolicy
    }
}

public struct MobileAutoRefreshPolicy: Equatable, Sendable {
    public static let `default` = MobileAutoRefreshPolicy(
        activeJobIntervalSeconds: 8,
        idleIntervalSeconds: 30
    )

    public let activeJobIntervalSeconds: TimeInterval
    public let idleIntervalSeconds: TimeInterval

    public init(activeJobIntervalSeconds: TimeInterval, idleIntervalSeconds: TimeInterval) {
        self.activeJobIntervalSeconds = max(6, activeJobIntervalSeconds)
        self.idleIntervalSeconds = max(self.activeJobIntervalSeconds, idleIntervalSeconds)
    }

    public func hasLiveJobs(_ jobs: [TASTEJob]) -> Bool {
        jobs.contains { $0.status.isLive }
    }

    public func nextIntervalSeconds(for jobs: [TASTEJob]) -> TimeInterval {
        hasLiveJobs(jobs) ? activeJobIntervalSeconds : idleIntervalSeconds
    }
}

public enum MobileConnectionImportAction: String, CaseIterable, Sendable {
    case pastedText
    case clipboard
    case qrCode

    public var buttonTitle: String {
        switch self {
        case .pastedText: "Import Connection Link"
        case .clipboard: "Import From Clipboard"
        case .qrCode: "Scan Connection QR"
        }
    }

    public var systemImage: String {
        switch self {
        case .pastedText: "link.badge.plus"
        case .clipboard: "doc.on.clipboard"
        case .qrCode: "qrcode.viewfinder"
        }
    }

    public var accessibilityLabel: String {
        switch self {
        case .pastedText: "Import pasted TASTE connection link"
        case .clipboard: "Import TASTE connection link from clipboard"
        case .qrCode: "Scan TASTE connection QR code"
        }
    }
}

public enum MobileFormFieldID: String, CaseIterable, Sendable {
    case researchTopic = "research_topic"
    case researchInterest = "research_interest"
    case researcherProfile = "researcher_profile"
    case targetVenue = "target_venue"
    case paperTitle = "paper_title"
    case newProjectID = "new_project_id"
    case connectionProfileName = "connection_profile_name"
    case connectionLink = "connection_link"
    case serverURL = "server_url"
    case serverAccessToken = "server_access_token"
    case llmProvider = "llm_provider"
    case llmBaseURL = "llm_base_url"
    case llmModel = "llm_model"
    case llmAPIKey = "llm_api_key"
    case claudePath = "claude_path"
    case managementPython = "management_python"
    case nodeBin = "node_bin"
    case condaEnv = "conda_env"
    case condaBase = "conda_base"
    case experimentPython = "experiment_python"
    case extraPathEntries = "extra_path_entries"
}

public struct MobileFormFieldSpec: Equatable, Sendable {
    public let id: MobileFormFieldID
    public let title: String
    public let prompt: String
    public let accessibilityLabel: String

    public init(id: MobileFormFieldID, title: String, prompt: String, accessibilityLabel: String) {
        self.id = id
        self.title = title
        self.prompt = prompt
        self.accessibilityLabel = accessibilityLabel
    }
}

public enum MobileFormFieldCatalog {
    public static func spec(for id: MobileFormFieldID) -> MobileFormFieldSpec {
        switch id {
        case .researchTopic:
            return .init(id: id, title: "Research topic", prompt: "Briefly describe the research direction", accessibilityLabel: "Research topic")
        case .researchInterest:
            return .init(id: id, title: "Research interest", prompt: "Specific problems, methods, and exclusions for Find", accessibilityLabel: "Research interest")
        case .researcherProfile:
            return .init(id: id, title: "Researcher profile", prompt: "Background, constraints, and preferred evidence", accessibilityLabel: "Researcher profile")
        case .targetVenue:
            return .init(id: id, title: "Target venue or journal", prompt: "ICLR, NeurIPS, Nature, or leave blank", accessibilityLabel: "Target venue or journal")
        case .paperTitle:
            return .init(id: id, title: "Paper title", prompt: "Optional title used for Paper stage", accessibilityLabel: "Paper title")
        case .newProjectID:
            return .init(id: id, title: "New project ID", prompt: "letters, numbers, dash, or underscore", accessibilityLabel: "New project ID")
        case .connectionProfileName:
            return .init(id: id, title: "Connection name", prompt: "Mac, lab server, or cloud worker", accessibilityLabel: "Connection profile name")
        case .connectionLink:
            return .init(id: id, title: "Connection link", prompt: "paste taste://connect link", accessibilityLabel: "TASTE connection link")
        case .serverURL:
            return .init(id: id, title: "TASTE server URL", prompt: "http://computer-or-server:8765", accessibilityLabel: "TASTE server URL")
        case .serverAccessToken:
            return .init(id: id, title: "Server access token", prompt: "Optional bearer token for protected server", accessibilityLabel: "TASTE server access token")
        case .llmProvider:
            return .init(id: id, title: "Find LLM provider", prompt: "openai_compatible", accessibilityLabel: "Find LLM provider")
        case .llmBaseURL:
            return .init(id: id, title: "Find LLM base URL", prompt: "https://api.example.com/v1", accessibilityLabel: "Find LLM base URL")
        case .llmModel:
            return .init(id: id, title: "Find LLM model", prompt: "model name used for scoring", accessibilityLabel: "Find LLM model")
        case .llmAPIKey:
            return .init(id: id, title: "Find LLM API key", prompt: "sent once to TASTE, not saved locally", accessibilityLabel: "Find LLM API key")
        case .claudePath:
            return .init(id: id, title: "Claude path", prompt: "/usr/local/bin/claude", accessibilityLabel: "Claude executable path")
        case .managementPython:
            return .init(id: id, title: "Management Python", prompt: "/srv/taste/.venv/bin/python", accessibilityLabel: "Management Python path")
        case .nodeBin:
            return .init(id: id, title: "Node bin", prompt: "/opt/node/bin", accessibilityLabel: "Node bin directory")
        case .condaEnv:
            return .init(id: id, title: "Conda env", prompt: "taste-exp", accessibilityLabel: "Conda environment")
        case .condaBase:
            return .init(id: id, title: "Conda base", prompt: "/opt/miniforge3", accessibilityLabel: "Conda base path")
        case .experimentPython:
            return .init(id: id, title: "Experiment Python", prompt: "/srv/taste/envs/exp/bin/python", accessibilityLabel: "Experiment Python path")
        case .extraPathEntries:
            return .init(id: id, title: "Extra PATH entries", prompt: "/opt/cuda/bin:/srv/tools/bin", accessibilityLabel: "Extra PATH entries")
        }
    }
}

public enum ReadinessCheckState: String, Codable, Equatable, Sendable {
    case ready
    case blocked
}

public struct ReadinessCheck: Identifiable, Codable, Equatable, Sendable {
    public let id: String
    public let title: String
    public let detail: String
    public let state: ReadinessCheckState
    public let symbolName: String

    public init(id: String, title: String, detail: String, state: ReadinessCheckState, symbolName: String) {
        self.id = id
        self.title = title
        self.detail = detail
        self.state = state
        self.symbolName = symbolName
    }
}

public struct MobileReadinessSummary: Equatable, Sendable {
    public let checks: [ReadinessCheck]

    public init(
        serverURLText: String,
        connectionKind: ServerConnectionKind = .computer,
        serverReachable: Bool?,
        serverMeta: TASTEServerMeta? = nil,
        selectedProjectID: String,
        llmProvider: String,
        llmBaseURLText: String,
        llmModel: String,
        runtimeConfiguration: ProjectRuntimeConfiguration,
        runtimeStatus: ProjectRuntimeStatus? = nil
    ) {
        self.checks = [
            Self.serverCheck(serverURLText: serverURLText, connectionKind: connectionKind, serverReachable: serverReachable, serverMeta: serverMeta),
            Self.projectCheck(selectedProjectID: selectedProjectID),
            Self.llmCheck(provider: llmProvider, baseURLText: llmBaseURLText, model: llmModel),
            Self.runtimeCheck(runtimeConfiguration, runtimeStatus: runtimeStatus),
            .init(
                id: "storage",
                title: "Phone storage",
                detail: "Only settings and compact summaries stay on this device.",
                state: .ready,
                symbolName: "internaldrive"
            ),
        ]
    }

    public var readyCount: Int {
        checks.filter { $0.state == .ready }.count
    }

    public var blockedCount: Int {
        checks.filter { $0.state == .blocked }.count
    }

    public var isReadyForFullWorkflow: Bool {
        blockedCount == 0
    }

    public var statusLine: String {
        if isReadyForFullWorkflow {
            return "Ready to run remote TASTE workflows"
        }
        return "\(blockedCount) setup \(blockedCount == 1 ? "item" : "items") need attention"
    }

    private static func serverCheck(serverURLText: String, connectionKind: ServerConnectionKind, serverReachable: Bool?, serverMeta: TASTEServerMeta?) -> ReadinessCheck {
        let settings: ConnectionSettings
        do {
            settings = try ConnectionSettings(serverURLText: serverURLText)
        } catch {
            return .init(
                id: "server",
                title: "TASTE server",
                detail: "Enter a full http:// or https:// server URL.",
                state: .blocked,
                symbolName: "network.slash"
            )
        }

        if settings.usesDeviceLoopbackServer, serverReachable != true {
            return .init(
                id: "server",
                title: "TASTE server",
                detail: "127.0.0.1/localhost only reaches this iPhone. Use your computer's LAN IP, VPN, or tunnel URL, then tap Test Connection.",
                state: .blocked,
                symbolName: "iphone.slash"
            )
        }

        if connectionKind == .cloud, settings.serverURL.scheme?.lowercased() == "http" {
            return .init(
                id: "server",
                title: "TASTE server",
                detail: "Cloud connections must use https:// or an authenticated tunnel so the server access token is not sent in clear text.",
                state: .blocked,
                symbolName: "lock.trianglebadge.exclamationmark"
            )
        }

        if serverReachable == true, let serverMeta, !serverMeta.supportsMobileControlPlane {
            return .init(
                id: "server",
                title: "TASTE server",
                detail: "Connected, but this TASTE server does not advertise the mobile control-plane API. Update branch-app, restart scripts/start_web.sh, then tap Test Connection.",
                state: .blocked,
                symbolName: "server.rack"
            )
        }

        if serverReachable == false {
            return .init(
                id: "server",
                title: "TASTE server",
                detail: "Server URL is valid, but the app has not reached it yet.",
                state: .blocked,
                symbolName: "network.slash"
            )
        }

        return .init(
            id: "server",
            title: "TASTE server",
            detail: serverReachable == true ? "Connected to the configured TASTE API." : "Tap Test Connection to verify the configured TASTE API.",
            state: serverReachable == true ? .ready : .blocked,
            symbolName: serverReachable == true ? "network" : "network.badge.shield.half.filled"
        )
    }

    private static func projectCheck(selectedProjectID: String) -> ReadinessCheck {
        let projectID = selectedProjectID.trimmingCharacters(in: .whitespacesAndNewlines)
        return .init(
            id: "project",
            title: "Research project",
            detail: projectID.isEmpty ? "Create or select a TASTE project on the server." : "Project \(projectID) is selected.",
            state: projectID.isEmpty ? .blocked : .ready,
            symbolName: projectID.isEmpty ? "folder.badge.questionmark" : "folder.badge.gearshape"
        )
    }

    private static func llmCheck(provider: String, baseURLText: String, model: String) -> ReadinessCheck {
        let provider = provider.trimmingCharacters(in: .whitespacesAndNewlines)
        let model = model.trimmingCharacters(in: .whitespacesAndNewlines)
        let baseURLText = baseURLText.trimmingCharacters(in: .whitespacesAndNewlines)
        let hasValidBaseURL = (try? ConnectionSettings(serverURLText: "http://localhost:8765", llmBaseURLText: baseURLText).llmBaseURL) != nil
        let ready = !provider.isEmpty && !model.isEmpty && hasValidBaseURL

        return .init(
            id: "llm",
            title: "Find LLM",
            detail: ready ? "\(provider) / \(model) is configured." : "Set provider, base URL, model, and sync the API key to TASTE.",
            state: ready ? .ready : .blocked,
            symbolName: ready ? "key.viewfinder" : "key.slash"
        )
    }

    private static func runtimeCheck(_ configuration: ProjectRuntimeConfiguration, runtimeStatus: ProjectRuntimeStatus?) -> ReadinessCheck {
        let ready = [
            configuration.condaEnv,
            configuration.condaBase,
            configuration.nodeBin,
            configuration.claudePath,
            configuration.managementPython,
            configuration.experimentPython,
            configuration.extraPathText,
        ].contains { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

        if ready, let runtimeStatus, !runtimeStatus.failingCriticalChecks.isEmpty {
            let names = runtimeStatus.failingCriticalChecks.map(\.displayName).joined(separator: ", ")
            return .init(
                id: "runtime",
                title: "Remote runtime",
                detail: "Fix remote runtime checks: \(names).",
                state: .blocked,
                symbolName: "desktopcomputer.trianglebadge.exclamationmark"
            )
        }

        return .init(
            id: "runtime",
            title: "Remote runtime",
            detail: ready ? (runtimeStatus?.hasChecks == true ? "Critical remote runtime checks passed." : "Server-side runtime hints are filled in.") : "Detect or save the Python, Claude, Node, and environment paths used by the TASTE server.",
            state: ready ? .ready : .blocked,
            symbolName: ready ? "desktopcomputer" : "desktopcomputer.trianglebadge.exclamationmark"
        )
    }
}

public struct MobileRunContext: Equatable, Sendable {
    public let serverURLText: String
    public let connectionKind: ServerConnectionKind
    public let serverReachable: Bool?
    public let serverMeta: TASTEServerMeta?
    public let selectedProjectID: String
    public let llmProvider: String
    public let llmBaseURLText: String
    public let llmModel: String
    public let runtimeConfiguration: ProjectRuntimeConfiguration
    public let runtimeStatus: ProjectRuntimeStatus?
    public let stageSnapshots: [WorkflowStage: StageSnapshot]

    public init(
        serverURLText: String,
        connectionKind: ServerConnectionKind = .computer,
        serverReachable: Bool?,
        serverMeta: TASTEServerMeta? = nil,
        selectedProjectID: String,
        llmProvider: String,
        llmBaseURLText: String,
        llmModel: String,
        runtimeConfiguration: ProjectRuntimeConfiguration,
        runtimeStatus: ProjectRuntimeStatus? = nil,
        stageSnapshots: [WorkflowStage: StageSnapshot] = [:]
    ) {
        self.serverURLText = serverURLText
        self.connectionKind = connectionKind
        self.serverReachable = serverReachable
        self.serverMeta = serverMeta
        self.selectedProjectID = selectedProjectID
        self.llmProvider = llmProvider
        self.llmBaseURLText = llmBaseURLText
        self.llmModel = llmModel
        self.runtimeConfiguration = runtimeConfiguration
        self.runtimeStatus = runtimeStatus
        self.stageSnapshots = stageSnapshots
    }

    public var readinessSummary: MobileReadinessSummary {
        MobileReadinessSummary(
            serverURLText: serverURLText,
            connectionKind: connectionKind,
            serverReachable: serverReachable,
            serverMeta: serverMeta,
            selectedProjectID: selectedProjectID,
            llmProvider: llmProvider,
            llmBaseURLText: llmBaseURLText,
            llmModel: llmModel,
            runtimeConfiguration: runtimeConfiguration,
            runtimeStatus: runtimeStatus
        )
    }

    var hasValidServerURL: Bool {
        (try? ConnectionSettings(serverURLText: serverURLText)) != nil
    }

    var hasSelectedProject: Bool {
        !selectedProjectID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var hasLLMConfiguration: Bool {
        let provider = llmProvider.trimmingCharacters(in: .whitespacesAndNewlines)
        let model = llmModel.trimmingCharacters(in: .whitespacesAndNewlines)
        let baseURLText = llmBaseURLText.trimmingCharacters(in: .whitespacesAndNewlines)
        let hasBaseURL = (try? ConnectionSettings(serverURLText: "http://localhost:8765", llmBaseURLText: baseURLText).llmBaseURL) != nil
        return !provider.isEmpty && !model.isEmpty && hasBaseURL
    }

    var hasRuntimeConfiguration: Bool {
        let hasAnyRuntimeHint = [
            runtimeConfiguration.condaEnv,
            runtimeConfiguration.condaBase,
            runtimeConfiguration.nodeBin,
            runtimeConfiguration.claudePath,
            runtimeConfiguration.managementPython,
            runtimeConfiguration.experimentPython,
            runtimeConfiguration.extraPathText,
        ].contains { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        guard hasAnyRuntimeHint else { return false }
        guard let runtimeStatus else { return true }
        return runtimeStatus.failingCriticalChecks.isEmpty
    }

    var hasCurrentFindPacket: Bool {
        guard let find = stageSnapshots[.find] else { return false }
        let normalized = find.status.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return [
            "done",
            "completed",
            "complete",
            "ready",
            "selected",
        ].contains(normalized)
    }

    var currentFindFailureReason: String {
        "Run Find first so the server has a current Find packet for this project."
    }

    var runtimeFailureReason: String {
        guard let runtimeStatus, !runtimeStatus.failingCriticalChecks.isEmpty else {
            return "Detect or save the remote runtime before starting this stage."
        }
        let names = runtimeStatus.failingCriticalChecks.map(\.displayName).joined(separator: ", ")
        return "Fix remote runtime checks before starting this stage: \(names)."
    }
}

public struct ProjectActionAvailability: Equatable, Sendable {
    public let action: ProjectAction
    public let isEnabled: Bool
    public let reason: String

    public init(action: ProjectAction, isEnabled: Bool, reason: String) {
        self.action = action
        self.isEnabled = isEnabled
        self.reason = reason
    }

    public static func evaluate(_ action: ProjectAction, context: MobileRunContext) -> ProjectActionAvailability {
        guard context.hasValidServerURL else {
            return disabled(action, "Enter a valid TASTE server URL before running.")
        }
        guard context.serverReachable != false else {
            return disabled(action, "Reconnect to the TASTE server before running.")
        }
        guard context.hasSelectedProject else {
            return disabled(action, "Create or select a TASTE project before running.")
        }

        switch action {
        case .status, .healthcheck:
            return enabled(action)
        case .currentFindSelection:
            guard context.hasCurrentFindPacket else {
                return disabled(action, context.currentFindFailureReason)
            }
            return enabled(action)
        case .find:
            guard context.hasLLMConfiguration else {
                return disabled(action, "Configure the Find LLM before starting Find.")
            }
            return enabled(action)
        case .fullCycle:
            let readiness = context.readinessSummary
            guard readiness.isReadyForFullWorkflow else {
                return disabled(action, readiness.statusLine)
            }
            return enabled(action)
        case .read, .idea, .plan:
            guard context.hasRuntimeConfiguration else {
                return disabled(action, context.runtimeFailureReason)
            }
            guard context.hasCurrentFindPacket else {
                return disabled(action, context.currentFindFailureReason)
            }
            return enabled(action)
        case .environment, .experiment, .paper:
            guard context.hasRuntimeConfiguration else {
                return disabled(action, context.runtimeFailureReason)
            }
            return enabled(action)
        }
    }

    private static func enabled(_ action: ProjectAction) -> ProjectActionAvailability {
        ProjectActionAvailability(action: action, isEnabled: true, reason: "Ready to run on the TASTE server.")
    }

    private static func disabled(_ action: ProjectAction, _ reason: String) -> ProjectActionAvailability {
        ProjectActionAvailability(action: action, isEnabled: false, reason: reason)
    }
}

public enum ProjectAction: String, Codable, CaseIterable, Sendable {
    case status
    case healthcheck
    case find
    case read
    case idea
    case plan
    case environment
    case experiment
    case paper
    case currentFindSelection = "current-find-selection"
    case fullCycle = "full-cycle"

    public var mobileRunTitle: String {
        switch self {
        case .status:
            return "Server Status"
        case .healthcheck:
            return "Health Check"
        case .find:
            return "Find"
        case .read:
            return "Read Current Find"
        case .idea:
            return "Generate Ideas"
        case .plan:
            return "Draft Plans"
        case .environment:
            return "Environment"
        case .experiment:
            return "Experiment"
        case .paper:
            return "Paper"
        case .currentFindSelection:
            return "Select Current Find Plan"
        case .fullCycle:
            return "Run Full Research Workflow"
        }
    }

    public var runsCurrentFindBridge: Bool {
        switch self {
        case .read, .idea, .plan:
            return true
        default:
            return false
        }
    }

    public var syncsProjectResearchPreferencesBeforeRun: Bool {
        switch self {
        case .status, .healthcheck:
            return false
        default:
            return true
        }
    }

    public var requiresMobileConfirmation: Bool {
        syncsProjectResearchPreferencesBeforeRun
    }

    public var mobileConfirmationMessage: String {
        "\(mobileRunTitle) will dispatch work on the configured TASTE server. The phone will stay a lightweight control plane and only follow progress."
    }
}

public enum ProjectActionOption: String, Codable, Hashable, Sendable {
    case autoInstallLatex = "auto_install_latex"
    case useExistingLiteraturePacket = "use_existing_literature_packet"
    case realBootstrapEnv = "real_bootstrap_env"
    case skipPaper = "skip_paper"
}

public struct MobileActionLaunchGate: Equatable, Sendable {
    private var inFlightKeys: Set<String> = []

    public init() {}

    public mutating func begin(projectID: String, action: ProjectAction, options: [ProjectActionOption: Bool]) -> Bool {
        let key = Self.key(projectID: projectID, action: action, options: options)
        guard !inFlightKeys.contains(key) else { return false }
        inFlightKeys.insert(key)
        return true
    }

    public mutating func finish(projectID: String, action: ProjectAction, options: [ProjectActionOption: Bool]) {
        inFlightKeys.remove(Self.key(projectID: projectID, action: action, options: options))
    }

    private static func key(projectID: String, action: ProjectAction, options: [ProjectActionOption: Bool]) -> String {
        let project = projectID.trimmingCharacters(in: .whitespacesAndNewlines)
        let optionText = options
            .sorted { $0.key.rawValue < $1.key.rawValue }
            .map { "\($0.key.rawValue)=\($0.value ? "1" : "0")" }
            .joined(separator: ",")
        return "\(project)|\(action.rawValue)|\(optionText)"
    }
}

public struct TASTEServerMeta: Codable, Equatable, Sendable {
    public let saved: Bool
    public let mobileAPIVersion: Int
    public let mobileCapabilities: [String]

    private enum CodingKeys: String, CodingKey {
        case saved
        case mobileAPIVersion = "mobile_api_version"
        case mobileCapabilities = "mobile_capabilities"
    }

    public init(saved: Bool, mobileAPIVersion: Int = 0, mobileCapabilities: [String] = []) {
        self.saved = saved
        self.mobileAPIVersion = mobileAPIVersion
        self.mobileCapabilities = mobileCapabilities
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.init(
            saved: try container.decodeIfPresent(Bool.self, forKey: .saved) ?? false,
            mobileAPIVersion: try container.decodeIfPresent(Int.self, forKey: .mobileAPIVersion) ?? 0,
            mobileCapabilities: try container.decodeIfPresent([String].self, forKey: .mobileCapabilities) ?? []
        )
    }

    public var supportsMobileControlPlane: Bool {
        let capabilities = Set(mobileCapabilities.map { $0.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() })
        return mobileAPIVersion >= 1
            && capabilities.isSuperset(of: [
                "projects",
                "jobs",
                "runtime",
                "llm_config",
                "claude_latest_response",
                "remote_artifacts",
            ])
    }
}

public struct LLMProbeSummary: Codable, Equatable, Sendable {
    public let provider: String
    public let baseURL: String
    public let model: String
    public let enabled: Bool

    private enum CodingKeys: String, CodingKey {
        case provider
        case baseURL = "base_url"
        case model
        case enabled
    }

    public init(provider: String = "", baseURL: String = "", model: String = "", enabled: Bool = false) {
        self.provider = provider
        self.baseURL = baseURL
        self.model = model
        self.enabled = enabled
    }
}

public struct LLMProbeResult: Codable, Equatable, Sendable {
    public let ok: Bool
    public let error: String
    public let probe: String
    public let summary: LLMProbeSummary

    public init(ok: Bool, error: String = "", probe: String = "", summary: LLMProbeSummary = .init()) {
        self.ok = ok
        self.error = error
        self.probe = probe
        self.summary = summary
    }
}

public struct ProjectRuntimeConfiguration: Codable, Equatable, Sendable {
    public let condaEnv: String
    public let condaBase: String
    public let nodeBin: String
    public let claudePath: String
    public let managementPython: String
    public let experimentPython: String
    public let extraPathText: String

    public init(
        condaEnv: String = "",
        condaBase: String = "",
        nodeBin: String = "",
        claudePath: String = "",
        managementPython: String = "",
        experimentPython: String = "",
        extraPathText: String = ""
    ) {
        self.condaEnv = condaEnv.trimmingCharacters(in: .whitespacesAndNewlines)
        self.condaBase = condaBase.trimmingCharacters(in: .whitespacesAndNewlines)
        self.nodeBin = nodeBin.trimmingCharacters(in: .whitespacesAndNewlines)
        self.claudePath = claudePath.trimmingCharacters(in: .whitespacesAndNewlines)
        self.managementPython = managementPython.trimmingCharacters(in: .whitespacesAndNewlines)
        self.experimentPython = experimentPython.trimmingCharacters(in: .whitespacesAndNewlines)
        self.extraPathText = extraPathText.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public static func decode(from data: Data) throws -> ProjectRuntimeConfiguration {
        try JSONDecoder.taste.decode(ProjectRuntimeConfiguration.self, from: data)
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: DynamicCodingKey.self)
        if let nested = try? container.decode(ProjectRuntimeConfiguration.self, forKey: .init("runtime")),
           nested.hasRuntimeValue {
            self = nested
            return
        }

        let managementPython = try container.decodeLossyStringIfPresent(forKey: .init("management_python"))
        let pythonExecutable = try container.decodeLossyStringIfPresent(forKey: .init("python_executable"))
        try self.init(
            condaEnv: container.decodeLossyStringIfPresent(forKey: .init("conda_env")),
            condaBase: container.decodeLossyStringIfPresent(forKey: .init("conda_base")),
            nodeBin: container.decodeLossyStringIfPresent(forKey: .init("node_bin")),
            claudePath: container.decodeLossyStringIfPresent(forKey: .init("claude_path")),
            managementPython: managementPython.isEmpty ? pythonExecutable : managementPython,
            experimentPython: container.decodeLossyStringIfPresent(forKey: .init("experiment_python")),
            extraPathText: container.decodeLossyStringListIfPresent(forKey: .init("extra_path")).joined(separator: ":")
        )
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: DynamicCodingKey.self)
        if !condaEnv.isEmpty { try container.encode(condaEnv, forKey: .init("conda_env")) }
        if !condaBase.isEmpty { try container.encode(condaBase, forKey: .init("conda_base")) }
        if !nodeBin.isEmpty { try container.encode(nodeBin, forKey: .init("node_bin")) }
        if !claudePath.isEmpty { try container.encode(claudePath, forKey: .init("claude_path")) }
        if !managementPython.isEmpty { try container.encode(managementPython, forKey: .init("management_python")) }
        if !experimentPython.isEmpty { try container.encode(experimentPython, forKey: .init("experiment_python")) }
        let extraPath = extraPathText
            .split(whereSeparator: { $0 == ":" || $0 == "," || $0 == "\n" })
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        if !extraPath.isEmpty {
            try container.encode(extraPath, forKey: .init("extra_path"))
        }
    }

    var hasRuntimeValue: Bool {
        [
            condaEnv,
            condaBase,
            nodeBin,
            claudePath,
            managementPython,
            experimentPython,
            extraPathText,
        ].contains { !$0.isEmpty }
    }
}

public struct RuntimeCheck: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let path: String
    public let ok: Bool
    public let version: String
    public let reason: String

    public init(id: String, path: String, ok: Bool, version: String, reason: String) {
        self.id = id
        self.path = path.trimmingCharacters(in: .whitespacesAndNewlines)
        self.ok = ok
        self.version = version.trimmingCharacters(in: .whitespacesAndNewlines)
        self.reason = reason.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var displayName: String {
        switch id {
        case "management_python":
            return "Management Python"
        case "experiment_python":
            return "Experiment Python"
        case "conda_base":
            return "Conda base"
        case "node":
            return "Node"
        case "npm":
            return "npm"
        case "claude":
            return "Claude"
        case "conda":
            return "Conda"
        case "python":
            return "Python"
        default:
            return id
                .replacingOccurrences(of: "_", with: " ")
                .split(separator: " ")
                .map { $0.prefix(1).uppercased() + $0.dropFirst() }
                .joined(separator: " ")
        }
    }

    private enum CodingKeys: String, CodingKey {
        case path
        case ok
        case version
        case reason
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.init(
            id: "",
            path: try container.decodeIfPresent(String.self, forKey: .path) ?? "",
            ok: try container.decodeIfPresent(Bool.self, forKey: .ok) ?? false,
            version: try container.decodeIfPresent(String.self, forKey: .version) ?? "",
            reason: try container.decodeIfPresent(String.self, forKey: .reason) ?? ""
        )
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(path, forKey: .path)
        try container.encode(ok, forKey: .ok)
        if !version.isEmpty { try container.encode(version, forKey: .version) }
        if !reason.isEmpty { try container.encode(reason, forKey: .reason) }
    }
}

public struct ProjectRuntimeStatus: Codable, Equatable, Sendable {
    public let project: String
    public let runtime: ProjectRuntimeConfiguration
    public let checks: [String: RuntimeCheck]
    public let pathHead: [String]

    public init(project: String, runtime: ProjectRuntimeConfiguration, checks: [String: RuntimeCheck], pathHead: [String]) {
        self.project = project.trimmingCharacters(in: .whitespacesAndNewlines)
        self.runtime = runtime
        self.checks = checks
        self.pathHead = pathHead
    }

    public static func decode(from data: Data) throws -> ProjectRuntimeStatus {
        try JSONDecoder.taste.decode(ProjectRuntimeStatus.self, from: data)
    }

    public var hasChecks: Bool {
        !checks.isEmpty
    }

    var hasStatusPayload: Bool {
        runtime.hasRuntimeValue || hasChecks || !pathHead.isEmpty
    }

    public var orderedChecks: [RuntimeCheck] {
        let priority = ["management_python", "experiment_python", "claude", "node", "npm", "conda", "conda_base", "python"]
        var emitted = Set<String>()
        var result: [RuntimeCheck] = []
        for id in priority {
            if let check = checks[id], emitted.insert(id).inserted {
                result.append(check)
            }
        }
        for id in checks.keys.sorted() where emitted.insert(id).inserted {
            if let check = checks[id] {
                result.append(check)
            }
        }
        return result
    }

    public var failingCriticalChecks: [RuntimeCheck] {
        ["management_python", "claude"]
            .compactMap { checks[$0] }
            .filter { !$0.ok }
    }

    public var summaryLine: String {
        guard hasChecks else { return "Runtime checks not loaded" }
        let failed = failingCriticalChecks.count
        guard failed > 0 else { return "Critical runtime checks passed" }
        return "\(failed) critical runtime \(failed == 1 ? "check needs" : "checks need") attention"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: DynamicCodingKey.self)
        project = try container.decodeLossyStringIfPresent(forKey: .init("project"))
        runtime = (try? container.decode(ProjectRuntimeConfiguration.self, forKey: .init("runtime"))) ?? .init()
        pathHead = try container.decodeLossyStringListIfPresent(forKey: .init("path_head"))
        let decodedChecks = (try? container.decode([String: RuntimeCheck].self, forKey: .init("checks"))) ?? [:]
        checks = Dictionary(uniqueKeysWithValues: decodedChecks.map { id, check in
            (id, RuntimeCheck(id: id, path: check.path, ok: check.ok, version: check.version, reason: check.reason))
        })
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: DynamicCodingKey.self)
        if !project.isEmpty { try container.encode(project, forKey: .init("project")) }
        try container.encode(runtime, forKey: .init("runtime"))
        if !checks.isEmpty { try container.encode(checks, forKey: .init("checks")) }
        if !pathHead.isEmpty { try container.encode(pathHead, forKey: .init("path_head")) }
    }
}

public struct CreateProjectPayload: Encodable, Equatable, Sendable {
    public let id: String
    public let topic: String

    public init(id: String, topic: String = "") {
        self.id = id.trimmingCharacters(in: .whitespacesAndNewlines)
        self.topic = topic.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: DynamicCodingKey.self)
        try container.encode(id, forKey: .init("id"))
        if !topic.isEmpty {
            try container.encode(topic, forKey: .init("topic"))
        }
    }
}

public struct ProjectResearchPreferences: Codable, Equatable, Sendable {
    public let researchInterest: String
    public let researcherProfile: String
    public let targetVenue: String
    public let paperTitle: String

    public init(
        researchInterest: String = "",
        researcherProfile: String = "",
        targetVenue: String = "",
        paperTitle: String = ""
    ) {
        self.researchInterest = researchInterest.trimmingCharacters(in: .whitespacesAndNewlines)
        self.researcherProfile = researcherProfile.trimmingCharacters(in: .whitespacesAndNewlines)
        self.targetVenue = targetVenue.trimmingCharacters(in: .whitespacesAndNewlines)
        self.paperTitle = paperTitle.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private enum CodingKeys: String, CodingKey {
        case researchInterest = "research_interest"
        case researcherProfile = "researcher_profile"
        case targetVenue = "target_venue"
        case venue
        case paperTitle = "title"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.init(
            researchInterest: try container.decodeIfPresent(String.self, forKey: .researchInterest) ?? "",
            researcherProfile: try container.decodeIfPresent(String.self, forKey: .researcherProfile) ?? "",
            targetVenue: try container.decodeIfPresent(String.self, forKey: .targetVenue)
                ?? container.decodeIfPresent(String.self, forKey: .venue)
                ?? "",
            paperTitle: try container.decodeIfPresent(String.self, forKey: .paperTitle) ?? ""
        )
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        if !researchInterest.isEmpty {
            try container.encode(researchInterest, forKey: .researchInterest)
        }
        if !researcherProfile.isEmpty {
            try container.encode(researcherProfile, forKey: .researcherProfile)
        }
        if !targetVenue.isEmpty {
            try container.encode(targetVenue, forKey: .targetVenue)
            try container.encode(targetVenue, forKey: .venue)
        }
        if !paperTitle.isEmpty {
            try container.encode(paperTitle, forKey: .paperTitle)
        }
    }
}

public struct MobileClaudeResponse: Codable, Equatable, Identifiable, Sendable {
    public static let mobileMaxCharacters = 16_000

    public let status: String
    public let stage: String
    public let requestedStage: String
    public let returnCode: Int?
    public let source: String
    public let responseMarkdown: String
    public let responseCharacterCount: Int
    public let returnedCharacterCount: Int
    public let truncated: Bool
    public let truncatedHeadCharacters: Int
    public let fullResponseAvailable: Bool

    public init(
        status: String = "",
        stage: String = "",
        requestedStage: String = "",
        returnCode: Int? = nil,
        source: String = "",
        responseMarkdown: String = "",
        responseCharacterCount: Int = 0,
        returnedCharacterCount: Int = 0,
        truncated: Bool = false,
        truncatedHeadCharacters: Int = 0,
        fullResponseAvailable: Bool = false
    ) {
        self.status = status.trimmingCharacters(in: .whitespacesAndNewlines)
        self.stage = stage.trimmingCharacters(in: .whitespacesAndNewlines)
        self.requestedStage = requestedStage.trimmingCharacters(in: .whitespacesAndNewlines)
        self.returnCode = returnCode
        self.source = source.trimmingCharacters(in: .whitespacesAndNewlines)
        self.responseMarkdown = responseMarkdown
        self.responseCharacterCount = max(0, responseCharacterCount)
        self.returnedCharacterCount = max(0, returnedCharacterCount)
        self.truncated = truncated
        self.truncatedHeadCharacters = max(0, truncatedHeadCharacters)
        self.fullResponseAvailable = fullResponseAvailable
    }

    private enum CodingKeys: String, CodingKey {
        case status
        case stage
        case requestedStage = "requested_stage"
        case returnCode = "return_code"
        case source
        case responseMarkdown = "response_markdown"
        case responseCharacterCount = "response_chcount"
        case returnedCharacterCount = "returned_chcount"
        case truncated
        case truncatedHeadCharacters = "truncated_head_chars"
        case fullResponseAvailable = "full_response_available"
    }

    public static func clampedMaxCharacters(_ value: Int) -> Int {
        min(2_000_000, max(1_000, value))
    }

    public var displayStage: String {
        requestedStage.isEmpty ? stage : requestedStage
    }

    public var id: String {
        [
            displayStage,
            source,
            "\(returnedCharacterCount)",
            "\(truncatedHeadCharacters)",
        ].joined(separator: "|")
    }

    public var containsSecretMaterial: Bool {
        let lowered = responseMarkdown.lowercased()
        return lowered.contains("api_key")
            || lowered.contains("authorization:")
            || lowered.contains("bearer ")
            || lowered.contains("server_access_token")
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.init(
            status: try container.decodeIfPresent(String.self, forKey: .status) ?? "",
            stage: try container.decodeIfPresent(String.self, forKey: .stage) ?? "",
            requestedStage: try container.decodeIfPresent(String.self, forKey: .requestedStage) ?? "",
            returnCode: try container.decodeIfPresent(Int.self, forKey: .returnCode),
            source: try container.decodeIfPresent(String.self, forKey: .source) ?? "",
            responseMarkdown: try container.decodeIfPresent(String.self, forKey: .responseMarkdown) ?? "",
            responseCharacterCount: try container.decodeIfPresent(Int.self, forKey: .responseCharacterCount) ?? 0,
            returnedCharacterCount: try container.decodeIfPresent(Int.self, forKey: .returnedCharacterCount) ?? 0,
            truncated: try container.decodeIfPresent(Bool.self, forKey: .truncated) ?? false,
            truncatedHeadCharacters: try container.decodeIfPresent(Int.self, forKey: .truncatedHeadCharacters) ?? 0,
            fullResponseAvailable: try container.decodeIfPresent(Bool.self, forKey: .fullResponseAvailable) ?? false
        )
    }
}

public struct ProjectActionPayload: Encodable, Equatable, Sendable {
    public let project: String
    public let action: ProjectAction
    public let topic: String
    public let venue: String
    public let title: String
    public let options: [ProjectActionOption: Bool]

    public init(
        project: String,
        action: ProjectAction,
        topic: String = "",
        venue: String = "",
        title: String = "",
        options: [ProjectActionOption: Bool] = [:]
    ) {
        self.project = project
        self.action = action
        self.topic = topic
        self.venue = venue
        self.title = title
        self.options = options
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: DynamicCodingKey.self)
        try container.encode(project, forKey: .init("project"))
        try container.encode(action.rawValue, forKey: .init("action"))
        if !topic.isEmpty { try container.encode(topic, forKey: .init("topic")) }
        if !venue.isEmpty { try container.encode(venue, forKey: .init("venue")) }
        if !title.isEmpty { try container.encode(title, forKey: .init("title")) }
        for (key, value) in options {
            try container.encode(value, forKey: .init(key.rawValue))
        }
    }
}

private struct DynamicCodingKey: CodingKey {
    let stringValue: String
    let intValue: Int? = nil

    init(_ stringValue: String) {
        self.stringValue = stringValue
    }

    init?(stringValue: String) {
        self.stringValue = stringValue
    }

    init?(intValue: Int) {
        return nil
    }
}

private extension KeyedDecodingContainer {
    func decodeLossyStringIfPresent(forKey key: Key) throws -> String {
        if let value = try decodeIfPresent(String.self, forKey: key) {
            return value
        }
        if let value = try decodeIfPresent(Int.self, forKey: key) {
            return String(value)
        }
        if let value = try decodeIfPresent(Double.self, forKey: key) {
            return String(value)
        }
        if let value = try decodeIfPresent(Bool.self, forKey: key) {
            return value ? "true" : "false"
        }
        return ""
    }

    func decodeLossyStringListIfPresent(forKey key: Key) throws -> [String] {
        if let values = try? decodeIfPresent([String].self, forKey: key) {
            return values.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
        }
        let text = try decodeLossyStringIfPresent(forKey: key)
        return text
            .split(whereSeparator: { $0 == ":" || $0 == "," || $0 == "\n" })
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }
}

private extension JSONDecoder {
    static var taste: JSONDecoder {
        JSONDecoder()
    }
}
