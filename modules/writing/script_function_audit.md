# Writing Script/Function Audit

Generated at: 2026-07-11T00:00:00Z

## Summary

- 1_replaced_by_main_deleted: 16
- 1_retained_entry_runtime: 1
- 2_skillized_deleted: 6
- 3_prompt_only_deleted: 15

## Retained Code-Flow Files

### `modules/writing/main.py`
- status: 1_retained_entry_runtime
- reason: 唯一公共入口；负责 conda taste 检查、模块自有项目到唯一 Writing 会话映射、队列和优先打断、项目目录 cwd、网页回复、canonical 项目论文产物、fresh 独立审计、blocked 返修与再审计。
- functions: _now_iso, _load_json, _write_json, _write_text, _locked_file, _require_taste, _valid_session_id, _safe_project, _controller_dir, _state_lock, _controller_state, _sync_session_index, _publish_project_state, _save_state, _project_config, _project_venue, _project_title, _paper_root, _system_prompt, _work_prompt, _chat_prompt, _repair_prompt, _audit_prompt, _json_output, _pid_alive, _recover_orphaned_active, _interrupt_active, _invoke_claude, _run_independent_audit, _write_pipeline_state, _run_workflow, _next_message, _requeue_interrupted, _append_history, _save_result, _drain_queue, run_controller_message, _asset_status, _contract_payload, _build_parser, main

## Retained Scripts

None. `modules/writing/scripts/` 当前不保留运行脚本；裁决/审计由 `main.py` 启动独立 Claude Code，并使用 `skills/writing-audit/SKILL.md`。

## Deleted Scripts

### `modules/writing/scripts/audit/audit_standalone_paper.py`
- disposition: 2_skillized_deleted
- migration: 裁决/审计职责改为 `skills/writing-audit/SKILL.md` + `main.py` 新开的独立 Claude Code 审计实例；旧确定性审计脚本删除。
- functions retired: load_json, read_text, write_json, write_text, pdf_pages, citation_keys, bib_keys, term_hits, regex_hits, collect_input_text, evidence_is_limited, venue_reference_floor, venue_kind, audit, main

### `modules/writing/scripts/core/entrypoint_guard.py`
- disposition: 1_replaced_by_main_deleted
- migration: `modules/writing/scripts/` 不再保留运行脚本，私有脚本入口守卫没有剩余用途；唯一入口仍是 `main.py`。
- functions retired: require_main_entrypoint

### `modules/writing/scripts/audit/__init__.py`
- disposition: 1_replaced_by_main_deleted
- migration: 包占位文件只服务旧脚本包结构；删除后不影响 main.py 唯一入口。
- functions retired: (none)

### `modules/writing/scripts/audit/audit_paper_evidence.py`
- disposition: 3_prompt_only_deleted
- migration: 项目态 evidence/readiness/figure/normality/claim 审计改为 writing-audit skill + 独立 Claude Code 审计实例；旧项目耦合审计删除。
- functions retired: _norm_path, _payload_run_id, _current_find_run_id, _current_selection_context, _rows_for_current_repo, _experiment_index, _current_claim_guard_issues, load_json, main

### `modules/writing/scripts/audit/audit_paper_figures.py`
- disposition: 3_prompt_only_deleted
- migration: 项目态 evidence/readiness/figure/normality/claim 审计改为 writing-audit skill + 独立 Claude Code 审计实例；旧项目耦合审计删除。
- functions retired: has_evidence_limit_term, evidence_limit_terms_in, braced_content, first_caption, png_dimensions, resolve_figure_path, option_width, width_fraction, strip_latex_commands, _read_latex_braced_argument, _skip_latex_space, first_tabular, likely_script_path, script_quality_issues, table_rows, figure_rows, main

### `modules/writing/scripts/audit/audit_paper_normality.py`
- disposition: 3_prompt_only_deleted
- migration: 项目态 evidence/readiness/figure/normality/claim 审计改为 writing-audit skill + 独立 Claude Code 审计实例；旧项目耦合审计删除。
- functions retired: pdf_pages, tex_text, markdown_headings, latex_sections, normalize_heading, has_canonical_section, count_citations, bibliography_entry_count, citation_keys, bib_keys_from_paths, venue_policy_source_detail, reference_start_page, estimate_page_breakdown, page_cap_label, page_range_label, venue_page_rule_label, author_identity_hits, textual_citation_commands, latex_citation_warning_findings, bibtex_error_findings, latex_compile_error_findings, pdf_unresolved_citation_markers, read_pdf_text, _existing_log_paths, citation_render_diagnostics, source_metrics, find_sources, _venue_zh_label, main

