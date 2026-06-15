import AVFoundation
import QuickLook
import SwiftUI

struct RootView: View {
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        TabView {
            DashboardView()
                .tabItem { Label("Dashboard", systemImage: "gauge.with.dots.needle.67percent") }
            ActionsView()
                .tabItem { Label("Run", systemImage: "play.circle") }
            SettingsView()
                .tabItem { Label("Settings", systemImage: "gearshape") }
        }
        .tint(.tasteBlue)
        .quickLookPreview($model.artifactPreviewURL)
        .task {
            await model.runAutoRefreshLoop()
        }
    }
}

struct DashboardView: View {
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    StatusHero(summary: model.progressSummary)
                    ProjectPicker()
                    WorkflowStrip(summary: model.progressSummary, stageSnapshots: model.stageSnapshots)
                    AttentionPanel(items: model.attentionItems)
                    ClaudeResponsePanel()
                    RemoteArtifactsPanel(artifacts: model.remoteArtifacts)
                    JobList(jobs: model.jobs)
                }
                .padding()
            }
            .background(Color.tasteBackground)
            .navigationTitle("TASTE")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Task { await model.refresh() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .accessibilityLabel("Refresh TASTE status")
                    .disabled(model.isLoading)
                }
            }
            .safeAreaInset(edge: .bottom) {
                FooterStatus()
            }
            .sheet(item: $model.latestClaudeResponse) { response in
                ClaudeResponseSheet(response: response)
            }
        }
    }
}

struct StatusHero: View {
    let summary: WorkflowProgressSummary

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text(summary.activeStage?.displayName ?? "No active stage")
                        .font(.title2.weight(.heavy))
                    Text(summary.statusLine)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .lineLimit(3)
                }
                Spacer()
                Text("\(summary.percentComplete)%")
                    .font(.title.weight(.heavy))
                    .monospacedDigit()
                    .foregroundStyle(Color.tasteBlue)
            }
            ProgressView(value: Double(summary.percentComplete), total: 100)
                .tint(.tasteBlue)
            Text("Completed stages: \(summary.completedStageCount)/\(WorkflowStage.allCases.count)")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(.secondary)
        }
        .padding()
        .background(Color.white)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

struct ProjectPicker: View {
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Research Project")
                .font(.headline)
            if model.projects.isEmpty {
                Text("No project loaded. Start TASTE Web on your computer or server, then refresh.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                Picker("Project", selection: $model.selectedProjectID) {
                    ForEach(model.projects) { project in
                        Text(project.name.isEmpty ? project.id : project.name).tag(project.id)
                    }
                }
                .pickerStyle(.menu)
                .onChange(of: model.selectedProjectID) { _, _ in
                    Task { await model.refresh() }
                }
            }
        }
        .padding()
        .background(Color.white)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

struct WorkflowStrip: View {
    let summary: WorkflowProgressSummary
    let stageSnapshots: [WorkflowStage: StageSnapshot]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Workflow")
                .font(.headline)
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 92), spacing: 8)], spacing: 8) {
                ForEach(WorkflowStage.allCases, id: \.self) { stage in
                    let active = stage == summary.activeStage
                    let status = stageSnapshots[stage]?.mobileStatusLabel
                    VStack(spacing: 8) {
                        Image(systemName: stage.symbolName)
                            .font(.title3.weight(.bold))
                        Text(stage.displayName)
                            .font(.caption.weight(.bold))
                        if let status, !status.isEmpty {
                            Text(status)
                                .font(.caption2.weight(.semibold))
                                .lineLimit(2)
                                .multilineTextAlignment(.center)
                                .minimumScaleFactor(0.8)
                                .foregroundStyle(active ? Color.white.opacity(0.88) : Color.secondary)
                        }
                    }
                    .frame(maxWidth: .infinity, minHeight: 96)
                    .foregroundStyle(active ? Color.white : Color.tasteText)
                    .background(active ? Color.tasteBlue : Color.white)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("\(stage.displayName) stage\(active ? ", active" : "")")
                }
            }
        }
    }
}

struct AttentionPanel: View {
    let items: [ProjectAttentionItem]

    var body: some View {
        if !items.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                Text("Needs Attention")
                    .font(.headline)
                ForEach(items) { item in
                    HStack(alignment: .top, spacing: 12) {
                        Image(systemName: symbolName(for: item.kind))
                            .font(.body.weight(.bold))
                            .frame(width: 28, height: 28)
                            .foregroundStyle(tintColor(for: item.kind))
                        VStack(alignment: .leading, spacing: 4) {
                            Text(item.title)
                                .font(.subheadline.weight(.heavy))
                                .foregroundStyle(Color.tasteText)
                                .lineLimit(2)
                            if !item.detail.isEmpty {
                                Text(item.detail)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(3)
                            }
                        }
                        Spacer(minLength: 0)
                    }
                    .padding()
                    .background(Color.white)
                    .overlay {
                        RoundedRectangle(cornerRadius: 8)
                            .stroke(tintColor(for: item.kind).opacity(0.2), lineWidth: 1)
                    }
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel(accessibilityLabel(for: item))
                }
            }
        }
    }

    private func symbolName(for kind: ProjectAttentionKind) -> String {
        switch kind {
        case .blocker:
            return "exclamationmark.triangle"
        case .nextAction:
            return "arrow.right.circle"
        }
    }

    private func tintColor(for kind: ProjectAttentionKind) -> Color {
        switch kind {
        case .blocker:
            return .tasteOrange
        case .nextAction:
            return .tasteBlue
        }
    }

    private func accessibilityLabel(for item: ProjectAttentionItem) -> String {
        let prefix = item.kind == .blocker ? "Blocker" : "Next action"
        return item.detail.isEmpty ? "\(prefix): \(item.title)" : "\(prefix): \(item.title). \(item.detail)"
    }
}

