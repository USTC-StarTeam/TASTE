import importlib.util
import os
import sys
from pathlib import Path

from auto_research.auto_idea.pipeline import _normalize_idea_schema, render_ideas_markdown


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location("ensure_current_find_research_plan", SCRIPTS / "ensure_current_find_research_plan.py")
ensure_current_find_research_plan = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(ensure_current_find_research_plan)


def _v4_deep_read_fields(title: str = "Full Paper") -> dict:
    abstract = (
        f"本文围绕《{title}》研究推荐系统中的条件扩散建模问题，系统说明如何把用户历史、物品语义、扩散时间步和排序目标放入同一个生成式推荐框架。"
        "论文摘要不仅给出任务背景，还概括了前向扰动、反向去噪、候选评分、语义条件注入以及多数据集实验的主要发现，用于支撑后续逐项核对方法和实验边界。"
        "摘要进一步说明该方法需要在统一数据切分、统一负采样和统一排序指标下验证，避免把语义相关性、检索相似度或推荐理由误当作真实推荐收益。"
        "它还概括了消融、效率和失败样本分析的必要性，使读者能够区分论文提出的机制贡献、实验协议贡献和仍需本地复核的适用范围。"
    )
    motivation = (
        "论文动机是解决传统协同推荐在稀疏用户和长尾物品上语义泛化不足、纯语义推荐又容易削弱真实交互偏好的矛盾。"
        "已有序列推荐或检索基准方法通常只优化平均排序指标，缺少对语义条件、噪声时间步和用户真实偏好之间冲突的细粒度分析。"
        "作者希望通过扩散式生成过程把偏好恢复、语义条件和排序优化统一起来，从而判断哪些信号真正改善推荐质量，哪些改动只是增加计算量或引入不可控偏差。"
        "这种动机要求精读时同时记录问题背景、既有方法不足、目标用户场景和评测协议限制。"
    )
    method = (
        "论文方法先把用户交互序列或物品偏好表示定义为扩散过程中的离散或连续状态，在前向过程中按时间步逐步加入扰动，模拟偏好信息被破坏的过程。"
        "反向去噪网络接收用户历史表示、候选物品语义表示、扩散时间步嵌入和可选的上下文条件，预测恢复后的偏好表示或物品得分方向。"
        "模型结构通常包含用户序列编码器、物品或文本语义编码器、时间步嵌入层、条件融合模块和排序头；其中条件融合模块可以是门控、交叉注意力、专家混合或投影约束。"
        "训练阶段联合优化重建损失、条件一致性约束和排序损失，使模型既学习协同过滤信号，也保留语义相似性和时间步相关的不确定性。"
        "推理阶段模型从扰动后的偏好状态开始迭代去噪，通过排序头把恢复表示映射成候选物品分数，并用同一指标协议比较基线、候选模型和消融变体。"
        "方法还要求把输入用户、候选物品、语义编码器输出、时间步编码和最终排序分数分别记录清楚，并记录训练批次、采样步数、随机种子和输出分数，便于在后续实验中替换单个模块并做可审计的局部消融。"
        "如果论文包含强化学习、偏好优化或奖励模型，还需要说明奖励如何作用于去噪轨迹、如何估计策略概率、如何避免奖励黑客以及如何把轨迹级反馈转化为推荐列表级评价。"
        "精读记录还必须说明关键公式或伪代码对应的模块边界，例如噪声调度、候选采样、语义特征拼接、损失权重、反向更新顺序和最终排序分数如何连接。"
        "这些细节用于确保后续项目代理能够把论文机制拆成最小可测模块，而不是只得到一个无法实现的概念描述。"
        "测试夹具还要覆盖论文精读对变量定义、模块依赖、训练数据流、推理数据流、消融开关和可复现实验入口的描述要求，确保短方法总结不能被误判为合格全文精读。"
    )
    experiments = (
        "实验设置覆盖多个公开推荐数据集或等价用户行为任务，使用固定数据划分、负采样协议和随机种子来比较传统序列推荐、语义重排、生成式推荐和检索基准基线。"
        "论文报告 Recall、NDCG、HR、AUC 或相邻排序指标，并给出主结果表来说明候选模型相对基线的收益，同时记录标准差或多次运行稳定性。"
        "消融实验分别去掉语义条件、扩散时间步编码、排序损失、奖励约束或关键门控模块，以验证各组成部分对最终排序质量、长尾切片和语义冲突坏例的影响。"
        "效率实验需要记录训练预算、采样步数、推理时延、显存占用和候选集规模，确保性能提升不是由额外计算或不一致协议造成。"
        "论文还应包含失败案例或边界分析，例如冷启动用户、极短序列、长尾物品、高置信错误和语义相似但行为不一致的候选，从而支持对方法适用范围的审慎判断。"
        "如果论文没有完整报告某些指标或切片，精读记录也必须指出缺口，并说明这些缺口会如何影响后续同协议复现、候选方法消融和论文结论边界。"
        "实验字段不能只写任务名和最终数字，还要把数据来源、训练配置、比较对象、统计方式和主要表格/图形结论组织成可审计的中文 synthesis。"
    )
    limitations = (
        "局限性包括扩散采样或多步去噪带来的计算成本，语义表示质量和负采样协议会影响最终指标结论，且不同数据集划分可能改变收益幅度。"
        "如果方法依赖大语言模型、奖励模型或外部语义编码器，还会引入额外的推理延迟、提示模板敏感性和跨领域语义偏差。"
        "论文没有证明该机制可以直接覆盖所有冷启动、跨域迁移、在线大规模召回或严格实时排序场景，因此仍需要在同一仓库、同一数据、同一 seed 和同一指标下做本地复现实验。"
        "局限性还包括消融范围可能不足、失败案例数量有限以及实验协议可能与目标项目的数据合同不完全一致。"
    )
    return {
        "abstract_zh": abstract,
        "summary": abstract,
        "motivation_zh": motivation,
        "method": method,
        "method_details_zh": method,
        "experiments": experiments,
        "experiments_zh": experiments,
        "limitations": limitations,
        "limitations_zh": limitations,
        "method_advantages_zh": [
            "把扩散时间步、用户历史、候选物品和物品语义放入同一个反向去噪过程，便于逐项消融协同信号、语义信号和时间步不确定性的贡献。",
            "训练目标同时包含重建、条件一致性、排序优化或奖励约束，使方法可以用同一推荐指标协议比较基线、候选模型、控制组和消融版本。",
        ],
        "method_disadvantages_zh": [
            "多步去噪、语义编码或轨迹奖励评估会增加训练和推理成本，在线推荐场景还需要额外的延迟、显存、吞吐量和批量候选排序审计。",
            "结论依赖具体数据划分、负采样、候选集规模和语义编码质量，跨数据集、冷启动或长尾迁移不能只凭论文结果直接外推，必须重新核对协议。",
        ],
    }


def _with_scored_idea_contract(idea: dict, idx: int = 0, overall: float = 8.2) -> dict:
    clean = dict(idea)
    clean.setdefault("id", f"idea-{idx}")
    clean.setdefault("title", f"Idea {idx}")
    clean.setdefault("status", "approved_for_planning")
    clean["score"] = overall
    clean["idea_score"] = overall
    clean["objective_scores"] = {
        "novelty": 8.0,
        "evidence_alignment": 8.1,
        "feasibility": 7.8,
        "experimentability": 8.3,
        "risk_control": 7.7,
        "overall": overall,
    }
    clean["idea_score_audit"] = {
        "mode": "task_subagent",
        "subagent_used": True,
        "status": "completed",
        "criteria": "TASTE-like objective idea scoring",
    }
    return clean


def _ready_scored_idea(idx: int, source_title: str = "Full-text reading anchor") -> dict:
    return _with_scored_idea_contract(
        {
            "id": f"idea-{idx}",
            "title": f"Idea {idx}",
            "new_method": "提出一个基于完整精读证据的新方法，包含清晰模块、训练作用点、推理路径和可拆分消融边界，并说明语义信号和协同信号如何在扩散时间步内互补。",
            "initial_experiment": "基于环境阶段选出的当前可审计基底执行最小同协议实验，对比 baseline、candidate、control 和 ablation，并记录 Recall、NDCG、长尾切片、语义冲突坏例和失败停止条件。",
            "inspired_by": [{"title": source_title, "reason": "方法模块和实验协议启发"}],
        },
        idx,
    )


def test_valid_claude_idea_accepts_scored_contract_without_status():
    idea = _ready_scored_idea(1, source_title="Statusless Contract Paper")
    idea.pop("status", None)

    assert ensure_current_find_research_plan._idea_rows_contract_issues([idea], 1) == []
    assert ensure_current_find_research_plan._valid_claude_idea(idea) is True


def test_claude_artifact_freshness_uses_file_mtime_when_payload_has_date_only_generated_at(tmp_path):
    path = tmp_path / "ideas.json"
    path.write_text("{}", encoding="utf-8")
    current_revision = ensure_current_find_research_plan.dt.datetime.now(ensure_current_find_research_plan.dt.timezone.utc) - ensure_current_find_research_plan.dt.timedelta(minutes=1)
    payload = {"generated_at": current_revision.date().isoformat()}

    assert ensure_current_find_research_plan.claude_output_payloads_or_files_are_current([payload], [path], current_revision) is True


def test_generic_initial_experiment_placeholder_is_removed_from_idea_schema():
    idea = {
        "id": "idea-old-placeholder",
        "title": "LLM semantic gated retrieval planner",
        "hypothesis": "通过门控机制把语义信号注入离散检索基准过程，验证语义泛化是否能补充协同行为信号。",
        "mechanism": "在反向去噪阶段加入语义专家与协同专家，依据 token 类型和扩散时间步动态选择专家输出。",
        "min_experiment": "After environment-stage base selection, run a minimal same-protocol baseline/candidate/ablation experiment with audited metrics and bad cases.",
        "inspired_by": [{"title": "Fading to Grow", "reason": "evidence fading retrieval"}],
    }

    normalized = _normalize_idea_schema(idea)

    assert normalized["initial_experiment_required"] is True
    assert "initial_experiment" not in normalized
    assert "min_experiment" not in normalized
    assert "minimum_experiment" not in normalized
    markdown = render_ideas_markdown([normalized])
    assert "After environment-stage base selection" not in markdown
    assert "Repo/Data Path" not in markdown
    assert "Bad-Case Slice" not in markdown
    assert "### 方法机制" not in markdown


def test_current_find_idea_ready_requires_three_research_fields_and_inspiration():
    old_schema = {
        "title": "Old schema idea",
        "hypothesis": "通过门控机制把语义信号注入离散检索基准过程，验证语义泛化是否能补充协同行为信号。",
        "mechanism": "在反向去噪阶段加入语义专家与协同专家，依据 token 类型和扩散时间步动态选择专家输出。",
        "min_experiment": "After environment review, run a minimal same-protocol baseline/candidate/ablation experiment with audited metrics and bad cases.",
        "supporting_papers": [{"title": "Fading to Grow"}],
    }
    unscored_schema = {
        "title": "Semantic-gated discrete retrieval benchmark",
        "new_method": "提出一个语义门控的离散检索基准方法，在偏好衰减和反向重建之间加入协同专家与语言语义专家，并用扩散时间步控制二者权重。",
        "initial_experiment": "基于 PreferGrow 的离散偏好衰减框架实现一个最小变体，对比原始 PreferGrow、仅语义重排和语义门控扩散三组，并报告 Recall@K、NDCG@K、长尾切片和语义冲突坏例。",
        "inspired_by": [{"title": "Fading to Grow", "reason": "evidence fading retrieval"}],
    }
    ready_schema = _with_scored_idea_contract(unscored_schema, 0)

    assert not ensure_current_find_research_plan._idea_three_part_ready(old_schema)
    assert not ensure_current_find_research_plan._idea_three_part_ready(unscored_schema)
    assert "score_or_idea_score_missing" in ensure_current_find_research_plan._idea_contract_issues(unscored_schema)
    assert "idea_score_audit_missing_or_not_subagent" in ensure_current_find_research_plan._idea_contract_issues(unscored_schema)
    assert ensure_current_find_research_plan._idea_three_part_ready(ready_schema)
    assert ensure_current_find_research_plan._ideas_three_part_ready([_with_scored_idea_contract(dict(ready_schema, title=f"idea {idx}"), idx) for idx in range(5)], 5)



def test_run_idea_syncs_generated_ideas_to_project_state(monkeypatch, tmp_path):
    from auto_research.auto_idea.pipeline import run_idea
    from auto_research.models import AppConfig, IdeaRequest
    from auto_research.storage import create_run_dir, delete_run, read_json, write_json

    run_id, directory = create_run_dir("idea_generation_sync_test")
    project_root = tmp_path / "root"
    taste_dir = project_root / "projects" / "demo_project" / "planning" / "finding"
    state_dir = project_root / "projects" / "demo_project" / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    try:
        write_json(
            directory / "find_results.json",
            {
                "run_id": run_id,
                "strong_recommendations": [
                    {
                        "id": "paper-1",
                        "title": "Semantic retrieval benchmark",
                        "url": "https://example.test/paper",
                        "reason": "retrieval benchmark and LLM semantic evidence",
                        "score": 9.0,
                    }
                ],
            },
        )
        write_json(
            directory / "read_results.json",
            {
                "run_id": run_id,
                "readings": [
                    {
                        "title": "Semantic retrieval benchmark",
                        "summary": "LLM semantic signals improve retrieval benchmark.",
                    }
                ],
            },
        )
        write_json(state_dir / "current_find_research_plan.json", {"run_id": run_id})
        monkeypatch.setenv("WORKSPACE_ROOT", str(project_root))
        monkeypatch.setenv("PROJECT_ID", "demo_project")
        monkeypatch.setenv("USE_LLM_IDEA", "0")

        result = run_idea(
            IdeaRequest(run_id=run_id, max_ideas=2),
            AppConfig(provider="mock", research_interest="LLM-assisted retrieval benchmark", max_ideas=2),
            log=lambda _msg: None,
        )

        assert result["ideas"]
        project_ideas = read_json(taste_dir / "ideas.json", {})
        assert project_ideas["run_id"] == run_id
        assert len(project_ideas["ideas"]) == len(result["ideas"])
        state = read_json(state_dir / "current_find_research_plan.json", {})
        assert state["current_find_idea_count"] == len(result["ideas"])
        assert state["human_supervision_source"] == "web_ideas_three_column_editor"
    finally:
        delete_run(run_id)


def test_patch_idea_syncs_project_current_find_state(monkeypatch, tmp_path):
    from auto_research.auto_idea.pipeline import patch_idea
    from auto_research.models import IdeaPatch
    from auto_research.storage import create_run_dir, delete_run, read_json, write_json

    run_id, directory = create_run_dir("idea_patch_sync_test")
    project_root = tmp_path / "root"
    taste_dir = project_root / "projects" / "demo_project" / "planning" / "finding"
    state_dir = project_root / "projects" / "demo_project" / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    try:
        write_json(
            directory / "ideas.json",
            {
                "run_id": run_id,
                "ideas": [
                    {
                        "id": "idea-1",
                        "title": "Runtime stale idea",
                        "new_method": "runtime method should be overridden by project idea",
                        "initial_experiment": "runtime experiment should be overridden by project idea",
                        "inspired_by": [{"title": "Runtime source"}],
                    }
                ],
            },
        )
        write_json(
            taste_dir / "ideas.json",
            {
                "run_id": run_id,
                "ideas": [
                    {
                        "idea_id": "idea-1",
                        "title": "Project idea",
                        "new_method": "项目代理已写入的新方法，包含足够详细的机制说明和可审计设计起点。",
                        "method_details": "项目代理补充的方法机制细节。",
                        "initial_experiment": "项目代理已写入的初步实验，包含基底、最小改动、baseline、指标和坏例切片。",
                        "inspired_by": [{"title": "Project source", "reason": "inspiration"}],
                    }
                ],
            },
        )
        write_json(state_dir / "current_find_research_plan.json", {"run_id": run_id, "ideas": [], "current_find_idea_count": 0})
        monkeypatch.setenv("WORKSPACE_ROOT", str(project_root))
        monkeypatch.setenv("PROJECT_ID", "demo_project")

        result = patch_idea(
            run_id,
            "idea-1",
            IdeaPatch(
                new_method="人类监督者修改后的新方法：保留项目代理内容入口，但把用户可见三段合同中的新方法字段作为唯一后续来源。",
                initial_experiment="人类监督者修改后的初步实验：明确基底、最小改动、baseline/control/ablation、指标和坏例切片。",
                inspired_by_text="Project source | paper | inspiration | https://example.test/paper",
                status="approved",
            ),
        )

        idea = result["ideas"][0]
        assert idea["title"] == "Project idea"
        assert idea["new_method"].startswith("人类监督者修改后的新方法")
        assert idea["method_details"] == ""
        assert idea["hypothesis"] == idea["new_method"]
        assert idea["approved_for_planning"] is True
        assert idea["pursue"] is True

        project_ideas = read_json(taste_dir / "ideas.json", {})
        project_idea = project_ideas["ideas"][0]
        assert project_idea["new_method"] == idea["new_method"]
        assert project_idea["initial_experiment"] == idea["initial_experiment"]
        markdown = (taste_dir / "idea.md").read_text(encoding="utf-8")
        assert "### 新方法" in markdown
        assert "### 初步实验" in markdown
        assert "### Inspired by" in markdown
        assert "人类监督者修改后的新方法" in markdown
        assert "Runtime stale idea" not in markdown

        state = read_json(state_dir / "current_find_research_plan.json", {})
        assert state["current_find_idea_count"] == 1
        assert state["ideas"][0]["new_method"] == idea["new_method"]
        assert state["human_supervision_source"] == "web_ideas_three_column_editor"
    finally:
        delete_run(run_id)



def test_current_find_plan_markdown_prefers_specific_initial_experiment_over_generic_steps():
    plan = {
        "plan_id": "plan-specific",
        "idea_id": "idea-specific",
        "title": "语义门控检索基准计划",
        "status": "waiting_for_environment_base_selection",
        "new_method": "提出语义门控离散检索基准方法，将 LLM 语义嵌入注入偏好衰减和反向重建过程。",
        "initial_experiment": "基于 PreferGrow 实现最小语义门控变体，对比 PreferGrow、仅语义重排和语义门控扩散，报告 HR@10、NDCG@10、长尾切片和语义冲突坏例。",
        "inspired_by": [{"title": "Fading to Grow", "reason": "离散偏好衰减"}],
        "versions": [
            {
                "final_plan": {
                    "steps": [
                        "Verify current Find run_id and guarded read/idea/plan outputs.",
                        "Environment-stage Claude Code reads all current strong recommendations and audits candidate repos/data/protocols.",
                        "Accept a base only by writing state/evidence_ready_repo_selection.json.",
                    ],
                    "go_no_go": "No repo/data/command execution until evidence_ready_repo_selection.json names a current-run environment-stage base and gates pass.",
                }
            }
        ],
    }

    markdown = ensure_current_find_research_plan.render_plan_md([plan], "find_demo")

    assert "### 初步实验" in markdown
    assert "基于 PreferGrow 实现最小语义门控变体" in markdown
    assert "以该初步实验作为执行合同" in markdown
    assert "Verify current Find run_id" not in markdown
    assert "Environment-stage Claude Code" not in markdown



def test_current_find_markdown_hides_empty_machine_fields_and_keeps_scores():
    idea = _ready_scored_idea(4, source_title="Ranking-based Preference Optimization for Diffusion Models")
    idea["recommendation"] = None
    idea["selected_for_execution"] = True
    plan = {
        "plan_id": "plan-004",
        "idea_id": idea["id"],
        "title": "检索基准模型的排名偏好对齐后训练实验计划",
        "status": "waiting_for_environment_base_selection",
        "selected_for_execution": True,
        "steps": ["基于当前基底执行同协议 baseline/candidate/ablation，并记录指标和坏例。"],
    }

    idea_md = ensure_current_find_research_plan.render_idea_md([idea], "find_demo")
    plan_md = ensure_current_find_research_plan.render_plan_md([plan], "find_demo")

    assert "recommendation: None" not in idea_md
    assert "score: None" not in idea_md
    assert "objective_scores:" in idea_md
    assert "scoring_audit: subagent completed" in idea_md
    assert "go_no_go:" not in plan_md
    assert "selected_for_execution: True" in plan_md



