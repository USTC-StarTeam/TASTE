import Foundation
import Security
import SwiftUI
import UIKit

@MainActor
final class AppViewModel: ObservableObject {
    @Published var serverURLText: String
    @Published var connectionProfiles: [ServerConnectionProfile]
    @Published var selectedConnectionProfileID: String
    @Published var connectionProfileName: String
    @Published var connectionProfileKind: ServerConnectionKind
    @Published var isEditingNewConnectionProfile = false
    @Published var serverAccessToken: String
    @Published var connectionLinkText: String = ""
    @Published var llmProvider: String
    @Published var llmBaseURLText: String
    @Published var llmModel: String
    @Published var apiKey: String = ""
    @Published var llmAPIKeySaved = false
    @Published var llmAPIKeySuffix = ""
    @Published var projectLLMSynced = false
    @Published var topic: String
    @Published var researchInterest: String
    @Published var researcherProfile: String
    @Published var venue: String
    @Published var paperTitle: String
    @Published var newProjectID: String = ""
    @Published var condaEnv: String
    @Published var condaBase: String
    @Published var nodeBin: String
    @Published var claudePath: String
    @Published var managementPython: String
    @Published var experimentPython: String
    @Published var extraPathText: String
    @Published var projects: [TASTEProject] = []
    @Published var jobs: [TASTEJob] = []
    @Published var stageSnapshots: [WorkflowStage: StageSnapshot] = [:]
    @Published var remoteArtifacts: [RemoteArtifact] = []
    @Published var attentionItems: [ProjectAttentionItem] = []
    @Published var runtimeStatus: ProjectRuntimeStatus?
    @Published var inspectedJobDetail: MobileJobDetailSummary?
    @Published var isInspectingJob = false
    @Published var latestClaudeResponse: MobileClaudeResponse?
    @Published var isLoadingClaudeResponse = false
    @Published var artifactPreviewURL: URL?
    @Published var isOpeningArtifact = false
    @Published var selectedProjectID: String = ""
    @Published var serverReachable: Bool? = nil
    @Published var serverMeta: TASTEServerMeta?
    @Published var isLoading = false
    @Published var errorMessage = ""
    @Published var statusMessage = "Ready"

    private let defaults = UserDefaults.standard
    private static let serverURLKey = "taste.serverURL"
    private static let connectionProfilesKey = "taste.connectionProfiles"
    private static let selectedConnectionProfileKey = "taste.selectedConnectionProfile"
    private static let selectedProjectKey = "taste.selectedProject"
    private var actionLaunchGate = MobileActionLaunchGate()

    init() {
        let fallbackServerURL = defaults.string(forKey: Self.serverURLKey) ?? "http://127.0.0.1:8765"
        let catalog = Self.loadConnectionProfileCatalog(defaults: defaults, fallbackURL: fallbackServerURL)
        let selectedProfile = catalog.selectedProfile
        let initialProfileID = selectedProfile?.id ?? catalog.selectedProfileID
        serverURLText = selectedProfile?.serverURLText ?? fallbackServerURL
        connectionProfiles = catalog.profiles
        selectedConnectionProfileID = initialProfileID
        connectionProfileName = selectedProfile?.name ?? "Local TASTE"
        connectionProfileKind = selectedProfile?.kind ?? .computer
        serverAccessToken = ServerAccessTokenStore.token(for: initialProfileID)
        llmProvider = defaults.string(forKey: "taste.llmProvider") ?? "openai_compatible"
        llmBaseURLText = defaults.string(forKey: "taste.llmBaseURL") ?? ""
        llmModel = defaults.string(forKey: "taste.llmModel") ?? ""
        topic = defaults.string(forKey: "taste.topic") ?? ""
        researchInterest = defaults.string(forKey: "taste.researchInterest") ?? ""
        researcherProfile = defaults.string(forKey: "taste.researcherProfile") ?? ""
        venue = defaults.string(forKey: "taste.venue") ?? ""
        paperTitle = defaults.string(forKey: "taste.paperTitle") ?? ""
        condaEnv = defaults.string(forKey: "taste.condaEnv") ?? ""
        condaBase = defaults.string(forKey: "taste.condaBase") ?? ""
        nodeBin = defaults.string(forKey: "taste.nodeBin") ?? ""
        claudePath = defaults.string(forKey: "taste.claudePath") ?? ""
        managementPython = defaults.string(forKey: "taste.managementPython") ?? ""
        experimentPython = defaults.string(forKey: "taste.experimentPython") ?? ""
        extraPathText = defaults.string(forKey: "taste.extraPathText") ?? ""
        selectedProjectID = defaults.string(forKey: Self.selectedProjectKey) ?? ""
    }