struct RemoteArtifactsPanel: View {
    let artifacts: [RemoteArtifact]
    @EnvironmentObject private var model: AppViewModel

    private var plans: [RemoteArtifactOpenPlan] {
        artifacts.compactMap { model.remoteOpenPlan(for: $0) }
    }

    var body: some View {
        if !plans.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                Text("Remote Artifacts")
                    .font(.headline)
                ForEach(plans, id: \.artifact.id) { plan in
                    artifactCard(for: plan)
                }
            }
        }
    }

    @ViewBuilder
    private func artifactCard(for plan: RemoteArtifactOpenPlan) -> some View {
        if plan.canOpenExternally, let url = plan.url {
            Link(destination: url) {
                artifactContent(for: plan, subtitle: subtitle(for: plan), trailingIcon: "arrow.up.forward")
            }
            .accessibilityLabel("\(plan.artifact.title), remote artifact")
        } else {
            Button {
                Task { await model.openRemoteArtifact(plan.artifact) }
            } label: {
                artifactContent(
                    for: plan,
                    subtitle: subtitle(for: plan),
                    trailingIcon: "lock.open",
                    isBusy: model.isOpeningArtifact
                )
            }
            .buttonStyle(.plain)
            .disabled(model.isOpeningArtifact)
            .accessibilityLabel("\(plan.artifact.title), authenticated remote artifact")
            .accessibilityHint(plan.note)
        }
    }

    private func artifactContent(
        for plan: RemoteArtifactOpenPlan,
        subtitle: String,
        trailingIcon: String,
        isBusy: Bool = false
    ) -> some View {
        HStack(spacing: 12) {
            Image(systemName: symbolName(for: plan.artifact.kind))
                .font(.body.weight(.bold))
                .frame(width: 28, height: 28)
                .foregroundStyle(Color.tasteBlue)
            VStack(alignment: .leading, spacing: 2) {
                Text(plan.artifact.title)
                    .font(.subheadline.weight(.heavy))
                    .foregroundStyle(Color.tasteText)
                    .lineLimit(2)
                Text(subtitle)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer(minLength: 0)
            if isBusy {
                ProgressView()
                    .controlSize(.small)
            } else {
                Image(systemName: trailingIcon)
                    .font(.caption.weight(.heavy))
                    .foregroundStyle(.secondary)
            }
        }
        .padding()
        .background(Color.white)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func subtitle(for plan: RemoteArtifactOpenPlan) -> String {
        if plan.mode == .authenticatedPreview {
            return "Authenticated preview"
        }
        guard let url = plan.url else { return "" }
        return url.host ?? url.absoluteString
    }

    private func symbolName(for kind: ArtifactKind) -> String {
        switch kind {
        case .paperPDF:
            return "doc.richtext"
        case .texSource:
            return "curlybraces.square"
        case .markdownSummary, .projectSummary:
            return "doc.text"
        case .jobList:
            return "list.bullet.rectangle"
        case .dataset:
            return "externaldrive"
        case .repositoryCheckout:
            return "shippingbox"
        }
    }
}

private struct ClaudeResponseButtonSpec: Identifiable {
    let id: String
    let title: String
    let icon: String
    let stage: WorkflowStage?

    static let all: [ClaudeResponseButtonSpec] = [
        .init(id: "latest", title: "Latest", icon: "text.bubble", stage: nil),
        .init(id: "environment", title: "Environment", icon: WorkflowStage.environment.symbolName, stage: .environment),
        .init(id: "experiment", title: "Experiment", icon: WorkflowStage.experiment.symbolName, stage: .experiment),
        .init(id: "paper", title: "Paper", icon: WorkflowStage.paper.symbolName, stage: .paper),
    ]
}

struct ClaudeResponsePanel: View {
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Claude Response")
                    .font(.headline)
                Spacer()
                if model.isLoadingClaudeResponse {
                    ProgressView()
                        .controlSize(.small)
                }
            }
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 132), spacing: 8)], spacing: 8) {
                ForEach(ClaudeResponseButtonSpec.all) { item in
                    Button {
                        Task { await model.loadClaudeLatestResponse(stage: item.stage) }
                    } label: {
                        Label(item.title, systemImage: item.icon)
                            .font(.subheadline.weight(.semibold))
                            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    }
                    .buttonStyle(.bordered)
                    .disabled(model.isLoadingClaudeResponse || (model.selectedProject?.id ?? model.selectedProjectID).isEmpty)
                    .accessibilityLabel("Load Claude \(item.title) response")
                }
            }
        }
    }
}

