import XCTest
@testable import TASTEAppCore

final class TASTEAppCoreTests: XCTestCase {
    func testWorkflowStagesMatchTasteSevenStageContract() {
        XCTAssertEqual(WorkflowStage.allCases.map(\.rawValue), [
            "find",
            "read",
            "idea",
            "plan",
            "environment",
            "experiment",
            "paper",
        ])
        XCTAssertEqual(WorkflowStage.experiment.displayName, "Experiment")
        XCTAssertEqual(WorkflowStage.paper.symbolName, "doc.richtext")
    }

    func testConnectionSettingsNormalizeServerURLAndRejectInvalidURLs() throws {
        let settings = try ConnectionSettings(
            serverURLText: " http://127.0.0.1:8765/ ",
            llmProvider: "openai_compatible",
            llmBaseURLText: "https://api.example.com/v1",
            llmModel: "research-model",
            apiKeyReference: "keychain:taste"
        )

        XCTAssertEqual(settings.serverURL.absoluteString, "http://127.0.0.1:8765")
        XCTAssertEqual(settings.llmBaseURL?.absoluteString, "https://api.example.com/v1")
        XCTAssertEqual(settings.apiKeyReference, "keychain:taste")
        XCTAssertThrowsError(try ConnectionSettings(serverURLText: "localhost:8765"))
    }

    func testConnectionSettingsApplyServerAccessTokenWithoutEncodingSecret() throws {
        let settings = try ConnectionSettings(
            serverURLText: "https://taste.example.com",
            serverAccessToken: " secret-token "
        )
        var request = URLRequest(url: settings.serverURL.appendingPathComponent("/api/projects"))

        settings.applyAuthentication(to: &request)

        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer secret-token")
        let json = String(decoding: try JSONEncoder().encode(settings), as: UTF8.self)
        XCTAssertFalse(json.localizedCaseInsensitiveContains("secret-token"))
        XCTAssertFalse(json.localizedCaseInsensitiveContains("authorization"))
    }

    func testRuntimeStatusRequestUsesServerAccessToken() async throws {
        RuntimeStatusAuthURLProtocol.recorder.reset()
        let settings = try ConnectionSettings(
            serverURLText: "https://taste.example.com",
            serverAccessToken: " cloud-token "
        )
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [RuntimeStatusAuthURLProtocol.self]
        let session = URLSession(configuration: configuration)
        let client = TASTEAPIClient(settings: settings, session: session)

        _ = try await client.fetchRuntimeStatus(project: "demo")

        let snapshot = RuntimeStatusAuthURLProtocol.recorder.snapshot
        XCTAssertEqual(snapshot.path, "/api/projects/demo/runtime")
        XCTAssertEqual(snapshot.authorization, "Bearer cloud-token")
    }

    func testEndpointBuilderCreatesTasteWebAPIURLsWithoutPersistingSecrets() throws {
        let settings = try ConnectionSettings(serverURLText: "http://taste.local:8765")
        let builder = TASTEEndpointBuilder(settings: settings)

        XCTAssertEqual(builder.projects().absoluteString, "http://taste.local:8765/api/projects")
        XCTAssertEqual(
            builder.jobs(project: "demo research").absoluteString,
            "http://taste.local:8765/api/jobs?compact=1&limit=12&include_history=1&project=demo%20research"
        )
        XCTAssertEqual(builder.projectAction().absoluteString, "http://taste.local:8765/api/jobs/project")
        XCTAssertEqual(builder.configMeta().absoluteString, "http://taste.local:8765/api/config/meta")
        XCTAssertEqual(builder.llmProbe().absoluteString, "http://taste.local:8765/api/config/llm-probe")
        XCTAssertEqual(builder.projectSummary(projectID: "demo research").absoluteString, "http://taste.local:8765/api/projects/demo%20research")
        XCTAssertEqual(builder.projectConfig(projectID: "demo research").absoluteString, "http://taste.local:8765/api/projects/demo%20research/config")
        XCTAssertEqual(
            builder.projectClaudeLatestResponse(projectID: "demo research", stage: .experiment, maxChars: 16_000).absoluteString,
            "http://taste.local:8765/api/projects/demo%20research/claude/latest-response?max_chars=16000&stage=experiment"
        )
        XCTAssertEqual(builder.projectRuntime(projectID: "demo research").absoluteString, "http://taste.local:8765/api/projects/demo%20research/runtime")
        XCTAssertEqual(builder.job(jobID: "paper_1", compact: true).absoluteString, "http://taste.local:8765/api/jobs/paper_1?compact=1")
        XCTAssertEqual(builder.cancelJob(jobID: "experiment_1").absoluteString, "http://taste.local:8765/api/jobs/experiment_1/cancel")

        let encoded = try JSONEncoder().encode(settings)
        let json = String(decoding: encoded, as: UTF8.self)
        XCTAssertFalse(json.contains("apiKey"))
        XCTAssertFalse(json.contains("secret"))
    }

    func testServerConnectionProfilesNormalizeAndDeduplicateServerTargets() throws {
        let local = try ServerConnectionProfile(
            id: "local",
            name: "Mac Studio",
            serverURLText: " http://127.0.0.1:8765/ ",
            kind: .computer
        )
        let cloud = try ServerConnectionProfile(
            id: "cloud",
            name: "Cloud Worker",
            serverURLText: "https://taste.example.com/",
            kind: .cloud
        )

        var catalog = ServerConnectionProfileCatalog(profiles: [local], selectedProfileID: local.id)
        catalog.upsert(cloud)
        catalog.upsert(try ServerConnectionProfile(id: "duplicate", name: "Duplicate", serverURLText: "https://taste.example.com", kind: .server))

        XCTAssertEqual(local.serverURLText, "http://127.0.0.1:8765")
        XCTAssertEqual(cloud.serverURLText, "https://taste.example.com")
        XCTAssertEqual(ServerConnectionProfile.stableID(for: "https://taste.example.com"), "server-https-taste-example-com")
        XCTAssertEqual(catalog.profiles.map(\.serverURLText), ["http://127.0.0.1:8765", "https://taste.example.com"])
        XCTAssertEqual(catalog.profiles[1].name, "Duplicate")
        XCTAssertEqual(catalog.selectedProfile?.serverURLText, "https://taste.example.com")

        let json = String(decoding: try JSONEncoder().encode(catalog), as: UTF8.self)
        XCTAssertTrue(json.contains("\"kind\":\"server\""))
        XCTAssertFalse(json.localizedCaseInsensitiveContains("api_key"))
        XCTAssertFalse(json.localizedCaseInsensitiveContains("secret"))
    }

    func testServerConnectionCatalogCreatesNewProfileWithoutReplacingSelectedTarget() throws {
        let local = try ServerConnectionProfile(
            id: "local",
            name: "Mac Studio",
            serverURLText: "http://127.0.0.1:8765",
            kind: .computer
        )
        var catalog = ServerConnectionProfileCatalog(profiles: [local], selectedProfileID: local.id)

        let created = try catalog.upsertProfile(
            id: "",
            name: "Lab Server",
            serverURLText: "http://192.168.1.20:8765/",
            kind: .server
        )

        XCTAssertEqual(created.id, "server-http-192-168-1-20-8765")
        XCTAssertEqual(catalog.profiles.map(\.name), ["Mac Studio", "Lab Server"])
        XCTAssertEqual(catalog.profiles.map(\.serverURLText), ["http://127.0.0.1:8765", "http://192.168.1.20:8765"])
        XCTAssertEqual(catalog.selectedProfile?.id, created.id)
    }

