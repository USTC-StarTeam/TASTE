#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from project_paths import build_paths


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def guess_entities(meta: dict[str, object]) -> list[str]:
    entities: list[str] = []
    for author in meta.get("authors", [])[:3]:
        if author:
            entities.append(author)
    for category in meta.get("categories", []):
        if category:
            entities.append(category)
    return entities


def update_markdown_list(path: Path, header: str, items: list[str]) -> None:
    path.write_text(header + "\n\n" + "".join(f"- {item}\n" for item in items), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    args = parser.parse_args()

    paths = build_paths(args.project)
    compiled = load_json(paths.state / "compiled_ids.json")
    category_to_papers: dict[str, list[str]] = defaultdict(list)
    entity_to_papers: dict[str, list[str]] = defaultdict(list)
    concept_freq: Counter[str] = Counter()
    overview_lines = [
        "# Overview\n\n",
        "Status: bootstrap-generated\n\n",
        "## Current landscape\n",
        "- This overview is a provisional field map built from currently ingested papers.\n",
        "- It should be replaced or refined by deeper backend-assisted synthesis after deeper reading.\n\n",
    ]
    field_map_lines = ["# Field Map\n\n", "## Problem -> Main Approaches -> Representative Papers\n"]
    assumptions_lines = ["# Shared Assumptions\n\n", "## Candidate assumptions to test\n"]

    for paper_dir in sorted([p for p in paths.raw_papers.iterdir() if p.is_dir()]) if paths.raw_papers.exists() else []:
        meta_path = paper_dir / "metadata.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        paper_id = meta["paper_id"]
        title = meta.get("title", "")
        categories = meta.get("categories", [])
        entities = guess_entities(meta)
        summary = meta.get("summary", "")
        tldr = meta.get("tldr") or "No TLDR available."
        concept_links = [f"[[{category.replace('.', '_')}]]" for category in categories]
        entity_links = [f"[[{entity.replace(' ', '_')}]]" for entity in entities]

        paper_text = (
            f"---\n"
            f"paper_id: {paper_id}\n"
            f"title: {title}\n"
            f"authors: {meta.get('authors', [])}\n"
            f"year: {str(meta.get('published', ''))[:4]}\n"
            f"venue: unknown\n"
            f"status: skimmed\n"
            f"confidence: low\n"
            f"tags: {categories}\n"
            f"---\n\n"
            "## One-sentence Contribution\n"
            f"{title} addresses part of the project landscape, but still needs deeper synthesis.\n\n"
            "## Problem Setting\n"
            f"Derived from metadata and abstract: {summary}\n\n"
            "## Method Core\n"
            "Bootstrap mode cannot guarantee faithful method reconstruction; replace this with deeper backend-assisted synthesis after reading.\n\n"
            "## Experimental Conclusions\n"
            "No trustworthy structured extraction yet; verify against source.\n\n"
            "## Limitations and Assumptions\n"
            "- Implicit assumptions are not yet deeply audited.\n"
            "- Bootstrap notes may miss crucial evaluation caveats.\n\n"
            "## Relation to Existing Work\n"
            f"- built_on: {' '.join(concept_links) if concept_links else 'unknown'}\n"
            f"- related_entities: {' '.join(entity_links) if entity_links else 'unknown'}\n"
            "- contradiction: none explicitly marked yet\n\n"
            "## Gap Signals\n"
            "- What minimal experiment would falsify the central claim is still unclear.\n"
            "- Possible weaknesses should be compared against nearby concept pages.\n\n"
            "## TLDR\n"
            f"{tldr}\n"
        )
        (paths.wiki_papers / f"{paper_id}.md").write_text(paper_text, encoding="utf-8")

        for category in categories:
            key = category.replace('.', '_')
            category_to_papers[key].append(paper_id)
            concept_freq[key] += 1
        for entity in entities:
            key = entity.replace(' ', '_')
            entity_to_papers[key].append(paper_id)
        if paper_id not in compiled:
            compiled.append(paper_id)

    for concept, paper_ids in sorted(category_to_papers.items()):
        (paths.wiki_concepts / f"{concept}.md").write_text(
            f"# {concept}\n\n"
            "## Cross-paper Summary\n"
            "This concept page should accumulate understanding across papers rather than repeat any one abstract.\n\n"
            "## Related Papers\n"
            + "".join(f"- [[{paper_id}]]\n" for paper_id in paper_ids)
            + "\n## Open Tensions\n- Are gains coming from this concept itself, or from surrounding engineering choices?\n",
            encoding="utf-8",
        )
        field_map_lines.append(f"- Problem cluster `{concept}` -> current approach family -> {', '.join(f'[[{p}]]' for p in paper_ids)}\n")
        assumptions_lines.append(f"- `[[{concept}]]` may depend on hidden assumptions that are currently under-specified.\n")

    for entity, paper_ids in sorted(entity_to_papers.items()):
        (paths.wiki_entities / f"{entity}.md").write_text(
            f"# {entity}\n\n"
            "## Entity Type\n"
            "Author / dataset / benchmark / system (needs manual refinement).\n\n"
            "## Related Papers\n"
            + "".join(f"- [[{paper_id}]]\n" for paper_id in paper_ids),
            encoding="utf-8",
        )

    high_freq = [concept for concept, freq in concept_freq.items() if freq >= 3]
    update_markdown_list(paths.wiki_gaps / "questions.md", "# Open Questions", [
        "Which common assumption across the current papers is least justified empirically?",
        "Which nearest-neighbor methods need a direct comparison page?",
        "Which idea has a clean minimal experiment but no published ablation yet?",
    ])
    update_markdown_list(paths.wiki_gaps / "confirmed-gaps.md", "# Confirmed Gaps", [
        "Cross-paper contradictions are still weakly structured and need explicit comparison pages.",
        "Many paper pages still lack strong method-core and experiment-core extraction.",
    ])
    update_markdown_list(paths.wiki_gaps / "hypotheses.md", "# Hypotheses", [
        "status: draft | Hypothesis: one fragile shared assumption may explain much of the current benchmark progress.",
        "status: draft | Hypothesis: the best improvement path may come from tightening evaluation rather than inventing a brand-new module.",
    ])
    research_gaps_text = (
        "# Research Gaps\n\n"
        "## Novelty Delta\n"
        "- Require a concrete delta over nearest baselines; novelty cannot be claimed from topic mixing alone.\n"
        "- Record what mechanism, data slice, or evaluation contract would make the idea genuinely different.\n\n"
        "## Counterexample And Falsification\n"
        "- Every direction needs a counterexample before scaling: sparse users, long-tail targets, temporal drift, semantic mismatch, or leakage.\n"
        "- If a direction fails its own counterexample slice, narrow the claim or prune the route.\n\n"
        "## Bad-Case Slicing\n"
        "- Experiments must export bad-case slices, not only aggregate metrics.\n"
        "- Missing bad-case evidence blocks paper promotion.\n\n"
        "## Prune Or Pause Rules\n"
        "- Prune after bounded evidence-valid attempts when gains, novelty, or slice behavior remain weak.\n"
        "- Data-blocked repos must either acquire verified real data or be replaced by an evidence-ready route.\n"
    )
    (paths.wiki_gaps / "research_gaps.md").write_text(research_gaps_text, encoding="utf-8")

    overview_lines.append("## High-frequency concept clusters\n")
    for concept in high_freq:
        overview_lines.append(f"- [[{concept}]] appears in 3+ ingested papers and likely deserves deeper synthesis.\n")
    paths.wiki_overview.write_text("".join(overview_lines), encoding="utf-8")
    (paths.wiki_synthesis / "field-map.md").write_text("".join(field_map_lines), encoding="utf-8")
    (paths.wiki_synthesis / "shared-assumptions.md").write_text("".join(assumptions_lines), encoding="utf-8")

    save_json(paths.state / "compiled_ids.json", sorted(set(compiled)))
    print(paths.wiki_papers)


if __name__ == "__main__":
    main()
