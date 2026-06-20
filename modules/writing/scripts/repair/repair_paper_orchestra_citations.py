#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path as _WritingDevPath
import sys as _writing_sys
_WRITING_SCRIPT_ROOT = next((p for p in _WritingDevPath(__file__).resolve().parents if p.name == "scripts"), _WritingDevPath(__file__).resolve().parent)
for _writing_path in [_WRITING_SCRIPT_ROOT, *[p for p in _WRITING_SCRIPT_ROOT.iterdir() if p.is_dir()]]:
    _writing_text = str(_writing_path)
    if _writing_text not in _writing_sys.path:
        _writing_sys.path.insert(0, _writing_text)


import argparse
import ast
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from paper_common import get_active_paper_state, read_text, slugify, update_pipeline_state, venue_reference_target, write_json, write_text
from project_paths import ROOT, build_paths
from pipeline_guard import guard_fresh_base_blocker_entry


CURRENT_YEAR = 2026
REQUEST_TIMEOUT = 10
REQUEST_RETRIES = 1
CITE_RE = re.compile(r"(\\cite\w*\*?(?:\s*\[[^\]]*\])*)\s*\{([^{}]+)\}")
ARXIV_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})(?:v\d+)?", re.IGNORECASE)
DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s?#]+)", re.IGNORECASE)
STOPWORDS = {
    "a", "an", "and", "the", "of", "for", "to", "with", "on", "in", "by",
    "from", "as", "is", "are", "be", "via", "into", "their", "our", "we",
    "this", "that", "using", "use", "about", "at", "or", "if",
}


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(text).lower())).strip()


def title_ratio(a: str, b: str) -> int:
    return int(round(100 * SequenceMatcher(None, normalize(a), normalize(b)).ratio()))


def first_significant_word(title: str) -> str:
    for word in re.findall(r"[A-Za-z][A-Za-z\-]*", title):
        lowered = word.lower()
        if lowered not in STOPWORDS and len(lowered) > 2:
            return re.sub(r"[^a-z]", "", lowered)
    return "paper"


def arxiv_id_from_url(url: str) -> str:
    match = ARXIV_RE.search(url or "")
    return match.group(1) if match else ""


def doi_from_url(url: str) -> str:
    match = DOI_RE.search(url or "")
    return urllib.parse.unquote(match.group(1).rstrip(").,")) if match else ""


def candidate_alias_keys(row: dict[str, Any]) -> set[str]:
    title = str(row.get("title") or "").strip()
    if not title:
        return set()
    word = first_significant_word(title)
    aliases = {f"anon0000{word}"}
    for raw_year in [row.get("year")]:
        try:
            year = int(str(raw_year))
        except (TypeError, ValueError):
            continue
        aliases.add(f"anon{year}{word}")
    arxiv_id = arxiv_id_from_url(str(row.get("url") or ""))
    if arxiv_id:
        aliases.add(f"anon{arxiv_id[:4]}{word}")
    key = str(row.get("bibtex_key") or row.get("citation_key") or "").strip()
    if key:
        aliases.add(key)
    return aliases


def parse_bib(path: Path) -> dict[str, dict[str, str]]:
    text = read_text(path) if path.exists() else ""
    out: dict[str, dict[str, str]] = {}
    for match in re.finditer(r"@(\w+)\s*\{\s*([^,\s]+)\s*,(.*?)(?=\n@\w+\s*\{|\Z)", text, flags=re.DOTALL):
        key = match.group(2).strip()
        body = match.group(3)
        fields: dict[str, str] = {"entry_type": match.group(1)}
        for field in ["title", "author", "year", "journal", "booktitle", "doi", "eprint"]:
            fm = re.search(rf"\b{field}\s*=\s*\{{(.*?)\}}\s*,?", body, flags=re.DOTALL | re.IGNORECASE)
            if fm:
                fields[field.lower()] = re.sub(r"\s+", " ", fm.group(1)).strip()
        out[key] = fields
    return out


def cited_keys(tex_paths: list[Path]) -> set[str]:
    keys: set[str] = set()
    for path in tex_paths:
        text = read_text(path) if path.exists() else ""
        for match in CITE_RE.finditer(text):
            keys.update(key.strip() for key in match.group(2).split(",") if key.strip())
    return keys


def citation_count(path: Path) -> int:
    return len(cited_keys([path])) if path.exists() else 0


