from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGES = ("finding", "reading", "ideation", "planning", "environment", "experimenting", "writing")


def _load_experiment_runner():
    experimenting_module_root = ROOT / "modules" / "experimenting"
    for name in ["experiment_plan", "experiment_records", "file_utils", "runtime_environment"]:
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        "experimenting_run_autonomous_experiment",
        experimenting_module_root / "scripts" / "orchestration" / "run_autonomous_experiment.py",
    )
    assert spec and spec.loader
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


def _load_reading_main():
    spec = importlib.util.spec_from_file_location("reading_main_cli", ROOT / "modules" / "reading" / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_environment_module(module_name: str, relative_path: str):
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, str(sys.path.pop(sys.path.index(str(environment_module_root)))))
    spec = importlib.util.spec_from_file_location(module_name, environment_module_root / relative_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reading_cli_explicit_action_keeps_project_for_child_command(monkeypatch):
    reading_main = _load_reading_main()
    calls = []

    def fake_run_script(action, args):
        calls.append((action, list(args)))
        return 0

    monkeypatch.setattr(reading_main, "_run_script", fake_run_script)

    rc = reading_main.main(["--action", "current_find_research_plan", "--project", "protein", "--read-limit", "0", "--idea-count", "5", "--force"])

    assert rc == 0
    assert calls == [(
        "ensure_current_find_research_plan",
        ["--project", "protein", "--read-limit", "0", "--idea-count", "5", "--force"],
    )]


def test_reading_cli_positional_action_still_forwards_remaining_args(monkeypatch):
    reading_main = _load_reading_main()
    calls = []

    def fake_run_script(action, args):
        calls.append((action, list(args)))
        return 0

    monkeypatch.setattr(reading_main, "_run_script", fake_run_script)

    rc = reading_main.main(["current-find-research-plan", "--project", "protein", "--force"])

    assert rc == 0
    assert calls == [("ensure_current_find_research_plan", ["--project", "protein", "--force"])]


def test_reading_current_find_wrapper_imports_with_private_common_first():
    reading_main = _load_reading_main()
    proc = subprocess.run(
        [sys.executable, str(ROOT / "modules" / "reading" / "scripts" / "ensure_current_find_research_plan.py"), "--help"],
        cwd=ROOT,
        env=reading_main._python_env(),
        text=True,
        capture_output=True,
        timeout=30,
    )

    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "No module named 'common.read_ranking'" not in output
    assert "--project" in output


def _load_claude_project_session():
    framework_scripts = ROOT / "framework" / "scripts"
    if str(framework_scripts) not in sys.path:
        sys.path.insert(0, str(framework_scripts))
    spec = importlib.util.spec_from_file_location(
        "framework_claude_project_session_policy",
        framework_scripts / "claude_project_session.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_current_find_orchestrator():
    framework_scripts = ROOT / "framework" / "scripts"
    reading_scripts = ROOT / "modules" / "reading" / "scripts"
    reading_private = reading_scripts / "private"
    finding_scripts = ROOT / "modules" / "finding" / "scripts"
    for path in [str(framework_scripts), str(finding_scripts), str(reading_scripts), str(reading_private)]:
        if path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)
    for name in ["common", "literature_policy", "project_paths", "runtime_env", "project_config"]:
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        "reading_current_find_orchestrator",
        reading_scripts / "orchestration" / "ensure_current_find_research_plan.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_current_find_allows_controlled_idea_scoring_audit_write():
    session = _load_claude_project_session()

    assert session.current_find_tool_policy_issue(
        "Write",
        {"file_path": "/home/fmh/workspace/TASTE/projects/protein/planning/finding/idea_scoring.json"},
        "current_find_read_idea_plan",
    ) == ""

    assert session.current_find_tool_policy_issue(
        "Write",
        {"file_path": "/home/fmh/workspace/TASTE/projects/protein/state/idea_scoring.json"},
        "current_find_read_idea_plan",
    ) == session.CURRENT_FIND_FILE_WRITE_WHITELIST_POLICY

    unsafe = "/home/fmh/workspace/miniforge/envs/ar_taste/bin/python -c \"open('planning/finding/idea_scoring.json','w').write('{}')\""
    assert session.current_find_artifact_generator_policy_issue(unsafe, "current_find_read_idea_plan") == session.CURRENT_FIND_ARTIFACT_WRITER_POLICY


def test_current_find_derives_targeted_queries_from_claude_artifacts(tmp_path):
    orchestrator = _load_current_find_orchestrator()

    class Paths:
        state = tmp_path / "state"

    Paths.state.mkdir()
    ideas = {
        "run_id": "find_test",
        "source": orchestrator.CLAUDE_TAKEOVER_SOURCE,
        "ideas": [
            {
                "id": "idea_5",
                "title": "知识引导解耦可解释评估框架",
                "new_method": "Use ProtDiS representations and Flexible Kernels GP ranking for protein design evaluation.",
                "initial_experiment": "Validate ProtDiS, Flexible Kernels, PDFBench, and ProtDBench protocols before experiments.",
                "inspired_by": [
                    {"paper_id": "paper_9f58", "title": "Learning Protein Structure-Function Relationships through Knowledge-guided Representation Decomposition"},
                    {"paper_id": "paper_6f85", "title": "Flexible Kernels for Protein Property Prediction"},
                ],
            }
        ],
    }
    plans = {
        "run_id": "find_test",
        "source": orchestrator.CLAUDE_TAKEOVER_SOURCE,
        "plans": [
            {
                "plan_id": "plan_5",
                "idea_id": "idea_5",
                "title": "知识引导解耦的可解释生成评估框架实施计划",
                "selected_for_execution": True,
                "execute_next": True,
                "execution_selection": {"selected": True},
                "environment_requirements": ["ProtDiS encoder", "Flexible Kernels GP", "PDFBench and ProtDBench data"],
            }
        ],
    }

    queries = orchestrator.extract_targeted_search_queries(Paths, {}, ideas, plans, {})

    assert len(queries) >= 3
    assert any("ProtDiS" in query for query in queries)
    assert any("Flexible Kernels" in query for query in queries)


def test_all_stage_contracts_and_framework_dry_run_are_callable():
    for stage in STAGES:
        proc = subprocess.run([sys.executable, str(ROOT / "modules" / stage / "main.py"), "--contract"], cwd=ROOT, text=True, capture_output=True, timeout=30)
        assert proc.returncode == 0, (stage, proc.stderr or proc.stdout)
        payload = json.loads(proc.stdout)
        assert payload["stage"] == stage
        assert payload["entrypoint"] == f"modules/{stage}/main.py"
        assert payload["scripts_are_private_backend"] is True
        assert payload["required_external_inputs"]
        assert payload["artifacts_out"]

    run_id = "pytest_contract_dry_run"
    state_root = ROOT / "framework" / "workspace" / "pytest"
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "framework" / "scripts" / "orchestration" / "run_taste_framework.py"),
            "run",
            "--mode",
            "dry-run",
            "--strategy",
            "deterministic",
            "--research-goal",
            "pytest contract smoke",
            "--run-id",
            run_id,
            "--state-root",
            str(state_root),
            "--no-contract-probe",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    status = json.loads((state_root / "runs" / run_id / "public" / "frontend_status.json").read_text(encoding="utf-8"))
    assert status["progress"] == {"completed": 7, "total": 7, "percent": 100.0}
    assert status["status"] == "paper_pipeline_finished"


def test_framework_only_stage_reports_single_stage_scope():
    run_id = "pytest_only_environment"
    state_root = ROOT / "framework" / "workspace" / "pytest"
    plan_path = state_root / "pytest_plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps({"title": "pytest plan", "repo_url": "https://github.com/example/repo"}), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "framework" / "scripts" / "orchestration" / "run_taste_framework.py"),
            "run",
            "--mode",
            "dry-run",
            "--strategy",
            "deterministic",
            "--only-stage",
            "environment",
            "--research-goal",
            "pytest single stage",
            "--run-id",
            run_id,
            "--state-root",
            str(state_root),
            "--plan-json",
            str(plan_path),
            "--module-arg",
            f"environment=--plan {plan_path} --run-id {run_id}",
            "--no-contract-probe",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    status = json.loads((state_root / "runs" / run_id / "public" / "frontend_status.json").read_text(encoding="utf-8"))
    assert status["stage_scope"] == ["environment"]
    assert status["progress"] == {"completed": 1, "total": 1, "percent": 100.0}
    assert status["status"] == "stage_scope_finished"