    func testMobileConnectionDeepLinkImportsServerProfileWithoutPersistingTokenInProfileJSON() throws {
        let url = try XCTUnwrap(URL(string: "taste://connect?server_url=http%3A%2F%2F192.168.1.20%3A8765%2F&token=server-token&profile=Lab%20Mac&kind=computer&project=demo_project"))

        let link = try MobileConnectionLink(url: url)
        let profile = link.profile
        let profileJSON = String(decoding: try JSONEncoder().encode(profile), as: UTF8.self)

        XCTAssertEqual(profile.name, "Lab Mac")
        XCTAssertEqual(profile.kind, .computer)
        XCTAssertEqual(profile.serverURLText, "http://192.168.1.20:8765")
        XCTAssertEqual(profile.id, "server-http-192-168-1-20-8765")
        XCTAssertEqual(link.serverAccessToken, "server-token")
        XCTAssertEqual(link.selectedProjectID, "demo_project")
        XCTAssertFalse(profileJSON.localizedCaseInsensitiveContains("server-token"))
        XCTAssertFalse(profileJSON.localizedCaseInsensitiveContains("authorization"))
    }

    func testLaunchConnectionImportExtractsTasteConnectArgumentOnly() throws {
        let rawLink = "taste://connect?server_url=http%3A%2F%2F127.0.0.1%3A8765&profile=Simulator&kind=computer&token=secret-token"
        let url = try XCTUnwrap(MobileLaunchConnectionImport.connectionURL(from: [
            "/path/TASTEApp",
            "--taste-connection-link",
            rawLink,
        ]))

        XCTAssertEqual(url.absoluteString, rawLink)
        XCTAssertNil(MobileLaunchConnectionImport.connectionURL(from: ["/path/TASTEApp", "--taste-connection-link"]))
        XCTAssertNil(MobileLaunchConnectionImport.connectionURL(from: ["/path/TASTEApp", "--taste-connection-link", "https://example.com"]))
    }

    func testProgressSummaryPrefersLiveJobThenProjectStage() {
        let live = TASTEJob(
            id: "experiment_123",
            stage: .experiment,
            status: .running,
            createdAt: "2026-06-13T12:00:00Z",
            progress: .init(phase: "training", current: 3, total: 10, percent: 30, message: "Running training command"),
            logs: ["experiment started"]
        )
        let summary = WorkflowProgressSummary(
            selectedProject: "demo",
            stageSnapshots: [.experiment: .init(stage: .experiment, status: "waiting", summary: "Waiting for evidence")],
            jobs: [live]
        )

        XCTAssertEqual(summary.activeStage, .experiment)
        XCTAssertEqual(summary.percentComplete, 30)
        XCTAssertEqual(summary.statusLine, "Running training command")
        XCTAssertEqual(summary.completedStageCount, 5)
    }

    func testProgressSummaryDoesNotTreatIncompletePaperSnapshotAsComplete() {
        let summary = WorkflowProgressSummary(
            selectedProject: "demo",
            stageSnapshots: [
                .paper: .init(stage: .paper, status: "needs_writing", summary: "Paper preview is not generated yet.")
            ],
            jobs: []
        )

        XCTAssertEqual(summary.activeStage, .paper)
        XCTAssertLessThan(summary.percentComplete, 100)
        XCTAssertEqual(summary.completedStageCount, 6)
        XCTAssertEqual(summary.statusLine, "Paper preview is not generated yet.")
    }

    func testStageSnapshotMobileStatusLabelShortensLongWorkflowTileText() {
        let repair = StageSnapshot(
            stage: .environment,
            status: "waiting_for_environment_repair",
            summary: "Environment repair is waiting for a server-side Claude run."
        )
        let review = StageSnapshot(
            stage: .environment,
            status: "waiting_for_environment_review",
            summary: "Environment review is waiting for a server-side Claude run."
        )
        let paper = StageSnapshot(stage: .paper, status: "needs_writing", summary: "")

        XCTAssertEqual(repair.mobileStatusLabel, "waiting for repair")
        XCTAssertLessThanOrEqual(repair.mobileStatusLabel.count, 20)
        XCTAssertEqual(review.mobileStatusLabel, "waiting for review")
        XCTAssertLessThanOrEqual(review.mobileStatusLabel.count, 20)
        XCTAssertEqual(paper.mobileStatusLabel, "needs writing")
    }

    func testMobileJobDetailSummaryKeepsOnlyTailLogsForPhoneStorage() {
        let logs = (1...40).map { "line \($0)" }
        let job = TASTEJob(
            id: "paper_123",
            stage: .paper,
            status: .running,
            createdAt: "2026-06-13T12:00:00Z",
            progress: .init(phase: "render", current: 14, total: 20, percent: 70, message: "Rendering PDF preview"),
            logs: logs
        )

        let summary = MobileJobDetailSummary(job: job)

        XCTAssertEqual(summary.title, "Paper")
        XCTAssertEqual(summary.statusLine, "Rendering PDF preview")
        XCTAssertEqual(summary.progressLabel, "14/20")
        XCTAssertEqual(summary.percentComplete, 70)
        XCTAssertEqual(summary.logTail.count, 24)
        XCTAssertEqual(summary.logTail.first, "line 17")
        XCTAssertEqual(summary.logTail.last, "line 40")
    }

    func testStoragePolicyKeepsPhoneClientLightweight() {
        XCTAssertLessThanOrEqual(MobileStoragePolicy.default.maxCachedBytes, 25 * 1024 * 1024)
        XCTAssertTrue(MobileStoragePolicy.default.cacheableArtifactKinds.contains(.markdownSummary))
        XCTAssertFalse(MobileStoragePolicy.default.cacheableArtifactKinds.contains(.paperPDF))
        XCTAssertFalse(MobileStoragePolicy.default.cacheableArtifactKinds.contains(.dataset))
    }

    func testMobileNetworkPolicyKeepsURLSessionCacheLightweight() {
        let policy = MobileNetworkPolicy.default
        XCTAssertLessThanOrEqual(policy.memoryCacheBytes, MobileStoragePolicy.default.maxCachedBytes)
        XCTAssertEqual(policy.diskCacheBytes, 0)
        XCTAssertEqual(policy.requestCachePolicy, .reloadIgnoringLocalCacheData)
        XCTAssertLessThanOrEqual(policy.timeoutForRequest, 30)

        let session = TASTEAPIClient.makeMobileSession(policy: policy)
        let configuration = session.configuration

        XCTAssertEqual(configuration.urlCache?.memoryCapacity, policy.memoryCacheBytes)
        XCTAssertEqual(configuration.urlCache?.diskCapacity, 0)
        XCTAssertEqual(configuration.requestCachePolicy, .reloadIgnoringLocalCacheData)
        XCTAssertFalse(configuration.httpShouldSetCookies)
    }

    func testMobileAutoRefreshPolicyFollowsLiveJobsWithoutBusyPolling() {
        let policy = MobileAutoRefreshPolicy.default
        let liveJobs = [
            TASTEJob(id: "healthcheck_1", stage: .environment, status: .running, createdAt: "2026-06-13T12:00:00Z"),
            TASTEJob(id: "status_1", stage: .find, status: .queued, createdAt: "2026-06-13T12:00:01Z"),
        ]
        let finishedJobs = [
            TASTEJob(id: "paper_1", stage: .paper, status: .done, createdAt: "2026-06-13T12:00:02Z"),
        ]

        XCTAssertTrue(policy.hasLiveJobs(liveJobs + finishedJobs))
        XCTAssertFalse(policy.hasLiveJobs(finishedJobs))
        XCTAssertGreaterThanOrEqual(policy.activeJobIntervalSeconds, 6)
        XCTAssertLessThanOrEqual(policy.activeJobIntervalSeconds, 10)
        XCTAssertGreaterThanOrEqual(policy.idleIntervalSeconds, 20)
        XCTAssertLessThanOrEqual(policy.idleIntervalSeconds, 60)
        XCTAssertEqual(policy.nextIntervalSeconds(for: liveJobs + finishedJobs), policy.activeJobIntervalSeconds)
        XCTAssertEqual(policy.nextIntervalSeconds(for: finishedJobs), policy.idleIntervalSeconds)
    }