### `modules/writing/scripts/audit/audit_paper_orchestra.py`
- disposition: 3_prompt_only_deleted
- migration: 项目态 evidence/readiness/figure/normality/claim 审计改为 writing-audit skill + 独立 Claude Code 审计实例；旧项目耦合审计删除。
- functions retired: latest_markdown, section_hits, count_citation_like, main

### `modules/writing/scripts/audit/audit_submission_readiness.py`
- disposition: 3_prompt_only_deleted
- migration: 项目态 evidence/readiness/figure/normality/claim 审计改为 writing-audit skill + 独立 Claude Code 审计实例；旧项目耦合审计删除。
- functions retired: now_iso, pass_check, module_status, real_ready_datasets, _norm_path, _payload_run_id, _current_find_run_id, _current_selection_context, _rows_for_current_repo, _experiment_index, _current_claim_guard, main

### `modules/writing/scripts/audit/build_claim_ledger.py`
- disposition: 3_prompt_only_deleted
- migration: 项目态 evidence/readiness/figure/normality/claim 审计改为 writing-audit skill + 独立 Claude Code 审计实例；旧项目耦合审计删除。
- functions retired: load_json, norm_path, current_route_row, extract_claims, score_claim, run_is_reproduction_only, run_has_real_claim_support, claim_matches_run, claim_weak_evidence_run, main

### `modules/writing/scripts/core/__init__.py`
- disposition: 1_replaced_by_main_deleted
- migration: 包占位文件只服务旧脚本包结构；删除后不影响 main.py 唯一入口。
- functions retired: (none)

### `modules/writing/scripts/core/paper_common.py`
- disposition: 1_replaced_by_main_deleted
- migration: 旧共享库主要服务已删除的项目态脚本；必要 path/json/copy/subprocess 逻辑已在 main.py 内小型化。
- functions retired: _repo_root_from_script, slugify, load_json, write_json, read_text, write_text, ensure_paper_dirs, draft_title_from_config, strip_markdown_header, is_summary_line, extract_summary_lines, compact_bullets, count_placeholder_lines, list_placeholder_lines, summarize_experiments, ascii_latex_text, escape_latex, markdown_to_latex, venue_info, active_project_from_env, load_venue_requirements, dynamic_venue_submission_policy, dynamic_venue_template_profile, allow_static_venue_policy_fallback, unresolved_venue_policy, venue_slug_aliases, venue_template_profile, venue_reference_target, venue_submission_policy, latex_documentclass, _strip_latex_comments, _is_springer_nature_family, normalized_latex_section_headings, heading_present, _latex_back_matter_headings, springer_nature_article_shape_failures, _extract_latex_macro, latex_plain_text, _latex_macro_spans, _springer_nature_affiliation_macros, _springer_nature_author_macros, _is_anonymous_author_body, _is_placeholder_affiliation_body, _normalize_springer_nature_anonymous_author_block, _remove_springer_nature_placeholder_affiliations, springer_nature_placeholder_front_matter_failures, normalize_venue_front_matter, springer_nature_front_matter_failures, pdf_first_page_text, springer_nature_pdf_front_matter_failures, validate_venue_template_format, venue_fallback_template, fetch_url, search_duckduckgo, filter_candidate_urls, find_download_links, download_binary, unpack_archive, find_main_tex, texlive_root_candidates, texlive_tool_candidates, workspace_tool_path, compiler_inventory, choose_compiler, install_latex_hint, _cletop_level_venue_scoped_state, update_pipeline_state, _configured_project_venue, get_active_paper_state, make_failure_report, try_install_latex_toolchain

### `modules/writing/scripts/core/paper_self_review.py`
- disposition: 1_replaced_by_main_deleted
- migration: 旧共享库主要服务已删除的项目态脚本；必要 path/json/copy/subprocess 逻辑已在 main.py 内小型化。
- functions retired: read_json, sha256_file, paper_self_review_receipt_path, normalize_token, _artifact_rows, _artifact_path, _artifact_sha, _artifact_has_text, _resolve_project_path, _path_exists, _hash_matches, _current_hash_matches, _list_payload, _compact_text, _row_field_text, _row_has_any, _finding_is_structured, _finding_has_issue, _finding_has_artifact_evidence, _finding_independence_evidence, _finding_only_restates_ar, _finding_is_generic_review_note, _finding_category_text, _normalized_marker_text, _finding_matches_evidence_blocker, _finding_resolution_status, _finding_has_verified_resolution, _evidence_blocker_id, self_review_evidence_blockers, _remaining_blocker_issue_text, _remaining_blocker_is_submission_evidence, _remaining_blocker_is_nonblocking_note, remaining_blocker_evidence_blockers, remaining_preview_blockers, _review_protocol, _review_protocol_open_ended, _position_any, _review_protocol_independent_first, _finding_from_independent_phase, _reading_log_entries, _reading_entry_matches, _reading_entry_has_method, _reading_entry_has_evidence, _repair_has_file, _repair_has_action, _repair_has_verification, validate_paper_self_review_receipt