def test_environment_dependency_policy_rewrites_pyg_conda_plan():
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_dependency_policy",
        environment_module_root / "scripts" / "orchestration" / "dependency_policy.py",
    )
    assert spec and spec.loader
    dependency_policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dependency_policy)
    normalize_environment_plan_commands = dependency_policy.normalize_environment_plan_commands

    plan = {
        "python_version": "3.9",
        "commands": [
            {"phase": "conda_create", "command": ["conda", "create", "-n", "rigid", "python=3.9", "pip", "-y"], "required": True},
            {"phase": "conda_install_pytorch", "command": ["conda", "install", "-n", "rigid", "pytorch>=2.5.1", "pytorch-cuda>=12.4", "-y"], "required": True},
            {"phase": "conda_install_pyg", "command": ["conda", "install", "-n", "rigid", "-c", "pyg", "pyg", "pytorch-scatter", "pytorch-sparse", "pytorch-cluster", "-y"], "required": True},
            {"phase": "verify_import", "command": ["conda", "run", "-n", "rigid", "python", "-c", "import torch_geometric"], "required": True},
        ],
    }
    machine = {"gpu": [{"name": "NVIDIA GeForce RTX 5090", "compute_capability": "12.0"}]}

    normalized = normalize_environment_plan_commands(plan, machine=machine, policy_version="test-policy")
    commands = [row["command"] for row in normalized["commands"]]
    command_text = "\n".join(" ".join(command) for command in commands)

    assert normalized["python_version"] == "3.11"
    assert normalized["commands"][0]["command"] == ["conda", "create", "-n", "rigid", "python=3.11", "pip", "-y"]
    assert "torch==2.9.1+cu128" in command_text
    assert "torchvision==0.24.1+cu128" in command_text
    assert "torchaudio==2.9.1+cu128" in command_text
    assert "https://download.pytorch.org/whl/cu128" in command_text
    assert "https://data.pyg.org/whl/torch-2.9.1+cu128.html" in command_text
    assert "conda install -n rigid -c pyg pyg" not in command_text
    assert any(row["phase"] == "verify_pyg_cuda_import" for row in normalized["commands"])
    assert normalized["backend_dependency_policy"]["policy_version"] == "test-policy"
    assert len(normalized["plan_policy_rewrites"]) >= 4


def test_environment_rewrites_python_entrypoints_to_run_local_prefix():
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_autonomous_deploy",
        environment_module_root / "scripts" / "orchestration" / "autonomous_deploy.py",
    )
    assert spec and spec.loader
    autonomous_deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(autonomous_deploy)

    env_prefix = ROOT / "modules" / "environment" / "runs" / "pytest_run" / "conda_envs" / "rigid"
    conda_exe = "/home/fmh/workspace/miniforge/bin/conda"

    pip_command = autonomous_deploy.rewrite_command(["pip", "install", "torch"], conda_exe, "rigid", env_prefix)
    assert pip_command == [str(env_prefix / "bin" / "python"), "-m", "pip", "install", "torch"]
    assert autonomous_deploy.command_uses_conda_prefix(pip_command, env_prefix)
    assert autonomous_deploy._conda_prefix_tokens_have_setup_action(pip_command)

    run_command = autonomous_deploy.rewrite_command(
        ["conda", "run", "-n", "rigid", "python", "-c", "import torch; import dm_tree; from dm_tree import map_structure"],
        conda_exe,
        "rigid",
        env_prefix,
    )
    assert run_command == [str(env_prefix / "bin" / "python"), "-c", "import torch; import tree as dm_tree; from tree import map_structure"]
    assert autonomous_deploy.command_uses_conda_prefix(run_command, env_prefix)
    assert autonomous_deploy._conda_prefix_tokens_have_verify_action(run_command)


def test_environment_repo_review_falls_back_to_plan_github_candidates():
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_autonomous_deploy_repo_review",
        environment_module_root / "scripts" / "orchestration" / "autonomous_deploy.py",
    )
    assert spec and spec.loader
    autonomous_deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(autonomous_deploy)

    candidates = ["https://github.com/ZhanghanNi/RigidSSL", "https://github.com/Long-Kai/Steering-PLMs"]
    selected, issues, fallback = autonomous_deploy.repo_candidates_after_review(candidates, {"return_code": 0, "json": {}, "stdout_tail": "ready"})
    assert selected == candidates
    assert fallback is True
    assert "repo candidate review did not produce valid JSON" in issues

    selected, issues, fallback = autonomous_deploy.repo_candidates_after_review(
        candidates,
        {"return_code": 0, "json": {"status": "ready", "ordered_repo_urls": [candidates[1], candidates[0]]}},
    )
    assert selected == [candidates[1], candidates[0]]
    assert fallback is False


def test_environment_rewrites_huggingface_cli_to_current_hf_cli(tmp_path):
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_autonomous_deploy_hf",
        environment_module_root / "scripts" / "orchestration" / "autonomous_deploy.py",
    )
    assert spec and spec.loader
    autonomous_deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(autonomous_deploy)

    env_prefix = tmp_path / "run" / "conda_envs" / "rigid"
    command = autonomous_deploy.rewrite_command(
        ["huggingface-cli", "download", "tonynzh/RigidSSL", "--repo-type", "dataset", "--resume-download", "--local-dir", "data/raw"],
        "/home/fmh/workspace/miniforge/bin/conda",
        "rigid",
        env_prefix,
    )
    assert command == [str(env_prefix / "bin" / "hf"), "download", "tonynzh/RigidSSL", "--repo-type", "dataset", "--local-dir", "data/raw"]

    run_dir = tmp_path / "run"
    script = run_dir / "scripts" / "download_setup.sh"
    script.parent.mkdir(parents=True)
    script.write_text(
        """#!/usr/bin/env bash
huggingface-cli download tonynzh/RigidSSL \
  --repo-type dataset \
  --resume-download \
  --local-dir data/raw
""",
        encoding="utf-8",
    )
    migrations = autonomous_deploy.normalize_generated_script_commands_for_command(
        ["conda", "run", "-p", str(env_prefix), "--no-capture-output", "bash", "scripts/download_setup.sh"],
        run_dir,
        run_dir,
    )
    updated = script.read_text(encoding="utf-8")
    assert migrations and migrations[0]["path"] == str(script)
    assert "hf download tonynzh/RigidSSL" in updated
    assert "huggingface-cli" not in updated
    assert "--resume-download" not in updated


def test_environment_isolated_runtime_scrubs_inconsistent_conda_activation_state(tmp_path):
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))

    from scripts.common.shell import isolated_runtime_env

    env = isolated_runtime_env(tmp_path, extra={"CONDA_SHLVL": "1", "CONDA_EXE": "/bad/conda", "CONDA_PREFIX": "/bad/env"})
    assert "CONDA_SHLVL" not in env
    assert "CONDA_EXE" not in env
    assert "CONDA_PREFIX" not in env


def test_environment_blocks_missing_generated_shell_scripts(tmp_path):
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_autonomous_deploy_missing_script",
        environment_module_root / "scripts" / "orchestration" / "autonomous_deploy.py",
    )
    assert spec and spec.loader
    autonomous_deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(autonomous_deploy)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    command = ["bash", str(run_dir / "round_01" / "write_setup_script.sh")]
    issue = autonomous_deploy.missing_shell_script_issue(command, run_dir, run_dir)
    assert "shell 脚本不存在" in issue
    assert "write_setup_script.sh" in issue

    existing = run_dir / "scripts" / "download.sh"
    existing.parent.mkdir()
    existing.write_text("""#!/usr/bin/env bash
echo ok
""", encoding="utf-8")
    assert autonomous_deploy.missing_shell_script_issue(["bash", "scripts/download.sh"], run_dir, run_dir) == ""



def test_environment_prompt_forbids_dependency_matrix_search(tmp_path):
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_autonomous_deploy_prompt",
        environment_module_root / "scripts" / "orchestration" / "autonomous_deploy.py",
    )
    assert spec and spec.loader
    autonomous_deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(autonomous_deploy)

    prompt = autonomous_deploy.prompt_environment_plan(
        {"title": "RigidSSL", "target_metrics": []},
        {"gpu": [{"name": "NVIDIA GeForce RTX 5090", "memory_gb": 31}]},
        {"readmes": []},
        {"target_metrics": []},
        [],
        tmp_path / "plan.json",
        1,
    )
    assert "不要在计划生成阶段运行 `conda search`" in prompt
    assert "由后端 policy 在执行前统一规范化" in prompt

def test_environment_dependency_policy_pins_rigidssl_biopython_for_atom3d():
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_dependency_policy_biopython",
        environment_module_root / "scripts" / "orchestration" / "dependency_policy.py",
    )
    assert spec and spec.loader
    dependency_policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dependency_policy)

    plan = {
        "title": "RigidSSL reproduction",
        "env_name": "rigidssl_protein",
        "commands": [
            {"phase": "pip_install", "command": ["python", "-m", "pip", "install", "atom3d", "biopython", "mdtraj"], "required": True},
            {"phase": "pip_install_indirect", "command": ["python", "-m", "pip", "install", "atom3d", "mdtraj"], "required": True},
        ],
    }
    normalized = dependency_policy.normalize_environment_plan_commands(plan, machine={}, policy_version="test-policy")
    commands = [row["command"] for row in normalized["commands"]]
    assert commands[0] == ["python", "-m", "pip", "install", "atom3d", "biopython==1.81", "mdtraj"]
    assert commands[1] == ["python", "-m", "pip", "install", "biopython==1.81", "atom3d", "mdtraj"]
    assert normalized["backend_dependency_policy"]["biopython_legacy_spec"] == "biopython==1.81"



