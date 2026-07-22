from __future__ import annotations

import re
from typing import Any


CHANNEL_ALIASES = {
    "nips": "nips",
    "neurips": "nips",
    "iclr": "iclr",
    "icml": "icml",
    "sigkdd": "sigkdd",
    "kdd": "sigkdd",
    "sigir": "sigir",
    "cikm": "cikm",
    "aaai": "aaai",
    "iccv": "iccv",
    "www": "www",
    "thewebconference": "www",
    "cvpr": "cvpr",
    "acl": "acl",
    "ijcai": "ijcai",
    "eccv": "eccv",
    "emnlp": "emnlp",
}

OFFICIAL_SOURCE = {
    "nips": "NeurIPS Proceedings",
    "iclr": "OpenReview",
    "icml": "OpenReview/PMLR",
    "sigkdd": "ACM Digital Library",
    "sigir": "ACM Digital Library",
    "cikm": "ACM Digital Library",
    "www": "ACM Digital Library",
    "aaai": "AAAI Proceedings",
    "cvpr": "CVF Open Access",
    "iccv": "CVF Open Access",
    "eccv": "ECVA",
    "acl": "ACL Anthology",
    "emnlp": "ACL Anthology",
    "ijcai": "IJCAI Proceedings",
}

OFFICIAL_TITLE_SEARCH_DOMAINS = {
    "nips": ("proceedings.neurips.cc",),
    "iclr": ("proceedings.iclr.cc", "iclr.cc"),
    "icml": ("proceedings.mlr.press", "icml.cc"),
    "sigkdd": ("dl.acm.org",),
    "sigir": ("dl.acm.org",),
    "cikm": ("dl.acm.org",),
    "aaai": ("ojs.aaai.org",),
    "iccv": ("openaccess.thecvf.com",),
    "www": ("dl.acm.org",),
    "cvpr": ("openaccess.thecvf.com",),
    "acl": ("aclanthology.org",),
    "ijcai": ("ijcai.org",),
    "eccv": ("ecva.net", "link.springer.com"),
    "emnlp": ("aclanthology.org",),
}


def normalize_conference_channel(value: object) -> str:
    text = re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
    for alias, channel in CHANNEL_ALIASES.items():
        if text == alias or text.startswith(alias):
            return channel
    return text


def _neurips_pdf_url_from_abstract_url(value: object) -> str:
    match = re.search(
        r"https?://(?:papers\.nips\.cc|proceedings\.neurips\.cc)/paper_files/paper/(\d{4})/hash/([A-Za-z0-9]+)-Abstract-([^\"'<>\s/]+)\.html",
        str(value or ""),
    )
    if not match:
        return ""
    year, paper_hash, track = match.groups()
    return f"https://proceedings.neurips.cc/paper_files/paper/{year}/file/{paper_hash}-Paper-{track}.pdf"


def _iclr_pdf_url_from_abstract_url(value: object) -> str:
    match = re.search(
        r"https?://proceedings\.iclr\.cc/paper_files/paper/(\d{4})/hash/([A-Za-z0-9]+)-Abstract-([^\"'<>\s/]+)\.html",
        str(value or ""),
    )
    if not match:
        return ""
    year, paper_hash, track = match.groups()
    return f"https://proceedings.iclr.cc/paper_files/paper/{year}/file/{paper_hash}-Paper-{track}.pdf"


def _paper_conference_channel(paper: dict[str, Any]) -> str:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    values = [
        paper.get("conference_channel"),
        paper.get("source"),
        paper.get("venue"),
        metadata.get("conference_channel"),
        metadata.get("source"),
        metadata.get("venue"),
    ]
    for value in values:
        channel = normalize_conference_channel(value)
        if channel in OFFICIAL_SOURCE:
            return channel
    blob = " ".join(
        str(value or "")
        for value in [
            *values,
            paper.get("url"),
            paper.get("html_url"),
            paper.get("abs_url"),
            paper.get("pdf_url"),
            paper.get("doi"),
            metadata.get("url"),
            metadata.get("pdf_url"),
            metadata.get("doi"),
        ]
    ).lower()
    for marker, channel in [
        ("neurips", "nips"),
        ("nips.cc", "nips"),
        ("iclr", "iclr"),
        ("icml", "icml"),
        ("proceedings.mlr.press", "icml"),
        ("sigir", "sigir"),
        ("cikm", "cikm"),
        ("web conference", "www"),
        ("aaai", "aaai"),
        ("cvpr", "cvpr"),
        ("iccv", "iccv"),
        ("eccv", "eccv"),
        ("emnlp", "emnlp"),
        ("aclanthology.org", "acl"),
        ("ijcai", "ijcai"),
        ("kdd", "sigkdd"),
    ]:
        if marker in blob:
            return channel
    return ""


def official_conference_title_search_specs(paper: dict[str, Any]) -> list[dict[str, str]]:
    title = " ".join(str(paper.get("title") or "").split())
    if len(title.split()) < 3:
        return []
    channel = _paper_conference_channel(paper)
    return [
        {
            "channel": channel,
            "domain": domain,
            "query": f'site:{domain} "{title}"',
        }
        for domain in OFFICIAL_TITLE_SEARCH_DOMAINS.get(channel, ())
    ]


