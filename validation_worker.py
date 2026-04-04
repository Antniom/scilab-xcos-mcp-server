import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

import server


WORKER_PORT = int(os.environ.get("PORT", os.environ.get("XCOS_VALIDATION_WORKER_PORT", "7860")))


@dataclass
class WorkerJob:
    job_id: str
    status: str
    created_at: str
    validation_profile: str
    timeout_seconds: float
    started_at: str | None = None
    finished_at: str | None = None
    result: dict | None = None
    error: str | None = None
    progress: dict | None = None

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "validation_profile": self.validation_profile,
            "timeout_seconds": self.timeout_seconds,
            "result": self.result,
            "error": self.error,
            "progress": server.snapshot_validation_progress_tracker(self.progress),
        }


jobs: dict[str, WorkerJob] = {}
tasks: dict[str, asyncio.Task] = {}


def worker_token() -> str:
    return os.environ.get("XCOS_VALIDATION_WORKER_TOKEN", "").strip()


def worker_auth_required() -> bool:
    # Safety default: keep worker reachable unless auth is explicitly and
    # intentionally enabled with BOTH flags.
    return (
        server.parse_bool_env("XCOS_VALIDATION_WORKER_REQUIRE_AUTH", False)
        and server.parse_bool_env("XCOS_VALIDATION_WORKER_ENFORCE_AUTH", False)
    )


def require_auth(request: Request) -> JSONResponse | None:
    if not worker_auth_required():
        return None
    expected = worker_token()
    if not expected:
        return JSONResponse({"error": "Worker auth is required but XCOS_VALIDATION_WORKER_TOKEN is not set."}, status_code=500)
    actual = request.headers.get("authorization", "").strip()
    if actual == f"Bearer {expected}":
        return None
    return JSONResponse({"error": "Unauthorized"}, status_code=401)


async def run_worker_job(job_id: str, xml_content: str, validation_profile: str, timeout_seconds: float) -> None:
    job = jobs[job_id]
    job.status = "running"
    job.started_at = server.now_iso()
    progress_tracker = server.create_validation_progress_tracker(validation_profile)
    server.update_validation_progress_tracker(progress_tracker, validator_phase="worker-verification-started")
    job.progress = progress_tracker
    try:
        result = await asyncio.wait_for(
            server._run_verification_local(
                xml_content,
                validation_profile=validation_profile,
                worker_timeout_seconds=timeout_seconds,
                progress_tracker=progress_tracker,
            ),
            timeout=timeout_seconds,
        )
        job.result = result
        job.error = result.get("error")
        job.status = "succeeded" if result.get("success") else "failed"
    except asyncio.TimeoutError:
        job.result = {
            "success": False,
            "origin": "validation-worker",
            "validation_profile": validation_profile,
            "error": f"Validation worker timed out after {timeout_seconds:.0f} seconds.",
            **server.snapshot_validation_progress_tracker(progress_tracker),
        }
        job.error = job.result["error"]
        job.status = "timed_out"
    except Exception as exc:
        job.result = {
            "success": False,
            "origin": "validation-worker",
            "validation_profile": validation_profile,
            "error": f"Validation worker failed: {exc}",
            **server.snapshot_validation_progress_tracker(progress_tracker),
        }
        job.error = job.result["error"]
        job.status = "failed"
    finally:
        job.finished_at = server.now_iso()


async def http_healthz(_: Request):
    return JSONResponse(
        {
            "status": "ok",
            "role": "validation_worker",
            "timestamp": server.now_iso(),
        }
    )


async def http_root(_: Request):
    return HTMLResponse(
        "<html><body><h1>Xcos Validation Worker</h1>"
        "<p>Worker is running. Use <code>/healthz</code>, <code>/validate</code>, and <code>/jobs/{job_id}</code>.</p>"
        "</body></html>"
    )


async def http_create_validation_job(request: Request):
    auth_error = require_auth(request)
    if auth_error:
        return auth_error

    data = await request.json()
    xml_content = str(data.get("xml_content") or "")
    if not xml_content.strip():
        return JSONResponse({"error": "xml_content cannot be empty"}, status_code=400)

    try:
        validation_profile = server.normalize_validation_profile(data.get("validation_profile"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    timeout_seconds = float(data.get("timeout_seconds") or server.get_configured_validation_job_timeout_seconds())
    if timeout_seconds <= 0:
        return JSONResponse({"error": "timeout_seconds must be greater than 0"}, status_code=400)

    job_id = str(uuid.uuid4())
    job = WorkerJob(
        job_id=job_id,
        status="queued",
        created_at=server.now_iso(),
        validation_profile=validation_profile,
        timeout_seconds=timeout_seconds,
    )
    jobs[job_id] = job
    task = asyncio.create_task(run_worker_job(job_id, xml_content, validation_profile, timeout_seconds))
    tasks[job_id] = task

    def _cleanup(_: asyncio.Task):
        tasks.pop(job_id, None)

    task.add_done_callback(_cleanup)
    return JSONResponse(job.to_dict())


async def http_get_validation_job(request: Request):
    auth_error = require_auth(request)
    if auth_error:
        return auth_error

    job_id = request.path_params["job_id"]
    job = jobs.get(job_id)
    if not job:
        return JSONResponse({"error": f"Job {job_id} not found"}, status_code=404)
    return JSONResponse(job.to_dict())


@asynccontextmanager
async def lifespan(_: Starlette):
    os.environ["XCOS_SERVER_ROLE"] = "validation_worker"
    server.ensure_state_dirs()
    startup_task = None
    if server.should_prefer_poll_runtime(server.VALIDATION_PROFILE_FULL_RUNTIME):
        startup_task = asyncio.create_task(server.ensure_poll_worker_running())
    try:
        yield
    finally:
        if startup_task:
            startup_task.cancel()
        for task in list(tasks.values()):
            task.cancel()
        await server.stop_poll_worker()


def build_app() -> Starlette:
    return Starlette(
        debug=False,
        routes=[
            Route("/", http_root, methods=["GET"]),
            Route("/healthz", http_healthz, methods=["GET"]),
            Route("/validate", http_create_validation_job, methods=["POST"]),
            Route("/jobs/{job_id:str}", http_get_validation_job, methods=["GET"]),
            Route("/task", server.http_handle_get_task, methods=["GET"]),
            Route("/progress", server.http_handle_post_progress, methods=["POST"]),
            Route("/result", server.http_handle_post_result, methods=["POST"]),
        ],
        lifespan=lifespan,
    )


app = build_app()


def main():
    uvicorn.run(app, host="0.0.0.0", port=WORKER_PORT, log_level="info")


if __name__ == "__main__":
    main()