def test_environment_deterministic_rigidssl_plan_validates(tmp_path):
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_autonomous_deploy_deterministic",
        environment_module_root / "scripts" / "orchestration" / "autonomous_deploy.py",
    )
    assert spec and spec.loader
    autonomous_deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(autonomous_deploy)

    run_dir = tmp_path / "run"
    repo = run_dir / "repos" / "RigidSSL"
    (repo / "examples").mkdir(parents=True)
    (repo / "model").mkdir()
    (repo / "examples" / "RigidSSL_Perturb.py").write_text("", encoding="utf-8")
    (repo / "model" / "velocity_network.py").write_text("", encoding="utf-8")
    plan = autonomous_deploy.deterministic_rigidssl_environment_plan(run_dir, repo, {})
    plan = autonomous_deploy.normalize_environment_plan_commands(plan, machine={"gpu": [{"name": "NVIDIA GeForce RTX 5090", "compute_capability": "12.0"}]}, policy_version="test-policy")
    issues = autonomous_deploy.validate_environment_plan(plan, require_full_reproduction=False, repo_path=repo, run_dir=run_dir, machine={"gpu": [{"name": "NVIDIA GeForce RTX 5090", "memory_gb": 31}]}, paper_evidence={"target_metrics": []})
    assert not issues
    command_text = "\n".join(" ".join(row["command"]) for row in plan["commands"] if isinstance(row, dict))
    assert "biopython==1.81" in command_text
    assert "torch==2.9.1+cu128" in command_text
    assert any(row.get("phase") == "reproduce_smoke" and row.get("required") is True for row in plan["commands"] if isinstance(row, dict))

def test_environment_rewrites_rigidssl_model_and_smoke_probes(tmp_path):
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_autonomous_deploy_rigidssl",
        environment_module_root / "scripts" / "orchestration" / "autonomous_deploy.py",
    )
    assert spec and spec.loader
    autonomous_deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(autonomous_deploy)

    run_dir = tmp_path / "run"
    repo = run_dir / "repos" / "RigidSSL"
    (repo / "examples").mkdir(parents=True)
    (repo / "model").mkdir()
    (repo / "examples" / "RigidSSL_Perturb.py").write_text("", encoding="utf-8")
    (repo / "model" / "velocity_network.py").write_text("", encoding="utf-8")

    command, migrations = autonomous_deploy.normalize_repository_command_for_execution(
        {"phase": "verify_model"},
        [str(run_dir / "conda_envs" / "rigid" / "bin" / "python"), "-c", "from model.velocity_network import VelocityNetwork; m = VelocityNetwork()"],
        repo,
        run_dir,
    )
    assert command[1] == "-c"
    assert "model_setup" in command[2]
    assert "VelocityNetwork()" not in command[2]
    assert migrations

    smoke, smoke_migrations = autonomous_deploy.normalize_repository_command_for_execution(
        {"phase": "reproduce_smoke"},
        [str(run_dir / "conda_envs" / "rigid" / "bin" / "python"), "RigidSSL_Perturb.py", "--epochs", "1"],
        repo,
        run_dir,
    )
    assert smoke[1] == "-c"
    assert "load_dataset" in smoke[2]
    assert "next(iter(loader))" in smoke[2]
    assert "_single_worker_dataloader" in smoke[2]
    assert "loader_kwargs['num_workers'] = 0" in smoke[2]
    assert "loader_kwargs['pin_memory'] = False" in smoke[2]
    assert "RigidSSL_Perturb.py" not in smoke[0:2]
    assert smoke_migrations

    full, full_migrations = autonomous_deploy.normalize_repository_command_for_execution(
        {"phase": "reproduce_full"},
        [
            str(run_dir / "conda_envs" / "rigid" / "bin" / "python"),
            "RigidSSL_Perturb.py",
            "--dataset_portion",
            "full",
            "--epochs",
            "10",
            "--input_data_dir",
            str(run_dir / "data" / "RigidSSL_Perturb_data"),
            "--output_model_dir",
            str(run_dir / "output" / "perturb"),
            "--seed",
            "42",
        ],
        repo,
        run_dir,
    )
    assert full[1] == "-c"
    assert "runpy.run_path" in full[2]
    assert "_pyg_loader.DataLoader = _single_worker_dataloader" in full[2]
    assert "loader_kwargs['num_workers'] = 0" in full[2]
    assert "--epochs" in full[2] and "10" in full[2]
    assert "RigidSSL_Perturb.py" not in full[0:2]
    assert full_migrations

    env = autonomous_deploy.command_environment({"PYTHONPATH": str(run_dir / "extra")}, repo, {})
    assert env["PYTHONPATH"].split(":", 1)[0] == str(repo.resolve())
    assert env["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] == "1"


def test_environment_machine_alignment_accepts_noncritical_supported_hardware_row():
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_machine_alignment",
        "scripts/orchestration/autonomous_deploy.py",
    )

    plan = {
        "machine_assessment": {
            "paper_hardware_or_runtime_requirement": "≥1×24GB GPU",
            "local_machine_summary": "NVIDIA GeForce RTX 5090 with 32607 MiB VRAM and CUDA available",
            "fit_for_local_machine": True,
            "adaptation_actions": ["Use CUDA 12.8 wheels and FP32 validation on one GPU"],
            "evidence": ["machine_profile", "nvidia-smi", "GPU CUDA VRAM"],
        },
        "paper_config_alignment": [
            {
                "paper_item": "hardware/precision",
                "paper_value": "≥1×24GB GPU, no precision requirement",
                "implementation_choice": "RTX 5090 32GB, CUDA 12.8, FP32",
                "command_phase": "verify_cuda_imports",
                "evidence_source": "machine_profile runtime_probe nvidia-smi",
                "match_status": "matched",
                "critical": False,
            }
        ],
    }

    issues = autonomous_deploy.machine_assessment_issues(
        plan,
        {"gpu": [{"name": "NVIDIA GeForce RTX 5090", "memory_total": "32607 MiB", "compute_capability": "12.0"}]},
    )

    assert not issues


def test_environment_execute_plan_keeps_named_conda_envs_separate(monkeypatch, tmp_path):
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_named_envs",
        "scripts/orchestration/autonomous_deploy.py",
    )

    run_dir = tmp_path / "run"
    repo = run_dir / "repos" / "repo"
    repo.mkdir(parents=True)
    round_dir = run_dir / "round_01"
    executed: list[list[str]] = []

    def fake_run_logged(command, *, cwd, log_path, timeout_sec, env, required):
        executed.append([str(item) for item in command])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ok\n", encoding="utf-8")
        return {"command": autonomous_deploy.command_text(command), "status": "passed", "return_code": 0, "log_path": str(log_path), "required": required}

    monkeypatch.setattr(autonomous_deploy, "find_conda_executable", lambda: "conda")
    monkeypatch.setattr(autonomous_deploy, "run_logged", fake_run_logged)

    plan = {
        "env_name": "default_env",
        "commands": [
            {"phase": "conda_setup_protdis", "command": ["conda", "create", "-y", "-n", "protdis_env", "python=3.11"], "required": True},
            {"phase": "verify_protdis", "command": ["python", "-c", "print('protdis')"], "required": True},
            {"phase": "conda_setup_kermut", "command": ["conda", "create", "-y", "-n", "kermut_env", "python=3.11"], "required": True},
            {"phase": "verify_kermut", "command": ["python", "-c", "print('kermut')"], "required": True},
            {"phase": "verify_protdis_again", "command": ["conda", "run", "-n", "protdis_env", "python", "-c", "print('again')"], "required": True},
        ],
    }

    receipts = autonomous_deploy.execute_plan_commands(plan, repo, run_dir, round_dir, include_full=False, default_timeout=30, command_env={})

    protdis_python = str(run_dir / "conda_envs" / "protdis_env" / "bin" / "python")
    kermut_python = str(run_dir / "conda_envs" / "kermut_env" / "bin" / "python")
    assert len(receipts) == 5
    assert executed[0][0:4] == ["conda", "create", "-y", "-p"]
    assert executed[0][4] == str(run_dir / "conda_envs" / "protdis_env")
    assert executed[1][0] == protdis_python
    assert executed[2][4] == str(run_dir / "conda_envs" / "kermut_env")
    assert executed[3][0] == kermut_python
    assert executed[4][0] == protdis_python