struct ClaudeResponseSheet: View {
    let response: MobileClaudeResponse

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            Label(response.displayStage.isEmpty ? "Latest" : response.displayStage.capitalized, systemImage: "text.bubble")
                                .font(.headline)
                            Spacer()
                            if response.truncated {
                                Label("Tail", systemImage: "scissors")
                                    .font(.caption.weight(.bold))
                                    .foregroundStyle(Color.tasteOrange)
                            }
                        }
                        if response.returnedCharacterCount > 0 || response.responseCharacterCount > 0 {
                            Text("\(response.returnedCharacterCount)/\(response.responseCharacterCount) characters")
                                .font(.caption.monospacedDigit().weight(.semibold))
                                .foregroundStyle(.secondary)
                        }
                        if !response.source.isEmpty {
                            Text(response.source)
                                .font(.caption2.monospaced())
                                .foregroundStyle(.secondary)
                                .lineLimit(2)
                        }
                    }
                    .padding()
                    .background(Color.white)
                    .clipShape(RoundedRectangle(cornerRadius: 8))

                    Text(response.responseMarkdown.isEmpty ? "No response returned." : response.responseMarkdown)
                        .font(.footnote.monospaced())
                        .foregroundStyle(Color.tasteText)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding()
                        .background(Color.white)
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                }
                .padding()
            }
            .background(Color.tasteBackground)
            .navigationTitle("Claude Response")
            .navigationBarTitleDisplayMode(.inline)
        }
        .presentationDetents([.medium, .large])
    }
}

struct JobList: View {
    let jobs: [TASTEJob]
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Live and Recent Jobs")
                .font(.headline)
            if jobs.isEmpty {
                Text("No jobs yet.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .padding()
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.white)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
            } else {
                ForEach(jobs.prefix(8)) { job in
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            Label(job.stage.displayName, systemImage: job.stage.symbolName)
                                .font(.subheadline.weight(.heavy))
                            Spacer()
                            Text(job.status.rawValue.replacingOccurrences(of: "_", with: " "))
                                .font(.caption.weight(.bold))
                                .padding(.horizontal, 8)
                                .padding(.vertical, 4)
                                .background(job.status.isLive ? Color.tasteOrange.opacity(0.16) : Color.tasteBlue.opacity(0.12))
                                .clipShape(Capsule())
                        }
                        if job.cancelRequested {
                            Label("Cancel requested", systemImage: "stop.circle")
                                .font(.caption)
                                .foregroundStyle(Color.tasteOrange)
                        }
                        if let progress = job.progress {
                            Text(progress.message.isEmpty ? progress.phase : progress.message)
                                .font(.callout)
                                .foregroundStyle(.secondary)
                            if progress.total > 0 {
                                ProgressView(value: Double(progress.percent), total: 100)
                                    .tint(.tasteOrange)
                            }
                        }
                        HStack {
                            Button {
                                Task { await model.inspect(job) }
                            } label: {
                                Label("Details", systemImage: "doc.text.magnifyingglass")
                            }
                            .disabled(model.isInspectingJob)
                            if job.status.isLive {
                                Button(role: .destructive) {
                                    Task { await model.cancel(job) }
                                } label: {
                                    Label("Cancel Job", systemImage: "stop.fill")
                                }
                                .disabled(model.isLoading || job.cancelRequested)
                            }
                        }
                    }
                    .padding()
                    .background(Color.white)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                }
            }
        }
        .sheet(item: $model.inspectedJobDetail) { detail in
            JobDetailSheet(detail: detail, isLoading: model.isInspectingJob)
        }
    }
}

struct JobDetailSheet: View {
    let detail: MobileJobDetailSummary
    let isLoading: Bool

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    VStack(alignment: .leading, spacing: 10) {
                        HStack {
                            Text(detail.statusLine)
                                .font(.headline)
                                .foregroundStyle(Color.tasteText)
                            Spacer()
                            if isLoading {
                                ProgressView()
                                    .controlSize(.small)
                            }
                        }
                        if detail.percentComplete > 0 {
                            ProgressView(value: Double(detail.percentComplete), total: 100)
                                .tint(.tasteOrange)
                        }
                        if !detail.progressLabel.isEmpty {
                            Text(detail.progressLabel)
                                .font(.caption.weight(.bold))
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding()
                    .background(Color.white)
                    .clipShape(RoundedRectangle(cornerRadius: 8))

                    VStack(alignment: .leading, spacing: 10) {
                        Text("Log Tail")
                            .font(.headline)
                        if detail.logTail.isEmpty {
                            Text("No log lines returned.")
                                .font(.callout)
                                .foregroundStyle(.secondary)
                                .padding()
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .background(Color.white)
                                .clipShape(RoundedRectangle(cornerRadius: 8))
                        } else {
                            VStack(alignment: .leading, spacing: 6) {
                                ForEach(Array(detail.logTail.enumerated()), id: \.offset) { _, line in
                                    Text(line)
                                        .font(.caption.monospaced())
                                        .foregroundStyle(Color.tasteText)
                                        .textSelection(.enabled)
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                }
                            }
                            .padding()
                            .background(Color.white)
                            .clipShape(RoundedRectangle(cornerRadius: 8))
                        }
                    }
                }
                .padding()
            }
            .background(Color.tasteBackground)
            .navigationTitle(detail.title)
            .navigationBarTitleDisplayMode(.inline)
        }
        .presentationDetents([.medium, .large])
    }
}