    func testMobileConnectionImportActionsExposeClipboardAndQRCodePaths() {
        XCTAssertEqual(MobileConnectionImportAction.allCases, [.pastedText, .clipboard, .qrCode])

        let pasted = MobileConnectionImportAction.pastedText
        XCTAssertEqual(pasted.buttonTitle, "Import Connection Link")
        XCTAssertEqual(pasted.systemImage, "link.badge.plus")
        XCTAssertTrue(pasted.accessibilityLabel.localizedCaseInsensitiveContains("connection link"))

        let clipboard = MobileConnectionImportAction.clipboard
        XCTAssertEqual(clipboard.buttonTitle, "Import From Clipboard")
        XCTAssertEqual(clipboard.systemImage, "doc.on.clipboard")
        XCTAssertTrue(clipboard.accessibilityLabel.localizedCaseInsensitiveContains("clipboard"))

        let qrCode = MobileConnectionImportAction.qrCode
        XCTAssertEqual(qrCode.buttonTitle, "Scan Connection QR")
        XCTAssertEqual(qrCode.systemImage, "qrcode.viewfinder")
        XCTAssertTrue(qrCode.accessibilityLabel.localizedCaseInsensitiveContains("QR"))
    }

    func testMobileFormFieldSpecsProvideVisibleLabelsAndAccessibilityLabels() {
        XCTAssertTrue(MobileFormFieldID.allCases.contains(.serverURL))
        XCTAssertTrue(MobileFormFieldID.allCases.contains(.serverAccessToken))
        XCTAssertTrue(MobileFormFieldID.allCases.contains(.connectionProfileName))
        XCTAssertTrue(MobileFormFieldID.allCases.contains(.connectionLink))
        XCTAssertTrue(MobileFormFieldID.allCases.contains(.llmAPIKey))
        XCTAssertTrue(MobileFormFieldID.allCases.contains(.experimentPython))
        XCTAssertNotNil(MobileFormFieldID(rawValue: "research_interest"))
        XCTAssertNotNil(MobileFormFieldID(rawValue: "researcher_profile"))
        XCTAssertNotNil(MobileFormFieldID(rawValue: "connection_link"))
        XCTAssertTrue(MobileFormFieldCatalog.spec(for: .connectionLink).prompt.contains("taste://connect"))

        for id in MobileFormFieldID.allCases {
            let spec = MobileFormFieldCatalog.spec(for: id)
            XCTAssertFalse(spec.title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, "\(id.rawValue) needs a visible title")
            XCTAssertFalse(spec.prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, "\(id.rawValue) needs input prompt text")
            XCTAssertFalse(spec.accessibilityLabel.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, "\(id.rawValue) needs an accessibility label")
            XCTAssertNotEqual(spec.title, spec.prompt, "\(id.rawValue) should not rely on placeholder-only labeling")
        }
    }

    func testDecodesProjectAndJobResponsesFromTasteServer() throws {
        let projectsJSON = """
        [{"id":"demo","name":"Demo","topic":"AI research","path":"/srv/taste/projects/demo"}]
        """.data(using: .utf8)!
        let projectSummaryJSON = """
        {"project":"demo","path":"/srv/taste/projects/demo","config":{"name":"demo","topic":"AI research"}}
        """.data(using: .utf8)!
        let jobsJSON = """
        {"jobs":[{"job_id":"find_1","stage":"find","status":"running","created_at":"2026-06-13T12:00:00Z","cancel_requested":true,"logs":["find started"],"progress":{"phase":"abstract_scoring","current":2,"total":5,"percent":40,"message":"Scoring abstracts"}}]}
        """.data(using: .utf8)!

        let projects = try TASTEProject.decodeList(from: projectsJSON)
        let summaryProject = try TASTEProject.decodeOne(from: projectSummaryJSON)
        let jobs = try TASTEJob.decodeList(from: jobsJSON)

        XCTAssertEqual(projects.first?.id, "demo")
        XCTAssertEqual(projects.first?.topic, "AI research")
        XCTAssertEqual(summaryProject.name, "demo")
        XCTAssertEqual(summaryProject.path, "/srv/taste/projects/demo")
        XCTAssertEqual(jobs.first?.id, "find_1")
        XCTAssertEqual(jobs.first?.stage, .find)
        XCTAssertEqual(jobs.first?.cancelRequested, true)
        XCTAssertEqual(jobs.first?.progress?.percent, 40)
    }

    func testDecodesProjectSummaryStageSnapshotsFromTasteServer() throws {
        let summaryJSON = """
        {
          "project": "demo",
          "path": "/srv/taste/projects/demo",
          "stages": {
            "environment": {"status": "ready", "summary": "Environment selected"},
            "experiment": {"status": "blocked", "summary_zh": "等待 reference reproduction"},
            "paper": {"status": "blocked_before_paper_generation", "summary_en": "Paper blocked until experiments pass"},
            "unknown_internal": {"status": "ignored", "summary": "ignored"}
          }
        }
        """.data(using: .utf8)!

        let summary = try TASTEProjectSummary.decode(from: summaryJSON)

        XCTAssertEqual(summary.project, "demo")
        XCTAssertEqual(summary.path, "/srv/taste/projects/demo")
        XCTAssertEqual(summary.stageSnapshots[.environment]?.status, "ready")
        XCTAssertEqual(summary.stageSnapshots[.environment]?.summary, "Environment selected")
        XCTAssertEqual(summary.stageSnapshots[.experiment]?.summary, "等待 reference reproduction")
        XCTAssertEqual(summary.stageSnapshots[.paper]?.summary, "Paper blocked until experiments pass")
        XCTAssertNil(summary.stageSnapshots[.find])
        XCTAssertEqual(summary.stageSnapshots.count, 3)
    }

    func testDecodesProjectSummaryAttentionItemsFromBlockersAndNextActions() throws {
        let summaryJSON = """
        {
          "project": "demo",
          "path": "/srv/taste/projects/demo",
          "current_blocker": {
            "category": "fresh_base_reference_reproduction_required",
            "severity": "block",
            "issue": "Reference reproduction is required before experiments.",
            "next_action": "Run Environment then bounded reproduction."
          },
          "next_actions": [
            {
              "issue": "Load runtime paths",
              "next_action": "Detect runtime on the TASTE server."
            },
            {
              "title": "Review LLM quota",
              "reason": "Find scoring is blocked by quota."
            }
          ],
          "next_action": "Fallback should not duplicate when next_actions exists"
        }
        """.data(using: .utf8)!

        let summary = try TASTEProjectSummary.decode(from: summaryJSON)

        XCTAssertEqual(summary.attentionItems.map(\.kind), [.blocker, .nextAction, .nextAction])
        XCTAssertEqual(summary.attentionItems[0].title, "Reference reproduction is required before experiments.")
        XCTAssertEqual(summary.attentionItems[0].detail, "Run Environment then bounded reproduction.")
        XCTAssertEqual(summary.attentionItems[1].title, "Load runtime paths")
        XCTAssertEqual(summary.attentionItems[1].detail, "Detect runtime on the TASTE server.")
        XCTAssertEqual(summary.attentionItems[2].title, "Review LLM quota")
        XCTAssertEqual(summary.attentionItems[2].detail, "Find scoring is blocked by quota.")
    }

    func testProjectSummaryAttentionFallsBackToTopLevelNextAction() throws {
        let summaryJSON = """
        {
          "project": "demo",
          "path": "/srv/taste/projects/demo",
          "next_action": "Connect the iPhone to the TASTE server and run Find."
        }
        """.data(using: .utf8)!

        let summary = try TASTEProjectSummary.decode(from: summaryJSON)

        XCTAssertEqual(summary.attentionItems.map(\.kind), [.nextAction])
        XCTAssertEqual(summary.attentionItems.first?.title, "Next action")
        XCTAssertEqual(summary.attentionItems.first?.detail, "Connect the iPhone to the TASTE server and run Find.")
    }