def test_environment_execute_plan_skips_existing_conda_create_prefix(monkeypatch, tmp_path):
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_existing_prefix",
        "scripts/orchestration/autonomous_deploy.py",
    )

    run_dir = tmp_path / "run"
    repo = run_dir / "repos" / "repo"
    repo.mkdir(parents=True)
    prefix = run_dir / "conda_envs" / "demo_env"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "python").write_text("", encoding="utf-8")
    round_dir = run_dir / "round_01"
    executed: list[list[str]] = []

    def fake_run_logged(command, *, cwd, log_path, timeout_sec, env, required):
        executed.append([str(item) for item in command])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("verify ok\n", encoding="utf-8")
        return {"command": autonomous_deploy.command_text(command), "status": "passed", "return_code": 0, "log_path": str(log_path), "required": required}

    monkeypatch.setattr(autonomous_deploy, "find_conda_executable", lambda: "conda")
    monkeypatch.setattr(autonomous_deploy, "run_logged", fake_run_logged)

    plan = {
        "env_name": "demo_env",
        "commands": [
            {"phase": "conda_setup", "command": ["conda", "create", "-y", "-n", "demo_env", "python=3.11", "pip"], "required": True},
            {"phase": "verify", "command": ["python", "-c", "print('ok')"], "required": True},
        ],
    }

    receipts = autonomous_deploy.execute_plan_commands(plan, repo, run_dir, round_dir, include_full=False, default_timeout=30, command_env={})

    assert len(receipts) == 2
    assert receipts[0]["existing_prefix_reused"] is True
    assert receipts[0]["return_code"] == 0
    assert "复用已存在" in Path(receipts[0]["log_path"]).read_text(encoding="utf-8")
    assert len(executed) == 1
    assert executed[0][0] == str(prefix / "bin" / "python")


def test_environment_reusable_receipts_invalidated_by_later_conda_recreate(tmp_path):
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_reuse_invalidation",
        "scripts/orchestration/autonomous_deploy.py",
    )

    run_dir = tmp_path / "run"
    prefix = run_dir / "conda_envs" / "demo_env"
    round_01 = run_dir / "round_01"
    round_02 = run_dir / "round_02"
    round_01.mkdir(parents=True)
    round_02.mkdir(parents=True)
    install_cmd = str(prefix / "bin" / "python") + " -m pip install torch"
    create_cmd = "conda create -y -p " + str(prefix) + " python=3.11 pip"
    (round_01 / "command_receipts.json").write_text(json.dumps([
        {"phase": "install_torch", "command": install_cmd, "required": True, "status": "passed", "return_code": 0, "conda_env_prefix": str(prefix), "log_path": str(round_01 / "install.log")},
    ]), encoding="utf-8")
    (round_02 / "command_receipts.json").write_text(json.dumps([
        {"phase": "conda_setup", "command": create_cmd, "required": True, "status": "passed", "return_code": 0, "conda_env_prefix": str(prefix), "log_path": str(round_02 / "conda.log")},
    ]), encoding="utf-8")

    reusable = autonomous_deploy.build_reusable_command_receipt_index([
        {"round": 1, "receipts_path": str(round_01 / "command_receipts.json")},
        {"round": 2, "receipts_path": str(round_02 / "command_receipts.json")},
    ], run_dir)

    assert not reusable


def test_environment_rewrites_inline_python_import_aliases_and_compound_statements(tmp_path):
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_inline_normalize",
        "scripts/orchestration/autonomous_deploy.py",
    )

    command = autonomous_deploy.rewrite_command(
        [
            "conda",
            "run",
            "-n",
            "demo_env",
            "python",
            "-c",
            "import biopython; import dm_tree; x = 0; for i in range(2): x += i; print(x)",
        ],
        "conda",
        "demo_env",
        tmp_path / "conda_envs" / "demo_env",
    )

    assert command[0] == str(tmp_path / "conda_envs" / "demo_env" / "bin" / "python")
    assert command[1] == "-c"
    assert "import Bio" in command[2]
    assert "import biopython" not in command[2]
    assert "import tree as dm_tree" in command[2]
    assert "\nfor i in range(2):" in command[2]
    compile(command[2], "<environment-inline>", "exec")


def test_environment_inline_path_guard_handles_fstring_variable_prefix(tmp_path):
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_inline_fstring_guard",
        "scripts/orchestration/autonomous_deploy.py",
    )
    run_dir = tmp_path / "run"
    repo = run_dir / "repos" / "repo"
    repo.mkdir(parents=True)

    valid_code = "dr='demo_data'; import os; os.makedirs(f'{dr}/esm_encodings', exist_ok=True); open(f'{dr}/labels/demo.tsv','w').write('ok')"
    assert autonomous_deploy._python_inline_code_boundary_issues(valid_code, repo, run_dir) == []

    unsafe_code = "name='x'; open(f'/outside/{name}.txt','w').write('bad')"
    issues = autonomous_deploy._python_inline_code_boundary_issues(unsafe_code, repo, run_dir)
    assert issues and "/outside/" in issues[0]


def test_environment_skip_full_omits_full_dependent_output_checks():
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_skip_full_outputs",
        "scripts/orchestration/autonomous_deploy.py",
    )
    plan = {
        "commands": [
            {"phase": "verify_imports", "command": ["python", "-c", "import torch"], "required": True},
            {"phase": "reproduce_full", "command": ["python", "train.py"], "required": True},
            {"phase": "verify_outputs", "command": ["python", "-c", "check outputs"], "required": True},
            {"phase": "eval_metrics", "command": ["python", "eval.py"], "depends_on": "reproduce_full", "required": True},
        ]
    }

    skip_phases = [row["phase"] for row in autonomous_deploy.command_rows(plan, include_full=False)]
    full_phases = [row["phase"] for row in autonomous_deploy.command_rows(plan, include_full=True)]

    assert skip_phases == ["verify_imports"]
    assert full_phases == ["verify_imports", "reproduce_full", "verify_outputs", "eval_metrics"]


def test_environment_reuses_previous_success_receipts_but_never_reproduce_full(tmp_path):
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_autonomous_deploy_reuse",
        environment_module_root / "scripts" / "orchestration" / "autonomous_deploy.py",
    )
    assert spec and spec.loader
    autonomous_deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(autonomous_deploy)

    run_dir = tmp_path / "run"
    repo = run_dir / "repos" / "repo"
    repo.mkdir(parents=True)
    round_01 = run_dir / "round_01"
    round_02 = run_dir / "round_02"
    receipts_path = round_01 / "command_receipts.json"
    previous_install_log = round_01 / "logs" / "00_conda_create.log"
    previous_full_log = round_01 / "logs" / "01_reproduce_full.log"
    previous_install_log.parent.mkdir(parents=True)
    previous_install_log.write_text("install already ran\n", encoding="utf-8")
    previous_full_log.write_text("full already ran but must not be reused\n", encoding="utf-8")

    install_command = ["python", "-c", "from pathlib import Path; Path('install_ran.txt').write_text('ran', encoding='utf-8')"]
    full_command = ["python", "-c", "from pathlib import Path; Path('full_ran.txt').write_text('ran', encoding='utf-8')"]
    receipts_path.write_text(json.dumps([
        {"phase": "conda_create", "command": autonomous_deploy.command_text(install_command), "required": True, "status": "passed", "return_code": 0, "log_path": str(previous_install_log)},
        {"phase": "verify", "command": "python -c 'raise SystemExit(1)'", "required": True, "status": "failed", "return_code": 1, "log_path": str(round_01 / "logs" / "failed.log")},
        {"phase": "dataset", "command": "python -c 'print(1)'", "required": False, "status": "passed", "return_code": 0, "log_path": str(round_01 / "logs" / "optional.log")},
        {"phase": "reproduce_full", "command": autonomous_deploy.command_text(full_command), "required": True, "status": "passed", "return_code": 0, "log_path": str(previous_full_log)},
    ]), encoding="utf-8")

    reusable = autonomous_deploy.build_reusable_command_receipt_index([{"round": 1, "receipts_path": str(receipts_path)}], run_dir)
    assert ("conda_create", autonomous_deploy.command_text(install_command)) in reusable
    assert ("verify", "python -c 'raise SystemExit(1)'") not in reusable
    assert ("dataset", "python -c 'print(1)'") not in reusable
    assert ("reproduce_full", autonomous_deploy.command_text(full_command)) not in reusable

    plan = {"env_name": "", "commands": [
        {"phase": "conda_create", "command": install_command, "cwd": "run", "required": True},
        {"phase": "reproduce_full", "command": full_command, "cwd": "run", "required": True},
    ]}
    receipts = autonomous_deploy.execute_plan_commands(plan, repo, run_dir, round_02, True, 30, autonomous_deploy.runtime_env(), reusable_receipts=reusable)

    assert len(receipts) == 2
    assert receipts[0]["reused_receipt"] is True
    assert receipts[0]["reused_from_log_path"] == str(previous_install_log)
    assert not (run_dir / "install_ran.txt").exists()
    assert receipts[1].get("reused_receipt") is not True
    assert (run_dir / "full_ran.txt").read_text(encoding="utf-8") == "ran"
    assert "既有成功回执" in (round_02 / "logs" / "00_conda_create.log").read_text(encoding="utf-8")

