from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


READING_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = READING_ROOT / "scripts"
for entry in [SCRIPTS_ROOT, *[path for path in SCRIPTS_ROOT.rglob("*") if path.is_dir()]]:
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

SPEC = importlib.util.spec_from_file_location(
    "reading_pipeline_scoring_tests",
    SCRIPTS_ROOT / "pipeline" / "read_pipeline.py",
)
assert SPEC is not None and SPEC.loader is not None
READ_PIPELINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(READ_PIPELINE)


class ReadingScoringTests(unittest.TestCase):
    def test_default_selects_first_fifty_in_input_ranking_order(self) -> None:
        rows = [{"paper_id": f"p{index}", "title": f"Paper {index}"} for index in range(60)]
        available, selected, limit = READ_PIPELINE._select_ranked_input_articles(
            {"ranked_articles": rows},
            0,
        )
        self.assertEqual(60, len(available))
        self.assertEqual(50, limit)
        self.assertEqual([f"p{index}" for index in range(50)], [row["paper_id"] for row in selected])

        _, selected_three, limit_three = READ_PIPELINE._select_ranked_input_articles(
            {"ranked_articles": rows},
            3,
        )
        self.assertEqual(3, limit_three)
        self.assertEqual(["p0", "p1", "p2"], [row["paper_id"] for row in selected_three])

    def test_scores_sync_to_items_and_reading_then_rerank_by_average(self) -> None:
        items = [
            {"paper_index": 1, "reading": {}, "validation": {"deep_read_complete": True}},
            {"paper_index": 2, "reading": {}, "validation": {"deep_read_complete": True}},
            {"paper_index": 3, "reading": {}, "validation": {"deep_read_complete": True}},
        ]
        scores = READ_PIPELINE._normalize_reading_scores({
            "scores": [
                {"paper_index": 1, "match_score": 8, "transferability_score": 6},
                {"paper_index": 2, "match_score": 9, "transferability_score": 9},
                {"paper_index": 3, "match_score": 7, "transferability_score": 7},
            ]
        }, items)
        ranked = READ_PIPELINE._apply_reading_scores_and_rank(items, scores)

        self.assertEqual([2, 1, 3], [item["paper_index"] for item in ranked])
        self.assertEqual([1, 2, 3], [item["final_read_rank"] for item in ranked])
        self.assertEqual(9.0, ranked[0]["average_score"])
        self.assertEqual(9.0, ranked[0]["reading"]["match_score"])
        self.assertEqual(9.0, ranked[0]["reading"]["transferability_score"])

    def test_partial_scoring_can_preserve_original_order_without_fake_scores(self) -> None:
        items = [
            {"paper_index": 1, "reading": {}, "validation": {"deep_read_complete": True}},
            {"paper_index": 2, "reading": {}, "validation": {"deep_read_complete": True}},
        ]
        scores = READ_PIPELINE._normalize_reading_scores({
            "scores": [{"paper_index": 2, "match_score": 10, "transferability_score": 10}]
        }, items)
        ranked = READ_PIPELINE._apply_reading_scores_and_rank(items, scores, rerank=False)

        self.assertEqual([1, 2], [item["paper_index"] for item in ranked])
        self.assertNotIn("match_score", ranked[0])
        self.assertEqual(10.0, ranked[1]["match_score"])

    def test_final_markdown_places_both_scores_next_to_paper_title(self) -> None:
        markdown = "# Paper\n\n## 摘要\n\n摘要正文。\n"
        rendered = READ_PIPELINE._demote_article_markdown(
            markdown,
            index=1,
            title="Paper",
            item={"match_score": 8.5, "transferability_score": 9},
        )
        self.assertIn("### 1. Paper\n\n**匹配度：** 8.5/10", rendered)
        self.assertIn("**可借鉴性：** 9/10", rendered)

    def test_score_prompt_requires_every_reading_artifact_and_two_dimensions(self) -> None:
        runtime_root = READING_ROOT / ".runtime"
        runtime_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime_root) as temp_dir:
            run_path = Path(temp_dir)
            prompt = READ_PIPELINE.build_reading_score_prompt(
                research_context={"research_topic": "protein design", "researcher_profile": "method reuse"},
                articles=[
                    {"paper_index": 1, "title": "A", "article_markdown_path": "papers/001/read.md"},
                    {"paper_index": 2, "title": "B", "article_markdown_path": "papers/002/read.md"},
                ],
                run_path=run_path,
                output_path=run_path / "outputs" / "reading_scores.json",
            )
        self.assertIn("match_score", prompt)
        self.assertIn("transferability_score", prompt)
        self.assertIn("papers/001/read.md", prompt)
        self.assertIn("papers/002/read.md", prompt)
        self.assertIn("researcher_profile", prompt)

    def test_complete_scoring_reorders_read_results_items_and_read_md_together(self) -> None:
        output_root = READING_ROOT / ".runtime" / "output"
        output_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output_root) as temp_dir:
            run_path = Path(temp_dir)
            items = []
            for paper_index, title in [(1, "First input"), (2, "Second input")]:
                item_dir = run_path / "papers" / f"{paper_index:03d}"
                item_dir.mkdir(parents=True)
                article_path = item_dir / "read.md"
                article_path.write_text(
                    f"# {title}\n\n"
                    "## 摘要\n\n摘要。\n\n"
                    "## 动机与核心创新\n\n动机与创新。\n\n"
                    "## 方法\n\n方法。\n\n"
                    "## 实验结果\n\n实验。\n\n"
                    "## 优缺点总结\n\n优缺点。\n",
                    encoding="utf-8",
                )
                items.append({
                    "paper_index": paper_index,
                    "paper": {"paper_id": f"p{paper_index}", "title": title},
                    "reading": {"title": title},
                    "validation": {"deep_read_complete": True},
                    "claude_result": {"article_markdown_path": "read.md"},
                    "artifacts": {"article_markdown": str(article_path)},
                })

            def fake_scoring_runner(*, expected_output_path: Path, **_kwargs):
                payload = {
                    "status": "complete",
                    "scores": [
                        {"paper_index": 1, "match_score": 4, "transferability_score": 6},
                        {"paper_index": 2, "match_score": 9, "transferability_score": 9},
                    ],
                }
                expected_output_path.parent.mkdir(parents=True, exist_ok=True)
                expected_output_path.write_text(json.dumps(payload), encoding="utf-8")
                return {
                    "status": "complete",
                    "return_code": 0,
                    "run_executed": True,
                    "expected_output_audit": {"exists": True, "valid_json": True},
                    "nonruntime_artifact_audit": {"status": "passed", "problem_count": 0},
                    "external_temp_artifact_audit": {"status": "passed", "problem_count": 0},
                    "result_payload": payload,
                }

            with mock.patch.object(READ_PIPELINE, "run_claude_deep_read", fake_scoring_runner):
                ranked, scoring = READ_PIPELINE._run_final_reading_scoring(
                    directory=run_path,
                    items=items,
                    research_context={"research_topic": "target topic", "researcher_profile": "target profile"},
                    claude_mode="run",
                    timeout_sec=60,
                    log=lambda _message: None,
                )

            read_md_path = run_path / "read.md"
            aggregation = READ_PIPELINE._aggregate_read_md_from_article_markdown(
                run_path=run_path,
                items=ranked,
                read_md_path=read_md_path,
            )
            machine_items = [READ_PIPELINE._machine_read_result(item) for item in ranked]
            read_md = read_md_path.read_text(encoding="utf-8")

            self.assertEqual("complete", scoring["status"])
            self.assertTrue(aggregation["valid"])
            self.assertEqual(["p2", "p1"], [item["paper"]["paper_id"] for item in machine_items])
            self.assertLess(read_md.index("### 1. Second input"), read_md.index("### 2. First input"))
            self.assertIn("**匹配度：** 9/10", read_md)
            self.assertIn("**可借鉴性：** 9/10", read_md)
            self.assertEqual(9.0, machine_items[0]["reading"]["average_score"])

    def test_untrusted_claude_receipt_cannot_supply_scores_or_rerank(self) -> None:
        output_root = READING_ROOT / ".runtime" / "output"
        output_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output_root) as temp_dir:
            run_path = Path(temp_dir)
            items = []
            for paper_index in (1, 2):
                item_dir = run_path / "papers" / f"{paper_index:03d}"
                item_dir.mkdir(parents=True)
                article_path = item_dir / "read.md"
                article_path.write_text(f"# Paper {paper_index}\n\n## 摘要\n\n摘要。\n", encoding="utf-8")
                items.append({
                    "paper_index": paper_index,
                    "paper": {"paper_id": f"p{paper_index}", "title": f"Paper {paper_index}"},
                    "reading": {},
                    "validation": {"deep_read_complete": True},
                    "claude_result": {"article_markdown_path": "read.md"},
                    "artifacts": {"article_markdown": str(article_path)},
                })

            def fake_blocked_runner(*, expected_output_path: Path, **_kwargs):
                payload = {
                    "status": "complete",
                    "scores": [
                        {"paper_index": 1, "match_score": 1, "transferability_score": 1},
                        {"paper_index": 2, "match_score": 10, "transferability_score": 10},
                    ],
                }
                expected_output_path.parent.mkdir(parents=True, exist_ok=True)
                expected_output_path.write_text(json.dumps(payload), encoding="utf-8")
                return {
                    "status": "blocked_external_temp_artifact_created",
                    "return_code": 0,
                    "run_executed": True,
                    "expected_output_audit": {"exists": True, "valid_json": True},
                    "nonruntime_artifact_audit": {"status": "passed", "problem_count": 0},
                    "external_temp_artifact_audit": {
                        "status": "failed_external_temp_artifact_detected",
                        "problem_count": 1,
                    },
                    "result_payload": payload,
                }

            with mock.patch.object(READ_PIPELINE, "run_claude_deep_read", fake_blocked_runner):
                ranked, scoring = READ_PIPELINE._run_final_reading_scoring(
                    directory=run_path,
                    items=items,
                    research_context={"research_topic": "target"},
                    claude_mode="run",
                    timeout_sec=60,
                    log=lambda _message: None,
                )

            self.assertEqual("complete_with_warnings", scoring["status"])
            self.assertFalse(scoring["receipt_gate"]["accepted"])
            self.assertEqual([1, 2], [item["paper_index"] for item in ranked])
            self.assertTrue(all("match_score" not in item for item in ranked))
            score_artifact = json.loads((run_path / "outputs" / "reading_scores.json").read_text(encoding="utf-8"))
            self.assertEqual([], score_artifact["scores"])


if __name__ == "__main__":
    unittest.main()