    var selectedProject: TASTEProject? {
        projects.first { $0.id == selectedProjectID } ?? projects.first
    }

    var progressSummary: WorkflowProgressSummary {
        WorkflowProgressSummary(
            selectedProject: selectedProject?.id ?? selectedProjectID,
            stageSnapshots: stageSnapshots,
            jobs: jobs
        )
    }

    var readinessSummary: MobileReadinessSummary {
        runContext.readinessSummary
    }

    var runContext: MobileRunContext {
        MobileRunContext(
            serverURLText: serverURLText,
            connectionKind: connectionProfileKind,
            serverReachable: serverReachable,
            serverMeta: serverMeta,
            selectedProjectID: selectedProject?.id ?? selectedProjectID,
            llmProvider: llmProvider,
            llmBaseURLText: llmBaseURLText,
            llmModel: llmModel,
            runtimeConfiguration: .init(
                condaEnv: condaEnv,
                condaBase: condaBase,
                nodeBin: nodeBin,
                claudePath: claudePath,
                managementPython: managementPython,
                experimentPython: experimentPython,
                extraPathText: extraPathText
            ),
            runtimeStatus: runtimeStatus,
            stageSnapshots: stageSnapshots
        )
    }

    func availability(for action: ProjectAction) -> ProjectActionAvailability {
        ProjectActionAvailability.evaluate(action, context: runContext)
    }

    func remoteURL(for artifact: RemoteArtifact) -> URL? {
        guard let settings = try? ConnectionSettings(serverURLText: serverURLText) else { return nil }
        return artifact.remoteURL(relativeTo: settings.serverURL)
    }

    func remoteOpenPlan(for artifact: RemoteArtifact) -> RemoteArtifactOpenPlan? {
        guard let settings = try? ConnectionSettings(
            serverURLText: serverURLText,
            serverAccessToken: serverAccessToken
        ) else { return nil }
        let plan = artifact.openPlan(relativeTo: settings)
        return plan.url == nil ? nil : plan
    }

    func saveLocalSettings() {
        defaults.set(serverURLText, forKey: Self.serverURLKey)
        defaults.set(selectedConnectionProfileID, forKey: Self.selectedConnectionProfileKey)
        saveConnectionProfileCatalog(currentConnectionProfileCatalog())
        defaults.set(llmProvider, forKey: "taste.llmProvider")
        defaults.set(llmBaseURLText, forKey: "taste.llmBaseURL")
        defaults.set(llmModel, forKey: "taste.llmModel")
        defaults.set(topic, forKey: "taste.topic")
        defaults.set(researchInterest, forKey: "taste.researchInterest")
        defaults.set(researcherProfile, forKey: "taste.researcherProfile")
        defaults.set(venue, forKey: "taste.venue")
        defaults.set(paperTitle, forKey: "taste.paperTitle")
        defaults.set(condaEnv, forKey: "taste.condaEnv")
        defaults.set(condaBase, forKey: "taste.condaBase")
        defaults.set(nodeBin, forKey: "taste.nodeBin")
        defaults.set(claudePath, forKey: "taste.claudePath")
        defaults.set(managementPython, forKey: "taste.managementPython")
        defaults.set(experimentPython, forKey: "taste.experimentPython")
        defaults.set(extraPathText, forKey: "taste.extraPathText")
        defaults.set(selectedProjectID, forKey: Self.selectedProjectKey)
    }