def test_environment_dependency_policy_rewrites_incoherent_torch_pip_versions():
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_dependency_policy_torch_pip",
        environment_module_root / "scripts" / "orchestration" / "dependency_policy.py",
    )
    assert spec and spec.loader
    dependency_policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dependency_policy)

    plan = {
        "env_name": "rigid",
        "commands": [
            {"phase": "install_core", "command": ["python", "-m", "pip", "install", "torch==2.10.0", "torchvision==0.21.0", "torchaudio==2.10.0", "--index-url", "https://download.pytorch.org/whl/cu128"], "required": True},
            {"phase": "install_pyg", "command": ["python", "-m", "pip", "install", "torch_geometric", "torch_scatter", "torch_sparse", "torch_cluster", "-f", "https://data.pyg.org/whl/torch-2.10.0+cu128.html"], "required": True},
        ],
    }
    machine = {"gpu": [{"name": "NVIDIA GeForce RTX 5090", "compute_capability": "12.0"}]}

    normalized = dependency_policy.normalize_environment_plan_commands(plan, machine=machine, policy_version="test-policy")
    command_text = "\n".join(" ".join(row["command"]) for row in normalized["commands"] if isinstance(row, dict))

    assert normalized["commands"][0]["phase"] == "conda_create"
    assert "torch==2.10.0" not in command_text
    assert "torchvision==0.21.0" not in command_text
    assert "torch==2.9.1+cu128" in command_text
    assert "torchvision==0.24.1+cu128" in command_text
    assert "torchaudio==2.9.1+cu128" in command_text
    assert "https://data.pyg.org/whl/torch-2.9.1+cu128.html" in command_text
    assert any(row.get("phase") == "verify_pyg_cuda_import" for row in normalized["commands"] if isinstance(row, dict))



def test_environment_dependency_policy_rewrites_protdis_metrics_function_smoke():
    dependency_policy = _load_environment_module(
        "environment_dependency_policy_protdis_metrics",
        "scripts/orchestration/dependency_policy.py",
    )
    bad_snippet = (
        "import sys; sys.path.insert(0, 'tasks/proteinshake'); "
        "from src.models.metrics import compute_metrics, default_metrics; "
        "print(f'tasks metrics module OK, available: {list(default_metrics.keys())}')"
    )
    plan = {
        "env_name": "protdis_env",
        "commands": [
            {
                "phase": "verify_tasks_import",
                "command": ["conda", "run", "-n", "protdis_env", "python", "-c", bad_snippet],
                "cwd": "repo",
                "timeout_sec": 300,
                "required": True,
            }
        ],
    }

    normalized = dependency_policy.normalize_environment_plan_commands(plan, machine={}, policy_version="test-policy")
    row = normalized["commands"][0]
    command_text = dependency_policy.command_text(row["command"])
    code = row["command"][-1]

    assert row["phase"] == "verify_tasks_import"
    assert row["required"] is True
    assert "default_metrics.keys()" not in code
    assert "default_metrics('classification')" in code
    assert "compute_metrics" in code
    assert row["command"][:5] == ["conda", "run", "-n", "protdis_env", "python"]
    assert "default_metrics.keys()" not in command_text
    assert any("default_metrics is a function" in item.get("reason", "") for item in normalized.get("plan_policy_rewrites", []))


def test_environment_success_criteria_keeps_operational_handoff_gates():
    criteria_policy = _load_environment_module(
        "environment_criteria_policy_contract",
        "scripts/orchestration/criteria_policy.py",
    )
    plan = {
        "success_criteria": [
            {"name": "cuda_available", "operator": ">=", "value": 1, "source": "verify_imports confirms CUDA device_count >= 1"},
            {"name": "designability", "operator": ">=", "value": 0.758, "source": "paper Table 1"},
        ]
    }

    normalized = criteria_policy.normalize_success_criteria(plan, paper_evidence={}, policy_version="test-policy")

    assert len(normalized["success_criteria"]) == 2
    by_name = {row["name"]: row for row in normalized["success_criteria"]}
    assert by_name["cuda_available"]["approval_scope"] == "environment_gate"
    assert by_name["cuda_available"]["paper_metric"] is False
    assert by_name["designability"]["approval_scope"] == "paper_metric"
    assert by_name["designability"]["paper_metric"] is True


def test_environment_gate_success_criteria_cannot_approve_paper_metrics():
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_success_scope",
        "scripts/orchestration/autonomous_deploy.py",
    )
    env_plan = {
        "success_criteria": [
            {"name": "cuda_available", "operator": ">=", "value": 1, "source": "verify_imports", "approval_scope": "environment_gate", "paper_metric": False},
            {"name": "designability", "operator": ">=", "value": 0.758, "source": "paper Table 1", "approval_scope": "paper_metric", "paper_metric": True},
        ]
    }
    paper_evidence = {"target_metrics": [{"name": "designability_target", "operator": ">=", "value": 0.758, "source": "paper Table 1"}]}

    ok, evidence = autonomous_deploy._success_criteria_paper_binding_gate(env_plan, paper_evidence)

    assert ok, evidence
    assert evidence["criteria_count"] == 2
    assert evidence["paper_metric_criteria_count"] == 1
    assert evidence["environment_gate_criteria_count"] == 1
    assert evidence["matched_count"] == 1

    env_only_plan = {"success_criteria": [env_plan["success_criteria"][0]]}
    ok, evidence = autonomous_deploy._success_criteria_paper_binding_gate(env_only_plan, paper_evidence)
    assert not ok
    assert "环境交接" in " ".join(evidence["issues"])


def test_environment_binds_rigidssl_designability_target_alias_and_local_full_text_source(tmp_path):
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_autonomous_deploy_binding",
        environment_module_root / "scripts" / "orchestration" / "autonomous_deploy.py",
    )
    assert spec and spec.loader
    autonomous_deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(autonomous_deploy)

    env_plan = {
        "status": "ready_to_execute",
        "env_name": "rigidssl_protein",
        "success_criteria": [
            {"name": "designability", "metric": "designability", "operator": ">=", "value": 0.758, "source": "paper Table 1"},
            {"name": "scRMSD", "metric": "scRMSD", "operator": "<", "value": 2.0, "source": "paper Table 1"},
        ],
    }
    paper_evidence = {
        "target_metrics": [
            {"name": "designability_target", "operator": ">=", "value": 0.758, "source": "RigidSSL paper Table 1"},
            {"name": "scrmsd", "operator": "<", "value": 2.0, "source": "selected_plan.stages[1].tasks[0]"},
        ],
        "paper_claims_or_training_signals": [{"source": "paper", "text": "RigidSSL reports designability."}],
        "text_blocks": [{"source": "local_full_text:/tmp/rigidssl.txt", "text": "RigidSSL full paper text"}],
        "has_paper_context": True,
    }

    ok, evidence = autonomous_deploy._success_criteria_paper_binding_gate(env_plan, paper_evidence)
    assert ok, evidence
    assert evidence["matched_count"] == 2
    assert evidence["matches"][0]["paper_target_source"] == "RigidSSL paper Table 1"
    paper_ok, paper_context = autonomous_deploy._paper_context_gate(paper_evidence)
    assert paper_ok, paper_context
    assert paper_context["substantive_source_count"] == 1

def test_environment_normalizes_selected_plan_metrics_and_paper_source(tmp_path):
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))

    from scripts.common.plan_schema import load_experiment_plan, normalize_plan

    plan_path = tmp_path / "experiment_plan.json"
    plan_path.write_text(json.dumps({
        "selected_plan_id": "plan_rigidssl_controlled",
        "plans": [
            {
                "plan_id": "plan_rigidssl_controlled",
                "title": "RigidSSL controlled reproduction",
                "repo_url": "https://github.com/ZhanghanNi/RigidSSL",
                "data_protocol": {
                    "training_data": "AF2 Structure Database plus CATH domains",
                    "evaluation_metrics": [
                        "Designability improves by 43% on protein design benchmarks",
                        "设计复现容限不超过 3%",
                    ],
                },
            }
        ],
    }), encoding="utf-8")

    normalized = normalize_plan(load_experiment_plan(plan_path), plan_path)
    metrics = {row["name"]: row for row in normalized["target_metrics"]}

    assert normalized["schema_version"] == "environment.normalized_plan.v2"
    assert normalized["selected_plan_id"] == "plan_rigidssl_controlled"
    assert normalized["paper_url"] == "https://openreview.net/forum?id=YAWpZcXHnP"
    assert normalized["paper_source"]["title"].startswith("Rigidity-Aware Geometric Pretraining")
    assert metrics["designability_improvement"]["operator"] == ">="
    assert metrics["designability_improvement"]["value"] == "43%"
    assert metrics["designability_tolerance"]["operator"] == "<="
    assert metrics["designability_tolerance"]["value"] in {"3%", "5%"}
    assert "AF2 Structure Database" in normalized["dataset"]["training_data"]