def test_current_find_reading_validation_requires_full_text_evidence():
    find_results = {
        "run_id": "find_demo",
        "strong_recommendations": [
            {"id": "paper-1", "title": "Full Paper", "url": "https://example.test/paper", "evidence_tier": "strong_recommendation"}
        ],
    }
    metadata_only = [
        {
            "paper_id": "paper-1",
            "title": "Full Paper",
            "url": "https://example.test/paper",
            "verdict": "core_reading",
            "support_role": "core_method_reference",
            "relevance": "主题相关。",
            "method": "方法摘要。",
            "experiments": "实验摘要。",
            "limitations": "局限摘要。",
            "critique_reason": "",
            "full_text_available": False,
            "full_text_status": "pending_full_text_reading",
        }
    ]

    valid, report = ensure_current_find_research_plan.validate_claude_readings_against_current_find(metadata_only, find_results, 1)

    assert valid is False
    assert report["full_text_reading_count"] == 0
    assert report["pending_full_text_reading_count"] == 1
    assert any("full-text evidence" in item for item in report["blockers"])

    declared_only = [dict(metadata_only[0], full_text_available=True, full_text_status="pdf_text_read")]
    valid, report = ensure_current_find_research_plan.validate_claude_readings_against_current_find(declared_only, find_results, 1)

    assert valid is False
    assert report["full_text_evidence_count"] == 0
    assert report["full_text_reading_count"] == 0
    assert report["pending_full_text_reading_count"] == 1

    evidence_without_synthesis = [dict(metadata_only[0], full_text_status="pdf_text_read", pdf_text_chars=2000, source_evidence={"text_chars": 2000, "text_path": "texts/full-paper.txt"})]
    valid, report = ensure_current_find_research_plan.validate_claude_readings_against_current_find(evidence_without_synthesis, find_results, 1)

    assert valid is False
    assert report["full_text_evidence_count"] == 1
    assert report["full_text_reading_count"] == 0
    assert report["pending_deep_read_synthesis_count"] == 1
    assert any("deep-read synthesis" in item for item in report["blockers"])

    abstract_only_chars = [
        dict(
            metadata_only[0],
            abstract_zh="本文摘要说明推荐任务背景，但这仍只是摘要线索，不能替代论文正文精读。",
            motivation_zh="动机来自摘要级线索，仍缺少论文正文证据。",
            method_details_zh="方法描述来自摘要或题录，没有论文正文、PDF、HTML全文或正文包 text_path，因此不能算全文精读。",
            experiments_zh="实验描述来自摘要或题录，没有完整实验章节正文、表格或指标细节，不能作为全文精读。",
            limitations_zh="局限说明来自摘要级推断，仍缺少正文证据。",
            full_text_available=False,
            full_text_status="no_pdf_available",
            source_text_chars=2400,
            source_evidence="dblp_abstract_only",
        )
    ]
    valid, report = ensure_current_find_research_plan.validate_claude_readings_against_current_find(abstract_only_chars, find_results, 1)

    assert valid is False
    assert report["full_text_evidence_count"] == 0
    assert report["full_text_reading_count"] == 0
    assert report["pending_without_evidence_count"] == 1
    assert any("full-text evidence" in item for item in report["blockers"])

    deep_read = [
        dict(
            metadata_only[0],
            **_v4_deep_read_fields("Full Paper"),
            pdf_text_chars=2000,
            source_evidence={"text_chars": 2000, "text_path": "texts/full-paper.txt"},
            full_text_available=True,
            full_text_status="pdf_text_read",
        )
    ]
    valid, report = ensure_current_find_research_plan.validate_claude_readings_against_current_find(deep_read, find_results, 1)

    assert valid is True
    assert report["full_text_reading_count"] == 1
    assert report["pending_full_text_reading_count"] == 0


def test_current_find_reading_validation_accepts_nested_fragment_reading_and_title_punctuation():
    find_results = {
        "run_id": "find_demo_nested_fragment",
        "strong_recommendations": [
            {
                "title": "Breaking TASTE’s Sampling Bottleneck: Provable Acceleration via Diffusion Language Models",
                "evidence_tier": "strong_recommendation",
                "find_recommendation": True,
                "recommended_by_llm_ranking": True,
                "reason_source": "llm abstract evaluation",
                "score_source": "llm_title_abstract_score_only",
                "fit_score": 9.0,
                "abstract": "This paper studies accelerated sampling for diffusion language models with theoretical and empirical analysis across text generation settings, providing enough abstract detail for the Find recommendation contract.",
            }
        ],
    }
    nested_fragment = {
        "run_id": "find_demo_nested_fragment",
        "source": "claude_subagent_deep_read_fragment",
        "reading": {
            "title": "Breaking TASTE's Sampling Bottleneck: Provable Acceleration via Diffusion Language Models",
            "verdict": "core_reading",
            "support_role": "core_method_reference",
            "critique_reason": "",
            **_v4_deep_read_fields("Breaking TASTE's Sampling Bottleneck"),
            "full_text_available": True,
            "full_text_status": "pdf_text_read",
            "pdf_text_chars": 80752,
            "source_evidence": {"text_chars": 80752, "text_path": "texts/breaking-ar-sampling.txt"},
            "subagent_deep_read": True,
            "deep_read_audit": {"subagent_used": True, "status": "completed", "mode": "task_subagent"},
        },
    }

    valid, report = ensure_current_find_research_plan.validate_claude_readings_against_current_find([nested_fragment], find_results, 1)

    assert valid is True
    assert report["actual_reading_count"] == 1
    assert report["recommended_reading_count"] == 1
    assert report["full_text_reading_count"] == 1
    assert report["pending_full_text_reading_count"] == 0
    assert report["missing_recommendation_titles"] == []
    assert report["blockers"] == []


def test_current_find_reading_validation_rejects_short_full_text_synthesis():
    find_results = {
        "run_id": "find_demo_short_synthesis",
        "strong_recommendations": [
            {"id": "paper-1", "title": "Short Synthesis Paper", "url": "https://example.test/paper", "evidence_tier": "strong_recommendation"}
        ],
    }
    short_reading = {
        "paper_id": "paper-1",
        "title": "Short Synthesis Paper",
        "url": "https://example.test/paper",
        "verdict": "core_reading",
        "support_role": "core_method_reference",
        "abstract_zh": "论文提出一个检索基准方法，包含前向扰动、反向去噪和排序实验。",
        "motivation_zh": "动机是改善推荐。",
        "method_details_zh": "方法是扩散去噪推荐。",
        "experiments_zh": "实验报告推荐指标。",
        "limitations_zh": "局限是成本较高。",
        "method_advantages_zh": ["有扩散模块。", "有推荐实验。"],
        "method_disadvantages_zh": ["成本较高。", "泛化未知。"],
        "full_text_available": True,
        "full_text_status": "pdf_text_read",
        "pdf_text_chars": 50000,
        "source_evidence": {"text_chars": 50000, "text_path": "texts/short-synthesis-paper.txt"},
    }

    valid, report = ensure_current_find_research_plan.validate_claude_readings_against_current_find([short_reading], find_results, 1)

    assert valid is False
    assert report["full_text_evidence_count"] == 1
    assert report["pending_deep_read_synthesis_count"] == 1
    assert any("deep-read synthesis" in item for item in report["blockers"])
    assert report["deep_read_content_gap_details"]

def test_full_text_packet_conflict_requires_claude_rewrite_not_auto_pass():
    find_results = {
        "run_id": "find_demo_packet_conflict",
        "strong_recommendations": [
            {"id": "paper-1", "title": "Packet Conflict Paper", "url": "https://doi.org/10.1145/demo", "evidence_tier": "strong_recommendation"}
        ],
    }
    stale_reading = {
        "paper_id": "paper-1",
        "title": "Packet Conflict Paper",
        "url": "https://doi.org/10.1145/demo",
        "verdict": "recommended_reading_boundary",
        "support_role": "foundation_borrowing",
        "abstract_zh": "该论文摘要说明了推荐数据集和模型微调目标，但旧精读仍基于题录或网页摘要，没有读取新获得的论文正文。",
        "motivation_zh": "旧精读声称出版端和仓储端均不可访问，因此只给出摘要级动机，这不能在有正文包后继续通过。",
        "method_details_zh": "旧精读用摘要级线索描述方法，而不是从 full_text_packet 的正文 text_path 中提取模型结构、数据构建和训练流程。",
        "experiments_zh": "旧精读基于摘要或网页元数据描述实验，未从正文表格和实验章节核对数据集、指标、对照和消融。",
        "limitations_zh": "旧精读给出一般局限，但没有依据正文讨论实验边界、数据噪声和方法适用范围。",
        "full_text_available": False,
        "full_text_status": "full_text_inaccessible_all_channels_blocked",
        "source_evidence": "web_search_metadata_only",
        "full_text_evidence": {
            "full_text_status": "full_text_inaccessible_all_channels_blocked",
            "text_chars": 0,
            "inaccessibility_reason": "ACM paywall and arXiv network policy blocked",
        },
        "inaccessibility_reason": "ACM paywall and arXiv network policy blocked",
    }
    packet = {
        "run_id": "find_demo_packet_conflict",
        "papers": [
            {
                "paper_id": "paper-1",
                "title": "Packet Conflict Paper",
                "pdf_url": "https://arxiv.org/pdf/2508.05667",
                "text_path": "planning/finding/full_text_reading/texts/12_paper_61237f51cc03.txt",
                "text_chars": 116491,
                "page_count": 29,
                "full_text_status": "openalex_repository_pdf_text_read",
            }
        ],
    }

    normalized = ensure_current_find_research_plan.normalize_readings_full_text_evidence([stale_reading], packet)
    row = normalized[0]
    valid, report = ensure_current_find_research_plan.validate_claude_readings_against_current_find(normalized, find_results, 1)

    assert row["full_text_packet_conflict"] is True
    assert row["full_text_packet_text_chars"] == 116491
    assert row["full_text_packet_text_path"].endswith("12_paper_61237f51cc03.txt")
    assert row["full_text_status"] == "full_text_packet_ready_pending_claude_rewrite_conflict"
    assert row["full_text_available"] is False
    assert valid is False
    assert report["full_text_evidence_count"] == 1
    assert report["full_text_reading_count"] == 0
    assert report["pending_deep_read_synthesis_count"] == 1
    assert report["pending_full_text_reading_count"] == 1
    assert report["full_text_packet_conflict_titles"] == ["Packet Conflict Paper"]
    assert any("contradict full_text_packet evidence" in item for item in report["blockers"])
    assert any("full_text_packet_conflict" in item for item in report["deep_read_content_gap_details"][0]["missing_or_invalid_fields"])




def test_same_run_deep_read_fragment_survives_later_full_text_revision(tmp_path):
    run_id = "find_same_run_fragment"
    fragment = tmp_path / "01_paper.json"
    fragment.write_text("{}", encoding="utf-8")
    current_revision = ensure_current_find_research_plan.dt.datetime.now(ensure_current_find_research_plan.dt.timezone.utc)
    payload = {
        "run_id": run_id,
        "source": "claude_subagent_deep_read_fragment",
        "generated_at": (current_revision - ensure_current_find_research_plan.dt.timedelta(hours=2)).isoformat(),
        "reading": {"title": "Same Run Paper"},
    }

    assert ensure_current_find_research_plan._fragment_payload_is_current(payload, fragment, run_id, current_revision) is True
    assert ensure_current_find_research_plan._fragment_payload_is_current({**payload, "run_id": "find_old"}, fragment, run_id, current_revision) is False


def test_pending_deep_read_synthesis_is_not_full_text_unavailable_conflict():
    row = {
        "title": "Pending Synthesis Paper",
        "full_text_status": "full_text_packet_ready_pending_deep_read_synthesis",
        "reading_status_note_zh": "全文文本证据已抓取，但精读内容仍需项目代理基于正文重写。",
        "pdf_text_chars": 50000,
        "source_evidence": {"full_text_status": "pdf_text_read", "text_chars": 50000, "text_path": "texts/pending.txt"},
    }

    assert ensure_current_find_research_plan._reading_claims_full_text_unavailable(row) is False


def test_selects_higher_quality_same_paper_fragment_over_short_repair():
    find_results = {
        "run_id": "find_quality_selection",
        "strong_recommendations": [
            {"id": "paper-1", "title": "Quality Selection Paper", "url": "https://example.test/paper", "evidence_tier": "strong_recommendation"}
        ],
    }
    full_text_packet = {
        "run_id": "find_quality_selection",
        "papers": [
            {"paper_id": "paper-1", "title": "Quality Selection Paper", "pdf_url": "https://example.test/paper.pdf", "text_path": "texts/paper.txt", "text_chars": 50000}
        ],
    }
    short_repair = {
        "paper_id": "paper-1",
        "title": "Quality Selection Paper",
        "abstract_zh": "论文提出检索基准方法。",
        "motivation_zh": "动机是提升推荐。",
        "method_details_zh": "方法是扩散去噪。",
        "experiments_zh": "实验报告指标。",
        "limitations_zh": "局限是成本。",
        "method_advantages_zh": ["有扩散模块。", "有推荐实验。"],
        "method_disadvantages_zh": ["成本较高。", "泛化未知。"],
        "full_text_status": "pdf_text_read",
        "pdf_text_chars": 50000,
    }
    rich_original = {
        "paper_id": "paper-1",
        "title": "Quality Selection Paper",
        **_v4_deep_read_fields("Quality Selection Paper"),
        "full_text_status": "pdf_text_read",
        "pdf_text_chars": 50000,
        "source_evidence": {"text_chars": 50000, "text_path": "texts/paper.txt"},
        "subagent_deep_read": True,
        "deep_read_audit": {"subagent_used": True, "status": "completed", "mode": "task_subagent"},
    }

    selected = ensure_current_find_research_plan._select_current_find_readings_from_candidates([short_repair, rich_original], find_results, full_text_packet)

    assert len(selected) == 1
    assert selected[0]["method_details_zh"] == rich_original["method_details_zh"]
    valid, report = ensure_current_find_research_plan.validate_claude_readings_against_current_find(selected, find_results, 1)
    assert valid is True
    assert report["full_text_reading_count"] == 1


def test_normalize_clears_stale_packet_conflict_when_row_no_longer_claims_unavailable():
    row = {
        "paper_id": "paper-1",
        "title": "Recovered Conflict Paper",
        **_v4_deep_read_fields("Recovered Conflict Paper"),
        "full_text_status": "pdf_text_read",
        "full_text_available": True,
        "pdf_text_chars": 50000,
        "full_text_packet_conflict": True,
        "full_text_packet_text_path": "texts/old.txt",
        "full_text_packet_text_chars": 50000,
        "reading_status_note_zh": "TASTE 已取得全文文本证据，但当前 reading 仍声明全文不可访问；必须由项目代理打开 text_path 正文后重写精读内容。",
    }
    packet = {
        "run_id": "find_recovered_conflict",
        "papers": [
            {"paper_id": "paper-1", "title": "Recovered Conflict Paper", "pdf_url": "https://example.test/paper.pdf", "text_path": "texts/paper.txt", "text_chars": 50000}
        ],
    }

    normalized = ensure_current_find_research_plan.normalize_readings_full_text_evidence([row], packet)
    clean = normalized[0]

    assert clean.get("full_text_packet_conflict") is None
    assert clean["full_text_status"] == "pdf_text_read"
    assert clean["full_text_available"] is True
    assert clean["pdf_text_read"] is True
    assert "当前 reading 仍声明全文不可访问" not in str(clean.get("reading_status_note_zh") or "")

def test_run_plan_empty_selection_still_syncs_project_plan_state(monkeypatch, tmp_path):
    from auto_research.auto_plan.pipeline import run_plan
    from auto_research.models import AppConfig, PlanRequest
    from auto_research.storage import create_run_dir, delete_run, read_json, write_json

    run_id, directory = create_run_dir("plan_empty_project_sync_test")
    project_root = tmp_path / "root"
    taste_dir = project_root / "projects" / "demo_project" / "planning" / "finding"
    state_dir = project_root / "projects" / "demo_project" / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    try:
        write_json(directory / "ideas.json", {"run_id": run_id, "ideas": [{"id": "pending", "title": "Pending", "status": "pending"}]})
        write_json(taste_dir / "ideas.json", {"run_id": run_id, "ideas": [{"id": "pending", "title": "Pending", "status": "pending"}]})
        write_json(state_dir / "current_find_research_plan.json", {"run_id": run_id, "plans": [{"plan_id": "stale"}]})
        write_json(state_dir / "taste_plan_bridge.json", {"run_id": run_id, "plans_json": {"plans": [{"plan_id": "stale"}]}})
        monkeypatch.setenv("WORKSPACE_ROOT", str(project_root))
        monkeypatch.setenv("PROJECT_ID", "demo_project")

        result = run_plan(PlanRequest(run_id=run_id, idea_ids=["pending"], repair_rounds=1), AppConfig(provider="mock"), log=lambda _msg: None)

        assert result["plans"] == []
        project_plans = read_json(taste_dir / "plans.json", {})
        assert project_plans["run_id"] == run_id
        assert project_plans["plans"] == []
        assert project_plans["selected_plan_id"] == ""
        assert project_plans["execution_policy"]["status"] == "no_selected_plan"
        assert "No approved" not in (taste_dir / "plan.md").read_text(encoding="utf-8")
        state = read_json(state_dir / "current_find_research_plan.json", {})
        assert state["current_find_plan_count"] == 0
        assert state["plans"] == []
        assert state["selected_plan_id"] == ""
        assert state["execution_policy"]["status"] == "no_selected_plan"
        bridge = read_json(state_dir / "taste_plan_bridge.json", {})
        assert bridge["run_id"] == run_id
        assert bridge["plans_json"]["plans"] == []
        assert bridge["selected_plan_id"] == ""
        assert bridge["execution_policy"]["status"] == "no_selected_plan"
    finally:
        delete_run(run_id)


def test_run_plan_reads_project_same_run_ideas_and_syncs_project_plans(monkeypatch, tmp_path):
    from auto_research.auto_plan.pipeline import run_plan
    from auto_research.models import AppConfig, PlanRequest
    from auto_research.storage import create_run_dir, delete_run, read_json, write_json

    run_id, directory = create_run_dir("plan_project_sync_test")
    project_root = tmp_path / "root"
    taste_dir = project_root / "projects" / "demo_project" / "planning" / "finding"
    state_dir = project_root / "projects" / "demo_project" / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    try:
        write_json(
            directory / "ideas.json",
            {
                "run_id": run_id,
                "ideas": [
                    {
                        "id": "idea-1",
                        "title": "Runtime stale title",
                        "new_method": "runtime stale method",
                        "initial_experiment": "runtime stale experiment",
                        "approved": False,
                    }
                ],
            },
        )
        write_json(
            taste_dir / "ideas.json",
            {
                "run_id": run_id,
                "source": "project_agent_current_find_edit",
                "ideas": [
                    {
                        "idea_id": "idea-1",
                        "title": "Project edited semantic diffusion idea",
                        "new_method": "项目侧同一 Find run 写入的新方法，说明语义门控离散检索基准的机制和训练作用点。",
                        "method_details": "用协同专家和语义专家在扩散时间步上动态融合。",
                        "initial_experiment": "基于 PreferGrow 做最小语义门控变体，对比原始 PreferGrow、仅语义重排和语义门控扩散，报告 HR@10、NDCG@10 与长尾坏例。",
                        "inspired_by": [{"title": "Fading to Grow", "reason": "evidence fading retrieval"}],
                        "approved_for_planning": True,
                    }
                ],
            },
        )
        write_json(state_dir / "current_find_research_plan.json", {"run_id": run_id, "ideas": [], "plans": []})
        monkeypatch.setenv("WORKSPACE_ROOT", str(project_root))
        monkeypatch.setenv("PROJECT_ID", "demo_project")
        monkeypatch.setenv("PLAN_USE_LLM", "0")

        result = run_plan(
            PlanRequest(run_id=run_id, idea_ids=["idea-1"], repair_rounds=1),
            AppConfig(provider="mock"),
            log=lambda _msg: None,
        )

        assert len(result["plans"]) == 1
        plan = result["plans"][0]
        assert plan["title"] == "Project edited semantic diffusion idea"
        assert plan["initial_experiment"].startswith("基于 PreferGrow")
        project_plans = read_json(taste_dir / "plans.json", {})
        assert project_plans["run_id"] == run_id
        assert project_plans["plans"][0]["new_method"].startswith("项目侧同一 Find run")
        markdown = (taste_dir / "plan.md").read_text(encoding="utf-8")
        assert "基于 PreferGrow" in markdown
        assert "runtime stale" not in markdown
        state = read_json(state_dir / "current_find_research_plan.json", {})
        assert state["current_find_plan_count"] == 1
        assert state["plans"][0]["plan_id"] == plan["plan_id"]
        bridge = read_json(state_dir / "taste_plan_bridge.json", {})
        assert bridge["run_id"] == run_id
        assert "基于 PreferGrow" in bridge["plan_markdown_excerpt"]
    finally:
        delete_run(run_id)


def test_normalize_does_not_promote_metadata_only_readings(tmp_path, monkeypatch):
    run_id = "find_demo"
    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    find_results = {
        "run_id": run_id,
        "strong_recommendations": [
            {"id": "paper-1", "title": "Full Paper", "url": "https://example.test/paper", "evidence_tier": "strong_recommendation"}
        ],
    }
    ensure_current_find_research_plan.save_json(taste_dir / "find_results.json", find_results)
    paths = type("Paths", (), {"root": project_root, "planning": project_root / "planning", "state": state_dir})()
    takeover = {"status": "completed", "return_code": 0, "finished_at": "2026-01-01T00:00:00+00:00"}
    monkeypatch.setattr(ensure_current_find_research_plan, "load_project_config", lambda project: {"topic": "demo topic"})

    readings, ideas, plans, state_payload = ensure_current_find_research_plan.normalize_claude_outputs_to_current_find_policy(
        "demo_project", paths, run_id, find_results, takeover, 1, 5
    )

    assert readings == []
    assert ideas == []
    assert plans == []
    assert state_payload["status"] == "blocked_current_find_full_text_evidence_pending"
    assert state_payload["failure_type"] == "full_text_evidence_missing"
    assert state_payload["next_required_action"] == "acquire_current_find_full_text_evidence"
    assert state_payload["takeover_ready"] is False
    assert state_payload["claude_current_find_ready"] is False
    assert state_payload["current_find_reading_count"] == 0
    assert state_payload["raw_reading_count"] == 1
    validation = ensure_current_find_research_plan.load_json(state_dir / "current_find_claude_reading_validation.json", {})
    assert validation["valid"] is False
    assert validation["full_text_reading_count"] == 0
    assert validation["pending_full_text_reading_count"] == 1

    read_payload = ensure_current_find_research_plan.load_json(taste_dir / "read_results.json", {})
    idea_payload = ensure_current_find_research_plan.load_json(taste_dir / "ideas.json", {})
    plan_payload = ensure_current_find_research_plan.load_json(taste_dir / "plans.json", {})
    assert read_payload == {}
    assert idea_payload == {}
    assert plan_payload == {}
    assert not (taste_dir / "read.md").exists()
    assert not (taste_dir / "idea.md").exists()
    assert not (taste_dir / "plan.md").exists()