struct ActionsView: View {
    @EnvironmentObject private var model: AppViewModel
    @State private var pendingLaunch: PendingProjectActionLaunch?

    var body: some View {
        NavigationStack {
            Form {
                ReadinessSection(summary: model.readinessSummary)
                Section("Run Context") {
                    LabeledMobileTextField(fieldID: .researchTopic, text: $model.topic, axis: .vertical, disableAutocapitalization: false)
                    LabeledMobileTextField(fieldID: .researchInterest, text: $model.researchInterest, axis: .vertical, disableAutocapitalization: false)
                    LabeledMobileTextField(fieldID: .researcherProfile, text: $model.researcherProfile, axis: .vertical, disableAutocapitalization: false)
                    LabeledMobileTextField(fieldID: .targetVenue, text: $model.venue, disableAutocapitalization: false)
                    LabeledMobileTextField(fieldID: .paperTitle, text: $model.paperTitle, disableAutocapitalization: false)
                    Button {
                        Task { await model.saveProjectResearchPreferences() }
                    } label: {
                        Label("Sync Project Profile", systemImage: "person.text.rectangle")
                    }
                    .disabled(model.isLoading)
                }
                Section("Server Checks") {
                    ActionButton(title: ProjectAction.status.mobileRunTitle, icon: "checkmark.seal", availability: model.availability(for: .status)) {
                        requestStart(.status)
                    }
                    ActionButton(title: ProjectAction.healthcheck.mobileRunTitle, icon: "heart.text.square", availability: model.availability(for: .healthcheck)) {
                        requestStart(.healthcheck)
                    }
                }
                Section("Workflow Actions") {
                    ActionButton(title: ProjectAction.fullCycle.mobileRunTitle, icon: "play.fill", availability: model.availability(for: .fullCycle)) {
                        requestStart(.fullCycle, options: [.useExistingLiteraturePacket: true])
                    }
                    ActionButton(title: ProjectAction.find.mobileRunTitle, icon: WorkflowStage.find.symbolName, availability: model.availability(for: .find)) {
                        requestStart(.find)
                    }
                    ActionButton(title: ProjectAction.read.mobileRunTitle, icon: WorkflowStage.read.symbolName, availability: model.availability(for: .read)) {
                        requestStart(.read)
                    }
                    ActionButton(title: ProjectAction.idea.mobileRunTitle, icon: WorkflowStage.idea.symbolName, availability: model.availability(for: .idea)) {
                        requestStart(.idea)
                    }
                    ActionButton(title: ProjectAction.plan.mobileRunTitle, icon: WorkflowStage.plan.symbolName, availability: model.availability(for: .plan)) {
                        requestStart(.plan)
                    }
                    ActionButton(title: ProjectAction.currentFindSelection.mobileRunTitle, icon: "checklist.checked", availability: model.availability(for: .currentFindSelection)) {
                        requestStart(.currentFindSelection)
                    }
                    ActionButton(title: ProjectAction.environment.mobileRunTitle, icon: WorkflowStage.environment.symbolName, availability: model.availability(for: .environment)) {
                        requestStart(.environment, options: [.realBootstrapEnv: true])
                    }
                    ActionButton(title: ProjectAction.experiment.mobileRunTitle, icon: WorkflowStage.experiment.symbolName, availability: model.availability(for: .experiment)) {
                        requestStart(.experiment)
                    }
                    ActionButton(title: ProjectAction.paper.mobileRunTitle, icon: WorkflowStage.paper.symbolName, availability: model.availability(for: .paper)) {
                        requestStart(.paper, options: [.autoInstallLatex: true])
                    }
                }
                Section("Phone Load Policy") {
                    Label("Runs on your TASTE computer/server", systemImage: "desktopcomputer")
                    Label("Phone stores only settings and compact job summaries", systemImage: "internaldrive")
                    Label("PDFs, datasets, repos, and experiment logs stay remote", systemImage: "icloud")
                }
            }
            .navigationTitle("Run")
            .safeAreaInset(edge: .bottom) {
                FooterStatus()
            }
            .confirmationDialog(
                "Run on TASTE Server",
                isPresented: Binding(
                    get: { pendingLaunch != nil },
                    set: { isPresented in
                        if !isPresented { pendingLaunch = nil }
                    }
                ),
                titleVisibility: .visible,
                presenting: pendingLaunch
            ) { launch in
                Button("Start \(launch.action.mobileRunTitle)") {
                    pendingLaunch = nil
                    Task { await model.start(launch.action, options: launch.options) }
                }
                Button("Cancel", role: .cancel) {
                    pendingLaunch = nil
                }
            } message: { launch in
                Text(launch.action.mobileConfirmationMessage)
            }
        }
    }