def test_environment_workspace_audit_allows_web_runtime_log():
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_workspace_log",
        "scripts/orchestration/autonomous_deploy.py",
    )

    assert autonomous_deploy._is_framework_runtime_write("runtime/web_8765.log") is True
    assert autonomous_deploy._filter_framework_runtime_writes(["runtime/web_8765.log", "projects/protein/state/result.json"]) == ["projects/protein/state/result.json"]


def test_environment_handoff_reuses_historical_dataset_without_relaxing_full_approval(tmp_path):
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_handoff_history",
        "scripts/orchestration/autonomous_deploy.py",
    )

    run_dir = tmp_path / "run"
    round_01 = run_dir / "round_01"
    round_03 = run_dir / "round_03"
    repo = run_dir / "repos" / "paper_repo"
    dataset_repo = run_dir / "repos" / "TEDBench"
    env_prefix = run_dir / "conda_envs" / "protdis_env"
    for item in [round_01, round_03, repo, dataset_repo, env_prefix / "bin"]:
        item.mkdir(parents=True, exist_ok=True)
    (env_prefix / "bin" / "python").write_text("", encoding="utf-8")

    env_plan = {
        "env_name": "protdis_env",
        "commands": [
            {"phase": "conda_setup", "command": ["conda", "create", "-p", str(env_prefix), "python=3.11", "pip"], "required": True},
            {"phase": "verify_imports", "command": [str(env_prefix / "bin" / "python"), "-c", "import torch"], "required": True},
            {"phase": "verify_model", "command": [str(env_prefix / "bin" / "python"), "-c", "print('model ok')"], "required": True},
            {"phase": "reproduce_smoke", "command": [str(env_prefix / "bin" / "python"), "-c", "print('smoke ok')"], "required": True},
            {"phase": "reproduce_full", "command": [str(env_prefix / "bin" / "python"), "train.py"], "required": True},
        ],
        "machine_assessment": {
            "status": "suitable",
            "fit_for_local_machine": True,
            "paper_hardware_or_runtime_requirement": "CUDA GPU",
            "local_machine_summary": "local CUDA GPU runtime is available on RTX 5090",
            "adaptation_actions": ["use CUDA wheel smoke before full reproduction"],
            "evidence": ["runtime_probe", "nvidia-smi", "machine_profile.json"],
        },
        "paper_config_alignment": [
            {"paper_item": "model_architecture", "paper_value": "ProtDiS encoder", "implementation_choice": "verify_model imports and runs model", "command_phase": "verify_model", "evidence_source": "repo smoke", "match_status": "matched", "critical": True},
            {"paper_item": "dataset", "paper_value": "TEDBench", "implementation_choice": "dataset cloned in previous environment round", "command_phase": "dataset", "evidence_source": "dataset receipt", "match_status": "missing", "critical": True},
            {"paper_item": "hardware_precision", "paper_value": "CUDA", "implementation_choice": "verify_imports checks CUDA wheel", "command_phase": "verify_imports", "evidence_source": "runtime probe", "match_status": "adapted_for_machine", "critical": True},
        ],
        "success_criteria": [
            {"name": "auroc", "operator": ">=", "value": 0.8, "source": "paper table", "approval_scope": "paper_metric", "paper_metric": True}
        ],
    }
    env_plan_path = round_03 / "claude_environment_plan_round_03.json"
    env_plan_path.write_text(json.dumps(env_plan), encoding="utf-8")
    dataset_receipts = [
        {
            "phase": "dataset",
            "required": True,
            "return_code": 0,
            "status": "passed",
            "command": f"git clone --depth 1 https://github.com/BorgwardtLab/TEDBench {dataset_repo}",
            "tokens": ["git", "clone", "--depth", "1", "https://github.com/BorgwardtLab/TEDBench", str(dataset_repo)],
            "log_path": str(round_01 / "dataset.log"),
            "conda_env_prefix": str(env_prefix),
        }
    ]
    latest_receipts = [
        {"phase": "conda_setup", "required": True, "return_code": 0, "status": "passed", "command": f"conda create -y -p {env_prefix} python=3.11 pip", "conda_env_prefix": str(env_prefix), "log_path": str(round_03 / "conda.log")},
        {"phase": "verify_imports", "required": True, "return_code": 0, "status": "passed", "command": f"{env_prefix / 'bin' / 'python'} -c 'import torch'", "conda_env_prefix": str(env_prefix), "log_path": str(round_03 / "verify.log")},
        {"phase": "verify_model", "required": True, "return_code": 0, "status": "passed", "command": f"{env_prefix / 'bin' / 'python'} -c 'print(model)'", "conda_env_prefix": str(env_prefix), "log_path": str(round_03 / "model.log")},
        {"phase": "reproduce_smoke", "required": True, "return_code": 0, "status": "passed", "command": f"{env_prefix / 'bin' / 'python'} -c 'print(smoke)'", "conda_env_prefix": str(env_prefix), "log_path": str(round_03 / "smoke.log")},
    ]
    (round_01 / "command_receipts.json").write_text(json.dumps(dataset_receipts), encoding="utf-8")
    (round_03 / "command_receipts.json").write_text(json.dumps(latest_receipts), encoding="utf-8")

    strict_alignment_ok, strict_alignment_issues = autonomous_deploy.paper_config_alignment_passed(env_plan)
    assert strict_alignment_ok is False
    assert any("missing" in item for item in strict_alignment_issues)

    decision = {
        "run_id": "pytest_handoff_history",
        "decision": "continue_repair",
        "exit_code": 30,
        "rounds": [
            {"round": 1, "receipts_path": str(round_01 / "command_receipts.json")},
            {"round": 3, "env_plan_path": str(env_plan_path), "receipts_path": str(round_03 / "command_receipts.json"), "metric_evidence": []},
        ],
        "approval_gate": {"checks": [{"name": "workspace_write_audit", "passed": True, "reason": "audit ok"}]},
        "workspace_write_audit": {"status": "passed", "outside_workspace_writes": []},
        "machine_summary": {"gpu": [{"name": "RTX 5090", "memory_gb": 31}]},
    }
    repo_info = {"repo_url": "https://github.com/example/paper_repo", "repo_path": str(repo), "exists": True, "head_commit": "abc", "clone_receipt": {"return_code": 0, "status": "passed"}}

    updated = autonomous_deploy.attach_environment_handoff(decision, run_dir, {"title": "ProtDiS"}, repo_info)
    handoff = updated["environment_handoff"]
    checks = {row["name"]: row for row in handoff["handoff_gate"]["checks"]}

    assert handoff["ready_for_experimenting"] is True
    assert updated["decision"] == "environment_ready"
    assert checks["dataset_runtime"]["passed"] is True
    assert checks["paper_config_alignment"]["passed"] is True
    assert checks["paper_config_alignment"]["evidence"]["pending_downstream_alignment"][0]["paper_item"] == "dataset"
    assert len(handoff["data"]["successful_dataset_receipts"]) == 1