def test_current_find_validation_reports_missing_chinese_deep_read_fields():
    find_results = {
        "run_id": "find_demo_contract_fields",
        "strong_recommendations": [
            {"id": "paper-1", "title": "Contract Field Paper", "url": "https://example.test/paper", "evidence_tier": "strong_recommendation"}
        ],
    }
    english_only = [
        {
            "paper_id": "paper-1",
            "title": "Contract Field Paper",
            "url": "https://example.test/paper",
            "verdict": "core_reading",
            "support_role": "core_method_reference",
            "critique_reason": "",
            "abstract_from_find": "这只是 Find 摘要，不能替代 Claude Code 基于正文写出的中文原论文摘要字段。",
            "relevance": "direct_target",
            "method": "The method uses diffusion denoising with semantic conditions and a ranking head, but this English text must not pass the Chinese deep-read contract.",
            "experiments": "The paper evaluates ranking metrics on several datasets and reports ablations, but this English text must not pass the Chinese deep-read contract.",
            "limitations": "The paper has dataset and labeling limitations, but this English text must not pass the Chinese deep-read contract.",
            "full_text_available": True,
            "full_text_status": "pdf_text_read",
            "pdf_text_chars": 2400,
            "source_evidence": {"text_chars": 2400, "text_path": "texts/contract-field-paper.txt"},
        }
    ]

    valid, report = ensure_current_find_research_plan.validate_claude_readings_against_current_find(english_only, find_results, 1)

    assert valid is False
    assert report["full_text_evidence_count"] == 1
    assert report["full_text_reading_count"] == 0
    assert report["pending_deep_read_synthesis_count"] == 1
    assert any("required Chinese deep-read JSON fields" in item for item in report["blockers"])
    gaps = report["deep_read_content_gap_details"][0]["missing_or_invalid_fields"]
    assert any("abstract_zh" in item for item in gaps)
    assert any("motivation_zh" in item for item in gaps)
    assert any("method_details_zh" in item for item in gaps)
    assert any("experiments_zh" in item for item in gaps)
    assert any("limitations_zh" in item for item in gaps)




def test_current_find_validation_allows_find_original_abstract_as_read_abstract():
    find_results = {
        "run_id": "find_demo_abstract_transfer",
        "strong_recommendations": [
            {
                "id": "paper-1",
                "title": "Original Abstract Paper",
                "url": "https://example.test/paper",
                "evidence_tier": "strong_recommendation",
                "abstract_zh": _v4_deep_read_fields("Original Abstract Paper")["abstract_zh"],
            }
        ],
    }
    fields = _v4_deep_read_fields("Original Abstract Paper")
    reading = {
        "paper_id": "paper-1",
        "title": "Original Abstract Paper",
        "url": "https://example.test/paper",
        "verdict": "core_reading",
        "support_role": "core_method_reference",
        "critique_reason": "",
        **fields,
        "abstract_from_find": fields["abstract_zh"],
        "find_abstract_zh": fields["abstract_zh"],
        "full_text_available": True,
        "full_text_status": "pdf_text_read",
        "pdf_text_chars": 2600,
        "source_evidence": {"text_chars": 2600, "text_path": "texts/original-abstract-paper.txt"},
    }

    valid, report = ensure_current_find_research_plan.validate_claude_readings_against_current_find([reading], find_results, 1)

    assert valid is True
    assert report["full_text_reading_count"] == 1
    assert report["pending_deep_read_synthesis_count"] == 0
    assert report["blockers"] == []


def test_read_markdown_prefers_qualified_original_abstract_over_short_trace():
    fields = _v4_deep_read_fields("Display Abstract Paper")
    row = {
        "paper_id": "paper-1",
        "title": "Display Abstract Paper",
        "venue": "ICLR",
        "year": 2026,
        **fields,
        "abstract_from_find": "短摘要。",
        "find_abstract_zh": "短摘要。",
        "full_text_available": True,
        "full_text_status": "pdf_text_read",
        "pdf_text_chars": 2600,
    }

    markdown = ensure_current_find_research_plan.render_read_md([row], "find_demo_display")

    assert fields["abstract_zh"] in markdown
    assert "\n短摘要。\n\n### 论文动机" not in markdown


def test_deep_read_abstract_preferred_when_find_trace_is_english():
    fields = _v4_deep_read_fields("English Trace Abstract Paper")
    chinese_abstract = fields["abstract_zh"]
    english_trace = (
        "This paper studies diffusion-based recommendation with semantic conditioning, denoising objectives, "
        "ranking losses, multi-dataset evaluation, ablation studies, and efficiency analysis. "
        "It is a long original abstract captured during Find, but it has not been translated and therefore "
        "must not be displayed as the Chinese deep-read abstract or used to fail a valid Chinese deep read. "
        "The trace intentionally contains enough English detail to look like an abstract while remaining English-only. "
    ) * 2
    find_results = {
        "run_id": "find_demo_english_trace",
        "strong_recommendations": [
            {
                "id": "paper-1",
                "title": "English Trace Abstract Paper",
                "url": "https://example.test/paper",
                "evidence_tier": "strong_recommendation",
                "abstract": english_trace,
            }
        ],
    }
    reading = {
        "paper_id": "paper-1",
        "title": "English Trace Abstract Paper",
        "url": "https://example.test/paper",
        "verdict": "core_reading",
        "support_role": "core_method_reference",
        "critique_reason": "",
        **fields,
        "abstract_zh": english_trace,
        "summary": english_trace,
        "abstract_from_find": english_trace,
        "find_abstract_zh": english_trace,
        "deep_read_abstract_zh": chinese_abstract,
        "full_text_available": True,
        "full_text_status": "pdf_text_read",
        "pdf_text_chars": 2600,
        "source_evidence": {"text_chars": 2600, "text_path": "texts/english-trace-abstract-paper.txt"},
    }

    valid, report = ensure_current_find_research_plan.validate_claude_readings_against_current_find([reading], find_results, 1)
    markdown = ensure_current_find_research_plan.render_read_md([reading], "find_demo_english_trace")

    assert valid is True
    assert report["pending_deep_read_synthesis_count"] == 0
    assert not report["deep_read_content_gap_details"]
    assert chinese_abstract in markdown
    assert english_trace[:120] not in markdown


def test_full_text_packet_abstract_updates_short_reading_trace():
    fields = _v4_deep_read_fields("Packet Abstract Paper")
    row = {"paper_id": "paper-1", "title": "Packet Abstract Paper", "abstract_from_find": "短摘要。", "abstract_zh": "短摘要。"}
    packet_entry = {
        "paper_id": "paper-1",
        "title": "Packet Abstract Paper",
        "abstract_from_find": fields["abstract_zh"],
        "pdf_url": "https://example.test/paper.pdf",
        "text_chars": 2600,
        "text_path": "texts/packet-abstract-paper.txt",
    }

    normalized = ensure_current_find_research_plan.normalize_reading_full_text_evidence(row, packet_entry)

    assert normalized["abstract_from_find"] == fields["abstract_zh"]
    assert normalized["find_abstract_zh"] == fields["abstract_zh"]
    assert normalized["abstract_zh"] == fields["abstract_zh"]


def test_current_find_validation_rejects_recommendation_rationale_as_abstract():
    rationale = "推荐精读：该论文因为命中当前主题、具备可借鉴机制和较高评分而进入推荐列表；这只是推荐理由，不是论文原摘要。" * 3
    find_results = {
        "run_id": "find_demo_rationale_abstract",
        "strong_recommendations": [
            {"id": "paper-1", "title": "Rationale Paper", "url": "https://example.test/paper", "evidence_tier": "strong_recommendation"}
        ],
    }
    fields = _v4_deep_read_fields("Rationale Paper")
    fields["abstract_zh"] = rationale
    fields["summary"] = rationale
    reading = {
        "paper_id": "paper-1",
        "title": "Rationale Paper",
        "url": "https://example.test/paper",
        "verdict": "core_reading",
        "support_role": "core_method_reference",
        "critique_reason": "",
        "recommendation_note_zh": rationale,
        **fields,
        "full_text_available": True,
        "full_text_status": "pdf_text_read",
        "pdf_text_chars": 2600,
        "source_evidence": {"text_chars": 2600, "text_path": "texts/rationale-paper.txt"},
    }

    valid, report = ensure_current_find_research_plan.validate_claude_readings_against_current_find([reading], find_results, 1)

    assert valid is False
    gaps = report["deep_read_content_gap_details"][0]["missing_or_invalid_fields"]
    assert any("推荐理由" in item and "abstract_zh" in item for item in gaps)


def test_current_find_validation_uses_find_boundary_role_for_non_positive_recommendation():
    find_results = {
        "run_id": "find_demo_boundary_role",
        "strong_recommendations": [
            {
                "id": "paper-1",
                "title": "Boundary Paper",
                "url": "https://example.test/boundary",
                "evidence_role": "weak_or_boundary",
                "evidence_tier": "critique_or_boundary_case",
                "not_positive_support": True,
                "weak_candidate_for_critique": True,
                "foundation_demoted_from_strong": True,
            }
        ],
    }
    reading = {
        "paper_id": "paper-1",
        "title": "Boundary Paper",
        "url": "https://example.test/boundary",
        "verdict": "recommended_reading",
        "support_role": "direct_target",
        "critique_reason": "",
        **_v4_deep_read_fields("Boundary Paper"),
        "full_text_available": True,
        "full_text_status": "pdf_text_read",
        "pdf_text_chars": 2600,
        "source_evidence": {"text_chars": 2600, "text_path": "texts/boundary-paper.txt"},
    }

    valid, report = ensure_current_find_research_plan.validate_claude_readings_against_current_find([reading], find_results, 1)

    assert valid is True
    assert report["unlabeled_non_positive_count"] == 0
    assert report["critique_or_boundary_count"] == 1
    assert report["unlabeled_non_positive_titles"] == []


def test_compact_validation_for_prompt_includes_boundary_repair_fields():
    validation = {
        "valid": False,
        "unlabeled_non_positive_titles": ["Boundary Paper"],
        "unlabeled_non_positive_count": 1,
        "critique_or_boundary_titles": ["Known Boundary"],
        "critique_or_boundary_count": 1,
        "expected_recommendation_titles": ["Boundary Paper", "Known Boundary"],
        "expected_positive_titles": ["Known Positive"],
        "invalid_positive_count": 0,
    }

    compacted = ensure_current_find_research_plan._compact_validation_for_prompt(validation)

    assert compacted["unlabeled_non_positive_titles"] == ["Boundary Paper"]
    assert compacted["unlabeled_non_positive_count"] == 1
    assert compacted["critique_or_boundary_titles"] == ["Known Boundary"]
    assert compacted["expected_recommendation_titles"] == ["Boundary Paper", "Known Boundary"]


def test_current_find_plan_observed_validation_is_synced_with_top_level():
    stale_validation = {
        "valid": False,
        "actual_reading_count": 20,
        "full_text_reading_count": 15,
        "pending_full_text_reading_count": 5,
        "pending_without_evidence_count": 5,
    }
    fresh_validation = {
        "valid": True,
        "actual_reading_count": 20,
        "full_text_reading_count": 20,
        "pending_full_text_reading_count": 0,
        "pending_without_evidence_count": 0,
    }
    payload = {"reading_validation": stale_validation, "observed": {"reading_validation": stale_validation}}

    synced = ensure_current_find_research_plan._sync_current_find_plan_reading_validation(payload, fresh_validation)

    assert synced["reading_validation"]["valid"] is True
    assert synced["observed"]["reading_validation"]["valid"] is True
    assert synced["observed"]["validation_full_text_reading_count"] == 20
    assert synced["observed"]["validation_pending_full_text_reading_count"] == 0


def test_unread_recommendations_are_pending_full_text():
    find_results = {
        "run_id": "find_unread_validation",
        "strong_recommendations": [
            {"id": "paper-1", "title": "Unread One", "url": "https://example.test/one", "abstract": "This paper has a sufficiently detailed abstract for recommendation scoring.", "abstract_zh": "这是一篇具有真实摘要的推荐论文。", "fit_score": 8.0, "llm_fit_score": 8.0, "score_source": "llm_title_abstract_score_only", "reason_source": "llm abstract evaluation", "find_recommendation": True, "recommended_by_llm_ranking": True},
            {"id": "paper-2", "title": "Unread Two", "url": "https://example.test/two", "abstract": "This second paper also has a sufficiently detailed abstract for recommendation scoring.", "abstract_zh": "这是第二篇具有真实摘要的推荐论文。", "fit_score": 8.0, "llm_fit_score": 8.0, "score_source": "llm_title_abstract_score_only", "reason_source": "llm abstract evaluation", "find_recommendation": True, "recommended_by_llm_ranking": True},
        ],
    }

    valid, report = ensure_current_find_research_plan.validate_claude_readings_against_current_find([], find_results, 2)

    assert valid is False
    assert report["expected_recommendation_count"] == 2
    assert report["actual_reading_count"] == 0
    assert report["pending_full_text_reading_count"] == 2
    assert report["pending_without_evidence_count"] == 2
    assert report["pending_full_text_reading_titles"] == ["Unread One", "Unread Two"]


def test_load_claude_outputs_overwrites_stale_validation_when_artifacts_pending(tmp_path):
    run_id = "find_current_pending"
    taste_dir = tmp_path / "planning" / "finding"
    state_dir = tmp_path / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    find_results = {
        "run_id": run_id,
        "strong_recommendations": [
            {"id": "paper-1", "title": "Current Pending Paper", "url": "https://example.test/current", "abstract": "This paper has a sufficiently detailed abstract for recommendation scoring.", "abstract_zh": "这是一篇具有真实摘要的当前推荐论文。", "fit_score": 8.0, "llm_fit_score": 8.0, "score_source": "llm_title_abstract_score_only", "reason_source": "llm abstract evaluation", "find_recommendation": True, "recommended_by_llm_ranking": True}
        ],
    }
    ensure_current_find_research_plan.save_json(taste_dir / "read_results.json", {"run_id": run_id, "source": "pending_new_find_read", "readings": []})
    ensure_current_find_research_plan.save_json(taste_dir / "ideas.json", {"run_id": run_id, "source": "pending_new_find_idea", "ideas": []})
    ensure_current_find_research_plan.save_json(taste_dir / "plans.json", {"run_id": run_id, "source": "pending_new_find_plan", "plans": []})
    ensure_current_find_research_plan.save_json(state_dir / "current_find_claude_reading_validation.json", {"run_id": "find_old", "valid": True, "actual_reading_count": 20, "full_text_reading_count": 20, "pending_full_text_reading_count": 0})

    readings, ideas, plans = ensure_current_find_research_plan.load_claude_outputs(taste_dir, run_id, find_results, 1, state_dir, None)
    validation = ensure_current_find_research_plan.load_json(state_dir / "current_find_claude_reading_validation.json", {})

    assert readings == []
    assert ideas == []
    assert plans == []
    assert validation["run_id"] == run_id
    assert validation["valid"] is False
    assert validation["pending_full_text_reading_count"] == 1
    assert validation["artifact_status"] == "pending_current_claude_read_idea_plan"


def test_load_claude_outputs_keeps_fragment_readings_when_ideas_and_plans_are_stale(tmp_path):
    run_id = "find_demo_fragment_readings_stale_idea_plan"
    taste_dir = tmp_path / "planning" / "finding"
    state_dir = tmp_path / "state"
    fragment_dir = taste_dir / "current_find_deep_read_fragments"
    fragment_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    find_results = {
        "run_id": run_id,
        "strong_recommendations": [
            {"id": "paper-1", "title": "Fragment Paper", "url": "https://example.test/paper", "evidence_tier": "strong_recommendation"}
        ],
    }
    reading = {
        "paper_id": "paper-1",
        "title": "Fragment Paper",
        "url": "https://example.test/paper",
        "verdict": "core_reading",
        "support_role": "core_method_reference",
        "critique_reason": "",
        **_v4_deep_read_fields("Fragment Paper"),
        "full_text_available": True,
        "full_text_status": "pdf_text_read",
        "pdf_text_chars": 6000,
        "source_evidence": {"text_chars": 6000, "text_path": "texts/fragment-paper.txt"},
        "subagent_deep_read": True,
        "deep_read_audit": {"mode": "task_subagent", "subagent_used": True, "status": "completed", "text_path": "texts/fragment-paper.txt", "evidence_chars": 6000},
    }
    ensure_current_find_research_plan.save_json(
        fragment_dir / "1_paper-1.json",
        {"run_id": run_id, "source": ensure_current_find_research_plan.CURRENT_FIND_DEEP_READ_FRAGMENT_SOURCE, "reading": reading},
    )
    stale_time = "2026-01-01T00:00:00+00:00"
    ensure_current_find_research_plan.save_json(taste_dir / "read_results.json", {"run_id": run_id, "source": "pending_new_find_read", "readings": []})
    ensure_current_find_research_plan.save_json(taste_dir / "ideas.json", {"run_id": run_id, "source": ensure_current_find_research_plan.CLAUDE_TAKEOVER_SOURCE, "generated_at": stale_time, "ideas": [_ready_scored_idea(idx, source_title="Fragment Paper") for idx in range(5)]})
    ensure_current_find_research_plan.save_json(taste_dir / "plans.json", {"run_id": run_id, "source": ensure_current_find_research_plan.CLAUDE_TAKEOVER_SOURCE, "generated_at": stale_time, "plans": [{"plan_id": f"plan-{idx}", "idea_id": f"idea-{idx}", "steps": ["等待环境阶段选择基底并执行同协议实验"]} for idx in range(5)]})
    current_revision = ensure_current_find_research_plan.dt.datetime.now(ensure_current_find_research_plan.dt.timezone.utc) - ensure_current_find_research_plan.dt.timedelta(seconds=30)

    readings, loaded_ideas, loaded_plans = ensure_current_find_research_plan.load_claude_outputs(taste_dir, run_id, find_results, 1, state_dir, current_revision)
    validation = ensure_current_find_research_plan.load_json(state_dir / "current_find_claude_reading_validation.json", {})

    assert len(readings) == 1
    assert readings[0]["title"] == "Fragment Paper"
    assert loaded_ideas == []
    assert loaded_plans == []
    assert validation["valid"] is True
    assert validation["full_text_reading_count"] == 1
    assert validation["pending_full_text_reading_count"] == 0
    assert validation["idea_plan_artifact_status"] == "stale_or_pending_current_claude_idea_plan"


def test_deep_read_audit_evidence_counts_as_full_text_and_prefers_repair_fragment():
    old = {
        "paper_id": "paper-demo",
        "title": "Repair Evidence Paper",
        "abstract_zh": "旧摘要说明方法和实验。",
        "motivation_zh": "旧动机字段足够长，用于触发精读内容合同。",
        "method_details_zh": "旧方法字段已经包含一些内容，但没有全文证据。" * 20,
        "experiments_zh": "旧实验字段已经包含一些内容，但没有全文证据。" * 12,
        "limitations_zh": "旧局限字段。" * 20,
        "method_advantages_zh": ["旧优点。"],
        "method_disadvantages_zh": ["旧缺点。"],
        "full_text_available": False,
        "full_text_status": "abstract_only_icml_no_pdf",
        "subagent_deep_read": True,
        "deep_read_audit": {"mode": "task_subagent", "subagent_used": True, "status": "completed", "evidence_chars": 0},
    }
    repair = dict(old)
    repair.update({
        "full_text_available": True,
        "full_text_status": "pdf_text_read",
        "method_details_zh": "修复后的方法字段基于全文，详细说明模型、目标函数、训练和推理流程。" * 30,
        "experiments_zh": "修复后的实验字段基于全文，详细说明数据集、指标、基线、消融和主要结果。" * 20,
        "deep_read_audit": {
            "mode": "task_subagent",
            "subagent_used": True,
            "status": "completed",
            "text_path": "planning/finding/full_text_reading/texts/paper-demo.txt",
            "evidence_chars": 12345,
        },
    })

    assert ensure_current_find_research_plan._reading_has_full_text_evidence(repair) is True
    assert ensure_current_find_research_plan._reading_quality_score(repair) > ensure_current_find_research_plan._reading_quality_score(old)

    candidate_index = {}
    old_with_url = {**old, "url": "https://example.test/paper-demo"}
    for candidate in [old_with_url, repair]:
        for identity in ensure_current_find_research_plan._reading_identity_values(candidate):
            ensure_current_find_research_plan._index_better_candidate(candidate_index, identity, candidate)
    find_row = {"id": "paper-demo", "title": "Repair Evidence Paper", "url": "https://example.test/paper-demo"}
    selected = ensure_current_find_research_plan._best_candidate_for_find_row(candidate_index, [old_with_url, repair], find_row)
    assert selected is repair