    func selectConnectionProfile(_ profileID: String) {
        var catalog = currentConnectionProfileCatalog()
        catalog.select(profileID)
        guard let profile = catalog.selectedProfile else { return }
        let previousServerURL = serverURLText
        isEditingNewConnectionProfile = false
        applyConnectionProfile(profile, from: catalog)
        if profile.serverURLText != previousServerURL {
            clearRemoteStateAfterConnectionChange()
        }
        saveLocalSettings()
        statusMessage = "Using \(profile.name)"
    }

    func startNewConnectionProfile() {
        isEditingNewConnectionProfile = true
        connectionProfileName = ""
        connectionProfileKind = .server
        serverAccessToken = ""
        serverURLText = suggestedNewServerURLText()
        serverReachable = nil
        statusMessage = "Editing a new connection profile"
    }

    func saveCurrentConnectionProfile() {
        do {
            let previousSelectedURL = currentConnectionProfileCatalog().selectedProfile?.serverURLText
            let tokenToSave = serverAccessToken
            var catalog = currentConnectionProfileCatalog()
            let profile = try catalog.upsertProfile(
                id: isEditingNewConnectionProfile ? "" : selectedConnectionProfileID,
                name: connectionProfileName,
                serverURLText: serverURLText,
                kind: connectionProfileKind
            )
            ServerAccessTokenStore.save(tokenToSave, for: profile.id)
            applyConnectionProfile(profile, from: catalog)
            if isEditingNewConnectionProfile || previousSelectedURL != profile.serverURLText {
                clearRemoteStateAfterConnectionChange()
            }
            isEditingNewConnectionProfile = false
            errorMessage = ""
            saveLocalSettings()
            statusMessage = "Saved connection profile \(profile.name)"
        } catch {
            updateReachability(after: error)
            errorMessage = TASTEErrorMessage.userFacing(error)
            statusMessage = "Needs attention"
        }
    }

    func handleConnectionDeepLink(_ url: URL) async {
        do {
            let link = try MobileConnectionLink(url: url)
            importConnectionLink(link)
            await testConnection()
        } catch {
            updateReachability(after: error)
            errorMessage = TASTEErrorMessage.userFacing(error)
            statusMessage = "Connection link needs attention"
        }
    }

    func importPastedConnectionLink() async {
        let text = connectionLinkText.trimmingCharacters(in: .whitespacesAndNewlines)
        await importConnectionLinkText(text, clearPastedTextOnSuccess: true)
    }

    func importClipboardConnectionLink() async {
        let text = UIPasteboard.general.string?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        await importConnectionLinkText(text, clearPastedTextOnSuccess: false)
    }

    func importScannedConnectionCode(_ text: String) async {
        await importConnectionLinkText(text, clearPastedTextOnSuccess: false)
    }

    private func importConnectionLinkText(_ text: String, clearPastedTextOnSuccess: Bool) async {
        do {
            guard !text.isEmpty else { throw MobileConnectionLinkError.missingServerURL }
            guard let url = URL(string: text) else { throw MobileConnectionLinkError.unsupportedURL }
            let link = try MobileConnectionLink(url: url)
            importConnectionLink(link)
            if clearPastedTextOnSuccess {
                connectionLinkText = ""
            }
            await testConnection()
        } catch {
            updateReachability(after: error)
            errorMessage = TASTEErrorMessage.userFacing(error)
            statusMessage = "Connection link needs attention"
        }
    }