def test_environment_handoff_ready_without_promoting_paper_metrics(tmp_path):
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, str(sys.path.pop(sys.path.index(str(environment_module_root)))))
    spec = importlib.util.spec_from_file_location(
        "environment_autonomous_deploy_handoff",
        environment_module_root / "scripts" / "orchestration" / "autonomous_deploy.py",
    )
    assert spec and spec.loader
    autonomous_deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(autonomous_deploy)

    run_dir = tmp_path / "run"
    repo = run_dir / "repos" / "repo"
    env_prefix = run_dir / "conda_envs" / "rigid"
    (repo / "examples").mkdir(parents=True)
    (env_prefix / "bin").mkdir(parents=True)
    (env_prefix / "bin" / "python").write_text("", encoding="utf-8")
    env_plan = {
        "env_name": "rigid",
        "commands": [
            {"phase": "conda_create", "command": ["conda", "run", "-p", str(env_prefix), "python", "-m", "pip", "install", "torch"], "required": True},
            {"phase": "verify", "command": ["conda", "run", "-p", str(env_prefix), "python", "-c", "import torch"], "required": True},
            {"phase": "dataset", "command": ["hf", "download", "AF2"], "required": True},
            {"phase": "reproduce_smoke", "command": ["conda", "run", "-p", str(env_prefix), "python", "-c", "print('loader smoke')"], "required": True},
            {"phase": "reproduce_full", "command": ["python", "train.py"], "required": True},
        ],
        "success_criteria": [{"name": "designability", "operator": ">=", "value": 0.758, "source": "paper Table 1"}],
        "machine_assessment": {
            "status": "suitable",
            "fit_for_local_machine": True,
            "paper_hardware_or_runtime_requirement": "single GPU CUDA training",
            "local_machine_summary": "local CUDA GPU runtime is available for the smoke and reproduction commands",
            "adaptation_actions": ["use CUDA wheel and bounded smoke before full reproduction"],
            "evidence": ["runtime_probe", "nvidia-smi", "machine_profile.json"],
        },
        "paper_config_alignment": [
            {"paper_item": "designability metric", "paper_value": "0.758", "implementation_choice": "success_criteria designability >= 0.758", "command_phase": "reproduce_full", "evidence_source": "paper Table 1", "match_status": "matched", "critical": True},
            {"paper_item": "epochs", "paper_value": "10 epochs", "implementation_choice": "reproduce_full trains 10 epochs", "command_phase": "reproduce_full", "evidence_source": "repo config", "match_status": "matched", "critical": True},
            {"paper_item": "batch_size", "paper_value": "batch_size 64", "implementation_choice": "reproduce_full uses batch_size 64", "command_phase": "reproduce_full", "evidence_source": "repo config", "match_status": "matched", "critical": True},
            {"paper_item": "learning_rate", "paper_value": "lr=1e-4", "implementation_choice": "reproduce_full uses learning rate 1e-4", "command_phase": "reproduce_full", "evidence_source": "repo config", "match_status": "matched", "critical": True},
            {"paper_item": "hardware/precision", "paper_value": "CUDA GPU", "implementation_choice": "verify uses local CUDA GPU with CUDA wheels", "command_phase": "verify", "evidence_source": "runtime_probe nvidia-smi", "match_status": "adapted_for_machine", "critical": True},
        ],
    }
    receipts = [
        {"phase": "conda_create", "required": True, "return_code": 0, "status": "passed", "conda_env_prefix": str(env_prefix), "command": f"conda run -p {env_prefix} python -m pip install torch"},
        {"phase": "verify", "required": True, "return_code": 0, "status": "passed", "conda_env_prefix": str(env_prefix), "command": f"conda run -p {env_prefix} python -c 'import torch'"},
        {"phase": "dataset", "required": True, "return_code": 0, "status": "passed", "conda_env_prefix": str(env_prefix), "command": "hf download AF2 Structure Database", "stdout_tail": "AF2 Structure Database ready"},
        {"phase": "reproduce_smoke", "required": True, "return_code": 0, "status": "passed", "conda_env_prefix": str(env_prefix), "command": f"conda run -p {env_prefix} python -c 'loader smoke'", "stdout_tail": "loader smoke passed"},
        {"phase": "reproduce_full", "required": True, "return_code": 30, "status": "blocked", "conda_env_prefix": str(env_prefix), "command": "python train.py", "stdout_tail": "full metrics pending"},
    ]
    approval_gate = {"checks": [
        {"name": "repository_source", "passed": True, "reason": "repo ok"},
        {"name": "repository_documentation", "passed": False, "reason": "paper-level docs pending"},
        {"name": "conda_environment", "passed": True, "reason": "env ok"},
        {"name": "machine_fit", "passed": True, "reason": "machine ok"},
        {"name": "dataset_evidence", "passed": False, "reason": "paper-level dataset evidence pending"},
        {"name": "required_commands", "passed": False, "reason": "full reproduction pending"},
        {"name": "paper_config_alignment", "passed": True, "reason": "alignment ok"},
        {"name": "workspace_write_audit", "passed": True, "reason": "audit ok"},
        {"name": "metric_evidence", "passed": False, "reason": "metrics pending"},
        {"name": "reproduce_full", "passed": False, "reason": "full pending"},
    ]}
    handoff = autonomous_deploy.build_environment_handoff(
        "pytest_run",
        run_dir,
        {"title": "RigidSSL", "paper_url": "https://openreview.net/forum?id=YAWpZcXHnP", "selected_plan_id": "plan"},
        {
            "repo_url": "https://github.com/example/repo",
            "repo_path": str(repo),
            "exists": True,
            "head_commit": "abc",
            "clone_receipt": {"return_code": 0, "status": "passed"},
        },
        env_plan,
        receipts,
        approval_gate,
        [{"metric": "designability", "passed": False}],
        machine={},
        workspace_audit={"status": "passed", "outside_workspace_writes": []},
    )
    assert handoff["ready_for_experimenting"] is True
    assert handoff["handoff_gate"]["passed"] is True
    assert handoff["pending_downstream_metrics"][0]["metric"] == "designability"
    assert handoff["pending_downstream_metrics"][0]["status"] == "pending_experimenting_evaluation"
    handoff_checks = {row["name"]: row for row in handoff["handoff_gate"]["checks"]}
    assert "metric_evidence" not in handoff_checks
    assert "reproduce_full" not in handoff_checks
    assert "repository_documentation" not in handoff_checks
    assert "dataset_evidence" not in handoff_checks
    assert handoff_checks["required_commands"]["passed"] is True
    assert handoff_checks["required_commands"]["evidence"]["ignored_reproduce_full_count"] == 1


def test_experimenting_default_permission_mode_is_bypass_permissions():
    runner = _load_experiment_runner()
    args = runner.parse_args(["--plan", "plan.json", "--repo-path", "repo"])
    assert args.permission_mode == "bypassPermissions"


def test_experimenting_rejects_permission_denied_claude_success(tmp_path):
    runner = _load_experiment_runner()
    artifact_dir = tmp_path / "iteration_01"
    artifact_dir.mkdir()
    log_path = artifact_dir / "claude_stdout.log"
    payload = {
        "type": "result",
        "subtype": "success",
        "result": "The bash/python execution requires approval.",
        "permission_denials": [
            {"tool_name": "Bash", "tool_input": {"command": "python smoke_test.py"}},
        ],
    }
    log_path.write_text("# started_at: now\n\n" + json.dumps(payload, ensure_ascii=False) + "\n# finished_at: now\n# return_code: 0\n", encoding="utf-8")

    acceptance = runner.evaluate_iteration_acceptance(
        artifact_dir,
        {"return_code": 0, "log_path": str(log_path)},
        {"return_code": 0, "status": "not_configured", "log_path": ""},
        {},
    )

    assert acceptance["accepted"] is False
    assert acceptance["acceptance_status"] == "blocked_claude_permission_denied"
    assert any(row["code"] == "claude_permission_denied" for row in acceptance["acceptance_blockers"])
    fallback_summary = json.loads((artifact_dir / "experiment_iteration_summary.json").read_text(encoding="utf-8"))
    assert fallback_summary["status"] == "blocked_claude_permission_denied"
    assert fallback_summary["metrics"] == {}