def test_normalize_absorbs_current_find_deep_read_fragments_when_read_results_pending(tmp_path, monkeypatch):
    run_id = "find_demo_normalize_fragments"
    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    fragment_dir = taste_dir / ensure_current_find_research_plan.CURRENT_FIND_DEEP_READ_FRAGMENT_DIR_NAME
    fragment_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    title = "Fragment Normalization Paper"
    find_results = {
        "run_id": run_id,
        "strong_recommendations": [
            {"id": "paper-1", "title": title, "url": "https://example.test/fragment", "pdf_url": "https://example.test/fragment.pdf", "venue": "ICLR", "year": 2026, "score": 9.1, "evidence_tier": "strong_recommendation", "find_recommendation": True, "recommended_by_llm_ranking": True, "abstract": "A complete abstract about retrieval benchmark experiments and limitations.", "abstract_zh": "本文提供一个用于测试的检索基准论文摘要，包含方法、实验和局限。"}
        ],
    }
    reading = {
        "paper_id": "paper-1",
        "title": title,
        "url": "https://example.test/fragment",
        "pdf_url": "https://example.test/fragment.pdf",
        "venue": "ICLR",
        "year": 2026,
        "verdict": "core_reading",
        "support_role": "core_method_reference",
        "critique_reason": "",
        "full_text_available": True,
        "full_text_status": "pdf_text_read",
        "pdf_text_chars": 9000,
        "source_evidence": {"text_chars": 9000, "text_path": "texts/fragment.txt"},
        "subagent_deep_read": True,
        "deep_read_audit": {"mode": "task_subagent", "subagent_used": True, "status": "completed", "text_path": "texts/fragment.txt", "evidence_chars": 9000},
    }
    reading.update(_v4_deep_read_fields(title))
    ensure_current_find_research_plan.save_json(fragment_dir / "1_paper-1.json", {"run_id": run_id, "source": ensure_current_find_research_plan.CURRENT_FIND_DEEP_READ_FRAGMENT_SOURCE, "reading": reading})
    ensure_current_find_research_plan.save_json(taste_dir / "read_results.json", {"run_id": run_id, "source": "pending_new_find_read", "readings": []})
    ensure_current_find_research_plan.save_json(taste_dir / "ideas.json", {"run_id": run_id, "source": ensure_current_find_research_plan.CLAUDE_TAKEOVER_SOURCE, "ideas": [_ready_scored_idea(1, source_title=title)]})
    ensure_current_find_research_plan.save_json(taste_dir / "plans.json", {"run_id": run_id, "source": ensure_current_find_research_plan.CLAUDE_TAKEOVER_SOURCE, "plans": [{"plan_id": "plan-1", "idea_id": "idea-1", "title": "Fragment execution plan", "steps": ["基于当前精读证据执行同协议实验。"]}]})
    paths = type("Paths", (), {"root": project_root, "planning": project_root / "planning", "state": state_dir})()
    current_revision = ensure_current_find_research_plan.dt.datetime.now(ensure_current_find_research_plan.dt.timezone.utc) - ensure_current_find_research_plan.dt.timedelta(seconds=30)
    takeover = {"status": "completed", "return_code": 0, "find_results_updated_at": current_revision.isoformat()}
    monkeypatch.setattr(ensure_current_find_research_plan, "load_project_config", lambda project: {"topic": "demo topic"})

    readings, ideas, plans, state_payload = ensure_current_find_research_plan.normalize_claude_outputs_to_current_find_policy(
        "demo_project", paths, run_id, find_results, takeover, 1, 1
    )

    assert len(readings) == 1
    assert readings[0]["title"] == title
    assert readings[0]["full_text_status"] == "pdf_text_read"
    assert len(ideas) == 1
    assert len(plans) == 1
    assert state_payload["run_id"] == run_id
    validation = ensure_current_find_research_plan.load_json(state_dir / "current_find_claude_reading_validation.json", {})
    assert validation["valid"] is True
    assert validation["full_text_reading_count"] == 1
    assert validation["pending_full_text_reading_count"] == 0
    read_payload = ensure_current_find_research_plan.load_json(taste_dir / "read_results.json", {})
    assert read_payload["readings"][0]["title"] == title
    assert "精读等待执行" not in (taste_dir / "read.md").read_text(encoding="utf-8")