    func importConnectionLink(_ link: MobileConnectionLink) {
        let previousServerURL = serverURLText
        let previousProjectID = selectedProjectID
        var catalog = currentConnectionProfileCatalog()
        catalog.upsert(link.profile)
        ServerAccessTokenStore.save(link.serverAccessToken, for: link.profile.id)
        applyConnectionProfile(link.profile, from: catalog)
        serverAccessToken = link.serverAccessToken
        if link.profile.serverURLText != previousServerURL || (!link.selectedProjectID.isEmpty && link.selectedProjectID != previousProjectID) {
            clearRemoteStateAfterConnectionChange()
        }
        if !link.selectedProjectID.isEmpty {
            selectedProjectID = link.selectedProjectID
        }
        isEditingNewConnectionProfile = false
        errorMessage = ""
        saveLocalSettings()
        statusMessage = "Imported connection \(link.profile.name)"
    }

    func applyRuntimeConfiguration(_ runtime: ProjectRuntimeConfiguration) {
        if !runtime.condaEnv.isEmpty { condaEnv = runtime.condaEnv }
        if !runtime.condaBase.isEmpty { condaBase = runtime.condaBase }
        if !runtime.nodeBin.isEmpty { nodeBin = runtime.nodeBin }
        if !runtime.claudePath.isEmpty { claudePath = runtime.claudePath }
        if !runtime.managementPython.isEmpty { managementPython = runtime.managementPython }
        if !runtime.experimentPython.isEmpty { experimentPython = runtime.experimentPython }
        if !runtime.extraPathText.isEmpty { extraPathText = runtime.extraPathText }
    }

    func applyRuntimeStatus(_ status: ProjectRuntimeStatus?) {
        runtimeStatus = status
        if let status {
            applyRuntimeConfiguration(status.runtime)
        }
    }

    func applyLLMConfiguration(_ config: LLMConfiguration) {
        if !config.provider.isEmpty { llmProvider = config.provider }
        if !config.baseURL.isEmpty { llmBaseURLText = config.baseURL }
        if !config.model.isEmpty { llmModel = config.model }
        llmAPIKeySaved = config.apiKeySaved
        llmAPIKeySuffix = config.apiKeySuffix
        projectLLMSynced = config.projectLLMSynced
        apiKey = ""
    }