    func testDecodesRemoteArtifactsFromProjectSummaryWithoutLocalFilePaths() throws {
        let summaryJSON = """
        {
          "project": "demo",
          "path": "/srv/taste/projects/demo",
          "latest_generated_pdf_url": "/api/projects/demo/files/paper/output/demo.pdf",
          "blocked_pdf_url": "",
          "stages": {
            "paper": {
              "status": "preview_available",
              "pdf_url": "/api/projects/demo/files/paper/output/demo.pdf",
              "tex_url": "/api/projects/demo/files/paper/main.tex",
              "raw_pdf_path": "/srv/taste/projects/demo/paper/output/demo.pdf"
            }
          }
        }
        """.data(using: .utf8)!

        let summary = try TASTEProjectSummary.decode(from: summaryJSON)

        XCTAssertEqual(summary.remoteArtifacts.map(\.kind), [.paperPDF, .texSource])
        XCTAssertEqual(summary.remoteArtifacts.map(\.title), ["Paper PDF", "TeX Source"])
        XCTAssertEqual(summary.remoteArtifacts.map(\.urlString), [
            "/api/projects/demo/files/paper/output/demo.pdf",
            "/api/projects/demo/files/paper/main.tex",
        ])
        XCTAssertFalse(summary.remoteArtifacts.contains { $0.urlString.contains("/srv/taste") })
    }

    func testRemoteArtifactResolvesServerURLsAndRejectsLocalPaths() throws {
        let settings = try ConnectionSettings(serverURLText: "http://taste.local:8765")
        let relative = RemoteArtifact(
            id: "paper-pdf",
            title: "Paper PDF",
            kind: .paperPDF,
            urlString: "/api/projects/demo/files/paper/output/demo.pdf"
        )
        let absolute = RemoteArtifact(
            id: "paper-tex",
            title: "TeX Source",
            kind: .texSource,
            urlString: "https://taste.example/api/projects/demo/files/paper/main.tex"
        )
        let localPath = RemoteArtifact(
            id: "local-paper",
            title: "Local Paper",
            kind: .paperPDF,
            urlString: "/srv/taste/projects/demo/paper/output/demo.pdf"
        )

        XCTAssertEqual(relative.remoteURL(relativeTo: settings.serverURL)?.absoluteString, "http://taste.local:8765/api/projects/demo/files/paper/output/demo.pdf")
        XCTAssertEqual(absolute.remoteURL(relativeTo: settings.serverURL)?.absoluteString, "https://taste.example/api/projects/demo/files/paper/main.tex")
        XCTAssertNil(localPath.remoteURL(relativeTo: settings.serverURL))
    }

    func testProtectedRemoteArtifactRequiresAuthenticatedInAppPreview() throws {
        let settings = try ConnectionSettings(
            serverURLText: "https://taste.example.com",
            serverAccessToken: "server-token"
        )
        let artifact = RemoteArtifact(
            id: "paper-pdf",
            title: "Paper PDF",
            kind: .paperPDF,
            urlString: "/api/projects/demo/files/paper/output/demo.pdf"
        )

        let plan = artifact.openPlan(relativeTo: settings)

        XCTAssertEqual(plan.mode, .authenticatedPreview)
        XCTAssertEqual(plan.url?.absoluteString, "https://taste.example.com/api/projects/demo/files/paper/output/demo.pdf")
        XCTAssertFalse(plan.canOpenExternally)
        XCTAssertTrue(plan.note.contains("server access token"))

        let publicSettings = try ConnectionSettings(serverURLText: "https://taste.example.com")
        XCTAssertEqual(artifact.openPlan(relativeTo: publicSettings).mode, .externalLink)
        XCTAssertTrue(artifact.openPlan(relativeTo: publicSettings).canOpenExternally)
    }

    func testRemoteArtifactPreviewFetchUsesBearerTokenAndRejectsOversizedFiles() async throws {
        ArtifactPreviewURLProtocol.recorder.reset()
        let settings = try ConnectionSettings(
            serverURLText: "https://taste.example.com",
            serverAccessToken: "server-token"
        )
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ArtifactPreviewURLProtocol.self]
        let session = URLSession(configuration: configuration)
        let client = TASTEAPIClient(settings: settings, session: session)
        let artifact = RemoteArtifact(
            id: "paper-tex",
            title: "TeX Source",
            kind: .texSource,
            urlString: "/api/projects/demo/files/paper/main.tex"
        )

        let preview = try await client.fetchRemoteArtifactPreview(artifact, maxBytes: 64)

        XCTAssertEqual(String(decoding: preview.data, as: UTF8.self), "preview")
        XCTAssertEqual(preview.fileName, "main.tex")
        XCTAssertEqual(ArtifactPreviewURLProtocol.recorder.snapshot.authorization, "Bearer server-token")

