import asyncio
import os
import unittest
from unittest.mock import patch

import server
import validation_worker


class ValidationWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        validation_worker.jobs.clear()
        validation_worker.tasks.clear()

    def test_build_app_exposes_poll_bridge_routes(self):
        app = validation_worker.build_app()
        route_paths = {route.path for route in app.routes}

        self.assertIn("/task", route_paths)
        self.assertIn("/progress", route_paths)
        self.assertIn("/result", route_paths)

    async def test_run_worker_job_timeout_includes_progress_snapshot(self):
        job_id = "worker-timeout-job"
        validation_worker.jobs[job_id] = validation_worker.WorkerJob(
            job_id=job_id,
            status="queued",
            created_at=server.now_iso(),
            validation_profile=server.VALIDATION_PROFILE_FULL_RUNTIME,
            timeout_seconds=0.01,
        )

        async def slow_local_verification(
            _xml_content,
            validation_profile=server.VALIDATION_PROFILE_FULL_RUNTIME,
            worker_timeout_seconds=None,
            progress_tracker=None,
        ):
            server.update_validation_progress_tracker(
                progress_tracker,
                validator_phase="scilab-poll-fallback",
                poll_task_id="poll-task-123",
                scilab_stage_trace=[
                    {"stage": "LOAD_XCOS_LIBS", "status": "BEGIN"},
                    {"stage": "LOAD_XCOS_LIBS", "status": "END"},
                    {"stage": "SCICOS_SIMULATE", "status": "BEGIN"},
                ],
                scilab_active_stage="SCICOS_SIMULATE",
                scilab_last_completed_stage="LOAD_XCOS_LIBS",
            )
            await asyncio.sleep(0.05)
            return {"success": True}

        with patch.object(server, "_run_verification_local", side_effect=slow_local_verification):
            await validation_worker.run_worker_job(
                job_id,
                "<XcosDiagram/>",
                server.VALIDATION_PROFILE_FULL_RUNTIME,
                0.01,
            )

        job = validation_worker.jobs[job_id]
        self.assertEqual(job.status, "timed_out")
        self.assertEqual(job.result["validator_phase"], "scilab-poll-fallback")
        self.assertEqual(job.result["poll_task_id"], "poll-task-123")
        self.assertEqual(job.result["scilab_active_stage"], "SCICOS_SIMULATE")
        self.assertEqual(job.result["scilab_last_completed_stage"], "LOAD_XCOS_LIBS")
        self.assertEqual(
            job.result["scilab_stage_trace"][-1],
            {"stage": "SCICOS_SIMULATE", "status": "BEGIN"},
        )
        self.assertEqual(job.to_dict()["progress"]["validator_phase"], "scilab-poll-fallback")

    def test_require_auth_is_disabled_by_default(self):
        class DummyRequest:
            headers = {}

        with patch.dict(os.environ, {}, clear=False):
            self.assertIsNone(validation_worker.require_auth(DummyRequest()))

    def test_require_auth_rejects_missing_bearer_when_enabled(self):
        class DummyRequest:
            headers = {}

        with patch.dict(
            os.environ,
            {
                "XCOS_VALIDATION_WORKER_REQUIRE_AUTH": "1",
                "XCOS_VALIDATION_WORKER_TOKEN": "secret",
            },
            clear=False,
        ):
            response = validation_worker.require_auth(DummyRequest())

        self.assertIsNotNone(response)
        self.assertEqual(response.status_code, 401)

    def test_require_auth_accepts_matching_bearer_when_enabled(self):
        class DummyRequest:
            headers = {"authorization": "Bearer secret"}

        with patch.dict(
            os.environ,
            {
                "XCOS_VALIDATION_WORKER_REQUIRE_AUTH": "true",
                "XCOS_VALIDATION_WORKER_TOKEN": "secret",
            },
            clear=False,
        ):
            response = validation_worker.require_auth(DummyRequest())

        self.assertIsNone(response)


if __name__ == "__main__":
    unittest.main()
