from __future__ import annotations

import asyncio
import threading
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from auto_research.auto_find.catalog import catalog_by_id, load_catalog
from auto_research.auto_find.pipeline import run_find
from auto_research.auto_find.sources import fetch_venue_sample
from auto_research.auto_idea.pipeline import patch_idea, run_idea
from auto_research.auto_plan.pipeline import finish_plan, polish_plan, run_plan
from auto_research.auto_read.pipeline import run_read
from auto_research.emailer import send_run_email
from auto_research.jobs import JobCancelled
from auto_research.models import AppConfig, EmailJobRequest, FindRequest, IdeaPatch, IdeaRequest, PlanPolishRequest, PlanRequest, ReadRequest, VenueHealthRequest
from auto_research.paths import CONFIG_PATH, ensure_directories
from auto_research.storage import delete_run, existing_stage_path, list_runs, read_json, redacted_config, run_dir, write_json


ensure_directories()

app = FastAPI(title="TASTE Local API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


CLIENT_DIST = Path(__file__).resolve().parent / "client" / "dist"
if CLIENT_DIST.exists():
    assets = CLIENT_DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")


def load_config() -> AppConfig:
    data = read_json(CONFIG_PATH, {})
    return AppConfig(**data) if data else AppConfig()


def save_config(config: AppConfig) -> AppConfig:
    write_json(CONFIG_PATH, config.model_dump())
    return config


class JobState:
    def __init__(self, job_id: str, stage: str):
        self.job_id = job_id
        self.stage = stage
        self.status = "queued"
        self.created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self.logs: list[str] = []
        self.result: Any = None
        self.error: str = ""
        self.cancel_requested = False
        self.cancelled_at = ""
        self.progress = {"phase": "queued", "current": 0, "total": 0, "percent": 0, "message": "Queued"}
        self.progress_version = 0
        self.done = threading.Event()

    def log(self, message: str) -> None:
        self.logs.append(str(message))

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "stage": self.stage,
            "status": self.status,
            "created_at": self.created_at,
            "logs": self.logs,
            "result": self.result,
            "error": self.error,
            "cancel_requested": self.cancel_requested,
            "cancelled_at": self.cancelled_at,
            "progress": self.progress,
        }

    def should_cancel(self) -> bool:
        return self.cancel_requested

    def request_cancel(self) -> None:
        if self.status in {"done", "error", "cancelled"}:
            return
        self.cancel_requested = True
        self.status = "cancelling"
        self.log("Cancellation requested.")

    def set_progress(self, phase: str, current: int = 0, total: int = 0, message: str = "") -> None:
        percent = 0
        if total > 0:
            percent = max(0, min(100, int(round((current / total) * 100))))
        self.progress = {
            "phase": phase,
            "current": max(0, current),
            "total": max(0, total),
            "percent": percent,
            "message": message or phase,
        }
        self.progress_version += 1


JOBS: dict[str, JobState] = {}


def _auto_email_after_success(stage: str, result: Any) -> None:
    if stage == "email" or not isinstance(result, dict):
        return
    run_id = result.get("run_id")
    if not run_id:
        return
    config = load_config()
    email_config = config.email
    if not email_config.auto_send_enabled or stage not in set(email_config.auto_send_stages):
        return
    if not email_config.smtp_server or not email_config.sender or not email_config.smtp_password or not email_config.receivers:
        return
    request = EmailJobRequest(run_id=run_id, subject=f"TASTE {stage} complete: {run_id}")
    start_job("email", lambda log, should_cancel, _progress: send_run_email(request, config, log, should_cancel))