        do {
            _ = try await client.fetchRemoteArtifactPreview(artifact, maxBytes: 4)
            XCTFail("Expected oversized artifact to be rejected")
        } catch TASTEAPIClientError.responseTooLarge(let byteCount, let limit) {
            XCTAssertEqual(byteCount, 7)
            XCTAssertEqual(limit, 4)
        }
    }

    func testCreateProjectPayloadMatchesTasteProjectEndpoint() throws {
        let payload = CreateProjectPayload(id: " demo_project ", topic: " autonomous research agents ")
        let data = try JSONEncoder().encode(payload)
        let json = String(decoding: data, as: UTF8.self)

        XCTAssertTrue(json.contains("\"id\":\"demo_project\""))
        XCTAssertTrue(json.contains("\"topic\":\"autonomous research agents\""))
        XCTAssertFalse(json.contains("api_key"))
        XCTAssertFalse(json.contains("runtime"))
    }

    func testProjectResearchPreferencesPayloadMatchesTasteProjectConfigEndpoint() throws {
        let payload = ProjectResearchPreferences(
            researchInterest: " AI agents for academic research automation ",
            researcherProfile: " Prefer reproducible systems and lightweight experiments ",
            targetVenue: " ICLR ",
            paperTitle: " Mobile TASTE "
        )
        let data = try JSONEncoder().encode(payload)
        let json = String(decoding: data, as: UTF8.self)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])

        XCTAssertEqual(object["research_interest"] as? String, "AI agents for academic research automation")
        XCTAssertEqual(object["researcher_profile"] as? String, "Prefer reproducible systems and lightweight experiments")
        XCTAssertEqual(object["target_venue"] as? String, "ICLR")
        XCTAssertEqual(object["venue"] as? String, "ICLR")
        XCTAssertEqual(object["title"] as? String, "Mobile TASTE")
        XCTAssertFalse(json.contains("api_key"))
        XCTAssertFalse(json.contains("server_access_token"))
    }

    func testProjectResearchPreferencesUpdatePostsConfigWithBearerToken() async throws {
        ProjectConfigURLProtocol.recorder.reset()
        let settings = try ConnectionSettings(
            serverURLText: "https://taste.example.com",
            serverAccessToken: "server-token"
        )
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ProjectConfigURLProtocol.self]
        let session = URLSession(configuration: configuration)
        let client = TASTEAPIClient(settings: settings, session: session)

        let summary = try await client.updateProjectResearchPreferences(
            project: "demo",
            ProjectResearchPreferences(
                researchInterest: "AI agents for academic research automation",
                researcherProfile: "Prefer reproducible systems",
                targetVenue: "ICLR",
                paperTitle: "Mobile TASTE"
            )
        )

        let snapshot = ProjectConfigURLProtocol.recorder.snapshot
        XCTAssertEqual(snapshot.path, "/api/projects/demo/config")
        XCTAssertEqual(snapshot.method, "POST")
        XCTAssertEqual(snapshot.authorization, "Bearer server-token")
        let requestObject = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(snapshot.body.utf8)) as? [String: Any])
        XCTAssertEqual(requestObject["research_interest"] as? String, "AI agents for academic research automation")
        XCTAssertEqual(requestObject["researcher_profile"] as? String, "Prefer reproducible systems")
        XCTAssertEqual(requestObject["target_venue"] as? String, "ICLR")
        XCTAssertFalse(snapshot.body.contains("api_key"))
        XCTAssertEqual(summary.runPreferences.researchInterest, "AI agents for academic research automation")
        XCTAssertEqual(summary.runPreferences.researcherProfile, "Prefer reproducible systems")
        XCTAssertEqual(summary.runPreferences.targetVenue, "ICLR")
        XCTAssertEqual(summary.runPreferences.paperTitle, "Mobile TASTE")
    }

    func testClaudeLatestResponseFetchUsesBearerTokenAndMobileCharacterLimit() async throws {
        ClaudeLatestResponseURLProtocol.recorder.reset()
        let settings = try ConnectionSettings(
            serverURLText: "https://taste.example.com",
            serverAccessToken: "server-token"
        )
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ClaudeLatestResponseURLProtocol.self]
        let session = URLSession(configuration: configuration)
        let client = TASTEAPIClient(settings: settings, session: session)

        let response = try await client.fetchClaudeLatestResponse(project: "demo", stage: .paper, maxChars: 16_000)

        let snapshot = ClaudeLatestResponseURLProtocol.recorder.snapshot
        XCTAssertEqual(snapshot.path, "/api/projects/demo/claude/latest-response")
        XCTAssertEqual(snapshot.query["max_chars"], "16000")
        XCTAssertEqual(snapshot.query["stage"], "paper")
        XCTAssertEqual(snapshot.authorization, "Bearer server-token")
        XCTAssertEqual(response.stage, "paper")
        XCTAssertEqual(response.requestedStage, "paper")
        XCTAssertEqual(response.responseMarkdown, "Claude project agent response")
        XCTAssertEqual(response.returnedCharacterCount, 29)
        XCTAssertTrue(response.truncated)
        XCTAssertFalse(response.containsSecretMaterial)
    }

    func testRuntimeConfigurationPayloadKeepsExecutionRemote() throws {
        let payload = ProjectRuntimeConfiguration(
            condaEnv: "taste-exp",
            condaBase: "/opt/miniforge3",
            nodeBin: "/opt/node/bin",
            claudePath: "/Users/me/.local/bin/claude",
            managementPython: "/srv/taste/.venv/bin/python",
            experimentPython: "/srv/taste/envs/exp/bin/python",
            extraPathText: "/opt/cuda/bin:/srv/tools/bin"
        )
        let data = try JSONEncoder().encode(payload)
        let json = String(decoding: data, as: UTF8.self)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])

        XCTAssertTrue(json.contains("\"conda_env\":\"taste-exp\""))
        XCTAssertEqual(object["extra_path"] as? [String], ["/opt/cuda/bin", "/srv/tools/bin"])
        XCTAssertFalse(json.contains("api_key"))
        XCTAssertFalse(json.contains("dataset"))
    }

    func testRuntimeConfigurationDecodesServerRuntimeStatus() throws {
        let runtimeJSON = """
        {
          "project": "demo",
          "runtime": {
            "node_bin": "/opt/node/bin",
            "claude_path": "/usr/local/bin/claude",
            "conda_base": "/opt/miniforge3",
            "management_python": "/srv/taste/.venv/bin/python",
            "python_executable": "/srv/taste/.venv/bin/python",
            "experiment_python": "/srv/taste/envs/exp/bin/python",
            "extra_path": ["/opt/cuda/bin", "/srv/tools/bin"]
          },
          "checks": {}
        }
        """.data(using: .utf8)!

        let runtime = try ProjectRuntimeConfiguration.decode(from: runtimeJSON)

        XCTAssertEqual(runtime.nodeBin, "/opt/node/bin")
        XCTAssertEqual(runtime.claudePath, "/usr/local/bin/claude")
        XCTAssertEqual(runtime.condaBase, "/opt/miniforge3")
        XCTAssertEqual(runtime.managementPython, "/srv/taste/.venv/bin/python")
        XCTAssertEqual(runtime.experimentPython, "/srv/taste/envs/exp/bin/python")
        XCTAssertEqual(runtime.extraPathText, "/opt/cuda/bin:/srv/tools/bin")
    }

    func testRuntimeConfigurationDecodesNestedDetectResponse() throws {
        let runtimeJSON = """
        {
          "project": "demo",
          "runtime": {
            "project": "demo",
            "runtime": {
              "node_bin": "/detected/node/bin",
              "claude_path": "/Users/me/.local/bin/claude",
              "management_python": "/usr/bin/python3",
              "extra_path": "/one/bin:/two/bin"
            },
            "detected": {}
          },
          "checks": {}
        }
        """.data(using: .utf8)!

        let runtime = try ProjectRuntimeConfiguration.decode(from: runtimeJSON)

        XCTAssertEqual(runtime.nodeBin, "/detected/node/bin")
        XCTAssertEqual(runtime.claudePath, "/Users/me/.local/bin/claude")
        XCTAssertEqual(runtime.managementPython, "/usr/bin/python3")
        XCTAssertEqual(runtime.extraPathText, "/one/bin:/two/bin")
    }

    func testRuntimeStatusDecodesServerChecksForMobileDiagnostics() throws {
        let runtimeJSON = """
        {
          "project": "demo",
          "runtime": {
            "node_bin": "/opt/node/bin",
            "claude_path": "/missing/claude",
            "management_python": "/srv/taste/.venv/bin/python",
            "experiment_python": "/srv/taste/envs/exp/bin/python"
          },
          "checks": {
            "node": {"path": "/opt/node/bin/node", "ok": true, "version": "v20.11.1", "reason": "ok"},
            "claude": {"path": "/missing/claude", "ok": false, "version": "", "reason": "claude_path does not exist"},
            "management_python": {"path": "/srv/taste/.venv/bin/python", "ok": true, "version": "Python 3.12.13", "reason": "ok"}
          },
          "path_head": ["/opt/node/bin", "/srv/taste/.venv/bin"]
        }
        """.data(using: .utf8)!

        let status = try ProjectRuntimeStatus.decode(from: runtimeJSON)

        XCTAssertEqual(status.project, "demo")
        XCTAssertEqual(status.runtime.nodeBin, "/opt/node/bin")
        XCTAssertEqual(status.checks["node"]?.version, "v20.11.1")
        XCTAssertEqual(status.checks["claude"]?.displayName, "Claude")
        XCTAssertEqual(status.failingCriticalChecks.map(\.id), ["claude"])
        XCTAssertEqual(status.summaryLine, "1 critical runtime check needs attention")
        XCTAssertEqual(status.orderedChecks.map(\.id), ["management_python", "claude", "node"])
    }

    func testProjectSummaryDecodesRuntimeDiagnosticsFromServerPayload() throws {
        let summaryJSON = """
        {
          "project": "demo",
          "path": "/srv/taste/projects/demo",
          "runtime": {
            "project": "demo",
            "runtime": {
              "management_python": "/srv/taste/.venv/bin/python",
              "claude_path": "/usr/local/bin/claude"
            },
            "checks": {
              "management_python": {"path": "/srv/taste/.venv/bin/python", "ok": true, "version": "Python 3.12.13", "reason": "ok"},
              "claude": {"path": "/usr/local/bin/claude", "ok": true, "version": "claude 1.0.0", "reason": "ok"}
            },
            "path_head": ["/srv/taste/.venv/bin"]
          }
        }
        """.data(using: .utf8)!

        let summary = try TASTEProjectSummary.decode(from: summaryJSON)

        XCTAssertEqual(summary.runtimeStatus?.runtime.managementPython, "/srv/taste/.venv/bin/python")
        XCTAssertEqual(summary.runtimeStatus?.checks["claude"]?.ok, true)
        XCTAssertEqual(summary.runtimeStatus?.summaryLine, "Critical runtime checks passed")
    }

    func testServerMetaAndLLMProbeDecodeForConnectionChecks() throws {
        let metaJSON = #"{"saved":true}"#.data(using: .utf8)!
        let mobileMetaJSON = """
        {
          "saved": true,
          "mobile_api_version": 1,
          "mobile_capabilities": [
            "projects",
            "jobs",
            "runtime",
            "llm_config",
            "claude_latest_response",
            "remote_artifacts"
          ]
        }
        """.data(using: .utf8)!
        let probeJSON = """
        {"ok":false,"error":"quota exceeded","probe":"scoring_shape","summary":{"provider":"openai_compatible","base_url":"https://api.example.com/v1","model":"research-model","enabled":true}}
        """.data(using: .utf8)!

        let meta = try JSONDecoder().decode(TASTEServerMeta.self, from: metaJSON)
        let mobileMeta = try JSONDecoder().decode(TASTEServerMeta.self, from: mobileMetaJSON)
        let probe = try JSONDecoder().decode(LLMProbeResult.self, from: probeJSON)

        XCTAssertTrue(meta.saved)
        XCTAssertEqual(mobileMeta.mobileAPIVersion, 1)
        XCTAssertTrue(mobileMeta.supportsMobileControlPlane)
        XCTAssertFalse(meta.supportsMobileControlPlane)
        XCTAssertFalse(probe.ok)
        XCTAssertEqual(probe.probe, "scoring_shape")
        XCTAssertEqual(probe.summary.provider, "openai_compatible")
        XCTAssertEqual(probe.summary.model, "research-model")
    }

    func testLLMConfigurationDecodesPublicServerConfigWithoutKeepingSecrets() throws {
        let configJSON = """
        {
          "provider": "openai_compatible",
          "base_url": "https://api.example.com/v1",
          "model": "research-model",
          "api_key": "should-not-leave-server",
          "api_key_saved": true,
          "api_key_suffix": "abcd",
          "project_llm_synced": true
        }
        """.data(using: .utf8)!

        let config = try LLMConfiguration.decode(from: configJSON)

        XCTAssertEqual(config.provider, "openai_compatible")
        XCTAssertEqual(config.baseURL, "https://api.example.com/v1")
        XCTAssertEqual(config.model, "research-model")
        XCTAssertEqual(config.apiKey, "")
        XCTAssertTrue(config.apiKeySaved)
        XCTAssertEqual(config.apiKeySuffix, "abcd")
        XCTAssertTrue(config.projectLLMSynced)
    }

    func testMobileErrorMessagesHideRawFoundationNoise() {
        let message = TASTEErrorMessage.userFacing(URLError(.cannotConnectToHost))

        XCTAssertEqual(message, "Cannot reach the TASTE server. Check the server URL, network, and whether scripts/start_web.sh is running.")
        XCTAssertFalse(message.contains("NSURLErrorDomain"))
        XCTAssertFalse(message.contains("UserInfo"))
    }

    func testMobileErrorMessagesSummarizeTasteJSONErrorBodies() {
        let body = """
        {
          "error": "action blocked",
          "reason": "Find LLM is not configured for this project.",
          "next_action": "Open Settings and sync the Find LLM config."
        }
        """

        let message = TASTEErrorMessage.userFacing(TASTEAPIClientError.httpStatus(409, body))

        XCTAssertEqual(message, "Find LLM is not configured for this project. Next action: Open Settings and sync the Find LLM config.")
        XCTAssertFalse(message.contains("{"))
        XCTAssertFalse(message.contains("\"error\""))
    }

    func testMobileErrorMessagesGuideServerTokenRepairForAuthFailures() {
        let body = """
        {
          "detail": "Invalid or missing server access token: wrong-secret"
        }
        """

        let message = TASTEErrorMessage.userFacing(TASTEAPIClientError.httpStatus(401, body))

        XCTAssertEqual(message, "TASTE server rejected the server access token. Open Settings, update the Server access token, then tap Test Connection.")
        XCTAssertFalse(message.contains("wrong-secret"))
        XCTAssertFalse(message.contains("{"))
    }

    func testProjectActionPayloadMatchesTasteProjectJobEndpoint() throws {
        let payload = ProjectActionPayload(
            project: "demo",
            action: .fullCycle,
            topic: "AI research",
            venue: "ICLR",
            title: "A Mobile TASTE Study",
            options: [.autoInstallLatex: true, .useExistingLiteraturePacket: true]
        )
        let data = try JSONEncoder().encode(payload)
        let json = String(decoding: data, as: UTF8.self)

        XCTAssertTrue(json.contains("\"project\":\"demo\""))
        XCTAssertTrue(json.contains("\"action\":\"full-cycle\""))
        XCTAssertTrue(json.contains("\"auto_install_latex\":true"))
        XCTAssertTrue(json.contains("\"use_existing_literature_packet\":true"))
        XCTAssertFalse(json.contains("api_key"))
    }

    func testLightweightServerActionsAreAvailableForMobileValidation() throws {
        XCTAssertTrue(ProjectAction.allCases.contains(.status))
        XCTAssertTrue(ProjectAction.allCases.contains(.healthcheck))

        let statusPayload = ProjectActionPayload(project: "demo", action: .status)
        let healthcheckPayload = ProjectActionPayload(project: "demo", action: .healthcheck)
        let statusJSON = String(decoding: try JSONEncoder().encode(statusPayload), as: UTF8.self)
        let healthcheckJSON = String(decoding: try JSONEncoder().encode(healthcheckPayload), as: UTF8.self)

        XCTAssertTrue(statusJSON.contains("\"action\":\"status\""))
        XCTAssertTrue(healthcheckJSON.contains("\"action\":\"healthcheck\""))
        XCTAssertFalse(statusJSON.contains("api_key"))
        XCTAssertFalse(healthcheckJSON.contains("api_key"))
    }

    func testMobileRunConfirmationProtectsRemoteComputeActions() {
        XCTAssertFalse(ProjectAction.status.requiresMobileConfirmation)
        XCTAssertFalse(ProjectAction.healthcheck.requiresMobileConfirmation)

        for action in ProjectAction.allCases where action.syncsProjectResearchPreferencesBeforeRun {
            XCTAssertTrue(action.requiresMobileConfirmation, "\(action.rawValue) should require confirmation before dispatching remote work")
            XCTAssertTrue(action.mobileConfirmationMessage.contains(action.mobileRunTitle))
            XCTAssertTrue(action.mobileConfirmationMessage.contains("TASTE server"))
            XCTAssertTrue(action.mobileConfirmationMessage.contains("phone"))
        }
    }

    func testMobileActionTitlesClarifyCurrentFindBridgeStages() {
        XCTAssertEqual(ProjectAction.read.mobileRunTitle, "Read Current Find")
        XCTAssertEqual(ProjectAction.idea.mobileRunTitle, "Generate Ideas")
        XCTAssertEqual(ProjectAction.plan.mobileRunTitle, "Draft Plans")
        XCTAssertTrue(ProjectAction.read.runsCurrentFindBridge)
        XCTAssertTrue(ProjectAction.idea.runsCurrentFindBridge)
        XCTAssertTrue(ProjectAction.plan.runsCurrentFindBridge)
        XCTAssertFalse(ProjectAction.experiment.runsCurrentFindBridge)
    }

    func testMobileActionLaunchGateRejectsDuplicateInFlightStartsForSameProjectActionAndOptions() {
        var gate = MobileActionLaunchGate()
        let options: [ProjectActionOption: Bool] = [.useExistingLiteraturePacket: true]

        XCTAssertTrue(gate.begin(projectID: "demo", action: .fullCycle, options: options))
        XCTAssertFalse(gate.begin(projectID: "demo", action: .fullCycle, options: options))
        XCTAssertTrue(gate.begin(projectID: "demo", action: .find, options: [:]))

        gate.finish(projectID: "demo", action: .fullCycle, options: options)

        XCTAssertTrue(gate.begin(projectID: "demo", action: .fullCycle, options: options))
    }

    func testMobileActionAvailabilityBlocksCurrentFindBridgeUntilFindIsReady() {
        let context = MobileRunContext(
            serverURLText: "http://192.168.1.10:8765",
            serverReachable: true,
            selectedProjectID: "demo",
            llmProvider: "openai_compatible",
            llmBaseURLText: "https://api.example.com/v1",
            llmModel: "research-model",
            runtimeConfiguration: .init(managementPython: "/srv/taste/.venv/bin/python")
        )

        for action in [ProjectAction.read, .idea, .plan, .currentFindSelection] {
            let availability = ProjectActionAvailability.evaluate(action, context: context)
            XCTAssertFalse(availability.isEnabled)
            XCTAssertEqual(availability.reason, "Run Find first so the server has a current Find packet for this project.")
        }

        let readyContext = MobileRunContext(
            serverURLText: "http://192.168.1.10:8765",
            serverReachable: true,
            selectedProjectID: "demo",
            llmProvider: "openai_compatible",
            llmBaseURLText: "https://api.example.com/v1",
            llmModel: "research-model",
            runtimeConfiguration: .init(managementPython: "/srv/taste/.venv/bin/python"),
            stageSnapshots: [
                .find: .init(stage: .find, status: "selected", summary: "Current Find packet is ready")
            ]
        )

        XCTAssertTrue(ProjectActionAvailability.evaluate(.read, context: readyContext).isEnabled)
        XCTAssertTrue(ProjectActionAvailability.evaluate(.idea, context: readyContext).isEnabled)
        XCTAssertTrue(ProjectActionAvailability.evaluate(.plan, context: readyContext).isEnabled)
        XCTAssertTrue(ProjectActionAvailability.evaluate(.currentFindSelection, context: readyContext).isEnabled)
    }

    func testMobileActionAvailabilityAllowsLightweightChecksBeforeLLMAndRuntime() {
        let context = MobileRunContext(
            serverURLText: "http://192.168.1.10:8765",
            serverReachable: nil,
            selectedProjectID: "demo",
            llmProvider: "",
            llmBaseURLText: "",
            llmModel: "",
            runtimeConfiguration: .init()
        )

        XCTAssertTrue(ProjectActionAvailability.evaluate(.status, context: context).isEnabled)
        XCTAssertTrue(ProjectActionAvailability.evaluate(.healthcheck, context: context).isEnabled)

        let find = ProjectActionAvailability.evaluate(.find, context: context)
        XCTAssertFalse(find.isEnabled)
        XCTAssertEqual(find.reason, "Configure the Find LLM before starting Find.")

        let fullCycle = ProjectActionAvailability.evaluate(.fullCycle, context: context)
        XCTAssertFalse(fullCycle.isEnabled)
        XCTAssertEqual(fullCycle.reason, "3 setup items need attention")
    }

    func testMobileActionAvailabilityBlocksExpensiveWorkflowUntilRuntimeIsReady() {
        let context = MobileRunContext(
            serverURLText: "http://192.168.1.10:8765",
            serverReachable: true,
            selectedProjectID: "demo",
            llmProvider: "openai_compatible",
            llmBaseURLText: "https://api.example.com/v1",
            llmModel: "research-model",
            runtimeConfiguration: .init()
        )

        XCTAssertTrue(ProjectActionAvailability.evaluate(.find, context: context).isEnabled)

        let experiment = ProjectActionAvailability.evaluate(.experiment, context: context)
        XCTAssertFalse(experiment.isEnabled)
        XCTAssertEqual(experiment.reason, "Detect or save the remote runtime before starting this stage.")

        let readyContext = MobileRunContext(
            serverURLText: "http://192.168.1.10:8765",
            serverReachable: true,
            selectedProjectID: "demo",
            llmProvider: "openai_compatible",
            llmBaseURLText: "https://api.example.com/v1",
            llmModel: "research-model",
            runtimeConfiguration: .init(managementPython: "/srv/taste/.venv/bin/python")
        )

        XCTAssertTrue(ProjectActionAvailability.evaluate(.fullCycle, context: readyContext).isEnabled)
        XCTAssertTrue(ProjectActionAvailability.evaluate(.paper, context: readyContext).isEnabled)
    }

    func testMobileActionAvailabilityBlocksExpensiveWorkflowWhenCriticalRuntimeChecksFail() {
        let failingStatus = ProjectRuntimeStatus(
            project: "demo",
            runtime: .init(
                claudePath: "/missing/claude",
                managementPython: "/srv/taste/.venv/bin/python"
            ),
            checks: [
                "claude": .init(id: "claude", path: "/missing/claude", ok: false, version: "", reason: "claude_path does not exist"),
                "management_python": .init(id: "management_python", path: "/srv/taste/.venv/bin/python", ok: true, version: "Python 3.12.13", reason: "ok"),
            ],
            pathHead: []
        )
        let context = MobileRunContext(
            serverURLText: "http://192.168.1.10:8765",
            serverReachable: true,
            selectedProjectID: "demo",
            llmProvider: "openai_compatible",
            llmBaseURLText: "https://api.example.com/v1",
            llmModel: "research-model",
            runtimeConfiguration: failingStatus.runtime,
            runtimeStatus: failingStatus
        )

        let experiment = ProjectActionAvailability.evaluate(.experiment, context: context)
        XCTAssertFalse(experiment.isEnabled)
        XCTAssertEqual(experiment.reason, "Fix remote runtime checks before starting this stage: Claude.")

        let summary = context.readinessSummary
        XCTAssertFalse(summary.isReadyForFullWorkflow)
        XCTAssertEqual(summary.checks.first(where: { $0.id == "runtime" })?.detail, "Fix remote runtime checks: Claude.")
    }

    func testMobileReadinessSummarySurfacesRunBlockingSetupGaps() {
        let summary = MobileReadinessSummary(
            serverURLText: "localhost:8765",
            serverReachable: false,
            selectedProjectID: "",
            llmProvider: "openai_compatible",
            llmBaseURLText: "",
            llmModel: "",
            runtimeConfiguration: .init()
        )

        XCTAssertFalse(summary.isReadyForFullWorkflow)
        XCTAssertEqual(summary.blockedCount, 4)
        XCTAssertEqual(summary.statusLine, "4 setup items need attention")
        XCTAssertEqual(summary.checks.map(\.id), ["server", "project", "llm", "runtime", "storage"])
        XCTAssertEqual(summary.checks.first(where: { $0.id == "server" })?.state, .blocked)
        XCTAssertEqual(summary.checks.first(where: { $0.id == "storage" })?.state, .ready)
    }

    func testMobileReadinessSummaryExplainsLoopbackURLsForRealIPhones() {
        let summary = MobileReadinessSummary(
            serverURLText: "http://127.0.0.1:8765",
            serverReachable: nil,
            selectedProjectID: "demo_project",
            llmProvider: "openai_compatible",
            llmBaseURLText: "https://api.example.com/v1",
            llmModel: "research-model",
            runtimeConfiguration: .init(
                managementPython: "/srv/taste/.venv/bin/python"
            )
        )

        let server = summary.checks.first(where: { $0.id == "server" })

        XCTAssertEqual(summary.blockedCount, 1)
        XCTAssertEqual(server?.state, .blocked)
        XCTAssertEqual(
            server?.detail,
            "127.0.0.1/localhost only reaches this iPhone. Use your computer's LAN IP, VPN, or tunnel URL, then tap Test Connection."
        )
    }

    func testMobileReadinessSummaryBlocksCloudProfilesUsingPlainHTTP() {
        let context = MobileRunContext(
            serverURLText: "http://taste.example.com",
            connectionKind: .cloud,
            serverReachable: true,
            selectedProjectID: "demo_project",
            llmProvider: "openai_compatible",
            llmBaseURLText: "https://api.example.com/v1",
            llmModel: "research-model",
            runtimeConfiguration: .init(
                managementPython: "/srv/taste/.venv/bin/python"
            )
        )

        let server = context.readinessSummary.checks.first(where: { $0.id == "server" })
        let fullCycle = ProjectActionAvailability.evaluate(.fullCycle, context: context)

        XCTAssertEqual(server?.state, .blocked)
        XCTAssertEqual(
            server?.detail,
            "Cloud connections must use https:// or an authenticated tunnel so the server access token is not sent in clear text."
        )
        XCTAssertFalse(fullCycle.isEnabled)
        XCTAssertEqual(fullCycle.reason, "1 setup item need attention")
    }

    func testMobileReadinessSummaryBlocksServersWithoutMobileControlPlaneMeta() {
        let legacyMeta = try! JSONDecoder().decode(TASTEServerMeta.self, from: #"{"saved":true}"#.data(using: .utf8)!)
        let context = MobileRunContext(
            serverURLText: "https://taste.example.com",
            connectionKind: .cloud,
            serverReachable: true,
            serverMeta: legacyMeta,
            selectedProjectID: "demo_project",
            llmProvider: "openai_compatible",
            llmBaseURLText: "https://api.example.com/v1",
            llmModel: "research-model",
            runtimeConfiguration: .init(
                managementPython: "/srv/taste/.venv/bin/python"
            )
        )

        let server = context.readinessSummary.checks.first(where: { $0.id == "server" })
        let fullCycle = ProjectActionAvailability.evaluate(.fullCycle, context: context)

        XCTAssertEqual(server?.state, .blocked)
        XCTAssertEqual(
            server?.detail,
            "Connected, but this TASTE server does not advertise the mobile control-plane API. Update branch-app, restart scripts/start_web.sh, then tap Test Connection."
        )
        XCTAssertFalse(fullCycle.isEnabled)
        XCTAssertEqual(fullCycle.reason, "1 setup item need attention")
    }

    func testMobileReadinessSummaryPassesWhenRemoteControlPlaneIsConfigured() {
        let summary = MobileReadinessSummary(
            serverURLText: "http://192.168.1.10:8765",
            serverReachable: true,
            selectedProjectID: "demo_project",
            llmProvider: "openai_compatible",
            llmBaseURLText: "https://api.example.com/v1",
            llmModel: "research-model",
            runtimeConfiguration: .init(
                condaEnv: "taste",
                claudePath: "/Users/me/.local/bin/claude",
                managementPython: "/srv/taste/.venv/bin/python"
            )
        )

        XCTAssertTrue(summary.isReadyForFullWorkflow)
        XCTAssertEqual(summary.blockedCount, 0)
        XCTAssertEqual(summary.readyCount, 5)
        XCTAssertEqual(summary.statusLine, "Ready to run remote TASTE workflows")
    }
}