### `modules/writing/scripts/core/writing_paths.py`
- disposition: 1_replaced_by_main_deleted
- migration: 旧共享库主要服务已删除的项目态脚本；必要 path/json/copy/subprocess 逻辑已在 main.py 内小型化。
- functions retired: module_root, local_script_dirs, external_script_dirs, add_script_paths, pythonpath, script_path, local_script_resolver, module_action_cmd, external_stage_cmd

### `modules/writing/scripts/maintenance/__init__.py`
- disposition: 1_replaced_by_main_deleted
- migration: 包占位文件只服务旧脚本包结构；删除后不影响 main.py 唯一入口。
- functions retired: (none)

### `modules/writing/scripts/maintenance/check_internal_assets.py`
- disposition: 1_replaced_by_main_deleted
- migration: 资产检查已并入 main.py --action assets；旧维护脚本删除。
- functions retired: rel, collect, main

### `modules/writing/scripts/pipeline/__init__.py`
- disposition: 1_replaced_by_main_deleted
- migration: 包占位文件只服务旧脚本包结构；删除后不影响 main.py 唯一入口。
- functions retired: (none)

### `modules/writing/scripts/pipeline/build_conference_preview_paper.py`
- disposition: 3_prompt_only_deleted
- migration: Markdown 草稿、预览状态、orchestra state 等中间产物可由 Claude prompt 从输入快照生成；旧脚本删除。
- functions retired: read_json, _normalized_latex_sections, _canonical_sections_for_venue, _heading_matches_required, _route_term_candidates, _route_string_looks_artifact_path, _legacy_route_terms, _latex_section_ranges, _section_at, _sentence_window, _is_prior_work_context, _is_route_story_context, _scientific_progress_allows_completed_experiment_claims, _project_claim_support_payloads, _walk_json_values, _explicit_repeated_run_evidence, _hardware_tokens, _explicit_hardware_evidence, _reference_calibration_available, _compact_manuscript_window, unsupported_completed_experiment_claim_violations, legacy_route_story_violations, latex_text_with_inputs, pdf_pages, _candidate_manifest, _first_existing_candidate_path, _candidate_rejection_blockers, _public_blocker_detail, _preview_gate_blockers, _preview_blocker_summary, select_manuscript_candidate, manuscript_policy_violations, run, copy_if_ready, copy_template_sidecars, _page_cap_label, _page_range_label, venue_page_rule_label, preview_labels, main

### `modules/writing/scripts/pipeline/build_paper_md.py`
- disposition: 3_prompt_only_deleted
- migration: Markdown 草稿、预览状态、orchestra state 等中间产物可由 Claude prompt 从输入快照生成；旧脚本删除。
- functions retired: clean_block, norm_path, current_route_row, claim_ready_positive_rows, render_experiment_table, render_citation_candidates, load_orchestra_state, render_section_ledger, render_global_blockers, render_claims_from_orchestra, main

### `modules/writing/scripts/pipeline/build_paper_orchestra_state.py`
- disposition: 3_prompt_only_deleted
- migration: Markdown 草稿、预览状态、orchestra state 等中间产物可由 Claude prompt 从输入快照生成；旧脚本删除。
- functions retired: now_iso, latest_markdown, section_present, path_exists, evidence_item, citation_candidates, artifact_inventory, real_ready_datasets, run_counts, claim_summary, section_status, build_state, write_report, main

### `modules/writing/scripts/pipeline/build_project_input_pack.py`
- disposition: 1_replaced_by_main_deleted
- migration: 入口、输入包、workspace 和 Claude 管理职责已并入 main.py；旧入口脚本删除以保证唯一入口。
- functions retired: now_id, slugify, read_text, load_json, write_text, write_json, copy_file, csv_preview, summarize_registry, collect_artifact_records, main