def canonical_score(path: Path) -> tuple[int, float]:
    return (citation_count(path), path.stat().st_mtime if path.exists() else 0.0)


def load_bridge_baseline_rows() -> list[dict[str, Any]]:
    bridge = SCRIPTS_ROOT / "run_paper_orchestra_bridge.py"
    if not bridge.exists():
        return []
    try:
        tree = ast.parse(bridge.read_text(encoding="utf-8"))
    except Exception:
        return []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "BASELINE_QUERIES" for target in node.targets):
            continue
        try:
            value = ast.literal_eval(node.value)
        except Exception:
            return []
        if isinstance(value, list):
            return [{"title": str(item), "source": "run_paper_orchestra_bridge.BASELINE_QUERIES"} for item in value if str(item).strip()]
    return []


def load_candidate_rows(workspace: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in ["citation_pool.json", "raw_pool.json", "deduped_candidates.json", "raw_candidates.json"]:
        path = workspace / name
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        raw_rows = payload.get("papers") or payload.get("candidates") or []
        if not isinstance(raw_rows, list):
            continue
        for row in raw_rows:
            if isinstance(row, dict) and row.get("title"):
                rows.append(row)
    rows.extend(load_bridge_baseline_rows())
    return rows


def build_title_lookup(workspace: Path, pool_by_key: dict[str, dict[str, Any]], old_refs: dict[str, dict[str, str]]) -> tuple[dict[str, str], dict[str, str], list[dict[str, Any]]]:
    key_to_title: dict[str, str] = {}
    title_to_url: dict[str, str] = {}
    candidates = load_candidate_rows(workspace)
    for key, row in pool_by_key.items():
        title = str(row.get("title") or "").strip()
        if title:
            key_to_title[key] = title
    for key, row in old_refs.items():
        title = str(row.get("title") or "").strip()
        if title:
            key_to_title.setdefault(key, title)
    for row in candidates:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        norm = normalize(title)
        if row.get("url"):
            title_to_url.setdefault(norm, str(row.get("url") or ""))
        for alias in candidate_alias_keys(row):
            key_to_title.setdefault(alias, title)
    return key_to_title, title_to_url, candidates


def abstract_from_openalex(work: dict[str, Any]) -> str:
    inv = work.get("abstract_inverted_index")
    if not isinstance(inv, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, slots in inv.items():
        if isinstance(slots, list):
            for slot in slots:
                if isinstance(slot, int):
                    positions.append((slot, str(word)))
    return " ".join(word for _idx, word in sorted(positions))


def openalex_search(title: str, *, per_page: int = 5) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    params = urllib.parse.urlencode({
        "search": title,
        "per-page": per_page,
        "select": "id,doi,display_name,publication_year,authorships,primary_location,abstract_inverted_index,type,cited_by_count",
    })
    url = f"https://api.openalex.org/works?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "TASTE-Writing-CitationRepair/1.0 (mailto:anonymous@example.com)"})
    meta: dict[str, Any] = {"query": title, "url": url, "started_at": time.time()}
    payload: dict[str, Any] = {}
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as exc:
            meta["error"] = str(exc)
            meta["attempts"] = attempt
            if attempt < 3:
                time.sleep(1.5 * attempt)
    if not payload:
        return None, meta
    candidates = payload.get("results", []) if isinstance(payload, dict) else []
    best = None
    best_score = 0
    for row in candidates:
        if not isinstance(row, dict):
            continue
        score = title_ratio(title, str(row.get("display_name") or ""))
        if score > best_score:
            best_score = score
            best = row
    meta["best_score"] = best_score
    meta["best_title"] = best.get("display_name") if isinstance(best, dict) else ""
    if not isinstance(best, dict) or best_score < 72:
        return None, meta
    year = best.get("publication_year")
    if not isinstance(year, int) or year < 1950 or year > CURRENT_YEAR:
        meta["error"] = f"invalid publication_year={year}"
        return None, meta
    authorships = best.get("authorships") if isinstance(best.get("authorships"), list) else []
    authors = []
    for item in authorships:
        if not isinstance(item, dict):
            continue
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        name = str(author.get("display_name") or "").strip()
        if name:
            authors.append({"name": name, "authorId": author.get("id", "")})
    if not authors:
        meta["error"] = "no authors"
        return None, meta
    source = ""
    primary = best.get("primary_location") if isinstance(best.get("primary_location"), dict) else {}
    source_obj = primary.get("source") if isinstance(primary.get("source"), dict) else {}
    if source_obj:
        source = str(source_obj.get("display_name") or "")
    paper = {
        "title": str(best.get("display_name") or title).strip(),
        "year": year,
        "abstract": abstract_from_openalex(best),
        "authors": authors,
        "venue": source,
        "externalIds": {
            "DOI": str(best.get("doi") or "").replace("https://doi.org/", ""),
            "OpenAlex": str(best.get("id") or ""),
        },
        "paperId": str(best.get("id") or ""),
        "citationCount": int(best.get("cited_by_count") or 0),
        "verified": True,
        "verification_source": "OpenAlex",
        "title_match_ratio": best_score,
    }
    return paper, meta


def openalex_by_doi(doi: str, title: str = "") -> tuple[dict[str, Any] | None, dict[str, Any]]:
    doi = doi.strip().lower()
    meta: dict[str, Any] = {"query": title or doi, "doi": doi, "started_at": time.time(), "source": "OpenAlex DOI"}
    if not doi:
        meta["error"] = "empty doi"
        return None, meta
    url = "https://api.openalex.org/works/doi:" + urllib.parse.quote(doi, safe="")
    req = urllib.request.Request(url, headers={"User-Agent": "TASTE-Writing-CitationRepair/1.0 (mailto:anonymous@example.com)"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            work = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        meta["error"] = str(exc)
        return None, meta
    if not isinstance(work, dict):
        meta["error"] = "non-object OpenAlex response"
        return None, meta
    candidate, search_meta = openalex_work_to_paper(work, title or str(work.get("display_name") or ""))
    meta.update(search_meta)
    return candidate, meta


def openalex_work_to_paper(work: dict[str, Any], title: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    meta: dict[str, Any] = {"best_title": work.get("display_name"), "source": "OpenAlex"}
    score = title_ratio(title, str(work.get("display_name") or "")) if title else 100
    meta["best_score"] = score
    if score < 72:
        return None, meta
    year = work.get("publication_year")
    if not isinstance(year, int) or year < 1950 or year > CURRENT_YEAR:
        meta["error"] = f"invalid publication_year={year}"
        return None, meta
    authorships = work.get("authorships") if isinstance(work.get("authorships"), list) else []
    authors = []
    for item in authorships:
        if not isinstance(item, dict):
            continue
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        name = str(author.get("display_name") or "").strip()
        if name:
            authors.append({"name": name, "authorId": author.get("id", "")})
    if not authors:
        meta["error"] = "no authors"
        return None, meta
    primary = work.get("primary_location") if isinstance(work.get("primary_location"), dict) else {}
    source_obj = primary.get("source") if isinstance(primary.get("source"), dict) else {}
    source = str(source_obj.get("display_name") or "") if source_obj else ""
    return {
        "title": str(work.get("display_name") or title).strip(),
        "year": year,
        "abstract": abstract_from_openalex(work),
        "authors": authors,
        "venue": source,
        "externalIds": {
            "DOI": str(work.get("doi") or "").replace("https://doi.org/", ""),
            "OpenAlex": str(work.get("id") or ""),
        },
        "paperId": str(work.get("id") or ""),
        "citationCount": int(work.get("cited_by_count") or 0),
        "verified": True,
        "verification_source": "OpenAlex",
        "title_match_ratio": score,
    }, meta


def arxiv_search(arxiv_id: str, title: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    meta: dict[str, Any] = {"query": title, "arxiv_id": arxiv_id, "started_at": time.time(), "source": "arXiv"}
    if not arxiv_id:
        meta["error"] = "empty arxiv_id"
        return None, meta
    url = "https://export.arxiv.org/api/query?id_list=" + urllib.parse.quote(arxiv_id)
    meta["url"] = url
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as resp:
            xml_text = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        meta["error"] = str(exc)
        return None, meta
    root = ET.fromstring(xml_text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entry = root.find("atom:entry", ns)
    if entry is None:
        meta["error"] = "no arxiv entry"
        return None, meta
    found_title = re.sub(r"\s+", " ", (entry.findtext("atom:title", default="", namespaces=ns) or "")).strip()
    score = title_ratio(title, found_title)
    meta["best_title"] = found_title
    meta["best_score"] = score
    if score < 70:
        return None, meta
    published = entry.findtext("atom:published", default="", namespaces=ns) or ""
    try:
        year = int(published[:4])
    except ValueError:
        year = 0
    if year < 1950 or year > CURRENT_YEAR:
        meta["error"] = f"invalid arxiv year={year}"
        return None, meta
    authors = []
    for author in entry.findall("atom:author", ns):
        name = re.sub(r"\s+", " ", author.findtext("atom:name", default="", namespaces=ns) or "").strip()
        if name:
            authors.append({"name": name})
    if not authors:
        meta["error"] = "no authors"
        return None, meta
    summary = re.sub(r"\s+", " ", entry.findtext("atom:summary", default="", namespaces=ns) or "").strip()
    return {
        "title": found_title,
        "year": year,
        "abstract": summary,
        "authors": authors,
        "venue": "arXiv",
        "externalIds": {"ArXiv": arxiv_id},
        "paperId": "arxiv:" + arxiv_id,
        "citationCount": 0,
        "verified": True,
        "verification_source": "arXiv",
        "title_match_ratio": score,
    }, meta


def verify_title(title: str, title_to_url: dict[str, str]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    url = title_to_url.get(normalize(title), "")
    doi = doi_from_url(url)
    if doi:
        paper, meta = openalex_by_doi(doi, title)
        if paper:
            return paper, meta
    arxiv_id = arxiv_id_from_url(url)
    if arxiv_id:
        paper, meta = arxiv_search(arxiv_id, title)
        if paper:
            return paper, meta
    return openalex_search(title)


def load_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def save_cache(path: Path, cache: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def verify_title_cached(
    title: str,
    title_to_url: dict[str, str],
    cache_path: Path,
    cache: dict[str, Any],
    *,
    retry_failed_cache: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    norm = normalize(title)
    cached = cache.get(norm)
    if isinstance(cached, dict):
        paper = cached.get("paper")
        meta = cached.get("meta")
        if isinstance(paper, dict):
            return paper, {"query": title, "source": "cache", **(meta if isinstance(meta, dict) else {})}
        if isinstance(meta, dict) and not retry_failed_cache:
            return None, {"query": title, "source": "cache", **meta}
    paper, meta = verify_title(title, title_to_url)
    cache[norm] = {"paper": paper, "meta": meta, "cached_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    save_cache(cache_path, cache)
    print(f"[citation-repair] verified={bool(paper)} title={title[:100]}", flush=True)
    return paper, meta


def is_good_bib_entry(fields: dict[str, str]) -> bool:
    key = str(fields.get("key") or "")
    author = str(fields.get("author") or "").strip()
    title = str(fields.get("title") or "").strip()
    try:
        year = int(str(fields.get("year") or "0"))
    except ValueError:
        year = 0
    return bool(title and author and not key.startswith("anon") and 1950 <= year <= CURRENT_YEAR)


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair writing citation metadata with verified OpenAlex records and rewrite cited keys.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", required=True)
    parser.add_argument("--min-good-refs", type=int, default=0, help="Optional explicit reference target; default reads current venue_requirements.json.")
    parser.add_argument("--max-queries", type=int, default=120)
    parser.add_argument("--request-timeout", type=int, default=10)
    parser.add_argument("--request-retries", type=int, default=1)
    parser.add_argument("--retry-failed-cache", action="store_true")
    args = parser.parse_args()
    global REQUEST_TIMEOUT, REQUEST_RETRIES
    REQUEST_TIMEOUT = max(3, args.request_timeout)
    REQUEST_RETRIES = max(1, args.request_retries)

    guard_rc = guard_fresh_base_blocker_entry(args.project, args.venue, Path(__file__).name, safe_unblock=False)
    if guard_rc is not None:
        return int(guard_rc)

    reference_target_info = venue_reference_target(args.venue, project=args.project, explicit_min=args.min_good_refs)
    args.min_good_refs = int(reference_target_info.get("target") or 0)
    paths = build_paths(args.project)
    state = get_active_paper_state(args.project, venue=args.venue)
    venue_slug = slugify(args.venue)
    workspace = Path(str(state.get("paper_orchestra_workspace") or paths.root / "paper" / "orchestra" / venue_slug / "workspace"))
    refs_path = workspace / "refs.bib"
    pool_path = workspace / "citation_pool.json"
    cache_path = workspace / "citation_verification_cache.json"
    verification_cache = load_cache(cache_path)
    output_dir = paths.root / "paper" / "output" / venue_slug
    tex_paths = [
        output_dir / "paper_orchestra_raw.tex",
        output_dir / "paper.tex",
        workspace / "final" / "paper.tex",
        workspace / "drafts" / "paper.tex",
    ]
    tex_paths = [path for idx, path in enumerate(tex_paths) if path.exists() and path not in tex_paths[:idx]]
    canonical_tex = max(tex_paths, key=canonical_score) if tex_paths else workspace / "final" / "paper.tex"
    old_refs = parse_bib(refs_path)
    old_pool = json.loads(pool_path.read_text(encoding="utf-8")) if pool_path.exists() else {"papers": []}
    pool_by_key = {str(p.get("bibtex_key") or ""): p for p in old_pool.get("papers", []) if isinstance(p, dict)}
    key_to_title, title_to_url, candidates = build_title_lookup(workspace, pool_by_key, old_refs)
    cited = cited_keys(tex_paths)
    queries: list[tuple[str, str]] = []
    for key in sorted(cited):
        title = key_to_title.get(key, "").strip()
        if title:
            queries.append((key, title))
    seen_titles: set[str] = set()
    repaired: dict[str, dict[str, Any]] = {}
    query_log: list[dict[str, Any]] = []
    old_to_title: dict[str, str] = {}
    for old_key, title in queries:
        norm = normalize(title)
        old_to_title[old_key] = norm
        if norm in seen_titles:
            continue
        seen_titles.add(norm)
        if len(query_log) >= args.max_queries:
            break
        paper, meta = verify_title_cached(title, title_to_url, cache_path, verification_cache, retry_failed_cache=args.retry_failed_cache)
        query_log.append(meta)
        if paper:
            paper["previous_bibtex_keys"] = [old_key]
            repaired[normalize(paper["title"])] = paper
        time.sleep(0.12)

    # If the cited-text repair is still below the minimum, verify additional
    # writing candidate rows so Claude can revise with a real, larger
    # citation pool instead of inventing bibliography records.
    for row in candidates:
        if len(repaired) >= args.min_good_refs or len(query_log) >= args.max_queries:
            break
        title = str(row.get("title") or "").strip()
        norm = normalize(title)
        if not title or norm in repaired or norm in seen_titles:
            continue
        seen_titles.add(norm)
        paper, meta = verify_title_cached(title, title_to_url, cache_path, verification_cache, retry_failed_cache=args.retry_failed_cache)
        query_log.append(meta)
        if paper:
            paper["previous_bibtex_keys"] = sorted(candidate_alias_keys(row))
            repaired[normalize(paper["title"])] = paper
        time.sleep(0.12)

    papers = sorted(repaired.values(), key=lambda row: (-(int(row.get("citationCount") or 0)), int(row.get("year") or 9999), str(row.get("title") or "")))
    pool = {
        "papers": papers,
        "min_cite_paper_count": max(1, int(len(papers) * 0.9)),
        "n_total": len(papers),
        "verification_source": "OpenAlex/arXiv",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if pool_path.exists():
        pool_path.with_suffix(".before_repair.json").write_text(pool_path.read_text(encoding="utf-8"), encoding="utf-8")
    write_json(pool_path, pool)
    write_json(workspace / "citation_pool.verified_repaired.json", pool)
    bibtex = ROOT / "third_party" / "PaperOrchestra" / "skills" / "literature-review-agent" / "scripts" / "bibtex_format.py"
    bib_result = subprocess.run([sys.executable, str(bibtex), "--pool", str(pool_path), "--out", str(refs_path)], cwd=ROOT, text=True, capture_output=True)
    pool_after = json.loads(pool_path.read_text(encoding="utf-8")) if pool_path.exists() else {"papers": []}
    title_to_new_key = {normalize(str(p.get("title") or "")): str(p.get("bibtex_key") or "") for p in pool_after.get("papers", []) if isinstance(p, dict)}
    old_to_new: dict[str, str] = {}
    for old_key, norm_title in old_to_title.items():
        if norm_title in title_to_new_key:
            old_to_new[old_key] = title_to_new_key[norm_title]
            continue
        best_key = ""
        best_score = 0
        for title_norm, new_key in title_to_new_key.items():
            score = title_ratio(norm_title, title_norm)
            if score > best_score:
                best_score = score
                best_key = new_key
        if best_score >= 88 and best_key:
            old_to_new[old_key] = best_key

    def replace_cite(match: re.Match[str]) -> str:
        command = match.group(1)
        keys = [key.strip() for key in match.group(2).split(",") if key.strip()]
        mapped = []
        for key in keys:
            new_key = old_to_new.get(key)
            if new_key and new_key not in mapped:
                mapped.append(new_key)
        if not mapped:
            return ""
        return command + "{" + ",".join(mapped) + "}"

    changed_tex: list[str] = []
    for tex_path in tex_paths:
        if not tex_path.exists():
            continue
        old_text = tex_path.read_text(encoding="utf-8", errors="replace")
        new_text = CITE_RE.sub(replace_cite, old_text)
        if new_text != old_text:
            tex_path.write_text(new_text, encoding="utf-8")
            changed_tex.append(str(tex_path))

    existing_sources = [path for path in tex_paths if path.exists()]
    if existing_sources:
        canonical_tex = max(existing_sources, key=canonical_score)
    sync_targets = [workspace / "final" / "paper.tex", workspace / "drafts" / "paper.tex", output_dir / "paper.tex"]
    if canonical_tex.exists():
        canonical_text = canonical_tex.read_text(encoding="utf-8", errors="replace")
        for target in sync_targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() or target.read_text(encoding="utf-8", errors="replace") != canonical_text:
                target.write_text(canonical_text, encoding="utf-8")
                changed_tex.append(str(target))
    for target in [workspace / "final" / "refs.bib", output_dir / "refs.bib"]:
        if refs_path.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(refs_path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")

    new_refs = parse_bib(refs_path)
    for key, fields in new_refs.items():
        fields["key"] = key
    good_keys = {key for key, fields in new_refs.items() if is_good_bib_entry(fields)}
    remaining_cited = cited_keys(tex_paths)
    if canonical_tex.exists():
        remaining_cited.update(cited_keys([canonical_tex]))
    bad_cited = sorted(key for key in remaining_cited if key not in good_keys)
    report = {
        "project": args.project,
        "venue": args.venue,
        "reference_target": reference_target_info,
        "workspace": str(workspace),
        "queried": len(query_log),
        "repaired_papers": len(papers),
        "good_bib_entries": len(good_keys),
        "cited_keys": len(remaining_cited),
        "bad_cited_keys": bad_cited,
        "old_to_new_count": len(old_to_new),
        "changed_tex": changed_tex,
        "canonical_tex": str(canonical_tex),
        "candidate_rows": len(candidates),
        "cache_path": str(cache_path),
        "cache_entries": len(verification_cache),
        "bibtex_return_code": bib_result.returncode,
        "bibtex_stdout": bib_result.stdout[-2000:],
        "bibtex_stderr": bib_result.stderr[-2000:],
        "query_log": query_log,
        "status": "pass" if len(good_keys) >= args.min_good_refs and len(remaining_cited) >= args.min_good_refs and not bad_cited else "blocked",
    }
    out_json = paths.state / "paper_citation_quality_repair.json"
    out_md = paths.reports / "paper_citation_quality_repair.md"
    write_json(out_json, report)
    lines = [
        "# Paper Citation Quality Repair\n\n",
        f"- status: {report['status']}\n",
        f"- repaired_papers: {len(papers)}\n",
        f"- good_bib_entries: {len(good_keys)}\n",
        f"- cited_keys: {len(remaining_cited)}\n",
        f"- bad_cited_keys: {', '.join(bad_cited) if bad_cited else 'none'}\n",
        f"- changed_tex: {', '.join(changed_tex) if changed_tex else 'none'}\n",
    ]
    write_text(out_md, "".join(lines))
    update_pipeline_state(args.project, {
        "paper_citation_quality_status": report["status"],
        "paper_citation_quality_report": str(out_md),
        "paper_citation_quality_json": str(out_json),
        "paper_citation_good_bib_entries": len(good_keys),
        "paper_citation_bad_cited_keys": bad_cited,
    }, venue=args.venue, promote_to_top=True)
    print(out_md)
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
