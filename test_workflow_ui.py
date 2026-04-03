import json
import os
import tempfile
import unittest

import server


class WorkflowUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_dirs = {
            "STATE_DIR": server.STATE_DIR,
            "DRAFT_STATE_DIR": server.DRAFT_STATE_DIR,
            "WORKFLOW_STATE_DIR": server.WORKFLOW_STATE_DIR,
            "VALIDATION_JOB_STATE_DIR": server.VALIDATION_JOB_STATE_DIR,
            "SESSION_OUTPUT_DIR": server.SESSION_OUTPUT_DIR,
            "TEMP_OUTPUT_DIR": server.TEMP_OUTPUT_DIR,
        }
        server.STATE_DIR = os.path.join(self.tempdir.name, "state")
        server.DRAFT_STATE_DIR = os.path.join(server.STATE_DIR, "drafts")
        server.WORKFLOW_STATE_DIR = os.path.join(server.STATE_DIR, "workflows")
        server.VALIDATION_JOB_STATE_DIR = os.path.join(server.STATE_DIR, "validation_jobs")
        server.SESSION_OUTPUT_DIR = os.path.join(self.tempdir.name, "sessions")
        server.TEMP_OUTPUT_DIR = os.path.join(self.tempdir.name, "temp")
        server.ensure_state_dirs()
        server.state.workflows.clear()
        server.state.drafts.clear()
        server.state.phase_plans.clear()
        server.state.draft_to_workflow.clear()
        server.state.validation_jobs.clear()
        for task in list(server.state.validation_tasks.values()):
            task.cancel()
        server.state.validation_tasks.clear()

    def tearDown(self):
        server.state.workflows.clear()
        server.state.drafts.clear()
        server.state.phase_plans.clear()
        server.state.draft_to_workflow.clear()
        server.state.validation_jobs.clear()
        for task in list(server.state.validation_tasks.values()):
            task.cancel()
        server.state.validation_tasks.clear()
        for key, value in self.old_dirs.items():
            setattr(server, key, value)
        self.tempdir.cleanup()

    async def test_workflow_requires_approvals_before_draft_start(self):
        create_response = await server.xcos_create_workflow("Design a PID loop for a DC motor.")
        create_payload = json.loads(create_response[0].text)
        workflow_id = create_payload["workflow"]["workflow_id"]

        draft_error = await server.xcos_start_draft(workflow_id=workflow_id)
        self.assertIn("Phase 2 must be approved", draft_error[0].text)

        phase1_response = await server.xcos_submit_phase(
            workflow_id,
            "phase1_math_model",
            "Derived the transfer function G(s) = 1 / (s (s + 1)).",
        )
        phase1_payload = json.loads(phase1_response[0].text)
        self.assertEqual(
            phase1_payload["workflow"]["phases"]["phase1_math_model"]["status"],
            "awaiting_approval",
        )

        await server.xcos_review_phase(workflow_id, "phase1_math_model", "approve", "Looks good.")
        phase2_response = await server.xcos_submit_phase(
            workflow_id,
            "phase2_architecture",
            "Blocks: SUM_f -> PID -> plant -> CSCOPE with SPLIT_f on the feedback branch.",
        )
        phase2_payload = json.loads(phase2_response[0].text)
        self.assertEqual(
            phase2_payload["workflow"]["phases"]["phase2_architecture"]["status"],
            "awaiting_approval",
        )

        await server.xcos_review_phase(workflow_id, "phase2_architecture", "approve", "Proceed to implementation.")
        draft_response = await server.xcos_start_draft(workflow_id=workflow_id)
        draft_payload = json.loads(draft_response[0].text)

        self.assertIn("session_id", draft_payload)
        self.assertEqual(draft_payload["workflow_id"], workflow_id)

        workflow_response = await server.xcos_get_workflow(workflow_id)
        workflow_payload = json.loads(workflow_response[0].text)
        self.assertEqual(
            workflow_payload["workflow"]["phases"]["phase3_implementation"]["status"],
            "in_progress",
        )

    async def test_ui_resource_is_readable(self):
        resources = await server.handle_list_resources()
        self.assertEqual(str(resources[0].uri), server.WORKFLOW_UI_RESOURCE_URI)

        contents = await server.handle_read_resource(server.WORKFLOW_UI_RESOURCE_URI)
        self.assertEqual(contents[0].mime_type, server.MCP_APP_MIME_TYPE)
        self.assertIn("Scilab Xcos MCP Server", contents[0].content)


if __name__ == "__main__":
    unittest.main()
