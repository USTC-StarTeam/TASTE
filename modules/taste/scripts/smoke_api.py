from __future__ import annotations

import json
import time

import requests


BASE = "http://127.0.0.1:8765"


payload = {
    "config": {
        "provider": "mock",
        "research_interest": "LLM agents retrieval",
        "researcher_profile": "local smoke test",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
        "temperature": 0.4,
        "max_fetch_papers": 2,
        "max_recommended_papers": 2,
        "max_ideas": 2,
        "arxiv_categories": ["cs.AI"],
        "arxiv_start_date": "",
        "arxiv_end_date": "",
        "github_languages": ["all"],
        "github_since": "daily",
        "hf_include_papers": True,
        "hf_include_models": True,
    },
    "selection": {
        "venue_ids": ["openreview_iclr_2026"],
        "years": [2026],
        "include_arxiv": False,
        "include_huggingface": False,
        "include_github": False,
    },
}


def wait_job(job_id: str) -> dict:
    for _ in range(60):
        job = requests.get(f"{BASE}/api/jobs/{job_id}", timeout=10).json()
        if job["status"] in {"done", "error"}:
            return job
        time.sleep(0.5)
    raise TimeoutError(job_id)


def main() -> None:
    health = requests.get(f"{BASE}/health", timeout=10)
    health.raise_for_status()
    print("health", health.text)

    response = requests.post(f"{BASE}/api/jobs/find", json=payload, timeout=10)
    response.raise_for_status()
    job = wait_job(response.json()["job_id"])
    print("find", job["status"])
    if job["status"] != "done":
        raise RuntimeError(job)
    run_id = job["result"]["run_id"]
    print(json.dumps(job.get("result", {}), ensure_ascii=False)[:500])

    response = requests.post(f"{BASE}/api/jobs/read", json={"run_id": run_id, "paper_ids": [], "max_papers": 1}, timeout=10)
    response.raise_for_status()
    job = wait_job(response.json()["job_id"])
    print("read", job["status"])
    if job["status"] != "done":
        raise RuntimeError(job)

    response = requests.post(f"{BASE}/api/jobs/idea", json={"run_id": run_id, "max_ideas": 2}, timeout=10)
    response.raise_for_status()
    job = wait_job(response.json()["job_id"])
    print("idea", job["status"])
    if job["status"] != "done":
        raise RuntimeError(job)
    idea_id = job["result"]["ideas"][0]["id"]

    response = requests.patch(f"{BASE}/api/runs/{run_id}/ideas/{idea_id}", json={"status": "approved"}, timeout=10)
    response.raise_for_status()

    response = requests.post(f"{BASE}/api/jobs/plan", json={"run_id": run_id, "idea_ids": [idea_id]}, timeout=10)
    response.raise_for_status()
    job = wait_job(response.json()["job_id"])
    print("plan", job["status"])
    if job["status"] != "done":
        raise RuntimeError(job)

    artifacts = requests.get(f"{BASE}/api/runs/{run_id}/artifacts", timeout=10).json()["artifacts"]
    print("artifacts", sorted(item["name"] for item in artifacts))


if __name__ == "__main__":
    main()