private final class RuntimeStatusAuthURLProtocol: URLProtocol {
    static let recorder = RuntimeStatusRequestRecorder()

    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        Self.recorder.record(request)
        let body = """
        {"project":"demo","runtime":{},"checks":{},"path_head":[]}
        """.data(using: .utf8)!
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: 200,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

private final class RuntimeStatusRequestRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var path = ""
    private var authorization: String?

    var snapshot: (path: String, authorization: String?) {
        lock.lock()
        defer { lock.unlock() }
        return (path, authorization)
    }

    func reset() {
        lock.lock()
        path = ""
        authorization = nil
        lock.unlock()
    }

    func record(_ request: URLRequest) {
        lock.lock()
        path = request.url?.path ?? ""
        authorization = request.value(forHTTPHeaderField: "Authorization")
        lock.unlock()
    }
}

private final class ProjectConfigURLProtocol: URLProtocol {
    static let recorder = ProjectConfigRequestRecorder()

    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        Self.recorder.record(request)
        let body = """
        {
          "project": "demo",
          "path": "/srv/taste/projects/demo",
          "config": {"name": "demo", "topic": "AI research"},
          "run_preferences": {
            "research_interest": "AI agents for academic research automation",
            "researcher_profile": "Prefer reproducible systems",
            "target_venue": "ICLR",
            "venue": "ICLR",
            "title": "Mobile TASTE"
          }
        }
        """.data(using: .utf8)!
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: 200,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

private final class ProjectConfigRequestRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var path = ""
    private var method = ""
    private var authorization: String?
    private var body = ""