def start_job(stage: str, fn: Callable[[Callable[[str], None], Callable[[], bool], Callable[[str, int, int, str], None]], Any]) -> JobState:
    job_id = f"{stage}_{uuid4().hex[:10]}"
    job = JobState(job_id, stage)
    JOBS[job_id] = job

    def runner() -> None:
        job.status = "running"
        job.log(f"{stage} started")
        job.set_progress("started", 0, 1, f"{stage} started")
        try:
            job.result = fn(job.log, job.should_cancel, job.set_progress)
            job.status = "cancelled" if job.cancel_requested else "done"
            if job.status == "cancelled":
                job.cancelled_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                job.set_progress("cancelled", 0, 1, f"{stage} cancelled")
            else:
                job.set_progress("complete", 1, 1, f"{stage} complete")
            job.log(f"{stage} {'cancelled' if job.status == 'cancelled' else 'complete'}")
            if job.status == "done":
                _auto_email_after_success(stage, job.result)
        except JobCancelled as exc:
            job.status = "cancelled"
            job.error = str(exc)
            job.cancelled_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            job.set_progress("cancelled", 0, 1, str(exc))
            job.log(str(exc))
        except Exception as exc:
            job.status = "error"
            job.error = str(exc)
            job.set_progress("error", 0, 1, str(exc))
            job.log(str(exc))
            job.log(traceback.format_exc())
        finally:
            job.done.set()

    threading.Thread(target=runner, daemon=True).start()
    return job


@app.get("/api/config")
def api_get_config() -> AppConfig:
    return load_config()


@app.post("/api/config")
def api_save_config(config: AppConfig) -> AppConfig:
    return save_config(config)


@app.get("/api/config/meta")
def api_config_meta() -> dict:
    return {"path": str(CONFIG_PATH)}


@app.get("/api/catalog/venues")
def api_catalog() -> list[dict]:
    return load_catalog()


@app.post("/api/catalog/venue-health")
def api_venue_health(request: VenueHealthRequest) -> dict:
    catalog = catalog_by_id()
    venue_ids = request.venue_ids or list(catalog.keys())
    results = []
    for venue_id in venue_ids:
        venue = catalog.get(venue_id)
        if not venue:
            for year in request.years:
                results.append({
                    "venue_id": venue_id,
                    "year": year,
                    "ok": False,
                    "sample_count": 0,
                    "source_adapter": "unknown",
                    "message": "Unknown venue id.",
                    "samples": [],
                })
            continue
        for year in request.years:
            results.append(fetch_venue_sample(venue, year, max(1, request.sample_limit)))
    return {"results": results}


@app.post("/api/jobs/find")
def api_find(request: FindRequest) -> dict:
    config = request.config or load_config()
    save_config(config)
    job = start_job("find", lambda log, should_cancel, progress: run_find(FindRequest(config=config, selection=request.selection), log, should_cancel, progress))
    return job.as_dict()


@app.post("/api/jobs/read")
def api_read(request: ReadRequest) -> dict:
    config = load_config()
    job = start_job("read", lambda log, should_cancel, _progress: run_read(request, config, log, should_cancel))
    return job.as_dict()


@app.post("/api/jobs/idea")
def api_idea(request: IdeaRequest) -> dict:
    config = load_config()
    job = start_job("idea", lambda log, should_cancel, _progress: run_idea(request, config, log, should_cancel))
    return job.as_dict()


@app.post("/api/jobs/plan")
def api_plan(request: PlanRequest) -> dict:
    config = load_config()
    job = start_job("plan", lambda log, should_cancel, _progress: run_plan(request, config, log, should_cancel))
    return job.as_dict()


@app.post("/api/jobs/plan-polish")
def api_plan_polish(request: PlanPolishRequest) -> dict:
    config = load_config()
    job = start_job("plan-polish", lambda log, should_cancel, _progress: polish_plan(request, config, log, should_cancel))
    return job.as_dict()


@app.post("/api/jobs/email")
def api_email(request: EmailJobRequest) -> dict:
    config = load_config()
    job = start_job("email", lambda log, should_cancel, _progress: send_run_email(request, config, log, should_cancel))
    return job.as_dict()