def test_load_claude_outputs_uses_file_mtime_when_generated_at_missing(tmp_path):
    run_id = "find_demo_mtime_current"
    taste_dir = tmp_path / "planning" / "finding"
    state_dir = tmp_path / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    find_results = {
        "run_id": run_id,
        "strong_recommendations": [
            {"id": "paper-1", "title": "Mtime Paper", "url": "https://example.test/paper", "evidence_tier": "strong_recommendation"}
        ],
    }
    reading = {
        "paper_id": "paper-1",
        "title": "Mtime Paper",
        "url": "https://example.test/paper",
        "verdict": "core_reading",
        "support_role": "core_method_reference",
        "critique_reason": "",
        **_v4_deep_read_fields("Mtime Paper"),
        "full_text_available": True,
        "full_text_status": "pdf_text_read",
        "pdf_text_chars": 2500,
        "source_evidence": {"text_chars": 2500, "text_path": "texts/mtime-paper.txt"},
    }
    ideas = [
        {
            "id": f"idea-{idx}",
            "title": f"Idea {idx}",
            "status": "approved",
            "new_method": "设计一个结合扩散去噪和语义条件的推荐模块，明确输入、门控、训练目标、反向重建位置和为什么能改善推荐排序。",
            "initial_experiment": "基于环境阶段选出的可审计基底做最小模块替换，说明替换的文件和模块，对比 baseline、control、ablation，记录 Recall、NDCG 和坏例切片。",
            "inspired_by": [{"title": "Mtime Paper", "reason": "条件检索基准机制"}],
        }
        for idx in range(5)
    ]
    plans = [{"plan_id": f"plan-{idx}", "idea_id": f"idea-{idx}", "steps": ["等待环境阶段选择基底并执行同协议实验"]} for idx in range(5)]
    ensure_current_find_research_plan.save_json(taste_dir / "read_results.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "readings": [reading]})
    ensure_current_find_research_plan.save_json(taste_dir / "ideas.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "ideas": ideas})
    ensure_current_find_research_plan.save_json(taste_dir / "plans.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "plans": plans})
    current_revision = ensure_current_find_research_plan.dt.datetime.now(ensure_current_find_research_plan.dt.timezone.utc) - ensure_current_find_research_plan.dt.timedelta(seconds=30)
    for path in [taste_dir / "read_results.json", taste_dir / "ideas.json", taste_dir / "plans.json"]:
        path.touch()

    readings, loaded_ideas, loaded_plans = ensure_current_find_research_plan.load_claude_outputs(taste_dir, run_id, find_results, 1, state_dir, current_revision)
    validation = ensure_current_find_research_plan.load_json(state_dir / "current_find_claude_reading_validation.json", {})

    assert len(readings) == 1
    assert len(loaded_ideas) == 5
    assert len(loaded_plans) == 5
    assert validation["valid"] is True
    assert validation["full_text_reading_count"] == 1



def test_full_text_packet_update_makes_claude_outputs_stale(tmp_path):
    run_id = "find_demo_full_text_packet_revision"
    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    (taste_dir / "full_text_reading").mkdir(parents=True)
    state_dir.mkdir(parents=True)
    find_results = {
        "run_id": run_id,
        "strong_recommendations": [
            {"id": "paper-1", "title": "Packet Paper", "url": "https://example.test/paper", "evidence_tier": "strong_recommendation"}
        ],
    }
    ensure_current_find_research_plan.save_json(taste_dir / "find_results.json", find_results)
    old_time = "2026-01-01T00:00:00+00:00"
    reading = {
        "paper_id": "paper-1",
        "title": "Packet Paper",
        "url": "https://example.test/paper",
        "verdict": "core_reading",
        "support_role": "core_method_reference",
        "abstract_zh": "本文围绕检索基准模型展开，说明如何利用全文证据分析论文动机、方法和实验，并报告推荐系统实验结果。",
        "motivation_zh": "论文动机是解决检索基准中的语义条件和协同偏好融合问题。",
        "method_details_zh": "论文方法构造一个条件化检索基准模型，在前向过程加入偏好扰动，在反向过程中结合用户历史、物品语义和时间步嵌入恢复偏好表示，并通过排序损失输出候选物品分数。",
        "experiments_zh": "实验比较多个推荐基线，报告 Recall、NDCG 等指标，并通过移除语义条件、时间步编码和排序损失验证模块贡献。",
        "limitations_zh": "局限包括采样成本较高、语义表示质量敏感以及负采样协议会影响指标。",
        "full_text_available": True,
        "full_text_status": "pdf_text_read",
        "pdf_text_chars": 2500,
        "source_evidence": {"text_chars": 2500, "text_path": "texts/old.txt"},
    }
    ideas = [
        {
            "id": f"idea-{idx}",
            "title": f"Idea {idx}",
            "status": "approved",
            "new_method": "设计一个结合扩散去噪和语义条件的推荐模块，明确输入、门控、训练目标、反向重建位置和为什么能改善推荐排序。",
            "initial_experiment": "基于环境阶段选出的可审计基底做最小模块替换，说明替换的文件和模块，对比 baseline、control、ablation，记录 Recall、NDCG 和坏例切片。",
            "inspired_by": [{"title": "Packet Paper", "reason": "条件检索基准机制"}],
        }
        for idx in range(5)
    ]
    plans = [{"plan_id": f"plan-{idx}", "idea_id": f"idea-{idx}", "steps": ["等待环境阶段选择基底并执行同协议实验"]} for idx in range(5)]
    ensure_current_find_research_plan.save_json(taste_dir / "read_results.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "generated_at": old_time, "readings": [reading]})
    ensure_current_find_research_plan.save_json(taste_dir / "ideas.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "generated_at": old_time, "ideas": ideas})
    ensure_current_find_research_plan.save_json(taste_dir / "plans.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "generated_at": old_time, "plans": plans})
    old_timestamp = ensure_current_find_research_plan.parse_iso_time(old_time).timestamp()
    for artifact in [taste_dir / "read_results.json", taste_dir / "ideas.json", taste_dir / "plans.json"]:
        os.utime(artifact, (old_timestamp, old_timestamp))
    ensure_current_find_research_plan.save_json(
        taste_dir / "full_text_reading" / "full_text_packet.json",
        {
            "run_id": run_id,
            "updated_at": "2026-01-02T00:00:00+00:00",
            "papers": [{"title": "Packet Paper", "text_chars": 3000, "text_path": "texts/new.txt", "pdf_url": "https://example.test/new.pdf"}],
        },
    )
    paths = type("Paths", (), {"planning": project_root / "planning", "state": state_dir})()
    revision = ensure_current_find_research_plan.current_find_revision_time(paths, find_results)

    readings, loaded_ideas, loaded_plans = ensure_current_find_research_plan.load_claude_outputs(taste_dir, run_id, find_results, 1, state_dir, revision)

    assert revision and revision > ensure_current_find_research_plan.parse_iso_time(old_time)
    assert readings == []
    assert loaded_ideas == []
    assert loaded_plans == []


def test_load_claude_outputs_accepts_chinese_deep_read_schema_without_legacy_fields(tmp_path):
    run_id = "find_demo_chinese_schema"
    taste_dir = tmp_path / "planning" / "finding"
    state_dir = tmp_path / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    find_results = {
        "run_id": run_id,
        "strong_recommendations": [
            {"id": "paper-1", "title": "Chinese Schema Paper", "url": "https://example.test/paper", "evidence_tier": "strong_recommendation"}
        ],
    }
    fields = _v4_deep_read_fields("Chinese Schema Paper")
    fields.pop("method", None)
    reading = {
        "paper_id": "paper-1",
        "title": "Chinese Schema Paper",
        "url": "https://example.test/paper",
        "verdict": "core_reading",
        "support_role": "core_method_reference",
        **fields,
        "full_text_available": True,
        "full_text_status": "pdf_text_read",
        "pdf_text_chars": 2600,
        "source_evidence": {"text_chars": 2600, "text_path": "texts/chinese-schema-paper.txt"},
    }
    ideas = [
        {
            "id": f"idea-{idx}",
            "title": f"Idea {idx}",
            "status": "approved",
            "new_method": "设计一个语义门控的离散检索基准模块，明确协同专家、语义专家和扩散时间步门控的输入输出，并说明该模块如何改善推荐排序。",
            "initial_experiment": "基于环境阶段选出的可审计检索基准基底做最小模块替换，对比 baseline、仅语义重排、语义门控扩散和去门控消融，记录 Recall、NDCG 与语义冲突坏例。",
            "inspired_by": [{"title": "Chinese Schema Paper", "reason": "语义条件检索基准机制"}],
        }
        for idx in range(5)
    ]
    plans = [{"plan_id": f"plan-{idx}", "idea_id": f"idea-{idx}", "steps": ["按同一协议执行 baseline、candidate 和 ablation"]} for idx in range(5)]
    ensure_current_find_research_plan.save_json(taste_dir / "read_results.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "readings": [reading]})
    ensure_current_find_research_plan.save_json(taste_dir / "ideas.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "ideas": ideas})
    ensure_current_find_research_plan.save_json(taste_dir / "plans.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "plans": plans})

    readings, loaded_ideas, loaded_plans = ensure_current_find_research_plan.load_claude_outputs(taste_dir, run_id, find_results, 1, state_dir, None)
    validation = ensure_current_find_research_plan.load_json(state_dir / "current_find_claude_reading_validation.json", {})

    assert len(readings) == 1
    assert len(loaded_ideas) == 5
    assert len(loaded_plans) == 5
    assert validation["valid"] is True
    assert validation["full_text_reading_count"] == 1
    assert "relevance" not in readings[0]
    assert "method" not in readings[0]
    assert readings[0]["method_details_zh"].startswith("论文方法先把用户交互")


def test_load_claude_outputs_reports_corrupt_current_find_json(tmp_path):
    run_id = "find_demo_corrupt_json"
    taste_dir = tmp_path / "planning" / "finding"
    state_dir = tmp_path / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    find_results = {"run_id": run_id, "strong_recommendations": [{"id": "paper-1", "title": "Corrupt Artifact Paper"}]}
    (taste_dir / "read_results.json").write_text('{"run_id": "find_demo_corrupt_json", "source": "claude_code_current_find_takeover", "readings": [', encoding="utf-8")
    ensure_current_find_research_plan.save_json(taste_dir / "ideas.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "ideas": []})
    ensure_current_find_research_plan.save_json(taste_dir / "plans.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "plans": []})

    readings, ideas, plans = ensure_current_find_research_plan.load_claude_outputs(taste_dir, run_id, find_results, 1, state_dir, None)
    validation = ensure_current_find_research_plan.load_json(state_dir / "current_find_claude_reading_validation.json", {})
    parse_failure = ensure_current_find_research_plan.load_json(state_dir / "current_find_claude_artifact_parse_failure.json", {})

    assert readings == []
    assert ideas == []
    assert plans == []
    assert validation["status"] == "artifact_parse_failed"
    assert validation["valid"] is False
    assert validation["artifact_parse_failures"][0]["artifact"] == "read_results.json"
    assert validation["artifact_parse_failures"][0]["error_type"] == "json_decode_error"
    assert "one complete Claude Write" in validation["blockers"][0]
    assert parse_failure["status"] == "artifact_parse_failed"
    assert ensure_current_find_research_plan.current_reading_validation_requires_fresh_takeover(validation, run_id)


def test_load_claude_outputs_sanitizes_reading_public_text(tmp_path):
    run_id = "find_demo_sanitize"
    taste_dir = tmp_path / "planning" / "finding"
    state_dir = tmp_path / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    dirty_method = "详细方法：扩散去噪重排候选内容。\n实验与证据限制：摘要级线索不能当作本地实验结果。"
    dirty_reading = {
        "paper_id": "paper-1",
        "title": "Full Paper",
        "verdict": "core_reading",
        "summary": "论文完整讨论可信检索基准问题，给出用户偏好扰动、反向去噪重建、可信子空间约束和多数据集评测，因此可以作为当前精读条目。",
        "abstract_zh": "论文完整讨论可信检索基准问题，给出用户偏好扰动、反向去噪重建、可信子空间约束和多数据集评测，因此可以作为当前精读条目。",
        "motivation_zh": "推荐系统和扩散模型相关。project_topic 命中当前主题。论文动机是让检索基准不仅优化点击或排序准确率，还显式降低不可信内容被推荐的风险。",
        "relevance": "推荐系统和扩散模型相关。project_topic 命中当前主题。论文动机是让检索基准不仅优化点击或排序准确率，还显式降低不可信内容被推荐的风险。",
        "method": "论文方法先用扩散过程扰动用户交互，再在反向重建中分离偏好相关信号和不可信内容信号，并用子空间投影抑制不可信方向。模型还把重建目标、可信约束和候选排序目标联合起来，使去噪网络同时学习协同偏好、内容风险方向和最终排序分数。" + dirty_method + "模型通过联合重建目标和可信约束训练，使生成候选同时保留协同过滤信息和内容可信度控制。",
        "method_details_zh": "论文方法先用扩散过程扰动用户交互，再在反向重建中分离偏好相关信号和不可信内容信号，并用子空间投影抑制不可信方向。模型还把重建目标、可信约束和候选排序目标联合起来，使去噪网络同时学习协同偏好、内容风险方向和最终排序分数。" + dirty_method + "模型通过联合重建目标和可信约束训练，使生成候选同时保留协同过滤信息和内容可信度控制。",
        "experiments": "实验比较检索基准、传统推荐和可信约束变体，在多个真实内容推荐数据集上报告准确率、可信度和消融结果，并进一步用去除投影、去除可信约束和只保留重建目标的消融确认各模块作用。Strong/foundation anchors may guide planning, but only local repo/data/env/experiment gate can support paper claims.",
        "experiments_zh": "实验比较检索基准、传统推荐和可信约束变体，在多个真实内容推荐数据集上报告准确率、可信度和消融结果，并进一步用去除投影、去除可信约束和只保留重建目标的消融确认各模块作用。Strong/foundation anchors may guide planning, but only local repo/data/env/experiment gate can support paper claims.",
        "limitations": "局限包括可信标签依赖、子空间估计误差和跨领域迁移不确定性，且论文没有证明该机制可直接覆盖所有序列推荐或冷启动设置。Guardrail: no claim promotion.",
        "limitations_zh": "局限包括可信标签依赖、子空间估计误差和跨领域迁移不确定性，且论文没有证明该机制可直接覆盖所有序列推荐或冷启动设置。Guardrail: no claim promotion.",
        "support_role": "core_method_reference",
        "critique_reason": "对系统实现的直接含义：该条目是当前用户可见推荐文章，必须进入精读。",
        "full_text_available": True,
        "full_text_status": "pdf_text_read",
        "source_evidence": {"pdf_text_chars": 2000},
        "method_advantages_zh": ["paper claim 不能直接写。"],
        "method_disadvantages_zh": ["论文 claim 仍需实验。"],
    }
    ideas = [
        {
            "id": f"idea-{idx}",
            "title": f"Idea {idx}",
            "status": "approved",
            "new_method": "设计一个结合扩散去噪和语义条件的推荐模块，明确输入、门控、训练目标、反向重建位置和为什么能改善可信推荐排序。",
            "initial_experiment": "基于环境阶段选出的可审计基底做最小模块替换，说明替换的文件和模块，对比 baseline/control/ablation，记录 HR、NDCG、运行日志和坏例切片。",
            "inspired_by": [{"title": "Full Paper", "reason": "可信检索基准机制"}],
        }
        for idx in range(5)
    ]
    plans = [{"plan_id": f"plan-{idx}", "idea_id": f"idea-{idx}", "steps": ["等待环境阶段选择基底"]} for idx in range(5)]
    ensure_current_find_research_plan.save_json(taste_dir / "read_results.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "readings": [dirty_reading]})
    ensure_current_find_research_plan.save_json(taste_dir / "ideas.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "ideas": ideas})
    ensure_current_find_research_plan.save_json(taste_dir / "plans.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "plans": plans})
    find_results = {"run_id": run_id, "strong_recommendations": [{"id": "paper-1", "title": "Full Paper", "url": "https://example.test/paper"}]}

    readings, loaded_ideas, loaded_plans = ensure_current_find_research_plan.load_claude_outputs(taste_dir, run_id, find_results, 1, state_dir, None)

    assert len(readings) == 1
    assert len(loaded_ideas) == 5
    assert len(loaded_plans) == 5
    markdown = ensure_current_find_research_plan.render_read_md(readings, run_id)
    combined = str(readings) + markdown
    forbidden = ["对系统实现的直接含义", "Guardrail", "project_topic", "摘要级线索", "Strong/foundation", "paper claim", "论文 claim", "claim promotion", "实验与证据限制"]
    assert not any(item in combined for item in forbidden)
    assert "### 详细方法" in markdown
    assert "## 方法差异、优缺点总览" in markdown

def test_current_find_tool_policy_allows_heredoc_document_text_but_blocks_training():
    import importlib.util

    spec = importlib.util.spec_from_file_location("claude_project_session", SCRIPTS / "claude_project_session.py")
    claude_project_session = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(claude_project_session)

    harmless_builder = """cat << 'PYEOF' > /tmp/build_plans.py
plan = {}
plan[\"repo_data_audit\"] = \"Read ReferenceRec README, main.py, finetune.py. Check Science & Nature references.\"
plan[\"bad_case_slices\"] = [\"Long-tail items (<5 interactions)\"]
PYEOF
python3 /tmp/build_plans.py
"""
    assert claude_project_session.bash_command_tool_policy_issue(
        harmless_builder,
        "demo_project",
        "current-find-claude-read-idea-plan",
    ) == ""

    readonly_tail_monitor = """tail -f /workspace/taste/projects/sample_project/artifacts/sample_run/stdout_stderr.log &
BGPID=$!
sleep 180 && kill $BGPID 2>/dev/null
"""
    assert claude_project_session.bash_command_tool_policy_issue(
        readonly_tail_monitor,
        "sample_project",
        "experiment",
    ) == ""

    readonly_delayed_tail = "sleep 300 && tail -30 /workspace/taste/projects/sample_project/artifacts/run/stdout_stderr.log 2>/dev/null"
    assert claude_project_session.bash_command_tool_policy_issue(
        readonly_delayed_tail,
        "sample_project",
        "experiment",
    ) == ""

    naked_training = "/opt/conda/envs/project_env/bin/python -u train_diffusion.py --data ATV"
    assert "launcher contract" in claude_project_session.bash_command_tool_policy_issue(
        naked_training,
        "sample_project",
        "experiment",
    )

    claude_project_session.allowed_experiment_pythons = lambda project: {"/opt/conda/envs/project_env/bin/python"}
    launcher_training = "/opt/conda/envs/env/bin/python scripts/launch_experiment_run.py --project sample_project --artifact-name demo --cwd /tmp -- /opt/conda/envs/project_env/bin/python -u train_diffusion.py --data ATV"
    assert claude_project_session.bash_command_tool_policy_issue(
        launcher_training,
        "sample_project",
        "experiment",
    ) == ""


def test_main_blocks_failed_current_find_takeover_without_normalized_artifacts(tmp_path, monkeypatch, capsys):
    run_id = "find_demo_failed_takeover"
    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    find_results = {
        "run_id": run_id,
        "strong_recommendations": [
            {"id": "paper-1", "title": "Full Paper", "url": "https://example.test/paper", "evidence_tier": "strong_recommendation"}
        ],
    }
    ensure_current_find_research_plan.save_json(taste_dir / "find_results.json", find_results)
    packet_text_dir = taste_dir / "full_text_reading" / "texts"
    packet_text_dir.mkdir(parents=True)
    packet_text = packet_text_dir / "full-paper.txt"
    packet_text.write_text(
        "Full Paper abstract introduction method experiments evaluation results conclusion references " * 500,
        encoding="utf-8",
    )
    ensure_current_find_research_plan.save_json(
        taste_dir / "full_text_reading" / "full_text_packet.json",
        {
            "run_id": run_id,
            "papers": [
                {"paper_id": "paper-1", "title": "Full Paper", "text_path": str(packet_text), "text_chars": packet_text.stat().st_size}
            ],
        },
    )
    paths = type("Paths", (), {"planning": project_root / "planning", "state": state_dir})()

    monkeypatch.setattr(ensure_current_find_research_plan, "build_paths", lambda project: paths)
    monkeypatch.setattr(ensure_current_find_research_plan, "load_project_config", lambda project: {"topic": "demo topic"})
    monkeypatch.setattr(ensure_current_find_research_plan, "current_find_revision_time", lambda _paths, _find_results: None)
    monkeypatch.setattr(ensure_current_find_research_plan, "_find_run_changed", lambda _paths, _run_id: "")
    monkeypatch.setattr(ensure_current_find_research_plan, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(ensure_current_find_research_plan, "LEGACY_RUNS_DIR", tmp_path / "legacy_runs")

    def fake_takeover(project, paths_arg, run_id_arg, read_limit, idea_count, repair_validation=None, attempt=1):
        assert project == "demo_project"
        assert run_id_arg == run_id
        return {
            "status": "blocked_tool_policy",
            "return_code": 3,
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:00:01+00:00",
            "prompt_path": str(state_dir / "current_find_claude_takeover_prompt.md"),
        }

    monkeypatch.setattr(ensure_current_find_research_plan, "run_claude_current_find_takeover", fake_takeover)
    monkeypatch.setattr(sys, "argv", ["ensure_current_find_research_plan.py", "--project", "demo_project", "--read-limit", "0", "--idea-count", "5", "--force"])

    rc = ensure_current_find_research_plan.main()
    captured = capsys.readouterr().out

    assert rc == 2
    assert "normalized_after_failed_takeover" not in captured
    assert "deterministic compatibility templates are disabled" in captured
    assert "claude_current_find_takeover_failed" in captured
    read_payload = ensure_current_find_research_plan.load_json(taste_dir / "read_results.json", {})
    idea_payload = ensure_current_find_research_plan.load_json(taste_dir / "ideas.json", {})
    plan_payload = ensure_current_find_research_plan.load_json(taste_dir / "plans.json", {})
    state_payload = ensure_current_find_research_plan.load_json(state_dir / "current_find_research_plan.json", {})
    takeover_payload = ensure_current_find_research_plan.load_json(state_dir / "current_find_claude_takeover_result.json", {})
    validation = ensure_current_find_research_plan.load_json(state_dir / "current_find_claude_reading_validation.json", {})

    assert read_payload == {}
    assert idea_payload == {}
    assert plan_payload == {}
    assert not (taste_dir / "read.md").exists()
    assert state_payload["status"] == "blocked_claude_current_find_takeover_incomplete"
    assert state_payload["failure_type"] == "claude_current_find_takeover_failed"
    assert state_payload["next_required_action"] == "rerun_current_find_claude_takeover_after_process_failure"
    assert state_payload["takeover_ready"] is False
    assert state_payload["claude_current_find_ready"] is False
    assert state_payload["observed"]["takeover_return_code"] == 3
    assert takeover_payload["contract_validation_valid"] is False
    assert takeover_payload["contract_failure"]["failure_type"] == "claude_current_find_takeover_failed"
    assert validation.get("run_id") == run_id
    assert validation.get("valid") is False
    assert validation.get("actual_reading_count") == 0
    assert validation.get("full_text_evidence_count") == 1
    assert validation.get("pending_full_text_reading_count") == 1
    assert "current Find full-text evidence is ready" in "\n".join(validation.get("blockers") or [])


def test_current_find_artifact_writer_policy_is_recoverable_tool_policy():
    import importlib.util

    spec = importlib.util.spec_from_file_location("claude_project_session", SCRIPTS / "claude_project_session.py")
    claude_project_session = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(claude_project_session)

    generator = """python3 << 'PYEOF'
import json
with open('/workspace/taste/projects/demo/planning/finding/read_results.json', 'w') as f:
    json.dump({'readings': []}, f)
PYEOF
"""

    reason = claude_project_session.bash_command_tool_policy_issue(
        generator,
        "demo_project",
        "current-find-claude-read-idea-plan",
    )

    assert "current-Find Read/Idea/Plan JSON artifacts" in reason
    assert claude_project_session.is_current_find_artifact_policy_reason(reason)

    temp_diagnostic = """python3 << PYEOF
import json
with open(/workspace/taste/projects/demo/planning/finding/ideas.json) as f:
    content = f.read()
content = content.replace(bad, fixed)
with open(/tmp/ideas_fixed.json, w) as f:
    f.write(content)
json.loads(content)
PYEOF
"""
    assert claude_project_session.bash_command_tool_policy_issue(
        temp_diagnostic,
        "demo_project",
        "current-find-claude-read-idea-plan",
    ) == ""



def test_current_find_gate_state_write_is_blocked_but_read_is_allowed():
    import importlib.util

    spec = importlib.util.spec_from_file_location("claude_project_session", SCRIPTS / "claude_project_session.py")
    claude_project_session = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(claude_project_session)

    read_state = """python3 - << 'PYEOF'
import json
json.load(open('state/current_find_research_plan.json'))
PYEOF
"""
    read_content_artifact = """python3 -c "import json; json.load(open('/workspace/taste/projects/demo/planning/finding/read_results.json'))" 2>&1 | head -3"""
    write_state = """python3 - << 'PYEOF'
from pathlib import Path
Path('state/current_find_research_plan.json').write_text('{}')
PYEOF
"""
    edit_state = {
        "file_path": "/workspace/taste/projects/demo/state/current_find_research_plan.json",
    }
    edit_content = {
        "file_path": "/workspace/taste/projects/demo/planning/finding/read_results.json",
    }

    assert claude_project_session.bash_command_tool_policy_issue(
        read_state,
        "demo_project",
        "current-find-claude-read-idea-plan",
    ) == ""
    assert claude_project_session.bash_command_tool_policy_issue(
        read_content_artifact,
        "demo_project",
        "current-find-claude-read-idea-plan",
    ) == ""
    state_reason = claude_project_session.bash_command_tool_policy_issue(
        write_state,
        "demo_project",
        "current-find-claude-read-idea-plan",
    )
    assert "TASTE-owned current-Find gate/state files" in state_reason
    assert "TASTE-owned current-Find gate/state files" in claude_project_session.current_find_tool_policy_issue(
        "Edit",
        edit_state,
        "current-find-claude-read-idea-plan",
    )
    edit_content_reason = claude_project_session.current_find_tool_policy_issue(
        "Edit",
        edit_content,
        "current-find-claude-read-idea-plan",
    )
    assert "current-Find JSON artifacts" in edit_content_reason




def test_claude_takeover_restores_artifacts_after_tool_policy_or_parse_failure(tmp_path, monkeypatch):
    run_id = "find_demo_transaction_restore"
    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    old_read = {"run_id": run_id, "source": "claude_code_current_find_takeover", "readings": [{"title": "Old Paper"}]}
    old_ideas = {"run_id": run_id, "source": "claude_code_current_find_takeover", "ideas": [{"id": "old"}]}
    old_plans = {"run_id": run_id, "source": "claude_code_current_find_takeover", "plans": [{"plan_id": "old"}]}
    ensure_current_find_research_plan.save_json(taste_dir / "read_results.json", old_read)
    ensure_current_find_research_plan.save_json(taste_dir / "ideas.json", old_ideas)
    ensure_current_find_research_plan.save_json(taste_dir / "plans.json", old_plans)
    (taste_dir / "read.md").write_text("old read", encoding="utf-8")
    paths = type("Paths", (), {"planning": project_root / "planning", "state": state_dir})()

    class FakeProc:
        returncode = 3
        stdout = "blocked by tool policy"
        stderr = ""

    def fake_run(*_args, **_kwargs):
        (taste_dir / "read_results.json").write_text('{"run_id": "find_demo_transaction_restore", "source": "claude_code_current_find_takeover", "readings": [', encoding="utf-8")
        ensure_current_find_research_plan.save_json(
            state_dir / "claude_project_session_last_result.json",
            {
                "status": "blocked_tool_policy",
                "return_code": 3,
                "tool_policy_guard": {
                    "status": "blocked",
                    "policy_type": "current_find_artifact_writer",
                    "reason": "current-Find Read/Idea/Plan artifacts must be written with Claude Write only",
                },
            },
        )
        return FakeProc()

    monkeypatch.setattr(ensure_current_find_research_plan.subprocess, "run", fake_run)
    monkeypatch.setattr(ensure_current_find_research_plan, "_claude_takeover_timeout", lambda: 10)

    result = ensure_current_find_research_plan.run_claude_current_find_takeover(
        "demo_project", paths, run_id, read_limit=1, idea_count=5, repair_validation={}, attempt=2
    )

    restored_read = ensure_current_find_research_plan.load_json(taste_dir / "read_results.json", {})
    restored_ideas = ensure_current_find_research_plan.load_json(taste_dir / "ideas.json", {})
    restored_plans = ensure_current_find_research_plan.load_json(taste_dir / "plans.json", {})
    receipt = ensure_current_find_research_plan.load_json(state_dir / "current_find_artifact_transaction_restore.json", {})

    assert result["return_code"] == 3
    assert result["artifact_transaction"]["status"] == "restored"
    assert result["artifact_transaction"]["reason"] == "artifact_parse_failed_after_claude_takeover"
    assert restored_read == old_read
    assert restored_ideas == old_ideas
    assert restored_plans == old_plans
    assert (taste_dir / "read.md").read_text(encoding="utf-8") == "old read"
    assert receipt["restored_files"]
    assert result["artifact_transaction"]["post_restore_parse_failures"] == []

def test_repairable_current_find_tool_policy_failure_triggers_second_takeover(tmp_path, monkeypatch):
    run_id = "find_demo_repairable_tool_policy"
    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    find_results = {
        "run_id": run_id,
        "strong_recommendations": [
            {"id": "paper-1", "title": "Full Paper", "url": "https://example.test/paper", "evidence_tier": "strong_recommendation"}
        ],
    }
    validation = {
        "run_id": run_id,
        "valid": False,
        "policy_version": ensure_current_find_research_plan.FULL_TEXT_READ_POLICY_VERSION,
        "actual_reading_count": 1,
        "full_text_evidence_count": 1,
        "full_text_reading_count": 0,
        "pending_deep_read_synthesis_count": 1,
        "pending_full_text_reading_count": 1,
        "blockers": ["recommended readings have full-text packets but still need Claude Code deep-read synthesis in read_results/read.md"],
    }
    ensure_current_find_research_plan.save_json(state_dir / "current_find_claude_reading_validation.json", validation)
    ensure_current_find_research_plan.save_json(
        state_dir / "claude_project_session_last_result.json",
        {"status": "completed", "stdout": f"{run_id} subagent 逐篇精读任务 completed"},
    )
    paths = type("Paths", (), {"planning": project_root / "planning", "state": state_dir})()
    takeover = {
        "status": "blocked_tool_policy",
        "return_code": 3,
        "repair_attempt": 1,
        "tool_policy_guard": {
            "policy_type": "current_find_artifact_writer",
            "recoverable_by_current_find_repair": True,
            "reason": "current-Find Read/Idea/Plan artifacts must be written with Claude Write only",
        },
    }
    calls = []

    def fake_repair(project, paths_arg, run_id_arg, read_limit, idea_count, repair_validation=None, attempt=1):
        calls.append({
            "project": project,
            "run_id": run_id_arg,
            "read_limit": read_limit,
            "idea_count": idea_count,
            "repair_validation": repair_validation,
            "attempt": attempt,
        })
        return {
            "status": "blocked_tool_policy",
            "return_code": 3,
            "repair_attempt": attempt,
            "tool_policy_guard": {
                "policy_type": "current_find_artifact_writer",
                "recoverable_by_current_find_repair": True,
                "reason": "current-Find Read/Idea/Plan artifacts must be written with Claude Write only",
            },
        }

    monkeypatch.setattr(ensure_current_find_research_plan, "run_claude_current_find_takeover", fake_repair)
    monkeypatch.setattr(ensure_current_find_research_plan, "_find_run_changed", lambda _paths, _run_id: "")

    result, readings, ideas, plans, targeted_queries, new_validation, changed_run = ensure_current_find_research_plan.maybe_repair_current_find_takeover(
        "demo_project",
        paths,
        taste_dir,
        run_id,
        find_results,
        takeover,
        [],
        [],
        [],
        [],
        validation,
        1,
        1,
        5,
        None,
    )

    assert changed_run == ""
    assert len(calls) == 2
    assert [call["attempt"] for call in calls] == [2, 3]
    assert calls[0]["repair_validation"]["takeover"]["tool_policy_guard"]["policy_type"] == "current_find_artifact_writer"
    assert calls[1]["repair_validation"]["takeover"]["tool_policy_guard"]["policy_type"] == "current_find_artifact_writer"
    assert result["repair_attempt"] == 3
    assert result["contract_validation_valid"] is False
    assert result["contract_failure"]["observed"]["readings"] == 0
    failure = ensure_current_find_research_plan.load_json(state_dir / "current_find_claude_takeover_contract_failure.json", {})
    assert failure["takeover"]["tool_policy_guard"]["policy_type"] == "current_find_artifact_writer"
    assert readings == []
    assert ideas == []
    assert plans == []
    assert targeted_queries == []
    assert new_validation["valid"] is False


def test_claude_takeover_repair_prompt_bans_punctuation_only_json_edit(tmp_path):
    paths = type("Paths", (), {"state": tmp_path / "state"})()
    paths.state.mkdir(parents=True)
    prompt_path = ensure_current_find_research_plan.write_claude_takeover_prompt(
        paths,
        "demo_project",
        "find-test",
        read_limit=20,
        idea_count=5,
        repair_validation={
            "takeover": {
                "tool_policy_guard": {
                    "policy_type": "current_find_artifact_writer",
                    "recoverable_by_current_find_repair": True,
                }
            }
        },
        attempt=2,
    )
    text = prompt_path.read_text(encoding="utf-8")
    assert "禁止任何标点、引号、措辞润色类局部 Edit/MultiEdit" in text
    assert "这种微调也会导致整个 current-Find 事务回滚" in text
    assert "要么用 Write 再完整重写对应 JSON 一次" in text


def test_current_find_plan_state_requires_full_text_validation_even_when_counts_are_ready(tmp_path, monkeypatch):
    run_id = "find_demo_validation_gate"
    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)

    ensure_current_find_research_plan.save_json(
        taste_dir / "find_results.json",
        {
            "run_id": run_id,
            "strong_recommendations": [
                {"id": "paper-1", "title": "Full Paper", "url": "https://example.test/paper", "evidence_tier": "strong_recommendation"}
            ],
        },
    )
    ensure_current_find_research_plan.save_json(
        taste_dir / "find_progress.json",
        {"strong_recommendation_count": 1, "recommendation_target_count": 1, "recommendation_shortfall": 0, "counts": {}},
    )
    ensure_current_find_research_plan.save_json(
        state_dir / "current_find_claude_reading_validation.json",
        {
            "run_id": run_id,
            "valid": False,
            "policy_version": ensure_current_find_research_plan.FULL_TEXT_READ_POLICY_VERSION,
            "expected_recommendation_count": 1,
            "actual_reading_count": 1,
            "full_text_evidence_count": 0,
            "full_text_reading_count": 0,
            "pending_full_text_reading_count": 1,
            "blockers": ["recommended readings still lack full-text evidence"],
        },
    )
    for name, key in [("read_results.json", "readings"), ("ideas.json", "ideas"), ("plans.json", "plans")]:
        ensure_current_find_research_plan.save_json(
            taste_dir / name,
            {
                "run_id": run_id,
                "source": ensure_current_find_research_plan.CLAUDE_TAKEOVER_SOURCE,
                "targeted_search_queries": ["query one", "query two", "query three"],
                key: [],
            },
        )

    readings = [
        {
            "paper_id": "paper-1",
            "title": "Full Paper",
            "url": "https://example.test/paper",
            "support_role": "positive_anchor_for_planning",
            "claim_ready_anchor": True,
        }
    ]
    ideas = [
        {
            "id": f"idea-{idx}",
            "title": f"Idea {idx}",
            "new_method": "提出一个足够具体的新方法，包含机制、输入输出和可检验变化。",
            "initial_experiment": "基于同一数据、同一 seed、同一指标比较 baseline、candidate 和 ablation，并记录坏例切片。",
            "inspired_by": [{"title": "Full Paper", "reason": "method inspiration"}],
        }
        for idx in range(5)
    ]
    plans = [
        {
            "plan_id": f"plan-{idx}",
            "idea_id": f"idea-{idx}",
            "title": f"Plan {idx}",
            "new_method": ideas[idx]["new_method"],
            "initial_experiment": ideas[idx]["initial_experiment"],
            "inspired_by": ideas[idx]["inspired_by"],
        }
        for idx in range(5)
    ]
    ensure_current_find_research_plan.save_json(
        state_dir / "experiment_plan.json",
        {
            "run_id": run_id,
            "status": "blocked_missing_selected_plan",
            "failure_type": "missing_selected_plan",
            "next_required_action": "rerun_current_find_claude_takeover_select_single_best_plan",
            "base_selection_status": "blocked_missing_selected_plan",
            "selected_execution_issue": "missing_selected_plan",
        },
    )
    paths = type("Paths", (), {"planning": project_root / "planning", "state": state_dir})()
    monkeypatch.setattr(ensure_current_find_research_plan, "load_project_config", lambda _project: {"target_venue": "ICLR"})
    monkeypatch.setattr(ensure_current_find_research_plan, "project_target_venue", lambda _project, venue: venue)

    payload = ensure_current_find_research_plan.ensure_claude_plan_state(
        "demo_project",
        paths,
        run_id,
        readings,
        ideas,
        plans,
        {"status": "completed", "return_code": 0},
    )

    assert payload["status"] == "blocked_current_find_full_text_evidence_pending"
    assert payload["takeover_ready"] is False
    assert payload["claude_current_find_ready"] is False
    assert payload["current_find_reading_count"] == 1
    assert payload["current_find_idea_count"] == 5
    assert payload["current_find_plan_count"] == 5
    assert payload["base_selection_status"] == "blocked_by_current_find_full_text_evidence"
    assert payload["next_required_stage"] == "acquire_current_find_full_text_evidence"
    assert "recommended readings still lack full-text evidence" in payload["blockers"]
    persisted = ensure_current_find_research_plan.load_json(state_dir / "current_find_research_plan.json", {})
    assert persisted["takeover_ready"] is False


def test_current_find_plan_state_clears_stale_failure_fields_when_ready(tmp_path, monkeypatch):
    run_id = "find_demo_ready_clears_failure"
    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)

    ensure_current_find_research_plan.save_json(
        taste_dir / "find_results.json",
        {
            "run_id": run_id,
            "strong_recommendations": [
                {"id": "paper-1", "title": "Ready Full Paper", "url": "https://example.test/paper", "evidence_tier": "strong_recommendation"}
            ],
        },
    )
    ensure_current_find_research_plan.save_json(
        taste_dir / "find_progress.json",
        {"strong_recommendation_count": 1, "recommendation_target_count": 1, "recommendation_shortfall": 0, "counts": {}},
    )
    ensure_current_find_research_plan.save_json(
        state_dir / "current_find_claude_reading_validation.json",
        {
            "run_id": run_id,
            "valid": True,
            "policy_version": ensure_current_find_research_plan.FULL_TEXT_READ_POLICY_VERSION,
            "expected_recommendation_count": 1,
            "actual_reading_count": 1,
            "full_text_evidence_count": 1,
            "full_text_reading_count": 1,
            "pending_full_text_reading_count": 0,
            "pending_deep_read_synthesis_count": 0,
            "blockers": [],
        },
    )
    ensure_current_find_research_plan.save_json(
        state_dir / "current_find_research_plan.json",
        {
            "run_id": run_id,
            "status": "blocked_current_find_full_text_evidence_pending",
            "failure_type": "full_text_evidence_missing",
            "next_required_action": "acquire_current_find_full_text_evidence",
            "allowed_actions": ["stale repair command"],
        },
    )
    targeted_queries = ["retrieval benchmark semantics", "LLM semantic recommender", "discrete retrieval system"]
    for name, key in [("read_results.json", "readings"), ("ideas.json", "ideas"), ("plans.json", "plans")]:
        ensure_current_find_research_plan.save_json(
            taste_dir / name,
            {
                "run_id": run_id,
                "source": ensure_current_find_research_plan.CLAUDE_TAKEOVER_SOURCE,
                "targeted_search_queries": targeted_queries,
                key: [],
            },
        )

    readings = [
        {
            "paper_id": "paper-1",
            "title": "Ready Full Paper",
            "url": "https://example.test/paper",
            "support_role": "positive_anchor_for_planning",
            "claim_ready_anchor": True,
            "abstract_zh": "本文研究检索基准中的语义条件建模，围绕用户偏好、物品语义和去噪生成过程展开，并报告推荐排序实验。",
            "motivation_zh": "论文动机是解决检索基准难以同时利用协同偏好和语义泛化信号的问题。",
            "method_details_zh": "论文方法把用户交互表示作为扩散状态，在前向过程中逐步扰动偏好表示，反向网络结合用户历史、物品语义和时间步嵌入恢复偏好，并用排序头输出候选物品分数。",
            "experiments_zh": "实验比较传统推荐、语义推荐和检索基准基线，报告 Recall、NDCG 等排序指标，并通过移除语义条件和时间步编码做消融。",
            "limitations_zh": "局限包括扩散采样成本、语义表示质量敏感，以及不同数据切分和负采样协议可能影响指标。",
            "full_text_available": True,
            "full_text_status": "pdf_text_read",
            "pdf_text_chars": 2600,
            "source_evidence": {"text_chars": 2600, "text_path": "texts/ready-full-paper.txt"},
        }
    ]
    ideas = [_ready_scored_idea(idx, source_title="Ready Full Paper") for idx in range(5)]
    plans = [
        {
            "plan_id": f"plan-{idx}",
            "idea_id": f"idea-{idx}",
            "title": f"Plan {idx}",
            "new_method": ideas[idx]["new_method"],
            "initial_experiment": ideas[idx]["initial_experiment"],
            "inspired_by": ideas[idx]["inspired_by"],
        }
        for idx in range(5)
    ]
    paths = type("Paths", (), {"planning": project_root / "planning", "state": state_dir})()
    monkeypatch.setattr(ensure_current_find_research_plan, "load_project_config", lambda _project: {"target_venue": "ICLR"})
    monkeypatch.setattr(ensure_current_find_research_plan, "project_target_venue", lambda _project, venue: venue)

    payload = ensure_current_find_research_plan.ensure_claude_plan_state(
        "demo_project",
        paths,
        run_id,
        readings,
        ideas,
        plans,
        {"status": "completed", "return_code": 0},
    )

    assert payload["status"] == "blocked_missing_selected_plan"
    assert payload["content_ready"] is True
    assert payload["read_idea_plan_ready"] is True
    assert payload["execution_ready"] is False
    assert payload["takeover_ready"] is False
    assert payload["claude_current_find_ready"] is True
    assert payload["selected_plan_id"] == ""
    assert payload["failure_type"] == "missing_selected_plan"
    assert payload["selected_execution_issue"] == "missing_selected_plan"
    assert payload["next_required_action"] == "rerun_current_find_claude_takeover_select_single_best_plan"
    assert payload["next_required_stage"] == "rerun_current_find_claude_takeover_select_single_best_plan"
    assert payload["base_selection_status"] == "blocked_missing_selected_plan"
    assert payload["allowed_actions"] == []
    assert any("no explicit selected_plan_id" in item for item in payload["blockers"])
    persisted = ensure_current_find_research_plan.load_json(state_dir / "current_find_research_plan.json", {})
    assert persisted["failure_type"] == "missing_selected_plan"
    assert persisted["execution_ready"] is False
    assert persisted["takeover_ready"] is False
    assert persisted["next_required_action"] == "rerun_current_find_claude_takeover_select_single_best_plan"


def test_current_find_plan_state_allows_explicit_selected_plan_when_ready(tmp_path, monkeypatch):
    run_id = "find_demo_ready_selected_plan"
    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    ensure_current_find_research_plan.save_json(
        taste_dir / "find_results.json",
        {"run_id": run_id, "strong_recommendations": [{"id": "paper-1", "title": "Ready Full Paper", "url": "https://example.test/paper", "evidence_tier": "strong_recommendation"}]},
    )
    ensure_current_find_research_plan.save_json(
        taste_dir / "find_progress.json",
        {"strong_recommendation_count": 1, "recommendation_target_count": 1, "recommendation_shortfall": 0, "counts": {}},
    )
    ensure_current_find_research_plan.save_json(
        state_dir / "current_find_claude_reading_validation.json",
        {"run_id": run_id, "valid": True, "policy_version": ensure_current_find_research_plan.FULL_TEXT_READ_POLICY_VERSION, "expected_recommendation_count": 1, "actual_reading_count": 1, "full_text_evidence_count": 1, "full_text_reading_count": 1, "pending_full_text_reading_count": 0, "pending_deep_read_synthesis_count": 0, "blockers": []},
    )
    targeted_queries = ["semantic retrieval benchmark", "LLM recommender gating", "discrete retrieval system"]
    for name, key in [("read_results.json", "readings"), ("ideas.json", "ideas"), ("plans.json", "plans")]:
        ensure_current_find_research_plan.save_json(
            taste_dir / name,
            {"run_id": run_id, "source": ensure_current_find_research_plan.CLAUDE_TAKEOVER_SOURCE, "targeted_search_queries": targeted_queries, key: []},
        )
    reading = {
        "paper_id": "paper-1",
        "title": "Ready Full Paper",
        "url": "https://example.test/paper",
        "support_role": "positive_anchor_for_planning",
        "claim_ready_anchor": True,
        **_v4_deep_read_fields("Ready Full Paper"),
        "full_text_available": True,
        "full_text_status": "pdf_text_read",
        "pdf_text_chars": 2600,
        "source_evidence": {"text_chars": 2600, "text_path": "texts/ready-full-paper.txt"},
    }
    ideas = [_ready_scored_idea(idx, source_title="Ready Full Paper") for idx in range(5)]
    plans = [
        {"plan_id": f"plan-{idx}", "idea_id": f"idea-{idx}", "title": f"Plan {idx}", "new_method": ideas[idx]["new_method"], "initial_experiment": ideas[idx]["initial_experiment"], "inspired_by": ideas[idx]["inspired_by"], **({"selected_for_execution": True} if idx == 2 else {})}
        for idx in range(5)
    ]
    paths = type("Paths", (), {"planning": project_root / "planning", "state": state_dir})()
    monkeypatch.setattr(ensure_current_find_research_plan, "load_project_config", lambda _project: {"target_venue": "ICLR"})
    monkeypatch.setattr(ensure_current_find_research_plan, "project_target_venue", lambda _project, venue: venue)

    payload = ensure_current_find_research_plan.ensure_claude_plan_state(
        "demo_project",
        paths,
        run_id,
        [reading],
        ideas,
        plans,
        {"status": "completed", "return_code": 0},
    )

    assert payload["status"] == "claude_current_find_read_idea_plan_ready_waiting_for_environment_base_selection"
    assert payload["takeover_ready"] is True
    assert payload["claude_current_find_ready"] is True
    assert payload["selected_plan_id"] == "plan-2"
    assert payload["selected_idea_id"] == "idea-2"
    assert payload["failure_type"] == ""
    assert payload["execution_ready"] is True
    assert payload["takeover_ready"] is True
    assert payload["next_required_action"] == "environment_base_selection_and_repo_data_protocol_audit"
    assert payload["base_selection_status"] == "waiting_for_environment_claude_code"
    persisted_experiment = ensure_current_find_research_plan.load_json(state_dir / "experiment_plan.json", {})
    assert persisted_experiment["status"] == "claude_current_find_read_idea_plan_ready_waiting_for_environment_base_selection"
    assert persisted_experiment["failure_type"] == ""
    assert persisted_experiment["next_required_action"] == "environment_base_selection_and_repo_data_protocol_audit"
    assert persisted_experiment["base_selection_status"] == "waiting_for_environment_claude_code"
    assert persisted_experiment["selected_execution_issue"] == ""
    assert persisted_experiment["selected_plan_id"] == "plan-2"
    assert persisted_experiment["execution_ready"] is True


def test_current_find_plan_state_blocks_ambiguous_selected_plans(tmp_path, monkeypatch):
    run_id = "find_demo_ambiguous_selected_plan"
    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    ensure_current_find_research_plan.save_json(
        taste_dir / "find_results.json",
        {"run_id": run_id, "strong_recommendations": [{"id": "paper-1", "title": "Ready Full Paper", "url": "https://example.test/paper", "evidence_tier": "strong_recommendation"}]},
    )
    ensure_current_find_research_plan.save_json(
        taste_dir / "find_progress.json",
        {"strong_recommendation_count": 1, "recommendation_target_count": 1, "recommendation_shortfall": 0, "counts": {}},
    )
    ensure_current_find_research_plan.save_json(
        state_dir / "current_find_claude_reading_validation.json",
        {"run_id": run_id, "valid": True, "policy_version": ensure_current_find_research_plan.FULL_TEXT_READ_POLICY_VERSION, "expected_recommendation_count": 1, "actual_reading_count": 1, "full_text_evidence_count": 1, "full_text_reading_count": 1, "pending_full_text_reading_count": 0, "pending_deep_read_synthesis_count": 0, "blockers": []},
    )
    targeted_queries = ["semantic retrieval benchmark", "LLM recommender gating", "discrete retrieval system"]
    for name, key in [("read_results.json", "readings"), ("ideas.json", "ideas"), ("plans.json", "plans")]:
        ensure_current_find_research_plan.save_json(
            taste_dir / name,
            {"run_id": run_id, "source": ensure_current_find_research_plan.CLAUDE_TAKEOVER_SOURCE, "targeted_search_queries": targeted_queries, key: []},
        )
    reading = {
        "paper_id": "paper-1",
        "title": "Ready Full Paper",
        "url": "https://example.test/paper",
        "support_role": "positive_anchor_for_planning",
        "claim_ready_anchor": True,
        **_v4_deep_read_fields("Ready Full Paper"),
        "full_text_available": True,
        "full_text_status": "pdf_text_read",
        "pdf_text_chars": 2600,
        "source_evidence": {"text_chars": 2600, "text_path": "texts/ready-full-paper.txt"},
    }
    ideas = [_ready_scored_idea(idx, source_title="Ready Full Paper") for idx in range(5)]
    plans = [
        {"plan_id": f"plan-{idx}", "idea_id": f"idea-{idx}", "title": f"Plan {idx}", "new_method": ideas[idx]["new_method"], "initial_experiment": ideas[idx]["initial_experiment"], "inspired_by": ideas[idx]["inspired_by"], **({"selected_for_execution": True, "execute_next": True} if idx in {1, 3} else {"selected_for_execution": False, "execute_next": False})}
        for idx in range(5)
    ]
    paths = type("Paths", (), {"planning": project_root / "planning", "state": state_dir})()
    monkeypatch.setattr(ensure_current_find_research_plan, "load_project_config", lambda _project: {"target_venue": "ICLR"})
    monkeypatch.setattr(ensure_current_find_research_plan, "project_target_venue", lambda _project, venue: venue)

    payload = ensure_current_find_research_plan.ensure_claude_plan_state(
        "demo_project",
        paths,
        run_id,
        [reading],
        ideas,
        plans,
        {"status": "completed", "return_code": 0},
    )

    assert payload["status"] == "blocked_ambiguous_selected_plan"
    assert payload["content_ready"] is True
    assert payload["read_idea_plan_ready"] is True
    assert payload["execution_ready"] is False
    assert payload["takeover_ready"] is False
    assert payload["claude_current_find_ready"] is True
    assert payload["selected_plan_id"] == ""
    assert payload["failure_type"] == "ambiguous_selected_plan"
    assert payload["selected_execution_issue"] == "ambiguous_selected_plan"
    assert payload["next_required_action"] == "rerun_current_find_claude_takeover_select_single_best_plan"
    assert payload["next_required_stage"] == "rerun_current_find_claude_takeover_select_single_best_plan"
    assert payload["base_selection_status"] == "blocked_ambiguous_selected_plan"
    assert any("multiple plans were explicitly selected" in item for item in payload["blockers"])
    persisted = ensure_current_find_research_plan.load_json(state_dir / "current_find_research_plan.json", {})
    assert persisted["failure_type"] == "ambiguous_selected_plan"
    assert persisted["selected_plan_id"] == ""
    assert persisted["execution_ready"] is False
    assert persisted["takeover_ready"] is False


def test_current_find_execution_contract_counts_current_run_plans_once(tmp_path):
    import run_project

    run_id = "find_contract_current_run"
    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    current_ideas = [
        {"id": f"idea-{idx}", "title": f"Current Idea {idx}", "new_method": "方法", "initial_experiment": "实验", "inspired_by": []}
        for idx in range(5)
    ]
    current_plans = [
        {"plan_id": f"plan-{idx}", "idea_id": f"idea-{idx}", "title": f"Current Plan {idx}", "selected_for_execution": False}
        for idx in range(5)
    ]
    stale_bridge_plans = [
        {"plan_id": f"stale-plan-{idx}", "idea_id": f"stale-idea-{idx}", "title": f"Stale Plan {idx}", "selected_for_execution": idx == 0}
        for idx in range(5)
    ]
    ensure_current_find_research_plan.save_json(taste_dir / "ideas.json", {"run_id": run_id, "ideas": current_ideas})
    ensure_current_find_research_plan.save_json(taste_dir / "plans.json", {"run_id": run_id, "plans": current_plans})
    ensure_current_find_research_plan.save_json(
        state_dir / "taste_plan_bridge.json",
        {"source": "stale_bridge", "plans_json": {"run_id": run_id, "plans": stale_bridge_plans}},
    )
    paths = type("Paths", (), {"planning": project_root / "planning", "state": state_dir})()

    contract = run_project.current_find_execution_contract(paths)

    assert contract["required"] is True
    assert contract["run_id"] == run_id
    assert contract["candidate_counts"] == {"ideas": 5, "plans": 5}
    assert contract["selected_plan_id"] == ""
    assert contract["status"] == "blocked_missing_selected_plan"
    assert contract["selection_issue"] == "missing_selected_plan"
    assert contract["execution_policy"]["status"] == "no_selected_plan"


def test_current_reading_validation_missing_full_text_does_not_force_claude_rerun():
    run_id = "find_demo_missing_evidence"
    validation = {
        "run_id": run_id,
        "valid": False,
        "policy_version": ensure_current_find_research_plan.FULL_TEXT_READ_POLICY_VERSION,
        "expected_recommendation_count": 1,
        "actual_reading_count": 1,
        "full_text_evidence_count": 0,
        "full_text_reading_count": 0,
        "pending_full_text_reading_count": 1,
        "blockers": ["recommended readings still lack full-text evidence"],
    }
    observed = {
        "raw_artifact_reading_count": 1,
        "raw_artifact_idea_count": 5,
        "raw_artifact_plan_count": 5,
        "validation_actual_reading_count": 1,
        "validation_full_text_reading_count": 0,
        "validation_pending_full_text_reading_count": 1,
    }

    assert ensure_current_find_research_plan.current_reading_validation_needs_full_text_evidence(validation)
    assert not ensure_current_find_research_plan.current_reading_validation_requires_fresh_takeover(validation, run_id)
    assert ensure_current_find_research_plan.current_find_contract_failure_type(validation, observed) == "full_text_evidence_missing"
    assert ensure_current_find_research_plan.current_find_contract_next_required_action(validation, observed) == "acquire_current_find_full_text_evidence"


def test_stale_or_missing_takeover_cannot_validate_ready_current_find_outputs(tmp_path, monkeypatch):
    run_id = "find_demo_stale_takeover_ready_outputs"
    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    find_results = {
        "run_id": run_id,
        "strong_recommendations": [
            {"id": "paper-1", "title": "Full Paper", "url": "https://example.test/paper", "evidence_tier": "strong_recommendation"}
        ],
    }
    reading = {
        "paper_id": "paper-1",
        "title": "Full Paper",
        "url": "https://example.test/paper",
        "verdict": "core_reading",
        "support_role": "positive_anchor_for_planning",
        **_v4_deep_read_fields("Full Paper"),
        "full_text_available": True,
        "full_text_status": "pdf_text_read",
        "pdf_text_chars": 5000,
        "subagent_deep_read": True,
        "deep_read_audit": {"mode": "task_subagent", "subagent_used": True, "status": "completed", "text_path": "full_text.txt", "evidence_chars": 5000},
    }
    ideas = [_ready_scored_idea(idx, source_title="Full Paper") for idx in range(5)]
    plans = [
        {"plan_id": f"plan-{idx}", "idea_id": f"idea-{idx}", "steps": ["审计 repo/data/protocol", "运行 baseline/candidate/ablation"]}
        for idx in range(5)
    ]
    generated_at = "2026-01-02T00:00:00+00:00"
    ensure_current_find_research_plan.save_json(taste_dir / "read_results.json", {"run_id": run_id, "source": ensure_current_find_research_plan.CLAUDE_TAKEOVER_SOURCE, "generated_at": generated_at, "readings": [reading], "targeted_search_queries": ["query one", "query two", "query three"]})
    ensure_current_find_research_plan.save_json(taste_dir / "ideas.json", {"run_id": run_id, "source": ensure_current_find_research_plan.CLAUDE_TAKEOVER_SOURCE, "generated_at": generated_at, "ideas": ideas, "targeted_search_queries": ["query one", "query two", "query three"]})
    ensure_current_find_research_plan.save_json(taste_dir / "plans.json", {"run_id": run_id, "source": ensure_current_find_research_plan.CLAUDE_TAKEOVER_SOURCE, "generated_at": generated_at, "plans": plans, "targeted_search_queries": ["query one", "query two", "query three"]})
    validation = {
        "run_id": run_id,
        "valid": True,
        "policy_version": ensure_current_find_research_plan.FULL_TEXT_READ_POLICY_VERSION,
        "generated_at": generated_at,
        "expected_recommendation_count": 1,
        "actual_reading_count": 1,
        "full_text_evidence_count": 1,
        "full_text_reading_count": 1,
        "pending_full_text_reading_count": 0,
        "blockers": [],
    }
    ensure_current_find_research_plan.save_json(state_dir / "current_find_claude_reading_validation.json", validation)
    ensure_current_find_research_plan.save_json(
        state_dir / "claude_project_session_last_result.json",
        {"status": "completed", "stdout": f"{run_id} subagent 逐篇精读任务 completed"},
    )
    paths = type("Paths", (), {"planning": project_root / "planning", "state": state_dir})()
    stale_takeover = {
        "status": "stale_or_missing_current_find_takeover",
        "return_code": 0,
        "started_at": "2026-01-02T00:00:01+00:00",
        "finished_at": "2026-01-02T00:00:02+00:00",
        "prompt_path": "",
    }
    takeover_calls = []
    selection_calls = []

    def fake_repair(project, paths_arg, run_id_arg, read_limit, idea_count, repair_validation=None, attempt=1):
        takeover_calls.append({"repair_validation": repair_validation, "attempt": attempt})
        return {
            "status": "completed",
            "return_code": 0,
            "repair_attempt": attempt,
            "started_at": "2026-01-02T00:00:03+00:00",
            "finished_at": "2026-01-02T00:00:04+00:00",
            "prompt_path": str(state_dir / "current_find_claude_takeover_repair_prompt_attempt2.md"),
        }

    def fake_selection(project, paths_arg, run_id_arg, observed=None, attempt=1, **_kwargs):
        selection_calls.append({"observed": observed, "attempt": attempt})
        plan_payload = ensure_current_find_research_plan.load_json(taste_dir / "plans.json", {})
        updated_plans = []
        for index, row in enumerate(plan_payload.get("plans", [])):
            clean = dict(row)
            chosen = index == 0
            clean["selected_for_execution"] = chosen
            clean["execute_next"] = chosen
            clean["execution_selection"] = {
                "selected": chosen,
                "selected_by": "main_claude_code_after_deep_read" if chosen else "not_selected_candidate_backlog",
                "reason": "基于完整精读、idea 和 plan 对比后选择第一个候选。" if chosen else "保留为候选 backlog。",
            }
            updated_plans.append(clean)
        plan_payload["plans"] = updated_plans
        plan_payload["generated_at"] = "2026-01-02T00:00:04+00:00"
        ensure_current_find_research_plan.save_json(taste_dir / "plans.json", plan_payload)
        return {
            "status": "completed",
            "stage": "current-find-claude-select-plan",
            "selection_only": True,
            "return_code": 0,
            "repair_attempt": attempt,
            "started_at": "2026-01-02T00:00:03+00:00",
            "finished_at": "2026-01-02T00:00:04+00:00",
            "prompt_path": str(state_dir / "current_find_claude_selection_prompt_attempt2.md"),
        }

    monkeypatch.setattr(ensure_current_find_research_plan, "run_claude_current_find_takeover", fake_repair)
    monkeypatch.setattr(ensure_current_find_research_plan, "run_claude_current_find_selection", fake_selection)
    monkeypatch.setattr(ensure_current_find_research_plan, "_find_run_changed", lambda _paths, _run_id: "")

    result, readings, loaded_ideas, loaded_plans, targeted_queries, new_validation, changed_run = ensure_current_find_research_plan.maybe_repair_current_find_takeover(
        "demo_project",
        paths,
        taste_dir,
        run_id,
        find_results,
        stale_takeover,
        [reading],
        ideas,
        plans,
        ["query one", "query two", "query three"],
        validation,
        1,
        1,
        5,
        ensure_current_find_research_plan.parse_iso_time("2026-01-01T00:00:00+00:00"),
    )

    assert changed_run == ""
    assert takeover_calls == []
    assert len(selection_calls) == 1
    assert selection_calls[0]["attempt"] == 2
    assert selection_calls[0]["observed"]["failure_type"] == "missing_selected_plan"
    assert result["selection_only"] is True
    assert result["contract_validation_valid"] is True
    assert result.get("contract_failure") is None
    assert readings
    assert loaded_ideas
    assert loaded_plans[0]["selected_for_execution"] is True
    assert all(not row.get("selected_for_execution") for row in loaded_plans[1:])
    assert targeted_queries == ["query one", "query two", "query three"]
    assert new_validation["valid"] is True


def test_current_find_takeover_failure_observed_reports_raw_artifact_counts(tmp_path):
    run_id = "find_demo_raw_artifact_counts"
    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    ensure_current_find_research_plan.save_json(
        taste_dir / "read_results.json",
        {
            "run_id": run_id,
            "source": ensure_current_find_research_plan.CLAUDE_TAKEOVER_SOURCE,
            "readings": [{"title": "Pending Paper"}],
        },
    )
    ensure_current_find_research_plan.save_json(
        taste_dir / "ideas.json",
        {
            "run_id": run_id,
            "source": ensure_current_find_research_plan.CLAUDE_TAKEOVER_SOURCE,
            "ideas": [{"title": f"Idea {idx}", "new_method": "x", "initial_experiment": "y", "inspired_by": [{"title": "Pending Paper"}]} for idx in range(5)],
        },
    )
    ensure_current_find_research_plan.save_json(
        taste_dir / "plans.json",
        {
            "run_id": run_id,
            "source": ensure_current_find_research_plan.CLAUDE_TAKEOVER_SOURCE,
            "plans": [{"title": f"Plan {idx}"} for idx in range(5)],
        },
    )
    validation = {
        "run_id": run_id,
        "valid": False,
        "actual_reading_count": 1,
        "full_text_reading_count": 0,
        "pending_full_text_reading_count": 1,
        "pending_without_evidence_count": 1,
        "blockers": ["recommended readings still lack full-text evidence"],
    }
    observed = ensure_current_find_research_plan.current_find_takeover_observed(
        taste_dir,
        [],
        [],
        [],
        ["query one", "query two", "query three"],
        validation,
        5,
    )
    result = ensure_current_find_research_plan.record_claude_takeover_contract_result(
        type("Paths", (), {"state": state_dir})(),
        run_id,
        {"status": "completed", "return_code": 0},
        False,
        validation,
        observed,
        1,
    )

    assert observed["contract_reading_count"] == 0
    assert observed["raw_artifact_reading_count"] == 1
    assert observed["raw_artifact_idea_count"] == 5
    assert observed["raw_artifact_plan_count"] == 5
    assert observed["validation_actual_reading_count"] == 1
    assert observed["validation_full_text_reading_count"] == 0
    assert observed["validation_pending_full_text_reading_count"] == 1
    assert result["contract_failure"]["observed"]["raw_artifact_reading_count"] == 1
    failure = ensure_current_find_research_plan.load_json(state_dir / "current_find_claude_takeover_contract_failure.json", {})
    assert failure["observed"]["raw_artifact_idea_count"] == 5




def test_full_text_repair_ignores_stale_validation_pending_titles(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("repair_current_find_full_text_evidence", SCRIPTS / "repair_current_find_full_text_evidence.py")
    repair = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(repair)

    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    write_json = ensure_current_find_research_plan.save_json
    write_json(
        taste_dir / "find_results.json",
        {
            "run_id": "find_current",
            "strong_recommendations": [
                {"id": "paper-current", "title": "Current Recommended Paper", "url": "https://example.test/current"}
            ],
        },
    )
    write_json(state_dir / "current_find_claude_reading_validation.json", {"run_id": "find_old", "pending_full_text_reading_titles": ["Old Pending Paper"]})
    write_json(taste_dir / "full_text_reading" / "full_text_packet.json", {"run_id": "find_old", "papers": []})
    paths = type("Paths", (), {"planning": project_root / "planning", "state": state_dir})()
    monkeypatch.setattr(repair, "build_paths", lambda _project: paths)
    monkeypatch.setattr(repair, "try_acquire_for_paper", lambda _paths, _paper, _rank: (None, [{"kind": "mock"}]))

    rc, receipt = repair.repair_current_find_full_text_evidence("demo_project", force=True)

    assert rc == 2
    assert receipt["pending_titles"] == ["Current Recommended Paper"]
    assert receipt["unavailable"][0]["title"] == "Current Recommended Paper"


def test_current_find_full_text_preflight_rejects_stale_packet(tmp_path, monkeypatch):
    run_id = "find_new_packet_preflight"
    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    find_results = {
        "run_id": run_id,
        "strong_recommendations": [
            {"id": "paper-1", "title": "Current Full Paper", "url": "https://example.test/current", "pdf_url": "https://example.test/current.pdf"}
        ],
    }
    stale_packet = {
        "run_id": "find_old_packet",
        "papers": [
            {"paper_id": "paper-1", "title": "Current Full Paper", "text_path": "texts/current.txt", "text_chars": 50000}
        ],
    }
    ensure_current_find_research_plan.save_json(taste_dir / "find_results.json", find_results)
    ensure_current_find_research_plan.save_json(taste_dir / "full_text_reading" / "full_text_packet.json", stale_packet)
    paths = type("Paths", (), {"planning": project_root / "planning", "state": state_dir, "root": project_root})()

    calls = []

    def fake_repair(project, force=False):
        calls.append((project, force))
        return 2, {"status": "blocked_full_text_evidence_unavailable"}

    monkeypatch.setattr(ensure_current_find_research_plan, "repair_current_find_full_text_evidence", fake_repair, raising=False)
    monkeypatch.setitem(sys.modules, "repair_current_find_full_text_evidence", type("RepairModule", (), {"repair_current_find_full_text_evidence": fake_repair}))

    missing = ensure_current_find_research_plan.current_find_full_text_packet_missing_titles(taste_dir, run_id, find_results)
    result = ensure_current_find_research_plan.ensure_current_find_full_text_evidence_before_claude("demo_project", paths, taste_dir, run_id, find_results)

    assert missing == ["Current Full Paper"]
    assert calls == [("demo_project", True)]
    assert result["status"] == "blocked_current_find_full_text_evidence_pending"
    validation = ensure_current_find_research_plan.load_json(state_dir / "current_find_claude_reading_validation.json", {})
    assert validation["pending_full_text_reading_count"] == 1
    assert validation["pending_without_evidence_titles"] == ["Current Full Paper"]




def test_current_find_full_text_preflight_persists_partial_repair_progress(tmp_path, monkeypatch):
    run_id = 'find_partial_packet_preflight'
    project_root = tmp_path / 'demo_project'
    taste_dir = project_root / 'planning' / 'finding'
    state_dir = project_root / 'state'
    text_dir = taste_dir / 'full_text_reading' / 'texts'
    text_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    text_path = text_dir / 'paper-1.txt'
    text_path.write_text('Readable Paper abstract introduction method experiments evaluation results conclusion references ' * 500, encoding='utf-8')
    find_results = {
        'run_id': run_id,
        'strong_recommendations': [
            {'id': 'paper-1', 'title': 'Readable Paper', 'url': 'https://example.test/readable', 'pdf_url': 'https://example.test/readable.pdf'},
            {'id': 'paper-2', 'title': 'Missing Paper', 'url': 'https://example.test/missing', 'pdf_url': ''},
        ],
    }
    stale_packet = {'run_id': run_id, 'papers': []}
    repaired_packet = {
        'run_id': run_id,
        'papers': [
            {'paper_id': 'paper-1', 'title': 'Readable Paper', 'text_path': str(text_path), 'text_chars': 50000},
            {'paper_id': 'paper-2', 'title': 'Missing Paper', 'text_path': '', 'text_chars': 0},
        ],
    }
    ensure_current_find_research_plan.save_json(taste_dir / 'full_text_reading' / 'full_text_packet.json', stale_packet)
    paths = type('Paths', (), {'planning': project_root / 'planning', 'state': state_dir, 'root': project_root})()

    def fake_repair(project, force=False):
        ensure_current_find_research_plan.save_json(taste_dir / 'full_text_reading' / 'full_text_packet.json', repaired_packet)
        return 2, {'status': 'partial_full_text_evidence_repair', 'acquired_count': 1, 'unavailable_count': 1}

    monkeypatch.setattr(ensure_current_find_research_plan, 'repair_current_find_full_text_evidence', fake_repair, raising=False)
    monkeypatch.setitem(sys.modules, 'repair_current_find_full_text_evidence', type('RepairModule', (), {'repair_current_find_full_text_evidence': fake_repair}))

    result = ensure_current_find_research_plan.ensure_current_find_full_text_evidence_before_claude('demo_project', paths, taste_dir, run_id, find_results)

    validation = ensure_current_find_research_plan.load_json(state_dir / 'current_find_claude_reading_validation.json', {})
    assert result['status'] == 'blocked_current_find_full_text_evidence_pending'
    assert result['full_text_evidence_count'] == 1
    assert result['pending_without_evidence_count'] == 1
    assert validation['full_text_evidence_count'] == 1
    assert validation['pending_without_evidence_count'] == 1
    assert validation['pending_without_evidence_titles'] == ['Missing Paper']
    assert validation['full_text_evidence_titles'] == ['Readable Paper']

def test_current_find_full_text_preflight_accepts_same_run_packet(tmp_path, monkeypatch):
    run_id = "find_same_packet_preflight"
    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    text_dir = taste_dir / "full_text_reading" / "texts"
    text_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    text_path = text_dir / "paper-1.txt"
    text_path.write_text("Current Full Paper abstract introduction method experiments evaluation results conclusion references " * 500, encoding="utf-8")
    find_results = {
        "run_id": run_id,
        "strong_recommendations": [
            {"id": "paper-1", "title": "Current Full Paper", "url": "https://example.test/current", "pdf_url": "https://example.test/current.pdf"}
        ],
    }
    packet = {
        "run_id": run_id,
        "papers": [
            {"paper_id": "paper-1", "title": "Current Full Paper", "text_path": str(text_path), "text_chars": 50000}
        ],
    }
    ensure_current_find_research_plan.save_json(taste_dir / "full_text_reading" / "full_text_packet.json", packet)
    paths = type("Paths", (), {"planning": project_root / "planning", "state": state_dir, "root": project_root})()

    result = ensure_current_find_research_plan.ensure_current_find_full_text_evidence_before_claude("demo_project", paths, taste_dir, run_id, find_results)

    assert result["status"] == "current_find_full_text_evidence_ready"
    assert result["missing_count"] == 0


def test_full_text_repair_rejects_short_icml_abstract_page_as_paper_body():
    import importlib.util

    spec = importlib.util.spec_from_file_location("repair_current_find_full_text_evidence", SCRIPTS / "repair_current_find_full_text_evidence.py")
    repair = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(repair)

    short_page = "CANDI Hybrid Discrete Continuous Diffusion Models abstract method experiment evaluation conclusion references " * 40
    long_paper = (
        "CANDI Hybrid Discrete Continuous Diffusion Models abstract introduction method experiments evaluation results conclusion references " * 500
    )

    assert repair.text_looks_like_paper(short_page, "CANDI: Hybrid Discrete-Continuous Diffusion Models") is False
    assert repair.text_looks_like_paper(long_paper, "CANDI: Hybrid Discrete-Continuous Diffusion Models") is True


def test_arxiv_title_search_requires_author_overlap_for_repository_pdf(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("repair_current_find_full_text_evidence", SCRIPTS / "repair_current_find_full_text_evidence.py")
    repair = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(repair)

    feed = b"""<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns='http://www.w3.org/2005/Atom'>
  <entry>
    <title>ITDR: An Instruction Tuning Dataset for Enhancing Large Language Models in Recommendations</title>
    <id>http://arxiv.org/abs/2508.05667v1</id>
    <author><name>Unrelated Author</name></author>
    <link title='pdf' type='application/pdf' href='https://arxiv.org/pdf/2508.05667'/>
  </entry>
  <entry>
    <title>ITDR: An Instruction Tuning Dataset for Enhancing Large Language Models in Recommendations</title>
    <id>http://arxiv.org/abs/2508.05667v2</id>
    <author><name>Zekun Liu</name></author>
    <author><name>Xiaowen Huang</name></author>
    <link title='pdf' type='application/pdf' href='https://arxiv.org/pdf/2508.05667v2'/>
  </entry>
</feed>"""

    monkeypatch.setattr(repair, "fetch_url", lambda _url, timeout=45: (200, "application/atom+xml", feed, _url))
    paper = {
        "title": "ITDR: An Instruction Tuning Dataset for Enhancing Large Language Models in Recommendations",
        "authors": "Zekun Liu, Xiaowen Huang, Jitao Sang",
    }

    candidates = repair.arxiv_search_candidates(paper)

    assert len(candidates) == 2
    assert candidates[0]["similarity"] == 1.0
    assert candidates[0]["author_overlap"] == []
    assert candidates[0]["accepted"] is False
    assert candidates[1]["author_overlap"] == ["huang", "liu"]
    assert candidates[1]["accepted"] is True


def test_current_find_read_markdown_refreshes_from_structured_deep_read(tmp_path, monkeypatch):
    run_id = "find_demo_markdown_refresh"
    project_root = tmp_path / "demo_project"
    taste_dir = project_root / "planning" / "finding"
    run_dir = tmp_path / "runs" / run_id
    legacy_run_dir = tmp_path / "legacy_runs" / run_id
    taste_dir.mkdir(parents=True)
    run_dir.mkdir(parents=True)
    legacy_run_dir.mkdir(parents=True)
    monkeypatch.setattr(ensure_current_find_research_plan, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(ensure_current_find_research_plan, "LEGACY_RUNS_DIR", tmp_path / "legacy_runs")

    fields = _v4_deep_read_fields("Consistent Noisy Latent Rewards")
    fields.update(
        {
            "paper_id": "paper-slrm-tapo",
            "title": "Consistent Noisy Latent Rewards for Trajectory Preference Optimization in Diffusion Models",
            "venue": "ICLR",
            "year": "2026",
            "url": "https://openreview.net/forum?id=qGihS60jfT",
            "pdf_url": "https://openreview.net/pdf?id=qGihS60jfT",
            "full_text_available": True,
            "full_text_status": "pdf_text_read",
            "source_evidence": {"pdf_text_chars": 76194, "full_text_status": "pdf_text_read"},
        }
    )
    read_payload = {"run_id": run_id, "source": "claude_code_current_find_takeover", "readings": [fields]}
    ensure_current_find_research_plan.save_json(taste_dir / "read_results.json", read_payload)
    ensure_current_find_research_plan.save_json(taste_dir / "ideas.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "ideas": []})
    ensure_current_find_research_plan.save_json(taste_dir / "plans.json", {"run_id": run_id, "source": "claude_code_current_find_takeover", "plans": []})
    (taste_dir / "read.md").write_text("## 18. Consistent Noisy Latent Rewards\n\nSLRM+TAPO：多时间步轨迹级偏好优化。\n\n全文未读取，方法差异和优缺点待正文精读后确认。\n", encoding="utf-8")
    paths = type("Paths", (), {"planning": project_root / "planning"})()

    ensure_current_find_research_plan.write_current_find_artifact_markdowns(paths, taste_dir, run_id, [fields], [], [])

    markdown = (taste_dir / "read.md").read_text(encoding="utf-8")
    copied = (run_dir / "read.md").read_text(encoding="utf-8")
    assert "SLRM+TAPO：多时间步轨迹级偏好优化。" not in markdown
    assert "全文未读取" not in markdown
    assert "待正文精读" not in markdown
    assert "### 原论文摘要（中文）" in markdown
    assert "### 论文动机" in markdown
    assert "### 详细方法" in markdown
    assert "扩散时间步、用户历史、候选物品和物品语义" in markdown
    assert markdown == copied
    assert (legacy_run_dir / "read_results.json").exists()


def test_current_find_reading_sanitizer_adds_chinese_sentence_punctuation():
    row = {
        "abstract_zh": "中文摘要说明方法贡献",
        "motivation_zh": "论文动机说明推荐稀疏性问题",
        "method_details_zh": "方法详细说明扩散去噪和语义条件融合",
        "experiments_zh": "实验比较基线和消融",
        "limitations_zh": "局限包括采样成本",
        "method_advantages_zh": ["优点是能够拆分协同和语义信号贡献"],
        "method_disadvantages_zh": ["不足是推理成本和协议敏感性仍需复核"],
    }

    clean = ensure_current_find_research_plan._sanitize_reading_public_fields(row)

    for key in ["abstract_zh", "motivation_zh", "method_details_zh", "experiments_zh", "limitations_zh"]:
        assert clean[key].endswith("。")
    assert all(item.endswith("。") for item in clean["method_advantages_zh"])
    assert all(item.endswith("。") for item in clean["method_disadvantages_zh"])


def test_render_read_md_hides_placeholder_deep_read_fields():
    run_id = "find_demo_placeholder_filter"
    row = {
        "paper_id": "paper-placeholder",
        "title": "Placeholder Paper",
        "venue": "KDD",
        "year": "2026",
        "abstract_zh": "全文文本证据已抓取，但精读内容仍需项目代理基于正文重写摘要、动机、方法、实验和局限。",
        "motivation_zh": "论文动机待补；需要结合论文引言确认。",
        "method_details_zh": "详细方法待补；当前未读取到论文全文，不能仅凭题录或摘要确认模型结构、训练目标和推理流程。",
        "experiments_zh": "实验设置与结果待补；需要从论文正文确认数据集、评价指标、对照方法、负采样、消融和主要结果。",
        "limitations_zh": "当前可访问正文证据不足，实验设置、消融和失败边界尚不能确认。",
        "method_advantages_zh": ["通过扩散机制提供可分析的生成过程，便于后续和同协议基线比较。"],
        "method_disadvantages_zh": ["全文未读取，方法差异和优缺点待正文精读后确认。"],
        "source_evidence": {"pdf_text_chars": 50000, "full_text_status": "pdf_text_read"},
    }

    markdown = ensure_current_find_research_plan.render_read_md([row], run_id)

    assert "全文未读取" not in markdown
    assert "待正文精读" not in markdown
    assert "论文动机待补" not in markdown
    assert "详细方法待补" not in markdown
    assert "实验设置与结果待补" not in markdown
    assert "当前可访问正文证据不足" not in markdown
    assert "未通过精读合同" in markdown


def test_deep_read_list_accepts_concise_specific_items_but_rejects_placeholders():
    concrete_items = [
        "定长生成限制使模型无法自由探索不同长度推理路径，可能错过最优策略。",
        "生成速度慢会放大强化学习训练成本，限制更长序列的测试时计算缩放。",
        "未报告多次运行标准差，统计显著性仍需额外复核。",
    ]
    placeholder_items = [
        "全文未读取，方法差异和优缺点待正文精读后确认。",
        "待正文精读后再确认具体优点和局限。",
    ]

    assert ensure_current_find_research_plan._deep_read_list_ok(concrete_items)
    assert not ensure_current_find_research_plan._deep_read_list_ok(placeholder_items)


def test_deep_read_experiment_contract_accepts_generative_metrics_and_baselines():
    row = {
        "title": "Long-tailed diffusion example",
        **_v4_deep_read_fields("Long-tailed diffusion example"),
        "experiments_zh": (
            "实验使用 CIFAR10LT 长尾图像数据集，按指数衰减不平衡因子构造头部类和尾部类任务，并说明头部类约有数千样本而尾部类样本明显更少。"
            "对照包括 Vanilla DDPM、CBDM 和加入互学习的扩散模型，三者保持 U-net 评分网络、训练步数、学习率搜索范围和采样数量一致。"
            "指标报告 FID、IS 和 KL 散度，主结果显示互学习相对 DDPM 降低 FID，同时记录 IS 变化和最坏类别误差，避免只看平均生成质量。"
            "消融比较不同互学习权重和学习率，展示 beta 过小接近无互学习、beta 过大损伤头部质量的趋势，并给出超参数敏感性分析。"
            "论文还用玩具高斯混合实验可视化尾部高误差区域，结合真实图像数据说明尾部改善、头部风险和计算成本之间的取舍。"
            "记录内容还包括每类生成样本数、训练预算、学习率组合、最优 beta 选择、相对提升幅度和作者对理论贡献优先于经验全面超越的限制声明。"
            "这些信息使 TASTE 能把数据、基线、指标、主结果、消融和边界条件拆开核对，而不是把 FID 变化直接当成跨任务结论。"
        ),
        "method_disadvantages_zh": [
            "头部类别生成质量可能因互学习迁移而下降，需要单独审计头部性能和最坏类别误差。",
            "互学习分布固定为均匀分布，尚未证明它是最优加权策略，跨数据集迁移还需要复核。",
        ],
    }

    gaps = ensure_current_find_research_plan._reading_deep_read_content_gaps(row)

    assert not any(item.startswith("experiments_zh:") for item in gaps)
    assert not any(item.startswith("method_disadvantages_zh:") for item in gaps)


def test_current_find_artifact_writer_policy_marks_turn_for_termination():
    import importlib.util

    spec = importlib.util.spec_from_file_location("claude_project_session", SCRIPTS / "claude_project_session.py")
    claude_project_session = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(claude_project_session)

    reason = claude_project_session.bash_command_tool_policy_issue(
        "python3 << 'PYEOF'\nimport json\nopen('/workspace/taste/projects/demo/planning/finding/read_results.json','w').write('{}')\nPYEOF\n",
        "demo_project",
        "current-find-claude-read-idea-plan",
    )
    policy_type = "current_find_artifact_writer" if claude_project_session.is_current_find_artifact_policy_reason(reason) else "other"
    report = {
        "recoverable_by_current_find_repair": policy_type in {"current_find_artifact_writer", "current_find_gate_state_writer"},
        "terminate_current_turn": True,
        "termination_reason": "current_find_repair_required" if policy_type in {"current_find_artifact_writer", "current_find_gate_state_writer"} else "tool_policy_violation",
    }

    assert policy_type == "current_find_artifact_writer"
    assert report["recoverable_by_current_find_repair"] is True
    assert report["terminate_current_turn"] is True
    assert report["termination_reason"] == "current_find_repair_required"



def test_project_level_validation_requires_subagent_deep_read_audit(tmp_path):
    run_id = "find_demo_subagent_required"
    find_results = {
        "run_id": run_id,
        "strong_recommendations": [
            {"id": "paper-1", "title": "Subagent Required Paper", "url": "https://example.test/paper", "evidence_tier": "strong_recommendation"}
        ],
    }
    reading = {
        "paper_id": "paper-1",
        "title": "Subagent Required Paper",
        "url": "https://example.test/paper",
        "verdict": "core_reading",
        "support_role": "core_method_reference",
        **_v4_deep_read_fields("Subagent Required Paper"),
        "full_text_available": True,
        "full_text_status": "pdf_text_read",
        "pdf_text_chars": 60000,
        "source_evidence": {"text_chars": 60000, "text_path": "texts/subagent-required.txt"},
    }
    paths = type("Paths", (), {"reports": tmp_path / "reports", "state": tmp_path / "state"})()
    paths.reports.mkdir(parents=True)
    paths.state.mkdir(parents=True)
    (paths.reports / "claude_project_session.md").write_text(f"run {run_id}: no delegated paper-reading task here", encoding="utf-8")

    valid, report = ensure_current_find_research_plan.validate_claude_readings_against_current_find([reading], find_results, 1, paths, run_id)

    assert valid is False
    assert report["subagent_deep_read_audit"]["valid"] is False
    assert "Task/subagent" in " ".join(report["blockers"])


def test_project_level_validation_accepts_audited_subagent_deep_read(tmp_path):
    run_id = "find_demo_subagent_ready"
    find_results = {
        "run_id": run_id,
        "strong_recommendations": [
            {"id": "paper-1", "title": "Subagent Ready Paper", "url": "https://example.test/paper", "evidence_tier": "strong_recommendation"}
        ],
    }
    reading = {
        "paper_id": "paper-1",
        "title": "Subagent Ready Paper",
        "url": "https://example.test/paper",
        "verdict": "core_reading",
        "support_role": "core_method_reference",
        **_v4_deep_read_fields("Subagent Ready Paper"),
        "full_text_available": True,
        "full_text_status": "pdf_text_read",
        "pdf_text_chars": 60000,
        "source_evidence": {"text_chars": 60000, "text_path": "texts/subagent-ready.txt"},
        "subagent_deep_read": True,
        "deep_read_audit": {"mode": "task_subagent", "subagent_used": True, "status": "completed", "text_path": "texts/subagent-ready.txt", "evidence_chars": 60000},
    }
    paths = type("Paths", (), {"reports": tmp_path / "reports", "state": tmp_path / "state"})()
    paths.reports.mkdir(parents=True)
    paths.state.mkdir(parents=True)
    (paths.reports / "claude_project_session.md").write_text(f"run {run_id}: Claude 调用工具: Task input={{}}", encoding="utf-8")

    valid, report = ensure_current_find_research_plan.validate_claude_readings_against_current_find([reading], find_results, 1, paths, run_id)

    assert valid is True
    assert report["subagent_deep_read_audit"]["valid"] is True


def test_current_find_json_artifacts_are_write_only():
    import importlib.util

    spec = importlib.util.spec_from_file_location("claude_project_session", SCRIPTS / "claude_project_session.py")
    claude_project_session = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(claude_project_session)

    stage = "current-find-claude-read-idea-plan"
    assert claude_project_session.current_find_tool_policy_issue(
        "Edit",
        {"file_path": "/workspace/taste/projects/demo/planning/finding/read_results.json"},
        stage,
    )
    assert claude_project_session.current_find_tool_policy_issue(
        "Edit",
        {"file": "/workspace/taste/projects/demo/planning/finding/read_results.json"},
        stage,
    )
    assert claude_project_session.current_find_tool_policy_issue(
        "Edit",
        {
            "file_path": "/workspace/taste/projects/demo/planning/finding/read_results.json",
            "old_string": '块大小激活了模型中不同"专家"子网络。固定块大小导致偏向特定',
            "new_string": "块大小激活了模型中不同「专家」子网络。固定块大小导致偏向特定",
            "replace_all": False,
        },
        stage,
    )
    assert claude_project_session.current_find_tool_policy_issue(
        "Edit",
        {
            "file_path": "/workspace/taste/projects/demo/planning/finding/read_results.json",
            "old_string": '块大小激活了模型中不同"专家"子网络。固定块大小导致偏向特定',
            "new_string": "块大小激活了模型中不同专家子网络。并且新增了科研内容。",
            "replace_all": False,
        },
        stage,
    )
    assert claude_project_session.current_find_tool_policy_issue(
        "MultiEdit",
        {"file_path": "/workspace/taste/projects/demo/planning/finding/ideas.json"},
        stage,
    )
    assert claude_project_session.current_find_tool_policy_issue(
        "MultiEdit",
        {"file": "/workspace/taste/projects/demo/planning/finding/plans.json"},
        stage,
    )
    assert not claude_project_session.current_find_tool_policy_issue(
        "Write",
        {"file_path": "/workspace/taste/projects/demo/planning/finding/read_results.json"},
        stage,
    )
    fragment_path = "/workspace/taste/projects/demo/planning/finding/current_find_deep_read_fragments/01_paper.json"
    assert not claude_project_session.current_find_tool_policy_issue("Write", {"file_path": fragment_path}, stage)
    assert not claude_project_session.current_find_tool_policy_issue("Edit", {"file_path": fragment_path}, stage)
    assert not claude_project_session.current_find_tool_policy_issue("MultiEdit", {"file_path": fragment_path}, stage)
    markdown_reason = claude_project_session.current_find_tool_policy_issue(
        "Write",
        {"file": "/workspace/taste/projects/demo/planning/finding/read.md"},
        stage,
    )
    assert "Markdown artifacts" in markdown_reason


def test_current_find_deep_read_fragments_allow_claude_file_repairs_not_bash_generated():
    import importlib.util

    spec = importlib.util.spec_from_file_location("claude_project_session", SCRIPTS / "claude_project_session.py")
    claude_project_session = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(claude_project_session)

    stage = "current-find-claude-read-idea-plan"
    fragment = "/workspace/taste/projects/demo/planning/finding/current_find_deep_read_fragments/01_paper.json"
    assert not claude_project_session.current_find_tool_policy_issue("Write", {"file_path": fragment}, stage)
    assert not claude_project_session.current_find_tool_policy_issue("Edit", {"file_path": fragment}, stage)
    assert not claude_project_session.current_find_tool_policy_issue("MultiEdit", {"file_path": fragment}, stage)
    redirect_reason = claude_project_session.bash_command_tool_policy_issue(f"cat > {fragment} <<'JSON'\n{{}}\nJSON", "demo_project", stage)
    assert claude_project_session.is_current_find_artifact_policy_reason(redirect_reason)
    python_reason = claude_project_session.bash_command_tool_policy_issue(
        "python3 - <<'PY'\nfrom pathlib import Path\nPath('planning/finding/current_find_deep_read_fragments/01_paper.json').write_text('{}')\nPY",
        "demo_project",
        stage,
    )
    assert claude_project_session.is_current_find_artifact_policy_reason(python_reason)

def test_full_cycle_and_autonomous_require_exactly_one_selected_plan_contract():
    import importlib.util

    full_spec = importlib.util.spec_from_file_location("run_full_research_cycle", SCRIPTS / "run_full_research_cycle.py")
    run_full_research_cycle = importlib.util.module_from_spec(full_spec)
    assert full_spec and full_spec.loader
    full_spec.loader.exec_module(run_full_research_cycle)

    auto_spec = importlib.util.spec_from_file_location("run_autonomous_research", SCRIPTS / "run_autonomous_research.py")
    run_autonomous_research = importlib.util.module_from_spec(auto_spec)
    assert auto_spec and auto_spec.loader
    auto_spec.loader.exec_module(run_autonomous_research)

    missing = {
        "required": True,
        "status": "blocked_missing_selected_plan",
        "selected_plan_id": "",
        "selection_issue": "missing_selected_plan",
        "candidate_counts": {"ideas": 5, "plans": 5},
    }
    ambiguous = {
        "required": True,
        "status": "blocked_ambiguous_selected_plan",
        "selected_plan_id": "",
        "selection_issue": "ambiguous_selected_plan",
        "candidate_counts": {"ideas": 5, "plans": 5},
    }
    stale_bad_selection = {
        "required": True,
        "status": "selected_plan_ready",
        "selected_plan_id": "plan-1",
        "selection_issue": "selected_plan_missing_matching_idea",
        "candidate_counts": {"ideas": 5, "plans": 5},
    }
    ready = {
        "required": True,
        "status": "selected_plan_ready",
        "selected_plan_id": "plan-1",
        "selected_idea_id": "idea-1",
        "selection_issue": "",
        "candidate_counts": {"ideas": 5, "plans": 5},
    }

    gate = run_full_research_cycle.FullCycle.current_find_selected_plan_gate_blocking
    assert gate(object(), missing)
    assert gate(object(), ambiguous)
    assert gate(object(), stale_bad_selection)
    assert not gate(object(), ready)

    assert not run_autonomous_research.selected_plan_contract_ready(missing)
    assert not run_autonomous_research.selected_plan_contract_ready(ambiguous)
    assert not run_autonomous_research.selected_plan_contract_ready(stale_bad_selection)
    assert run_autonomous_research.selected_plan_contract_ready(ready)
    assert run_autonomous_research.selected_plan_contract_ready({"required": False})




def _ready_three_part_idea(idx: int) -> dict:
    return _ready_scored_idea(idx)


def test_missing_selected_plan_is_selection_only_failure_when_content_ready():
    readings = [
        dict(
            _v4_deep_read_fields(f"Paper {idx}"),
            title=f"Paper {idx}",
            paper_id=f"paper-{idx}",
            full_text_available=True,
            full_text_status="pdf_text_read",
            pdf_text_chars=5000,
            subagent_deep_read=True,
            deep_read_audit={"mode": "task_subagent", "subagent_used": True, "status": "completed", "text_path": "full_text.txt", "evidence_chars": 5000},
        )
        for idx in range(2)
    ]
    ideas = [_ready_three_part_idea(idx) for idx in range(5)]
    plans = [{"plan_id": f"plan-idea-{idx}", "idea_id": f"idea-{idx}", "title": f"plan {idx}"} for idx in range(5)]
    validation = {
        "valid": True,
        "run_id": "find-test",
        "policy_version": ensure_current_find_research_plan.FULL_TEXT_READ_POLICY_VERSION,
        "expected_recommendation_count": 2,
        "actual_reading_count": 2,
        "full_text_evidence_count": 2,
        "full_text_reading_count": 2,
        "pending_full_text_reading_count": 0,
        "blockers": [],
        "generated_at": "2030-01-01T00:00:00+00:00",
    }

    assert ensure_current_find_research_plan._current_find_content_ready_without_selection(
        readings, ideas, plans, ["query one", "query two", "query three"], validation, "find-test", 2, 5, None
    )
    assert not ensure_current_find_research_plan._current_find_contract_ready(
        readings, ideas, plans, ["query one", "query two", "query three"], validation, "find-test", 2, 5, None
    )

    observed = {
        "selected_execution_issue": "missing_selected_plan",
        "content_ready_without_selection": True,
        "takeover_process_current": False,
        "takeover_artifacts_current": False,
    }
    assert ensure_current_find_research_plan.current_find_contract_failure_type(validation, observed) == "missing_selected_plan"
    assert ensure_current_find_research_plan.current_find_contract_next_required_action(validation, observed) == "rerun_current_find_claude_takeover_select_single_best_plan"



def test_current_find_claude_session_extends_no_event_timeout():
    spec = importlib.util.spec_from_file_location("claude_project_session", SCRIPTS / "claude_project_session.py")
    claude_project_session = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(claude_project_session)

    assert claude_project_session.claude_no_event_timeout_seconds("current-find-claude-read-idea-plan", 3600, {}, {}) >= 1800
    assert claude_project_session.claude_no_event_timeout_seconds("experiment-loop", 3600, {}, {}) == 300
    assert claude_project_session.claude_no_event_timeout_seconds("current-find-claude-read-idea-plan", 600, {}, {}) == 540


def test_claude_selection_prompt_is_read_only_except_plans_json(tmp_path):
    paths = type("Paths", (), {"state": tmp_path / "state"})()
    paths.state.mkdir(parents=True)

    prompt_path = ensure_current_find_research_plan.write_claude_selection_prompt(
        paths,
        "demo_project",
        "find-test",
        observed={"failure_type": "missing_selected_plan", "content_ready_without_selection": True},
    )
    text = prompt_path.read_text(encoding="utf-8")

    assert "selection-only" in text
    assert "只能用 Claude Code 的 Write 工具完整重写 `planning/finding/plans.json`" in text
    assert "不准写 `read_results.json`" in text
    assert "`ideas.json`" in text and "不准写" in text
    assert "不准启动 Find" in text
    assert "训练" in text and "不准启动" in text
    assert "selected_for_execution: true" in text
    assert "execute_next: true" in text
    assert "main_claude_code_after_deep_read" in text
    assert "not_selected_candidate_backlog" in text



def test_current_find_selection_success_receipt_overwrites_stale_failure(tmp_path):
    run_id = "find_demo_selection_receipt"
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    paths = type("Paths", (), {"state": state_dir})()
    ideas = [_ready_three_part_idea(idx) for idx in range(5)]
    plans = [
        {
            "plan_id": f"plan-{idx}",
            "idea_id": f"idea-{idx}",
            "title": f"Plan {idx}",
            "selected_for_execution": idx == 2,
            "execute_next": idx == 2,
            "execution_selection": {
                "selected": idx == 2,
                "selected_by": "main_claude_code_after_deep_read" if idx == 2 else "not_selected_candidate_backlog",
                "reason": "主控 Claude Code 基于完整精读、idea 和 plan 对比选择该计划。" if idx == 2 else "保留为候选 backlog。",
            },
        }
        for idx in range(5)
    ]
    old_failure = {
        "status": "completed",
        "run_id": run_id,
        "contract_validation_valid": False,
        "contract_failure": {
            "status": "failed_contract_validation",
            "failure_type": "stale_or_missing_current_find_takeover",
        },
    }
    ensure_current_find_research_plan.save_json(state_dir / "current_find_claude_selection_result.json", old_failure)

    receipt = ensure_current_find_research_plan.sync_current_find_selection_success_receipt(
        paths,
        run_id,
        {
            "status": "already_current_valid_claude_artifacts",
            "return_code": 0,
            "prompt_path": str(state_dir / "current_find_claude_takeover_repair_prompt_attempt2.md"),
        },
        ideas,
        plans,
        {"run_id": run_id, "valid": True},
        reason="valid_artifacts_ready",
    )
    persisted = ensure_current_find_research_plan.load_json(state_dir / "current_find_claude_selection_result.json", {})

    assert receipt["contract_validation_valid"] is True
    assert persisted["contract_validation_valid"] is True
    assert persisted.get("contract_failure") is None
    assert persisted["selected_plan_id"] == "plan-2"
    assert persisted["selected_idea_id"] == "idea-2"
    assert persisted["sync_reason"] == "valid_artifacts_ready"
    assert persisted["status"] == "already_current_valid_claude_selection"


def test_current_find_selection_stage_only_allows_complete_plans_write():
    import importlib.util

    spec = importlib.util.spec_from_file_location("claude_project_session", SCRIPTS / "claude_project_session.py")
    claude_project_session = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(claude_project_session)

    stage = "current-find-claude-select-plan"
    plans_path = "/workspace/taste/projects/demo/planning/finding/plans.json"
    read_path = "/workspace/taste/projects/demo/planning/finding/read_results.json"
    ideas_path = "/workspace/taste/projects/demo/planning/finding/ideas.json"
    fragment_path = "/workspace/taste/projects/demo/planning/finding/current_find_deep_read_fragments/01_paper.json"

    assert not claude_project_session.current_find_tool_policy_issue("Write", {"file_path": plans_path}, stage)
    assert claude_project_session.is_current_find_artifact_policy_reason(
        claude_project_session.current_find_tool_policy_issue("Write", {"file_path": read_path}, stage)
    )
    assert claude_project_session.is_current_find_artifact_policy_reason(
        claude_project_session.current_find_tool_policy_issue("Write", {"file_path": ideas_path}, stage)
    )
    fragment_reason = claude_project_session.current_find_tool_policy_issue("Write", {"file_path": fragment_path}, stage)
    assert "selection-only stage" in fragment_reason
    assert claude_project_session.is_current_find_artifact_policy_reason(
        claude_project_session.current_find_tool_policy_issue("MultiEdit", {"file_path": plans_path}, stage)
    )


def test_import_experiment_artifacts_derives_generic_method_slug_from_model_type():
    import importlib.util

    spec = importlib.util.spec_from_file_location("import_experiment_artifacts", SCRIPTS / "import_experiment_artifacts.py")
    importer = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(importer)

    assert importer.method_from_command(["python", "train_diffusion.py", "--model_type", "Candidate Rec++"]) == "candidate_rec"
    assert importer.method_from_command(["python", "train_diffusion.py"]) == "diffusion_recommender"
    assert importer.method_from_command(["python", "train_wrapper.py", "--model_type", "Reference Encoder"]) == "reference_encoder_sasrec"
    assert importer.method_from_command(
        ["python", "train_diffusion.py", "--model_type", "Candidate Rec", "--method", "explicit_method"]
    ) == "explicit_method"
