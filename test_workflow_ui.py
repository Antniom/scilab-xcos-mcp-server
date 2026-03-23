import json
import unittest

import server


class WorkflowUiTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        server.state.workflows.clear()
        server.state.drafts.clear()
        server.state.draft_to_workflow.clear()

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
        self.assertIn("Xcos Workflow", contents[0].content)


if __name__ == "__main__":
    unittest.main()
