import Foundation

public struct LLMConfiguration: Codable, Equatable, Sendable {
    public let provider: String
    public let baseURL: String
    public let model: String
    public let apiKey: String
    public let apiKeySaved: Bool
    public let apiKeySuffix: String
    public let projectLLMSynced: Bool

    public init(provider: String, baseURL: String, model: String, apiKey: String = "", apiKeySaved: Bool = false, apiKeySuffix: String = "", projectLLMSynced: Bool = false) {
        self.provider = provider.trimmingCharacters(in: .whitespacesAndNewlines)
        self.baseURL = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        self.model = model.trimmingCharacters(in: .whitespacesAndNewlines)
        self.apiKey = apiKey
        self.apiKeySaved = apiKeySaved
        self.apiKeySuffix = apiKeySuffix.trimmingCharacters(in: .whitespacesAndNewlines)
        self.projectLLMSynced = projectLLMSynced
    }

    private enum CodingKeys: String, CodingKey {
        case provider
        case baseURL = "base_url"
        case model
        case apiKey = "api_key"
        case apiKeySaved = "api_key_saved"
        case apiKeySuffix = "api_key_suffix"
        case projectLLMSynced = "project_llm_synced"
    }

    public static func decode(from data: Data) throws -> LLMConfiguration {
        try JSONDecoder().decode(LLMConfiguration.self, from: data)
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.init(
            provider: try container.decodeIfPresent(String.self, forKey: .provider) ?? "",
            baseURL: try container.decodeIfPresent(String.self, forKey: .baseURL) ?? "",
            model: try container.decodeIfPresent(String.self, forKey: .model) ?? "",
            apiKey: "",
            apiKeySaved: try container.decodeIfPresent(Bool.self, forKey: .apiKeySaved) ?? false,
            apiKeySuffix: try container.decodeIfPresent(String.self, forKey: .apiKeySuffix) ?? "",
            projectLLMSynced: try container.decodeIfPresent(Bool.self, forKey: .projectLLMSynced) ?? false
        )
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(provider, forKey: .provider)
        try container.encode(baseURL, forKey: .baseURL)
        try container.encode(model, forKey: .model)
        if !apiKey.isEmpty {
            try container.encode(apiKey, forKey: .apiKey)
        }
    }
}

public final class TASTEAPIClient: @unchecked Sendable {
    private let settings: ConnectionSettings
    private let endpoints: TASTEEndpointBuilder
    private let session: URLSession

    public convenience init(settings: ConnectionSettings) {
        self.init(settings: settings, session: Self.makeMobileSession())
    }

    public init(settings: ConnectionSettings, session: URLSession) {
        self.settings = settings
        self.endpoints = TASTEEndpointBuilder(settings: settings)
        self.session = session
    }