    func applyProjectResearchPreferences(_ preferences: ProjectResearchPreferences, overwrite: Bool = false) {
        if overwrite || researchInterest.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            if !preferences.researchInterest.isEmpty { researchInterest = preferences.researchInterest }
        }
        if overwrite || researcherProfile.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            if !preferences.researcherProfile.isEmpty { researcherProfile = preferences.researcherProfile }
        }
        if overwrite || venue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            if !preferences.targetVenue.isEmpty { venue = preferences.targetVenue }
        }
        if overwrite || paperTitle.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            if !preferences.paperTitle.isEmpty { paperTitle = preferences.paperTitle }
        }
    }

    func applyProjectSummary(_ summary: TASTEProjectSummary?, overwriteResearchPreferences: Bool = false) {
        stageSnapshots = summary?.stageSnapshots ?? [:]
        remoteArtifacts = summary?.remoteArtifacts ?? []
        attentionItems = summary?.attentionItems ?? []
        applyRuntimeStatus(summary?.runtimeStatus)
        if let summary {
            applyProjectResearchPreferences(summary.runPreferences, overwrite: overwriteResearchPreferences)
        }
    }

    func currentProjectResearchPreferences() -> ProjectResearchPreferences {
        ProjectResearchPreferences(
            researchInterest: researchInterest,
            researcherProfile: researcherProfile,
            targetVenue: venue,
            paperTitle: paperTitle
        )
    }

    func runAutoRefreshLoop() async {
        let refreshPolicy = MobileAutoRefreshPolicy.default
        while !Task.isCancelled {
            await refresh()
            try? await Task.sleep(for: .seconds(refreshPolicy.nextIntervalSeconds(for: jobs)))
        }
    }

    func testConnection() async {
        await runTask("Testing TASTE server connection") {
            let meta = try await makeClient().fetchServerMeta()
            serverMeta = meta
            if !meta.supportsMobileControlPlane {
                statusMessage = "Connected; update TASTE server for mobile control APIs"
            } else {
                statusMessage = meta.saved ? "Connected; server has saved config" : "Connected; server config not saved yet"
            }
        }
    }

    func refresh() async {
        await runTask("Refreshing TASTE status") {
            let client = try makeClient()
            let fetchedProjects = try await client.fetchProjects()
            let projectID = (selectedProjectID.isEmpty || !fetchedProjects.contains(where: { $0.id == selectedProjectID }))
                ? fetchedProjects.first?.id ?? ""
                : selectedProjectID
            let fetchedJobs = try await client.fetchJobs(project: projectID.isEmpty ? nil : projectID)
            let fetchedSummary = projectID.isEmpty ? nil : try await client.fetchProjectSummary(project: projectID)
            projects = fetchedProjects
            selectedProjectID = projectID
            jobs = fetchedJobs
            applyProjectSummary(fetchedSummary)
            saveLocalSettings()
            statusMessage = "Updated \(fetchedProjects.count) projects and \(fetchedJobs.count) jobs"
        }
    }

    func createProject() async {
        await runTask("Creating TASTE project") {
            let projectID = newProjectID.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !projectID.isEmpty else { throw AppError.missingProjectID }
            let client = try makeClient()
            let project = try await client.createProject(.init(id: projectID, topic: topic))
            projects.removeAll { $0.id == project.id }
            projects.insert(project, at: 0)
            selectedProjectID = project.id
            newProjectID = ""
            _ = try await client.updateProjectResearchPreferences(project: project.id, currentProjectResearchPreferences())
            jobs = try await client.fetchJobs(project: project.id)
            let summary = try? await client.fetchProjectSummary(project: project.id)
            applyProjectSummary(summary, overwriteResearchPreferences: true)
            saveLocalSettings()
            statusMessage = "Created project \(project.id)"
        }
    }

    func cancel(_ job: TASTEJob) async {
        await runTask("Cancelling \(job.stage.displayName)") {
            let updated = try await makeClient().cancelJob(id: job.id)
            jobs.removeAll { $0.id == updated.id }
            jobs.insert(updated, at: 0)
            statusMessage = "Cancel requested for \(job.stage.displayName)"
        }
    }

    func inspect(_ job: TASTEJob) async {
        inspectedJobDetail = MobileJobDetailSummary(job: job)
        isInspectingJob = true
        errorMessage = ""
        statusMessage = "Loading \(job.stage.displayName) details"
        do {
            let detailedJob = try await makeClient().fetchJob(id: job.id, compact: true)
            inspectedJobDetail = MobileJobDetailSummary(job: detailedJob)
            serverReachable = true
            statusMessage = "Loaded \(detailedJob.stage.displayName) details"
        } catch {
            updateReachability(after: error)
            errorMessage = TASTEErrorMessage.userFacing(error)
            statusMessage = "Showing cached job summary"
        }
        isInspectingJob = false
    }

    func loadClaudeLatestResponse(stage: WorkflowStage? = nil) async {
        guard !isLoadingClaudeResponse else { return }
        isLoadingClaudeResponse = true
        errorMessage = ""
        let stageLabel = stage?.displayName ?? "latest"
        statusMessage = "Loading Claude \(stageLabel) response"
        defer { isLoadingClaudeResponse = false }

        do {
            let projectID = selectedProject?.id ?? selectedProjectID
            guard !projectID.isEmpty else { throw AppError.missingProject }
            let response = try await makeClient().fetchClaudeLatestResponse(project: projectID, stage: stage)
            latestClaudeResponse = response
            serverReachable = true
            statusMessage = response.responseMarkdown.isEmpty ? "No Claude response returned" : "Loaded Claude \(stageLabel) response"
        } catch {
            updateReachability(after: error)
            errorMessage = TASTEErrorMessage.userFacing(error)
            statusMessage = "Needs attention"
        }
    }

    func openRemoteArtifact(_ artifact: RemoteArtifact) async {
        guard !isOpeningArtifact else { return }
        isOpeningArtifact = true
        errorMessage = ""
        statusMessage = "Opening \(artifact.title)"
        defer { isOpeningArtifact = false }

        do {
            guard let plan = remoteOpenPlan(for: artifact), plan.mode == .authenticatedPreview else {
                throw AppError.invalidRemoteArtifact
            }
            let preview = try await makeClient().fetchRemoteArtifactPreview(
                plan.artifact,
                maxBytes: MobileStoragePolicy.default.maxCachedBytes
            )
            artifactPreviewURL = try Self.writeTemporaryPreview(preview)
            serverReachable = true
            statusMessage = "Opened \(preview.fileName)"
        } catch {
            updateReachability(after: error)
            errorMessage = TASTEErrorMessage.userFacing(error)
            statusMessage = "Needs attention"
        }
    }

    func start(_ action: ProjectAction, options: [ProjectActionOption: Bool] = [:]) async {
        await runTask("Starting \(action.rawValue)") {
            let availability = availability(for: action)
            guard availability.isEnabled else { throw AppError.actionUnavailable(availability.reason) }
            let projectID = selectedProject?.id ?? selectedProjectID
            guard !projectID.isEmpty else { throw AppError.missingProject }
            guard actionLaunchGate.begin(projectID: projectID, action: action, options: options) else {
                throw AppError.duplicateActionLaunch(action.mobileRunTitle)
            }
            defer { actionLaunchGate.finish(projectID: projectID, action: action, options: options) }
            let client = try makeClient()
            if action.syncsProjectResearchPreferencesBeforeRun {
                let summary = try await client.updateProjectResearchPreferences(project: projectID, currentProjectResearchPreferences())
                applyProjectSummary(summary, overwriteResearchPreferences: true)
            }
            let payload = ProjectActionPayload(
                project: projectID,
                action: action,
                topic: topic,
                venue: venue,
                title: paperTitle,
                options: options
            )
            let job = try await client.startProjectAction(payload)
            jobs.insert(job, at: 0)
            statusMessage = "Started \(action.rawValue)"
        }
    }

    func saveRuntimeConfiguration() async {
        await runTask("Saving remote runtime configuration") {
            let projectID = selectedProject?.id ?? selectedProjectID
            guard !projectID.isEmpty else { throw AppError.missingProject }
            let status = try await makeClient().updateRuntimeStatus(
                project: projectID,
                .init(
                    condaEnv: condaEnv,
                    condaBase: condaBase,
                    nodeBin: nodeBin,
                    claudePath: claudePath,
                    managementPython: managementPython,
                    experimentPython: experimentPython,
                    extraPathText: extraPathText
                )
            )
            applyRuntimeStatus(status)
            saveLocalSettings()
            statusMessage = status.summaryLine
        }
    }

    func loadRuntimeConfiguration() async {
        await runTask("Loading remote runtime configuration") {
            let projectID = selectedProject?.id ?? selectedProjectID
            guard !projectID.isEmpty else { throw AppError.missingProject }
            let status = try await makeClient().fetchRuntimeStatus(project: projectID)
            applyRuntimeStatus(status)
            saveLocalSettings()
            statusMessage = status.summaryLine
        }
    }

    func detectRuntimeConfiguration() async {
        await runTask("Detecting remote runtime") {
            let projectID = selectedProject?.id ?? selectedProjectID
            guard !projectID.isEmpty else { throw AppError.missingProject }
            let status = try await makeClient().detectRuntimeStatus(project: projectID)
            applyRuntimeStatus(status)
            saveLocalSettings()
            statusMessage = status.summaryLine
        }
    }

    func saveProjectResearchPreferences() async {
        await runTask("Saving project research profile") {
            let projectID = selectedProject?.id ?? selectedProjectID
            guard !projectID.isEmpty else { throw AppError.missingProject }
            let summary = try await makeClient().updateProjectResearchPreferences(project: projectID, currentProjectResearchPreferences())
            applyProjectSummary(summary, overwriteResearchPreferences: true)
            saveLocalSettings()
            statusMessage = "Project research profile synced to TASTE"
        }
    }

    func syncLLMConfiguration() async {
        await runTask("Syncing Find LLM configuration") {
            let client = try makeClient()
            let config = try await client.updateLLMConfiguration(.init(
                provider: llmProvider,
                baseURL: llmBaseURLText,
                model: llmModel,
                apiKey: apiKey
            ))
            applyLLMConfiguration(config)
            saveLocalSettings()
            statusMessage = "Find LLM config synced; key cleared from the form"
        }
    }

    func loadLLMConfiguration() async {
        await runTask("Loading Find LLM configuration") {
            let config = try await makeClient().fetchLLMConfiguration()
            applyLLMConfiguration(config)
            saveLocalSettings()
            statusMessage = config.apiKeySaved ? "Loaded Find LLM config; server has saved key" : "Loaded Find LLM config; no saved key on server"
        }
    }

    func probeLLMConfiguration() async {
        await runTask("Probing Find LLM configuration") {
            let client = try makeClient()
            let config = try await client.updateLLMConfiguration(.init(
                provider: llmProvider,
                baseURL: llmBaseURLText,
                model: llmModel,
                apiKey: apiKey
            ))
            applyLLMConfiguration(config)
            let result = try await client.probeLLMConfiguration()
            saveLocalSettings()
            statusMessage = result.ok
                ? "LLM probe ok: \(result.summary.provider) \(result.summary.model)"
                : "LLM probe failed: \(result.error.isEmpty ? result.probe : result.error)"
        }
    }

    private static func loadConnectionProfileCatalog(defaults: UserDefaults, fallbackURL: String) -> ServerConnectionProfileCatalog {
        if let data = defaults.data(forKey: connectionProfilesKey),
           var catalog = try? JSONDecoder().decode(ServerConnectionProfileCatalog.self, from: data),
           !catalog.profiles.isEmpty {
            if let selectedID = defaults.string(forKey: selectedConnectionProfileKey), !selectedID.isEmpty {
                catalog.select(selectedID)
            }
            return catalog
        }

        if let fallbackProfile = try? ServerConnectionProfile(
            id: "local",
            name: "Local TASTE",
            serverURLText: fallbackURL,
            kind: .computer
        ) {
            return ServerConnectionProfileCatalog(profiles: [fallbackProfile], selectedProfileID: fallbackProfile.id)
        }

        let defaultProfile = try? ServerConnectionProfile(
            id: "local",
            name: "Local TASTE",
            serverURLText: "http://127.0.0.1:8765",
            kind: .computer
        )
        return ServerConnectionProfileCatalog(profiles: defaultProfile.map { [$0] } ?? [], selectedProfileID: defaultProfile?.id ?? "")
    }

    private func currentConnectionProfileCatalog() -> ServerConnectionProfileCatalog {
        ServerConnectionProfileCatalog(profiles: connectionProfiles, selectedProfileID: selectedConnectionProfileID)
    }

    private func saveConnectionProfileCatalog(_ catalog: ServerConnectionProfileCatalog) {
        if let data = try? JSONEncoder().encode(catalog) {
            defaults.set(data, forKey: Self.connectionProfilesKey)
        }
    }

    private func suggestedNewServerURLText() -> String {
        let existingURLs = Set(connectionProfiles.map(\.serverURLText))
        for hostSuffix in 10...99 {
            let candidate = "http://192.168.1.\(hostSuffix):8765"
            if !existingURLs.contains(candidate) {
                return candidate
            }
        }
        return "https://taste.example.com"
    }

    private func applyConnectionProfile(_ profile: ServerConnectionProfile, from catalog: ServerConnectionProfileCatalog) {
        connectionProfiles = catalog.profiles
        selectedConnectionProfileID = profile.id
        connectionProfileName = profile.name
        connectionProfileKind = profile.kind
        serverURLText = profile.serverURLText
        serverAccessToken = ServerAccessTokenStore.token(for: profile.id)
    }

    private func clearRemoteStateAfterConnectionChange() {
        serverReachable = nil
        serverMeta = nil
        projects = []
        jobs = []
        stageSnapshots = [:]
        remoteArtifacts = []
        attentionItems = []
        runtimeStatus = nil
        inspectedJobDetail = nil
        isInspectingJob = false
        latestClaudeResponse = nil
        isLoadingClaudeResponse = false
        artifactPreviewURL = nil
        isOpeningArtifact = false
        selectedProjectID = ""
    }

    private static func writeTemporaryPreview(_ preview: RemoteArtifactPreview) throws -> URL {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("taste-artifact-preview", isDirectory: true)
        try? FileManager.default.removeItem(at: directory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let fileName = sanitizedPreviewFileName(preview.fileName)
        let fileURL = directory.appendingPathComponent(fileName, isDirectory: false)
        try preview.data.write(to: fileURL, options: [.atomic])
        return fileURL
    }

    private static func sanitizedPreviewFileName(_ value: String) -> String {
        let lastPath = URL(fileURLWithPath: value).lastPathComponent.trimmingCharacters(in: .whitespacesAndNewlines)
        let fallback = lastPath.isEmpty ? "taste-artifact" : lastPath
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "._-"))
        let scalars = fallback.unicodeScalars.map { allowed.contains($0) ? Character($0) : "-" }
        let cleaned = String(scalars).trimmingCharacters(in: CharacterSet(charactersIn: ".-"))
        return cleaned.isEmpty ? "taste-artifact" : cleaned
    }

    private func makeClient() throws -> TASTEAPIClient {
        saveLocalSettings()
        let settings = try ConnectionSettings(
            serverURLText: serverURLText,
            serverAccessToken: serverAccessToken,
            llmProvider: llmProvider,
            llmBaseURLText: llmBaseURLText,
            llmModel: llmModel,
            apiKeyReference: ""
        )
        return TASTEAPIClient(settings: settings)
    }

    private func runTask(_ message: String, operation: () async throws -> Void) async {
        isLoading = true
        errorMessage = ""
        statusMessage = message
        do {
            try await operation()
            serverReachable = true
        } catch {
            updateReachability(after: error)
            errorMessage = TASTEErrorMessage.userFacing(error)
            statusMessage = "Needs attention"
        }
        isLoading = false
    }

    private func updateReachability(after error: Error) {
        if error is URLError || error is ConnectionSettingsError {
            serverReachable = false
            return
        }
        if error is TASTEAPIClientError {
            serverReachable = true
        }
    }
}

