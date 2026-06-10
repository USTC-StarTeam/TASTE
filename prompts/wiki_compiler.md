You are running inside `<workspace_root>/projects/<project>`.

Task:
1. Read the paper records in `raw/papers/`, the context file `reports/shared_research.md`, the planning notes in `planning/`, and any repo or dataset reports in `reports/`.
2. Update the knowledge base in `wiki/`.
3. Create or refresh:
   - `wiki/index.md`
   - `wiki/papers/<paper-id>.md`
   - `wiki/concepts/*.md`
   - `gaps/research_gaps.md`
   - `reports/loop_summary.md`
4. Preserve append-only semantics for `raw/`; do not edit `raw/` files.
5. Explicitly track:
   - candidate baselines worth reproducing
   - promising codebases to adapt rather than rebuild
   - dataset readiness and missing prerequisites
   - contradictions, weak baselines, and likely failure cases
   - novelty delta versus nearby work
   - central claim strength and what would falsify it
   - bad-case slices or stress settings that deserve dedicated experiments
   - prune conditions for weak directions
6. When evidence is weak, say so clearly and point back to source papers or repo/dataset reports.
7. Prefer concise, structured Markdown with backlinks and actionable next steps.

Output requirements:
- Use Markdown files only.
- Keep content directly reusable by future automated loops.
- Create backlinks using `[[...]]` where helpful.
- Make the synthesis skeptical enough that a later executor can choose what to deepen and what to prune.