@app.post("/api/runs/{run_id}/plans/{plan_id}/finish")
def api_finish_plan(run_id: str, plan_id: str):
    try:
        return finish_plan(run_id, plan_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@app.get("/api/jobs")
def api_jobs() -> list[dict]:
    return sorted([job.as_dict() for job in JOBS.values()], key=lambda item: item["created_at"], reverse=True)


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return job.as_dict()


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "job not found"}, status_code=404)
    job.request_cancel()
    return job.as_dict()


@app.websocket("/ws/jobs/{job_id}")
async def ws_job(websocket: WebSocket, job_id: str):
    await websocket.accept()
    try:
        sent = 0
        sent_progress = -1
        while True:
            job = JOBS.get(job_id)
            if not job:
                await websocket.send_json({"type": "error", "message": "job not found"})
                return
            for line in job.logs[sent:]:
                await websocket.send_json({"type": "log", "message": line})
            sent = len(job.logs)
            if job.progress_version != sent_progress:
                await websocket.send_json({"type": "progress", "progress": job.progress})
                sent_progress = job.progress_version
            if job.status in {"done", "error", "cancelled"}:
                await websocket.send_json({"type": "complete", "job": job.as_dict()})
                return
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        return


@app.get("/api/runs")
def api_runs() -> list[dict]:
    return list_runs()


@app.get("/api/runs/{run_id}/artifacts")
def api_artifacts(run_id: str) -> dict:
    directory = run_dir(run_id)
    artifacts = []
    markdown_artifacts = [
        ("find", "article.md"),
        ("find", "hf.md"),
        ("find", "github.md"),
        ("find", "source_status.md"),
        ("read", "read.md"),
        ("idea", "idea.md"),
        ("plan", "plan.md"),
    ]
    for stage, name in markdown_artifacts:
        path = existing_stage_path(directory, stage, name)
        if path.exists():
            artifacts.append({"name": name, "kind": "markdown", "content": path.read_text(encoding="utf-8"), "path": str(path)})
    json_artifacts = [
        *[("find", name) for name in ["find_results.json", "stage0_profile.json", "venue_health_report.json", "category_scan_report.json", "title_filter_report.json", "venue_filter1.json", "filter2_trace.json", "filter2_survivors.json", "enriched_pre_filter3.json", "arxiv_raw.json", "arxiv_prefiltered.json", "biorxiv_raw.json", "biorxiv_prefiltered.json", "nature_raw.json", "science_raw.json", "huggingface_raw.json", "github_raw.json", "config.json", "selection.json"]],
        ("read", "read_results.json"),
        ("idea", "ideas.json"),
        ("plan", "plans.json"),
        ("", "email_report.json"),
    ]
    for stage, name in json_artifacts:
        path = existing_stage_path(directory, stage, name) if stage else directory / name
        if path.exists():
            content = read_json(path, {})
            if name == "config.json" and isinstance(content, dict):
                content = redacted_config(content)
            artifacts.append({"name": name, "kind": "json", "content": content, "path": str(path)})
    return {"run_id": run_id, "artifacts": artifacts}


@app.delete("/api/runs/{run_id}")
def api_delete_run(run_id: str) -> dict:
    delete_run(run_id)
    return {"status": "ok", "run_id": run_id}


@app.patch("/api/runs/{run_id}/ideas/{idea_id}")
def api_patch_idea(run_id: str, idea_id: str, patch: IdeaPatch) -> dict:
    return patch_idea(run_id, idea_id, patch)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/{path_name:path}")
def root(path_name: str = ""):
    index = CLIENT_DIST / "index.html"
    requested = CLIENT_DIST / path_name
    if path_name and requested.exists() and requested.is_file():
        return FileResponse(str(requested))
    if index.exists():
        return FileResponse(str(index))
    return HTMLResponse(
        """
<!doctype html>
<html><head><meta charset="utf-8"><title>TASTE</title></head>
<body>
  <h1>TASTE API is running</h1>
  <p>Build the frontend with <code>npm run build</code> in <code>auto_research/web/client</code>.</p>
</body></html>
"""
    )