    private func requestStart(_ action: ProjectAction, options: [ProjectActionOption: Bool] = [:]) {
        if action.requiresMobileConfirmation {
            pendingLaunch = .init(action: action, options: options)
        } else {
            Task { await model.start(action, options: options) }
        }
    }
}

struct ActionButton: View {
    let title: String
    let icon: String
    let availability: ProjectActionAvailability
    let action: () -> Void
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Button {
                action()
            } label: {
                Label(title, systemImage: icon)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .disabled(model.isLoading || !availability.isEnabled)
            .accessibilityLabel(title)
            .accessibilityHint(availability.isEnabled ? availability.reason : "Unavailable. \(availability.reason)")

            if !availability.isEnabled {
                Text(availability.reason)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .opacity(availability.isEnabled ? 1 : 0.72)
    }
}

private struct PendingProjectActionLaunch: Identifiable {
    let action: ProjectAction
    let options: [ProjectActionOption: Bool]

    var id: String {
        let optionText = options
            .sorted { $0.key.rawValue < $1.key.rawValue }
            .map { "\($0.key.rawValue)=\($0.value)" }
            .joined(separator: ",")
        return "\(action.rawValue):\(optionText)"
    }
}

struct SettingsView: View {
    @EnvironmentObject private var model: AppViewModel
    @State private var showingConnectionQRScanner = false