    var snapshot: (path: String, method: String, authorization: String?, body: String) {
        lock.lock()
        defer { lock.unlock() }
        return (path, method, authorization, body)
    }

    func reset() {
        lock.lock()
        path = ""
        method = ""
        authorization = nil
        body = ""
        lock.unlock()
    }

    func record(_ request: URLRequest) {
        lock.lock()
        path = request.url?.path ?? ""
        method = request.httpMethod ?? ""
        authorization = request.value(forHTTPHeaderField: "Authorization")
        if let data = request.httpBody {
            body = String(decoding: data, as: UTF8.self)
        } else if let stream = request.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var data = Data()
            let bufferSize = 4096
            let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: bufferSize)
            defer { buffer.deallocate() }
            while stream.hasBytesAvailable {
                let count = stream.read(buffer, maxLength: bufferSize)
                if count <= 0 { break }
                data.append(buffer, count: count)
            }
            body = String(decoding: data, as: UTF8.self)
        } else {
            body = ""
        }
        lock.unlock()
    }
}

private final class ClaudeLatestResponseURLProtocol: URLProtocol {
    static let recorder = ClaudeLatestResponseRequestRecorder()

    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        Self.recorder.record(request)
        let body = """
        {
          "status": "completed",
          "stage": "paper",
          "requested_stage": "paper",
          "return_code": 0,
          "source": "state/claude_project_session_last_result_paper.json",
          "response_markdown": "Claude project agent response",
          "response_chcount": 42000,
          "returned_chcount": 29,
          "truncated": true,
          "truncated_head_chars": 41971,
          "full_response_available": true
        }
        """.data(using: .utf8)!
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: 200,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

private final class ClaudeLatestResponseRequestRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var path = ""
    private var query: [String: String] = [:]
    private var authorization: String?