def _is_official_conference_pdf_url(url: str) -> bool:
    lowered = str(url or "").lower()
    if not lowered.startswith("http") or any(term in lowered for term in ["supplemental", "/supp/", "poster", "slides"]):
        return False
    hosts = [
        "papers.nips.cc",
        "proceedings.neurips.cc",
        "proceedings.iclr.cc",
        "openreview.net/pdf",
        "proceedings.mlr.press",
        "dl.acm.org/doi/pdf",
        "ojs.aaai.org/index.php/aaai/article/view",
        "openaccess.thecvf.com/content/",
        "aclanthology.org/",
        "ijcai.org/proceedings/",
        "ecva.net/papers/",
    ]
    pmlr_raw_pdf = bool(re.match(
        r"https?://raw\.githubusercontent\.com/mlresearch/v\d+/[^/]+/assets/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.pdf(?:\?.*)?$",
        str(url or ""),
        flags=re.I,
    ))
    return (any(host in lowered for host in hosts) or pmlr_raw_pdf) and (lowered.endswith(".pdf") or "/pdf" in lowered or "article/view" in lowered)


def _derived_official_pdf_urls(channel: str, blob: str) -> list[str]:
    urls: list[str] = []
    for match in re.finditer(
        r"https?://(?:papers\.nips\.cc|proceedings\.neurips\.cc)/paper_files/paper/\d{4}/hash/[A-Za-z0-9]+-Abstract-[^\"'<>\s/]+\.html",
        blob,
    ):
        url = _neurips_pdf_url_from_abstract_url(match.group(0))
        if url:
            urls.append(url)
    for match in re.finditer(
        r"https?://proceedings\.iclr\.cc/paper_files/paper/\d{4}/hash/[A-Za-z0-9]+-Abstract-[^\"'<>\s/]+\.html",
        blob,
    ):
        url = _iclr_pdf_url_from_abstract_url(match.group(0))
        if url:
            urls.append(url)
    for match in re.finditer(r"https?://proceedings\.mlr\.press/(v\d+)/([A-Za-z0-9_.-]+)\.html", blob):
        volume, paper_id = match.groups()
        urls.append(f"https://proceedings.mlr.press/{volume}/{paper_id}/{paper_id}.pdf")
        # PMLR volumes use two official storage layouts. Recent volumes may point
        # to the volume's mlresearch repository while older volumes use the
        # proceedings host, so keep both deterministic same-paper candidates.
        urls.append(f"https://raw.githubusercontent.com/mlresearch/{volume}/main/assets/{paper_id}/{paper_id}.pdf")
    for match in re.finditer(r"https?://openaccess\.thecvf\.com/content/([^/]+)/html/([^\"'<>\s]+)\.html", blob):
        event, paper_id = match.groups()
        urls.append(f"https://openaccess.thecvf.com/content/{event}/papers/{paper_id}.pdf")
    for match in re.finditer(
        r"https?://(?:www\.)?ecva\.net/papers/eccv_(\d{4})/papers_ECCV/html/(\d+)_ECCV_(\d{4})_paper\.php",
        blob,
        flags=re.I,
    ):
        year, paper_id, page_year = match.groups()
        if year == page_year:
            urls.append(f"https://www.ecva.net/papers/eccv_{year}/papers_ECCV/papers/{int(paper_id):05d}.pdf")
    for match in re.finditer(r"https?://aclanthology\.org/([0-9]{4}\.[A-Za-z0-9-]+\.\d+)/?", blob):
        urls.append(f"https://aclanthology.org/{match.group(1)}.pdf")
    doi_match = re.search(r"\b(10\.1145/\d+(?:\.\d+)?)\b", blob)
    if doi_match:
        urls.append("https://dl.acm.org/doi/pdf/" + doi_match.group(1))
    if channel == "ijcai":
        match = re.search(r"ijcai\.org/proceedings/(\d{4})/(\d+)", blob.lower())
        if match:
            urls.append(f"https://www.ijcai.org/proceedings/{match.group(1)}/{int(match.group(2)):04d}.pdf")
    if channel == "aaai":
        match = re.search(r"ojs\.aaai\.org/index\.php/aaai/article/view/(\d+)/(\d+)", blob.lower())
        if match:
            urls.append(f"https://ojs.aaai.org/index.php/AAAI/article/view/{match.group(1)}/{match.group(2)}")
    return list(dict.fromkeys(urls))


def official_conference_pdf_candidates(paper: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive full-text URLs only from locators already supplied in the input."""
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    channel = _paper_conference_channel(paper)
    values = [
        paper.get("url"),
        paper.get("abs_url"),
        paper.get("html_url"),
        paper.get("pdf_url"),
        paper.get("doi"),
        metadata.get("url"),
        metadata.get("pdf_url"),
        metadata.get("doi"),
    ]
    blob = " ".join(str(value or "") for value in values)
    urls = [str(value or "").strip() for value in [paper.get("pdf_url"), metadata.get("pdf_url")]]
    urls.extend(_derived_official_pdf_urls(channel, blob))
    out: list[dict[str, Any]] = []
    for url in dict.fromkeys(urls):
        if not _is_official_conference_pdf_url(url):
            continue
        official_source = OFFICIAL_SOURCE.get(channel, "")
        if "proceedings.iclr.cc" in url.lower():
            official_source = "ICLR Proceedings"
        out.append({
            "kind": "conference_official_pdf_from_input_metadata",
            "pdf_url": url,
            "accepted": True,
            "conference_channel": channel,
            "official_source": official_source,
        })
    return out