    var body: some View {
        NavigationStack {
            Form {
                ReadinessSection(summary: model.readinessSummary)
                Section("Project") {
                    LabeledMobileTextField(fieldID: .newProjectID, text: $model.newProjectID)
                    LabeledMobileTextField(fieldID: .researchTopic, text: $model.topic, axis: .vertical, disableAutocapitalization: false)
                    LabeledMobileTextField(fieldID: .researchInterest, text: $model.researchInterest, axis: .vertical, disableAutocapitalization: false)
                    LabeledMobileTextField(fieldID: .researcherProfile, text: $model.researcherProfile, axis: .vertical, disableAutocapitalization: false)
                    LabeledMobileTextField(fieldID: .targetVenue, text: $model.venue, disableAutocapitalization: false)
                    LabeledMobileTextField(fieldID: .paperTitle, text: $model.paperTitle, disableAutocapitalization: false)
                    Button {
                        Task { await model.createProject() }
                    } label: {
                        Label("Create on TASTE Server", systemImage: "plus")
                    }
                    .disabled(model.isLoading)
                    Button {
                        Task { await model.saveProjectResearchPreferences() }
                    } label: {
                        Label("Sync Project Profile", systemImage: "person.text.rectangle")
                    }
                    .disabled(model.isLoading)
                }
                Section("TASTE Server") {
                    Button {
                        model.startNewConnectionProfile()
                    } label: {
                        Label("New Connection", systemImage: "plus.circle")
                    }
                    .disabled(model.isLoading)
                    if model.isEditingNewConnectionProfile {
                        Label("New connection draft", systemImage: "plus.circle.fill")
                            .font(.footnote.weight(.semibold))
                            .foregroundStyle(Color.tasteBlue)
                    }
                    if !model.connectionProfiles.isEmpty {
                        Picker("Connection", selection: $model.selectedConnectionProfileID) {
                            ForEach(model.connectionProfiles) { profile in
                                Label(profile.name, systemImage: profile.kind.symbolName)
                                    .tag(profile.id)
                            }
                        }
                        .pickerStyle(.menu)
                        .onChange(of: model.selectedConnectionProfileID) { _, newValue in
                            model.selectConnectionProfile(newValue)
                        }
                    }
                    LabeledMobileTextField(fieldID: .connectionProfileName, text: $model.connectionProfileName)
                    Picker("Target type", selection: $model.connectionProfileKind) {
                        ForEach(ServerConnectionKind.allCases, id: \.self) { kind in
                            Label(kind.displayName, systemImage: kind.symbolName)
                                .tag(kind)
                        }
                    }
                    .pickerStyle(.segmented)
                    LabeledMobileTextField(fieldID: .serverURL, text: $model.serverURLText, useURLKeyboard: true)
                    LabeledMobileSecureField(fieldID: .serverAccessToken, text: $model.serverAccessToken)
                    LabeledMobileTextField(fieldID: .connectionLink, text: $model.connectionLinkText, axis: .vertical, useURLKeyboard: true)
                    Button {
                        Task { await model.importPastedConnectionLink() }
                    } label: {
                        Label(
                            MobileConnectionImportAction.pastedText.buttonTitle,
                            systemImage: MobileConnectionImportAction.pastedText.systemImage
                        )
                    }
                    .accessibilityLabel(MobileConnectionImportAction.pastedText.accessibilityLabel)
                    .disabled(model.isLoading || model.connectionLinkText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    Button {
                        Task { await model.importClipboardConnectionLink() }
                    } label: {
                        Label(
                            MobileConnectionImportAction.clipboard.buttonTitle,
                            systemImage: MobileConnectionImportAction.clipboard.systemImage
                        )
                    }
                    .accessibilityLabel(MobileConnectionImportAction.clipboard.accessibilityLabel)
                    .disabled(model.isLoading)
                    Button {
                        showingConnectionQRScanner = true
                    } label: {
                        Label(
                            MobileConnectionImportAction.qrCode.buttonTitle,
                            systemImage: MobileConnectionImportAction.qrCode.systemImage
                        )
                    }
                    .accessibilityLabel(MobileConnectionImportAction.qrCode.accessibilityLabel)
                    .disabled(model.isLoading)
                    Button {
                        model.saveCurrentConnectionProfile()
                    } label: {
                        Label(model.isEditingNewConnectionProfile ? "Save New Connection" : "Save Connection Profile", systemImage: "tray.and.arrow.down")
                    }
                    .disabled(model.isLoading)
                    Button {
                        Task { await model.testConnection() }
                    } label: {
                        Label("Test Connection", systemImage: "network")
                    }
                    .disabled(model.isLoading)
                    Button {
                        Task { await model.refresh() }
                    } label: {
                        Label("Refresh Projects", systemImage: "arrow.clockwise")
                    }
                    .disabled(model.isLoading)
                }
                Section("Find LLM API") {
                    LabeledMobileTextField(fieldID: .llmProvider, text: $model.llmProvider)
                    LabeledMobileTextField(fieldID: .llmBaseURL, text: $model.llmBaseURLText, useURLKeyboard: true)
                    LabeledMobileTextField(fieldID: .llmModel, text: $model.llmModel)
                    LabeledMobileSecureField(fieldID: .llmAPIKey, text: $model.apiKey)
                    if model.llmAPIKeySaved {
                        Label(model.llmAPIKeySuffix.isEmpty ? "Server has a saved key" : "Server saved key ending \(model.llmAPIKeySuffix)", systemImage: "key.horizontal.fill")
                            .font(.footnote)
                            .foregroundStyle(Color.tasteGreen)
                    }
                    if model.projectLLMSynced {
                        Label("Project LLM config is synced", systemImage: "checkmark.seal")
                            .font(.footnote)
                            .foregroundStyle(Color.tasteGreen)
                    }
                    Button {
                        Task { await model.loadLLMConfiguration() }
                    } label: {
                        Label("Load Find LLM Config", systemImage: "arrow.down.doc")
                    }
                    .disabled(model.isLoading)
                    Button {
                        Task { await model.syncLLMConfiguration() }
                    } label: {
                        Label("Sync Find LLM Config to TASTE", systemImage: "key")
                    }
                    .disabled(model.isLoading)
                    Button {
                        Task { await model.probeLLMConfiguration() }
                    } label: {
                        Label("Probe LLM", systemImage: "waveform.path.ecg")
                    }
                    .disabled(model.isLoading)
                }
                Section("Remote Runtime") {
                    LabeledMobileTextField(fieldID: .claudePath, text: $model.claudePath)
                    LabeledMobileTextField(fieldID: .managementPython, text: $model.managementPython)
                    LabeledMobileTextField(fieldID: .nodeBin, text: $model.nodeBin)
                    LabeledMobileTextField(fieldID: .condaEnv, text: $model.condaEnv)
                    LabeledMobileTextField(fieldID: .condaBase, text: $model.condaBase)
                    LabeledMobileTextField(fieldID: .experimentPython, text: $model.experimentPython)
                    LabeledMobileTextField(fieldID: .extraPathEntries, text: $model.extraPathText, axis: .vertical)
                    RuntimeDiagnosticsPanel(status: model.runtimeStatus)
                    Button {
                        Task { await model.loadRuntimeConfiguration() }
                    } label: {
                        Label("Load Runtime", systemImage: "arrow.down.doc")
                    }
                    .disabled(model.isLoading)
                    Button {
                        Task { await model.detectRuntimeConfiguration() }
                    } label: {
                        Label("Detect and Load Runtime", systemImage: "scope")
                    }
                    .disabled(model.isLoading)
                    Button {
                        Task { await model.saveRuntimeConfiguration() }
                    } label: {
                        Label("Save Runtime", systemImage: "externaldrive.connected.to.line.below")
                    }
                    .disabled(model.isLoading)
                }
                Section("Storage") {
                    Text("Local cache budget: \(MobileStoragePolicy.default.maxCachedBytes / 1024 / 1024) MB")
                    Text("Large artifacts stay on the server and open through TASTE links.")
                }
            }
            .navigationTitle("Settings")
            .safeAreaInset(edge: .bottom) {
                FooterStatus()
            }
        }
        .sheet(isPresented: $showingConnectionQRScanner) {
            ConnectionQRScannerSheet { code in
                showingConnectionQRScanner = false
                Task { await model.importScannedConnectionCode(code) }
            }
        }
    }
}

struct ConnectionQRScannerSheet: View {
    @Environment(\.dismiss) private var dismiss
    let onCode: (String) -> Void