### `modules/writing/scripts/pipeline/run_paper_orchestra_bridge.py`
- disposition: 2_skillized_deleted
- migration: PaperOrchestra 编排改为 skills/paper-orchestra 供 Claude Code 按需读取；旧桥接脚本不再作为代码流程。
- functions retired: _repo_root_from_script, section_titles_for_venue, mandatory_sections_markdown, manuscript_shape_requirement, now_iso, decode_output, run, sha256_file, active_pdf_fingerprint, existing_file_path, workspace_final_artifacts, count_bib_entries, count_tex_citations, latex_text_with_inputs, latex_section_titles, read_json_loose, json_safe, write_phase_log, paper_orchestra_scripts, citation_pool_size, workspace_status, phase_ready, ensure_writing_vendor, clone_or_update, link_skills, first_existing, norm_path, current_route_row, current_claim_ledger_for_writer, compact_json, _route_term_candidates, _route_string_looks_artifact_path, _route_identity, candidate_route_boundary_for_writer, render_idea, metric_value, render_experimental_log, render_method_contract, render_guidelines, fallback_template, copy_template_sidecars, copy_workspace_template_sidecars_to_final, template_candidate_paths, choose_template_source, prepare_workspace, maybe_run_prepare_phase, _valid_template_source_exists, ensure_venue_contract, refresh_current_paper_workspace_outputs, _append_query_value, _project_literature_queries, candidate_queries_from_outline, normalize_title, title_ratio, search_s2, paper_identity, build_verified_literature_pool, preflight, claude_prompt, invoke_claude, collect_outputs, run_phase_with_claude, compile_final_pdf, run_deterministic_gates, sync_preview, run_phase_machine, write_report, main

### `modules/writing/scripts/pipeline/run_paper_pipeline.py`
- disposition: 1_replaced_by_main_deleted
- migration: 入口、输入包、workspace 和 Claude 管理职责已并入 main.py；旧入口脚本删除以保证唯一入口。
- functions retired: _repo_root_from_script, module_cmd, read_json, now_iso, gate_passed, preflight_science_gates, should_regenerate_current_preview, existing_file, record_missing_preview_repair_artifact, run, refresh_venue_requirements, fetch_venue_template, main

### `modules/writing/scripts/pipeline/run_standalone_paper.py`
- disposition: 1_replaced_by_main_deleted
- migration: 入口、输入包、workspace 和 Claude 管理职责已并入 main.py；旧入口脚本删除以保证唯一入口。
- functions retired: now_iso, slugify, write_text, write_json, copy_input, find_claude, run_claude, run_quality_audit, build_prompt, main

### `modules/writing/scripts/rendering/__init__.py`
- disposition: 1_replaced_by_main_deleted
- migration: 包占位文件只服务旧脚本包结构；删除后不影响 main.py 唯一入口。
- functions retired: (none)

### `modules/writing/scripts/rendering/compile_paper_pdf.py`
- disposition: 2_skillized_deleted
- migration: LaTeX 渲染和 PDF 编译改由 taste-paper-writing/paper-orchestra/section-writing skills 指导 Claude Code 使用项目 canonical 模板完成；旧代码流程删除。
- functions retired: decode_output, run_compile_command, bibtex_result_is_usable_for_springer_nature, compile_springer_nature_preview, command_log, main

### `modules/writing/scripts/rendering/render_paper_tex.py`
- disposition: 2_skillized_deleted
- migration: LaTeX 渲染和 PDF 编译改由 taste-paper-writing/paper-orchestra/section-writing skills 指导 Claude Code 使用项目 canonical 模板完成；旧代码流程删除。
- functions retired: latest_markdown, strip_title_macros, find_venue_template_source, main

### `modules/writing/scripts/repair/__init__.py`
- disposition: 1_replaced_by_main_deleted
- migration: 包占位文件只服务旧脚本包结构；删除后不影响 main.py 唯一入口。
- functions retired: (none)

### `modules/writing/scripts/repair/repair_paper_figures_loop.py`
- disposition: 3_prompt_only_deleted
- migration: 论文修复、评审、回应和再评审可以由 Claude Code 根据 prompt 与 writing-quality/content-refinement skills 完成；不保留独立代码流程。
- functions retired: now_iso, run, read_text, claude_repair_prompt, main

### `modules/writing/scripts/repair/repair_paper_orchestra_citations.py`
- disposition: 3_prompt_only_deleted
- migration: 论文修复、评审、回应和再评审可以由 Claude Code 根据 prompt 与 writing-quality/content-refinement skills 完成；不保留独立代码流程。
- functions retired: normalize, title_ratio, first_significant_word, arxiv_id_from_url, doi_from_url, candidate_alias_keys, parse_bib, cited_keys, citation_count, canonical_score, load_bridge_baseline_rows, load_candidate_rows, build_title_lookup, abstract_from_openalex, openalex_search, openalex_by_doi, openalex_work_to_paper, arxiv_search, verify_title, load_cache, save_cache, verify_title_cached, is_good_bib_entry, main

