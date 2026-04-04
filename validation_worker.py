import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
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
        }


jobs: dict[str, WorkerJob] = {}
tasks: dict[str, asyncio.Task] = {}


def worker_token() -> str:
    return os.environ.get("XCOS_VALIDATION_WORKER_TOKEN", "").strip()


def require_auth(request: Request) -> JSONResponse | None:
    expected = worker_token()
    if not expected:
        return None
    actual = request.headers.get("authorization", "").strip()
    if actual == f"Bearer {expected}":
        return None
    return JSONResponse({"error": "Unauthorized"}, status_code=401)


async def run_worker_job(job_id: str, xml_content: str, validation_profile: str, timeout_seconds: float) -> None:
    job = jobs[job_id]
    job.status = "running"
    job.started_at = server.now_iso()
    try:
        result = await asyncio.wait_for(
            server._run_verification_local(
                xml_content,
                validation_profile=validation_profile,
                worker_timeout_seconds=timeout_seconds,
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
        }
        job.error = job.result["error"]
        job.status = "timed_out"
    except Exception as exc:
        job.result = {
            "success": False,
            "origin": "validation-worker",
            "validation_profile": validation_profile,
            "error": f"Validation worker failed: {exc}",
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
    try:
        yield
    finally:
        for task in list(tasks.values()):
            task.cancel()


def build_app() -> Starlette:
    return Starlette(
        debug=False,
        routes=[
            Route("/healthz", http_healthz, methods=["GET"]),
            Route("/validate", http_create_validation_job, methods=["POST"]),
            Route("/jobs/{job_id:str}", http_get_validation_job, methods=["GET"]),
        ],
        lifespan=lifespan,
    )


app = build_app()


def main():
    uvicorn.run(app, host="0.0.0.0", port=WORKER_PORT, log_level="info")


if __name__ == "__main__":
    main()
