import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _write(path: Path, text: str = "content") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_writing_vendor_requires_nature_reference_only_for_nature_family(tmp_path):
    sync_vendor = _load_script("sync_writing_vendor")
    vendor_root = tmp_path / "vendor"
    paper_orchestra = vendor_root / "PaperOrchestra"
    for marker in sync_vendor.PAPER_ORCHESTRA_MARKERS:
        _write(paper_orchestra / marker)

    iclr = sync_vendor.collect_status(vendor_root=vendor_root, third_party_root=tmp_path / "third_party", paper_orchestra_dir=paper_orchestra, venue="ICLR")
    nature = sync_vendor.collect_status(vendor_root=vendor_root, third_party_root=tmp_path / "third_party", paper_orchestra_dir=paper_orchestra, venue="Nature")

    assert iclr["required_ready"] is True
    assert iclr["all_ready"] is False
    assert nature["required_ready"] is False
    assert "nature_family_writing_reference" in nature["missing_required_components"]


def test_sync_vendor_check_only_blocks_missing_nature_reference_without_writes(tmp_path):
    sync_vendor = _load_script("sync_writing_vendor")
    vendor_root = tmp_path / "vendor"
    paper_orchestra = vendor_root / "PaperOrchestra"
    for marker in sync_vendor.PAPER_ORCHESTRA_MARKERS:
        _write(paper_orchestra / marker)

    payload = sync_vendor.sync_vendor(
        vendor_root=vendor_root,
        third_party_root=tmp_path / "third_party",
        paper_orchestra_dir=paper_orchestra,
        venue="Nature",
        check_only=True,
    )

    assert payload["required_ready"] is False
    assert payload["commands"] == []
    assert not (vendor_root / "WRITING_VENDOR_PROVENANCE.json").exists()


def test_paper_bridge_vendor_check_passes_compact_check_when_skip_clone(monkeypatch, tmp_path):
    bridge = _load_script("run_paper_orchestra_bridge")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return {
            "return_code": 0,
            "stdout_tail": json.dumps({"required_ready": True, "status": "ready", "warnings": []}),
            "stderr_tail": "",
        }

    monkeypatch.setattr(bridge, "run", fake_run)
    payload = bridge.ensure_writing_vendor("Nature", tmp_path / "PaperOrchestra", skip_clone=True)

    assert payload["required_ready"] is True
    assert calls
    assert "--compact" in calls[0]
    assert "--check" in calls[0]