    var body: some View {
        NavigationStack {
            ConnectionQRScannerView { code in
                onCode(code)
                dismiss()
            }
            .navigationTitle(MobileConnectionImportAction.qrCode.buttonTitle)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") {
                        dismiss()
                    }
                }
            }
        }
    }
}

struct ConnectionQRScannerView: UIViewControllerRepresentable {
    let onCode: (String) -> Void

    func makeUIViewController(context: Context) -> ConnectionQRScannerViewController {
        let controller = ConnectionQRScannerViewController()
        controller.onCode = { code in
            context.coordinator.handle(code)
        }
        return controller
    }

    func updateUIViewController(_ uiViewController: ConnectionQRScannerViewController, context: Context) {}

    func makeCoordinator() -> Coordinator {
        Coordinator(onCode: onCode)
    }

    final class Coordinator {
        private let onCode: (String) -> Void
        private var hasScanned = false

        init(onCode: @escaping (String) -> Void) {
            self.onCode = onCode
        }

        func handle(_ code: String) {
            guard !hasScanned else { return }
            hasScanned = true
            onCode(code)
        }
    }
}

final class ConnectionQRScannerViewController: UIViewController, @preconcurrency AVCaptureMetadataOutputObjectsDelegate {
    var onCode: ((String) -> Void)?

    private let session = AVCaptureSession()
    private var previewLayer: AVCaptureVideoPreviewLayer?
    private let messageLabel = UILabel()

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        configureMessageLabel()
        prepareCameraAccess()
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        previewLayer?.frame = view.bounds
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        if session.isRunning {
            session.stopRunning()
        }
    }

    private func configureMessageLabel() {
        messageLabel.text = "Point the camera at a TASTE connection QR code."
        messageLabel.textColor = .white
        messageLabel.textAlignment = .center
        messageLabel.font = .preferredFont(forTextStyle: .body)
        messageLabel.adjustsFontForContentSizeCategory = true
        messageLabel.numberOfLines = 0
        messageLabel.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(messageLabel)
        NSLayoutConstraint.activate([
            messageLabel.leadingAnchor.constraint(equalTo: view.layoutMarginsGuide.leadingAnchor),
            messageLabel.trailingAnchor.constraint(equalTo: view.layoutMarginsGuide.trailingAnchor),
            messageLabel.centerYAnchor.constraint(equalTo: view.centerYAnchor),
        ])
    }

    private func prepareCameraAccess() {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            configureSession()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video) { [weak self] granted in
                DispatchQueue.main.async {
                    granted ? self?.configureSession() : self?.showMessage("Camera access is needed to scan a TASTE connection QR code.")
                }
            }
        default:
            showMessage("Camera access is needed to scan a TASTE connection QR code.")
        }
    }

    private func configureSession() {
        guard let device = AVCaptureDevice.default(for: .video) else {
            showMessage("Camera is not available on this device.")
            return
        }

        do {
            let input = try AVCaptureDeviceInput(device: device)
            guard session.canAddInput(input) else {
                showMessage("Camera input is not available.")
                return
            }
            session.addInput(input)
        } catch {
            showMessage("Camera input is not available.")
            return
        }

        let output = AVCaptureMetadataOutput()
        guard session.canAddOutput(output) else {
            showMessage("QR scanning is not available.")
            return
        }
        session.addOutput(output)
        output.setMetadataObjectsDelegate(self, queue: .main)
        output.metadataObjectTypes = [.qr]

        let previewLayer = AVCaptureVideoPreviewLayer(session: session)
        previewLayer.videoGravity = .resizeAspectFill
        previewLayer.frame = view.bounds
        view.layer.insertSublayer(previewLayer, at: 0)
        self.previewLayer = previewLayer
        messageLabel.text = "Scan a TASTE connection QR code."
        session.startRunning()
    }

    private func showMessage(_ message: String) {
        messageLabel.text = message
    }

    func metadataOutput(_ output: AVCaptureMetadataOutput, didOutput metadataObjects: [AVMetadataObject], from connection: AVCaptureConnection) {
        guard let code = metadataObjects
            .compactMap({ $0 as? AVMetadataMachineReadableCodeObject })
            .first(where: { $0.type == .qr })?
            .stringValue?
            .trimmingCharacters(in: .whitespacesAndNewlines),
            !code.isEmpty
        else { return }

        session.stopRunning()
        onCode?(code)
    }
}