enum AppError: LocalizedError {
    case missingProjectID
    case missingProject
    case actionUnavailable(String)
    case invalidRemoteArtifact
    case duplicateActionLaunch(String)

    var errorDescription: String? {
        switch self {
        case .missingProjectID: "Enter a project ID before creating it on TASTE."
        case .missingProject: "Select or create a TASTE project in the web server first."
        case .actionUnavailable(let reason): reason
        case .invalidRemoteArtifact: "This remote artifact cannot be opened from the saved TASTE server connection."
        case .duplicateActionLaunch(let title): "\(title) is already being sent to the TASTE server."
        }
    }
}

private enum ServerAccessTokenStore {
    private static let service = "org.ustcstarteam.taste.server-access-token"

    static func token(for profileID: String) -> String {
        let account = profileID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !account.isEmpty else { return "" }
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data,
              let token = String(data: data, encoding: .utf8) else {
            return ""
        }
        return token
    }

    static func save(_ token: String, for profileID: String) {
        let account = profileID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !account.isEmpty else { return }
        deleteToken(for: account)
        let trimmedToken = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedToken.isEmpty, let data = trimmedToken.data(using: .utf8) else { return }
        let item: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
            kSecValueData as String: data,
        ]
        SecItemAdd(item as CFDictionary, nil)
    }

    private static func deleteToken(for profileID: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: profileID,
        ]
        SecItemDelete(query as CFDictionary)
    }
}