def test_experimenting_trusts_empty_structured_permission_denials(tmp_path):
    runner = _load_experiment_runner()
    artifact_dir = tmp_path / "iteration_01"
    artifact_dir.mkdir()
    log_path = artifact_dir / "claude_stdout.log"
    payload = {
        "type": "result",
        "subtype": "success",
        "result": "Previous iteration's permission-denied blocker is resolved; commands executed successfully.",
        "permission_denials": [],
    }
    log_path.write_text(
        "# started_at: now\n\n" + json.dumps(payload, ensure_ascii=False) + "\n# finished_at: now\n# return_code: 0\n",
        encoding="utf-8",
    )
    (artifact_dir / "experiment_iteration_summary.json").write_text(
        json.dumps(
            {
                "status": "success",
                "commands": [{"description": "validation", "status": "passed"}],
                "metrics": {"smoke_metric": 1.0},
                "acceptance_status": "accepted",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    acceptance = runner.evaluate_iteration_acceptance(
        artifact_dir,
        {"return_code": 0, "log_path": str(log_path)},
        {"return_code": 0, "status": "not_configured", "log_path": ""},
        {},
    )

    assert acceptance["accepted"] is True
    assert acceptance["acceptance_status"] == "accepted"
    assert acceptance["permission_denials"] == []


def test_experimenting_rejects_summary_acceptance_blockers_without_permission_denial(tmp_path):
    runner = _load_experiment_runner()
    artifact_dir = tmp_path / "iteration_01"
    artifact_dir.mkdir()
    log_path = artifact_dir / "claude_stdout.log"
    payload = {
        "type": "result",
        "subtype": "success",
        "result": "Previous iteration's permission-denied blocker is resolved; commands executed successfully.",
        "permission_denials": [],
    }
    log_path.write_text(
        "# started_at: now\n\n" + json.dumps(payload, ensure_ascii=False) + "\n# finished_at: now\n# return_code: 0\n",
        encoding="utf-8",
    )
    (artifact_dir / "experiment_iteration_summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "commands": [{"description": "validation", "status": "passed"}],
                "metrics": {"throughput": 2.9},
                "acceptance_status": "partial_with_generation_blocker",
                "acceptance_blockers": [
                    {"code": "missing_generation_pipeline", "message": "No generation script."},
                    {"code": "missing_evaluation_pipeline", "message": "No evaluation script."},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    acceptance = runner.evaluate_iteration_acceptance(
        artifact_dir,
        {"return_code": 0, "log_path": str(log_path)},
        {"return_code": 0, "status": "not_configured", "log_path": ""},
        {},
    )

    assert acceptance["accepted"] is False
    assert acceptance["acceptance_status"] == "blocked_generation_evaluation_pipeline_missing"
    assert acceptance["permission_denials"] == []
    codes = {row["code"] for row in acceptance["acceptance_blockers"]}
    assert "claude_permission_denied" not in codes
    assert {"missing_generation_pipeline", "missing_evaluation_pipeline"} <= codes


def test_experimenting_imports_autonomous_wrapper_to_project_registry(tmp_path):
    sys.path.insert(0, str(ROOT / "framework" / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "experimenting_import_experiment_artifacts",
        ROOT / "modules" / "experimenting" / "scripts" / "records" / "import_experiment_artifacts.py",
    )
    assert spec and spec.loader
    importer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(importer)

    class Paths:
        root = tmp_path / "projects" / "demo"
        state = root / "state"
        experiments = root / "experiments"

    Paths.state.mkdir(parents=True)
    Paths.experiments.mkdir(parents=True)
    artifact_dir = tmp_path / "runtime" / "runs" / "demo_run" / "iteration_01"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "experiment_iteration_summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "acceptance_status": "partial_with_generation_blocker",
                "acceptance_blockers": [{"code": "missing_generation_pipeline", "message": "No generation script."}],
                "metrics": {"throughput": 2.9},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (artifact_dir / "wrapper_iteration_result.json").write_text(
        json.dumps(
            {
                "record": {
                    "timestamp": "2026-06-21T00:10:00Z",
                    "run_id": "demo_run",
                    "experiment_id": "demo_experiment",
                    "iteration": 1,
                    "status": "failed",
                    "method": "experiment",
                    "repo_path": "/tmp/repo",
                    "artifact_path": str(artifact_dir),
                    "metrics": {"throughput": 2.9},
                    "metric_name": "throughput",
                    "metric_value": 2.9,
                    "acceptance_status": "blocked_generation_pipeline_missing",
                    "acceptance_blockers": [{"code": "missing_generation_pipeline", "message": "No generation script."}],
                    "experiment_iteration_summary_status": "completed",
                    "experiment_iteration_summary_acceptance_status": "partial_with_generation_blocker",
                    "next_action": "replan",
                },
                "acceptance": {"acceptance_status": "blocked_generation_pipeline_missing"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = importer.import_artifact(Paths, artifact_dir)
    registry = json.loads((Paths.state / "experiment_registry.json").read_text(encoding="utf-8"))

    assert result["status"] == "imported_autonomous_wrapper"
    assert len(registry) == 1
    assert registry[0]["run_id"] == "demo_run"
    assert registry[0]["acceptance_status"] == "blocked_generation_pipeline_missing"
    assert registry[0]["experiment_iteration_summary_acceptance_status"] == "partial_with_generation_blocker"
    assert registry[0]["metrics"] == {"throughput": 2.9}





def test_framework_syncs_experimenting_module_records_to_project(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "framework" / "scripts"))
    from taste_backend.orchestration import orchestrator
    from taste_backend.orchestration.state import WorkflowState
    from taste_backend.runtime.context import FrameworkContext
    from taste_backend.runtime.executor import CommandResult

    workspace = tmp_path / "taste"
    output_root = workspace / "modules" / "experimenting" / "runtime" / "web" / "demo"
    artifact_dir = output_root / "runs" / "demo_run" / "iteration_01"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "experiment_iteration_summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "commands": [{"command": "python train.py -C config_kon_demo.yaml", "status": "passed"}],
                "judgment": {"verdict": "pipeline_validated", "weakest_slice": "synthetic demo only"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (artifact_dir / "metrics.json").write_text(
        json.dumps({"run_metadata": {"dataset": "synthetic_demo"}, "best_monitor_loss": 16.5881}, ensure_ascii=False),
        encoding="utf-8",
    )
    (artifact_dir / "wrapper_iteration_result.json").write_text("{}", encoding="utf-8")
    registry_dir = output_root / "state"
    registry_dir.mkdir(parents=True)
    (registry_dir / "experiment_registry.json").write_text(
        json.dumps(
            [
                {
                    "timestamp": "2026-06-21T07:48:10Z",
                    "run_id": "demo_run",
                    "experiment_id": "plan_5",
                    "iteration": 1,
                    "status": "success",
                    "method": "experiment",
                    "artifact_path": str(artifact_dir),
                    "acceptance_status": "accepted",
                    "metrics": {"best_epoch": 1.0},
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    project_state = workspace / "projects" / "demo" / "state"
    project_state.mkdir(parents=True)
    monkeypatch.setattr(orchestrator, "_refresh_project_experiment_table", lambda ctx, project: {"return_code": 0})
    ctx = FrameworkContext(
        workspace_root=workspace,
        framework_root=workspace / "framework",
        state_root=workspace / "framework" / "workspace",
        run_id="framework_demo",
        python=sys.executable,
        mode="execute",
    )
    state = WorkflowState(run_id="framework_demo", research_goal="", project="demo")
    result = CommandResult(
        stage="experimenting",
        action="run",
        command=[sys.executable, str(workspace / "modules" / "experimenting" / "main.py"), "--output-root", str(output_root)],
        status="completed",
        return_code=0,
        started_at="2026-06-21T07:43:47Z",
        finished_at="2026-06-21T07:48:10Z",
    )

    orchestrator._sync_experimenting_outputs_to_project(ctx, state, result)
    project_registry = json.loads((project_state / "experiment_registry.json").read_text(encoding="utf-8"))

    assert len(project_registry) == 1
    row = project_registry[0]
    assert row["run_id"] == "demo_run"
    assert row["dataset"] == "synthetic_demo"
    assert row["decision"] == "synthetic_only"
    assert row["command"] == "python train.py -C config_kon_demo.yaml"
    assert row["metrics"]["best_monitor_loss"] == 16.5881
    assert row["project_record_source"].endswith("modules/experimenting/runtime/web/demo/state/experiment_registry.json")
    assert any("experimenting 记录已同步到项目 demo" in note for note in state.notes)


def test_experiment_record_tools_acceptance_accepted_is_not_blocker():
    for path in [
        ROOT / "modules" / "experimenting" / "scripts" / "common",
        ROOT / "framework" / "scripts",
    ]:
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)
    spec = importlib.util.spec_from_file_location(
        "experiment_record_tools_for_acceptance",
        ROOT / "modules" / "experimenting" / "scripts" / "records" / "experiment_record_tools.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.acceptance_gate_text({"acceptance_status": "accepted", "acceptance_blockers": []}) == ""
    assert "missing_generation_pipeline" in module.acceptance_gate_text(
        {"acceptance_status": "blocked_generation_evaluation_pipeline_missing", "acceptance_blockers": [{"code": "missing_generation_pipeline"}]}
    )

def test_experimenting_requires_iteration_summary_for_success(tmp_path):
    runner = _load_experiment_runner()
    artifact_dir = tmp_path / "iteration_01"
    artifact_dir.mkdir()
    log_path = artifact_dir / "claude_stdout.log"
    log_path.write_text("# started_at: now\n\n" + json.dumps({"type": "result", "subtype": "success", "result": "done"}) + "\n# finished_at: now\n# return_code: 0\n", encoding="utf-8")

    acceptance = runner.evaluate_iteration_acceptance(
        artifact_dir,
        {"return_code": 0, "log_path": str(log_path)},
        {"return_code": 0, "status": "not_configured", "log_path": ""},
        {},
    )

    assert acceptance["accepted"] is False
    assert acceptance["acceptance_status"] == "blocked_missing_experiment_summary"
    assert any(row["code"] == "missing_experiment_iteration_summary" for row in acceptance["acceptance_blockers"])


def test_experimenting_rejects_success_summary_without_execution_evidence(tmp_path):
    runner = _load_experiment_runner()
    artifact_dir = tmp_path / "iteration_01"
    artifact_dir.mkdir()
    log_path = artifact_dir / "claude_stdout.log"
    log_path.write_text("# started_at: now\n\n" + json.dumps({"type": "result", "subtype": "success", "result": "done"}) + "\n# finished_at: now\n# return_code: 0\n", encoding="utf-8")
    (artifact_dir / "experiment_iteration_summary.json").write_text(
        json.dumps({"status": "success", "changed_files": ["model.py"], "metrics": {}, "commands": []}, ensure_ascii=False),
        encoding="utf-8",
    )

    acceptance = runner.evaluate_iteration_acceptance(
        artifact_dir,
        {"return_code": 0, "log_path": str(log_path)},
        {"return_code": 0, "status": "not_configured", "log_path": ""},
        {},
    )

    assert acceptance["accepted"] is False
    assert acceptance["acceptance_status"] == "blocked_missing_iteration_evidence"
    assert any(row["code"] == "missing_iteration_evidence" for row in acceptance["acceptance_blockers"])