### `modules/writing/scripts/repair/repair_paper_preview_loop.py`
- disposition: 3_prompt_only_deleted
- migration: 论文修复、评审、回应和再评审可以由 Claude Code 根据 prompt 与 writing-quality/content-refinement skills 完成；不保留独立代码流程。
- functions retired: now_iso, sha256_file, decode_output, pdf_fingerprint, run, read_text, preview_ready, preview_repair_status, _sync_workspace_refs_to_final, _compile_input_files, _existing_pdf_is_fresh, prompt_only_pipeline_update, compile_workspace_pdf, gate_snapshot, intish, manuscript_shape_requirement, repair_focus_diagnosis, refresh_current_venue_contract, claude_repair_prompt, main

### `modules/writing/scripts/repair/revise_paper_citation_coverage.py`
- disposition: 3_prompt_only_deleted
- migration: 论文修复、评审、回应和再评审可以由 Claude Code 根据 prompt 与 writing-quality/content-refinement skills 完成；不保留独立代码流程。
- functions retired: parse_bib_titles, cited_keys, manuscript_shape_requirement, run, main

### `modules/writing/scripts/repair/revise_paper_md.py`
- disposition: 3_prompt_only_deleted
- migration: 论文修复、评审、回应和再评审可以由 Claude Code 根据 prompt 与 writing-quality/content-refinement skills 完成；不保留独立代码流程。
- functions retired: bullet_block, orchestra_section_plan, orchestra_blockers, paper_safe_items, main

### `modules/writing/scripts/review/__init__.py`
- disposition: 1_replaced_by_main_deleted
- migration: 包占位文件只服务旧脚本包结构；删除后不影响 main.py 唯一入口。
- functions retired: (none)

### `modules/writing/scripts/review/review_response_tools.py`
- disposition: 3_prompt_only_deleted
- migration: 论文修复、评审、回应和再评审可以由 Claude Code 根据 prompt 与 writing-quality/content-refinement skills 完成；不保留独立代码流程。
- functions retired: respond_to_reviews, re_review_paper, write_comparison, verdict_from_score, build_review, render_markdown, run_review_paper, verdict_from_scores, build_evidence_issues, run_aggregate_reviews, main

### `modules/writing/scripts/venue/__init__.py`
- disposition: 1_replaced_by_main_deleted
- migration: 包占位文件只服务旧脚本包结构；删除后不影响 main.py 唯一入口。
- functions retired: (none)

### `modules/writing/scripts/venue/fetch_latex_template.py`
- disposition: 2_skillized_deleted
- migration: 官方 venue 规则和模板处理改由 venue-intelligence skill 指导 Claude Code 在 run 内完成；旧抓取/同步脚本不再作为代码流程。
- functions retired: _repo_root_from_script, sha256_file, use_template_cache, run_git_checked, text_files, copy_source_dir, load_requirements, normalize_required_file_name, validate_template_source, existing_verified_source, select_springer_nature_template_option, sync_from_repository, sync_from_archive, prefer_official_archive, sync_from_repository_with_archive_fallback, discover_archive, fail, main

### `modules/writing/scripts/venue/resolve_venue_requirements.py`
- disposition: 2_skillized_deleted
- migration: 官方 venue 规则和模板处理改由 venue-intelligence skill 指导 Claude Code 在 run 内完成；旧抓取/同步脚本不再作为代码流程。
- functions retired: _repo_root_from_script, now_iso, load_json, is_fresh, sha256_file, run_git_checked, verified_local_template_source, use_template_cache, remote_repository_head, unverified_source, latest_iclr_template, fetch_official_text, html_to_compact_text, is_iclr_venue, discover_iclr_template_year, conference_reference_quality_target, build_iclr_requirements, is_nature_family_venue, fetch_head_status, build_nature_family_requirements, parse_body_page_limit, upsert_official_source, verify_iclr_author_guide, latest_yedirectory_template, infer_template_family, normalize_required_file_name, normalize_template_machine_fields, apply_yedirectory_template, clean_official_sources, reference_quality_floor, payload_has_self_contained_official_venue_contract, augment_with_repository_verification, normalize_requirement_payload, stale_repository_verification_blocker, heal_verified_venue_payload, validate_payload, build_prompt, run_claude, main