    public static func makeMobileSession(policy: MobileNetworkPolicy = .default) -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.requestCachePolicy = policy.requestCachePolicy
        configuration.timeoutIntervalForRequest = policy.timeoutForRequest
        configuration.timeoutIntervalForResource = policy.timeoutForResource
        configuration.httpShouldSetCookies = false
        configuration.httpCookieAcceptPolicy = .never
        configuration.urlCache = URLCache(
            memoryCapacity: policy.memoryCacheBytes,
            diskCapacity: policy.diskCacheBytes,
            diskPath: nil
        )
        return URLSession(configuration: configuration)
    }

    public func fetchProjects() async throws -> [TASTEProject] {
        let (data, response) = try await session.data(for: makeRequest(url: endpoints.projects()))
        try Self.validate(response: response, data: data)
        return try TASTEProject.decodeList(from: data)
    }

    public func fetchProjectSummary(project: String) async throws -> TASTEProjectSummary {
        let (data, response) = try await session.data(for: makeRequest(url: endpoints.projectSummary(projectID: project)))
        try Self.validate(response: response, data: data)
        return try TASTEProjectSummary.decode(from: data)
    }

    public func fetchClaudeLatestResponse(project: String, stage: WorkflowStage? = nil, maxChars: Int = MobileClaudeResponse.mobileMaxCharacters) async throws -> MobileClaudeResponse {
        let url = endpoints.projectClaudeLatestResponse(projectID: project, stage: stage, maxChars: maxChars)
        let (data, response) = try await session.data(for: makeRequest(url: url))
        try Self.validate(response: response, data: data)
        return try JSONDecoder().decode(MobileClaudeResponse.self, from: data)
    }

    @discardableResult
    public func updateProjectResearchPreferences(project: String, _ preferences: ProjectResearchPreferences) async throws -> TASTEProjectSummary {
        var request = makeRequest(url: endpoints.projectConfig(projectID: project), method: "POST", contentType: "application/json")
        request.httpBody = try JSONEncoder().encode(preferences)
        let (data, response) = try await session.data(for: request)
        try Self.validate(response: response, data: data)
        return try TASTEProjectSummary.decode(from: data)
    }

    public func fetchServerMeta() async throws -> TASTEServerMeta {
        let (data, response) = try await session.data(for: makeRequest(url: endpoints.configMeta()))
        try Self.validate(response: response, data: data)
        return try JSONDecoder().decode(TASTEServerMeta.self, from: data)
    }

    public func fetchLLMConfiguration() async throws -> LLMConfiguration {
        let (data, response) = try await session.data(for: makeRequest(url: endpoints.config()))
        try Self.validate(response: response, data: data)
        return try LLMConfiguration.decode(from: data)
    }

    @discardableResult
    public func createProject(_ payload: CreateProjectPayload) async throws -> TASTEProject {
        var request = makeRequest(url: endpoints.projects(), method: "POST", contentType: "application/json")
        request.httpBody = try JSONEncoder().encode(payload)
        let (data, response) = try await session.data(for: request)
        try Self.validate(response: response, data: data)
        return try TASTEProject.decodeOne(from: data)
    }

    public func fetchJobs(project: String?) async throws -> [TASTEJob] {
        let (data, response) = try await session.data(for: makeRequest(url: endpoints.jobs(project: project)))
        try Self.validate(response: response, data: data)
        return try TASTEJob.decodeList(from: data)
    }

    public func fetchJob(id: String, compact: Bool = false) async throws -> TASTEJob {
        let (data, response) = try await session.data(for: makeRequest(url: endpoints.job(jobID: id, compact: compact)))
        try Self.validate(response: response, data: data)
        return try JSONDecoder().decode(TASTEJob.self, from: data)
    }

    public func fetchRuntimeConfiguration(project: String) async throws -> ProjectRuntimeConfiguration {
        try await fetchRuntimeStatus(project: project).runtime
    }

    public func fetchRuntimeStatus(project: String) async throws -> ProjectRuntimeStatus {
        let (data, response) = try await session.data(for: makeRequest(url: endpoints.projectRuntime(projectID: project)))
        try Self.validate(response: response, data: data)
        return try ProjectRuntimeStatus.decode(from: data)
    }

    public func fetchRemoteArtifactPreview(_ artifact: RemoteArtifact, maxBytes: Int = MobileStoragePolicy.default.maxCachedBytes) async throws -> RemoteArtifactPreview {
        guard let url = artifact.remoteURL(relativeTo: settings.serverURL) else {
            throw TASTEAPIClientError.invalidRemoteArtifactURL
        }
        let (data, response) = try await session.data(for: makeRequest(url: url))
        try Self.validate(response: response, data: data)
        if data.count > maxBytes {
            throw TASTEAPIClientError.responseTooLarge(data.count, maxBytes)
        }
        let contentType = (response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Type") ?? ""
        return RemoteArtifactPreview(
            artifact: artifact,
            data: data,
            fileName: Self.previewFileName(for: artifact, url: url),
            contentType: contentType
        )
    }

    @discardableResult
    public func startProjectAction(_ payload: ProjectActionPayload) async throws -> TASTEJob {
        var request = makeRequest(url: endpoints.projectAction(), method: "POST", contentType: "application/json")
        request.httpBody = try JSONEncoder().encode(payload)
        let (data, response) = try await session.data(for: request)
        try Self.validate(response: response, data: data)
        return try JSONDecoder().decode(TASTEJob.self, from: data)
    }

    @discardableResult
    public func cancelJob(id: String) async throws -> TASTEJob {
        let request = makeRequest(url: endpoints.cancelJob(jobID: id), method: "POST")
        let (data, response) = try await session.data(for: request)
        try Self.validate(response: response, data: data)
        return try JSONDecoder().decode(TASTEJob.self, from: data)
    }

    @discardableResult
    public func updateRuntimeConfiguration(project: String, _ config: ProjectRuntimeConfiguration) async throws -> ProjectRuntimeConfiguration {
        try await updateRuntimeStatus(project: project, config).runtime
    }

    @discardableResult
    public func updateRuntimeStatus(project: String, _ config: ProjectRuntimeConfiguration) async throws -> ProjectRuntimeStatus {
        var request = makeRequest(url: endpoints.projectRuntime(projectID: project), method: "POST", contentType: "application/json")
        request.httpBody = try JSONEncoder().encode(config)
        let (data, response) = try await session.data(for: request)
        try Self.validate(response: response, data: data)
        return try ProjectRuntimeStatus.decode(from: data)
    }

    @discardableResult
    public func detectRuntimeConfiguration(project: String) async throws -> ProjectRuntimeConfiguration {
        try await detectRuntimeStatus(project: project).runtime
    }

    @discardableResult
    public func detectRuntimeStatus(project: String) async throws -> ProjectRuntimeStatus {
        let request = makeRequest(url: endpoints.projectRuntimeDetect(projectID: project), method: "POST")
        let (data, response) = try await session.data(for: request)
        try Self.validate(response: response, data: data)
        return try ProjectRuntimeStatus.decode(from: data)
    }

    @discardableResult
    public func updateLLMConfiguration(_ config: LLMConfiguration) async throws -> LLMConfiguration {
        let configURL = endpoints.config()
        let getRequest = makeRequest(url: configURL)
        let (data, getResponse) = try await session.data(for: getRequest)
        try Self.validate(response: getResponse, data: data)

        var object = (try JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        object["provider"] = config.provider
        object["base_url"] = config.baseURL
        object["model"] = config.model
        if !config.apiKey.isEmpty {
            object["api_key"] = config.apiKey
        }

        var postRequest = makeRequest(url: configURL, method: "POST", contentType: "application/json")
        postRequest.httpBody = try JSONSerialization.data(withJSONObject: object)
        let (postData, postResponse) = try await session.data(for: postRequest)
        try Self.validate(response: postResponse, data: postData)
        return try LLMConfiguration.decode(from: postData)
    }

    public func probeLLMConfiguration() async throws -> LLMProbeResult {
        let request = makeRequest(url: endpoints.llmProbe(), method: "POST")
        let (data, response) = try await session.data(for: request)
        try Self.validate(response: response, data: data)
        return try JSONDecoder().decode(LLMProbeResult.self, from: data)
    }

    private func makeRequest(url: URL, method: String = "GET", contentType: String? = nil) -> URLRequest {
        var request = URLRequest(url: url)
        request.httpMethod = method
        if let contentType {
            request.setValue(contentType, forHTTPHeaderField: "Content-Type")
        }
        settings.applyAuthentication(to: &request)
        return request
    }

    private static func validate(response: URLResponse, data: Data) throws {
        guard let http = response as? HTTPURLResponse else { return }
        guard (200..<300).contains(http.statusCode) else {
            let body = String(data: data, encoding: .utf8) ?? ""
            throw TASTEAPIClientError.httpStatus(http.statusCode, body)
        }
    }

    private static func previewFileName(for artifact: RemoteArtifact, url: URL) -> String {
        let lastPath = url.lastPathComponent.trimmingCharacters(in: .whitespacesAndNewlines)
        if !lastPath.isEmpty {
            return lastPath
        }
        let fallback = artifact.title
            .lowercased()
            .replacingOccurrences(of: "[^a-z0-9._-]+", with: "-", options: .regularExpression)
            .trimmingCharacters(in: CharacterSet(charactersIn: "-."))
        return fallback.isEmpty ? "taste-artifact" : fallback
    }
}

public enum TASTEAPIClientError: Error, Equatable {
    case httpStatus(Int, String)
    case invalidRemoteArtifactURL
    case responseTooLarge(Int, Int)
}

public struct RemoteArtifactPreview: Equatable, Sendable {
    public let artifact: RemoteArtifact
    public let data: Data
    public let fileName: String
    public let contentType: String

    public init(artifact: RemoteArtifact, data: Data, fileName: String, contentType: String) {
        self.artifact = artifact
        self.data = data
        self.fileName = fileName
        self.contentType = contentType
    }
}

public enum TASTEErrorMessage {
    public static func userFacing(_ error: Error) -> String {
        if let urlError = error as? URLError {
            switch urlError.code {
            case .cannotConnectToHost, .cannotFindHost, .networkConnectionLost, .notConnectedToInternet, .timedOut:
                return "Cannot reach the TASTE server. Check the server URL, network, and whether scripts/start_web.sh is running."
            default:
                return urlError.localizedDescription
            }
        }

        if let clientError = error as? TASTEAPIClientError {
            switch clientError {
            case let .httpStatus(status, body):
                if status == 401 {
                    return "TASTE server rejected the server access token. Open Settings, update the Server access token, then tap Test Connection."
                }
                let trimmed = body.trimmingCharacters(in: .whitespacesAndNewlines)
                if let summary = summarizeJSONErrorBody(trimmed) {
                    return summary
                }
                return trimmed.isEmpty ? "TASTE server returned HTTP \(status)." : "TASTE server returned HTTP \(status): \(trimmed)"
            case .invalidRemoteArtifactURL:
                return "This remote artifact link is not a valid TASTE server URL."
            case let .responseTooLarge(byteCount, limit):
                return "This artifact is too large to preview on the phone (\(byteCount) bytes, limit \(limit) bytes). Open it on the TASTE server instead."
            }
        }

        if let connectionError = error as? ConnectionSettingsError {
            switch connectionError {
            case let .invalidURL(value):
                return "Invalid server URL: \(value)"
            case let .unsupportedScheme(scheme):
                return "Unsupported URL scheme: \(scheme). Use http or https."
            }
        }

        if let linkError = error as? MobileConnectionLinkError {
            switch linkError {
            case .unsupportedURL:
                return "This is not a TASTE connection link."
            case .missingServerURL:
                return "The TASTE connection link is missing a server URL."
            case let .invalidConnectionKind(kind):
                return "Unsupported TASTE connection target: \(kind)."
            }
        }

        return error.localizedDescription
    }

    private static func summarizeJSONErrorBody(_ body: String) -> String? {
        guard let data = body.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }

        let primary = firstNonEmptyString(in: object, keys: ["reason", "message", "detail", "error", "status"])
        let nextAction = firstNonEmptyString(in: object, keys: ["next_action", "public_next_action", "suggested_action"])
        let text: String
        if primary.isEmpty {
            text = nextAction.isEmpty ? "" : "Next action: \(nextAction)"
        } else if nextAction.isEmpty {
            text = primary
        } else {
            text = "\(primary) Next action: \(nextAction)"
        }
        return text.isEmpty ? nil : text
    }

    private static func firstNonEmptyString(in object: [String: Any], keys: [String]) -> String {
        for key in keys {
            if let value = object[key] as? String {
                let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
                if !text.isEmpty { return text }
            }
        }
        return ""
    }
}