struct RuntimeDiagnosticsPanel: View {
    let status: ProjectRuntimeStatus?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(status?.summaryLine ?? "Runtime checks not loaded", systemImage: status?.failingCriticalChecks.isEmpty == false ? "exclamationmark.triangle.fill" : "checkmark.seal")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(status?.failingCriticalChecks.isEmpty == false ? Color.tasteOrange : Color.tasteGreen)
            if let status, !status.orderedChecks.isEmpty {
                ForEach(status.orderedChecks.prefix(8)) { item in
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: item.ok ? "checkmark.circle.fill" : "xmark.circle.fill")
                            .foregroundStyle(item.ok ? Color.tasteGreen : Color.tasteOrange)
                            .frame(width: 20)
                            .accessibilityHidden(true)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(item.displayName)
                                .font(.footnote.weight(.semibold))
                            Text(runtimeDetail(for: item))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(2)
                        }
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("\(item.displayName): \(item.ok ? "ready" : "needs attention"). \(runtimeDetail(for: item))")
                }
            } else {
                Text("Load or detect runtime to verify the server-side Python, Claude, and Node paths.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 4)
    }

    private func runtimeDetail(for item: RuntimeCheck) -> String {
        if item.ok {
            return item.version.isEmpty ? item.path : item.version
        }
        return item.reason.isEmpty ? "Check failed" : item.reason
    }
}

struct LabeledMobileTextField: View {
    let fieldID: MobileFormFieldID
    @Binding var text: String
    var axis: Axis = .horizontal
    var useURLKeyboard = false
    var disableAutocapitalization = true

    private var spec: MobileFormFieldSpec {
        MobileFormFieldCatalog.spec(for: fieldID)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(spec.title)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            TextField(spec.title, text: $text, prompt: Text(spec.prompt), axis: axis)
                .keyboardType(useURLKeyboard ? .URL : .default)
                .autocapitalizationPolicy(disabled: disableAutocapitalization)
                .accessibilityLabel(spec.accessibilityLabel)
        }
        .padding(.vertical, 2)
    }
}

struct LabeledMobileSecureField: View {
    let fieldID: MobileFormFieldID
    @Binding var text: String
    @State private var isRevealed = false

    private var spec: MobileFormFieldSpec {
        MobileFormFieldCatalog.spec(for: fieldID)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(spec.title)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            HStack {
                if isRevealed {
                    TextField(spec.title, text: $text, prompt: Text(spec.prompt))
                        .textInputAutocapitalization(.never)
                        .accessibilityLabel(spec.accessibilityLabel)
                } else {
                    SecureField(spec.title, text: $text, prompt: Text(spec.prompt))
                        .textInputAutocapitalization(.never)
                        .accessibilityLabel(spec.accessibilityLabel)
                }
                Button {
                    isRevealed.toggle()
                } label: {
                    Image(systemName: isRevealed ? "eye.slash" : "eye")
                        .frame(width: 44, height: 44)
                }
                .buttonStyle(.borderless)
                .accessibilityLabel(isRevealed ? "Hide \(spec.title)" : "Show \(spec.title)")
            }
        }
        .padding(.vertical, 2)
    }
}

private extension View {
    @ViewBuilder
    func autocapitalizationPolicy(disabled: Bool) -> some View {
        if disabled {
            textInputAutocapitalization(.never)
        } else {
            self
        }
    }
}

struct ReadinessSection: View {
    let summary: MobileReadinessSummary

    var body: some View {
        Section("Run Readiness") {
            HStack {
                Label(summary.statusLine, systemImage: summary.isReadyForFullWorkflow ? "checkmark.seal.fill" : "exclamationmark.triangle.fill")
                    .font(.headline)
                    .foregroundStyle(summary.isReadyForFullWorkflow ? Color.tasteGreen : Color.tasteOrange)
                Spacer()
                Text("\(summary.readyCount)/\(summary.checks.count)")
                    .font(.subheadline.monospacedDigit().weight(.bold))
                    .foregroundStyle(.secondary)
            }
            ForEach(summary.checks) { item in
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: item.symbolName)
                        .font(.body.weight(.semibold))
                        .frame(width: 24)
                        .foregroundStyle(item.state == .ready ? Color.tasteGreen : Color.tasteOrange)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(item.title)
                            .font(.subheadline.weight(.semibold))
                        Text(item.detail)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer()
                    Image(systemName: item.state == .ready ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                        .foregroundStyle(item.state == .ready ? Color.tasteGreen : Color.tasteOrange)
                        .accessibilityHidden(true)
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("\(item.title): \(item.state.rawValue). \(item.detail)")
            }
        }
    }
}

struct FooterStatus: View {
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if model.isLoading {
                ProgressView()
                    .controlSize(.small)
            }
            Text(model.errorMessage.isEmpty ? model.statusMessage : model.errorMessage)
                .font(.footnote.weight(.semibold))
                .foregroundStyle(model.errorMessage.isEmpty ? Color.tasteText : Color.red)
                .lineLimit(2)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal)
        .padding(.vertical, 8)
        .background(.ultraThinMaterial)
    }
}

extension Color {
    static let tasteBlue = Color(red: 37 / 255, green: 99 / 255, blue: 235 / 255)
    static let tasteOrange = Color(red: 249 / 255, green: 115 / 255, blue: 22 / 255)
    static let tasteGreen = Color(red: 22 / 255, green: 163 / 255, blue: 74 / 255)
    static let tasteBackground = Color(red: 248 / 255, green: 250 / 255, blue: 252 / 255)
    static let tasteText = Color(red: 30 / 255, green: 41 / 255, blue: 59 / 255)
}