    var snapshot: (path: String, query: [String: String], authorization: String?) {
        lock.lock()
        defer { lock.unlock() }
        return (path, query, authorization)
    }

    func reset() {
        lock.lock()
        path = ""
        query = [:]
        authorization = nil
        lock.unlock()
    }

    func record(_ request: URLRequest) {
        lock.lock()
        let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)
        path = components?.path ?? ""
        query = Dictionary(uniqueKeysWithValues: (components?.queryItems ?? []).map { ($0.name, $0.value ?? "") })
        authorization = request.value(forHTTPHeaderField: "Authorization")
        lock.unlock()
    }
}

private final class ArtifactPreviewURLProtocol: URLProtocol {
    static let recorder = ArtifactPreviewRequestRecorder()

    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        Self.recorder.record(request)
        let body = "preview".data(using: .utf8)!
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: 200,
            httpVersion: "HTTP/1.1",
            headerFields: [
                "Content-Type": "text/plain",
                "Content-Length": "\(body.count)",
            ]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

private final class ArtifactPreviewRequestRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var path = ""
    private var authorization: String?

    var snapshot: (path: String, authorization: String?) {
        lock.lock()
        defer { lock.unlock() }
        return (path, authorization)
    }

    func reset() {
        lock.lock()
        path = ""
        authorization = nil
        lock.unlock()
    }

    func record(_ request: URLRequest) {
        lock.lock()
        path = request.url?.path ?? ""
        authorization = request.value(forHTTPHeaderField: "Authorization")
        lock.unlock()
    }
}
