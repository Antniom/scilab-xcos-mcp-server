import asyncio
import base64
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import server


CONST_BLOCK_XML = """
<!-- draft comment -->
<BasicBlock id="const1" parent="0:2:0" interfaceFunctionName="CONST_m" blockType="d" dependsOnU="0" dependsOnT="0" simulationFunctionName="cstblk4_m" simulationFunctionType="DEFAULT" style="CONST_m">
  <ScilabString as="exprs" height="1" width="1">
    <data line="0" column="0" value="1"/>
  </ScilabString>
  <ScilabDouble as="realParameters" height="0" width="0"/>
  <ScilabDouble as="integerParameters" height="0" width="0"/>
  <Array as="objectsParameters" scilabClass="ScilabList"/>
  <ScilabInteger as="nbZerosCrossing" height="1" width="1" intPrecision="sci_int32">
    <data line="0" column="0" value="0"/>
  </ScilabInteger>
  <ScilabInteger as="nmode" height="1" width="1" intPrecision="sci_int32">
    <data line="0" column="0" value="0"/>
  </ScilabInteger>
  <ScilabDouble as="state" height="0" width="0"/>
  <ScilabDouble as="dState" height="0" width="0"/>
  <Array as="oDState" scilabClass="ScilabList"/>
  <Array as="equations" scilabClass="ScilabList"/>
  <mxGeometry as="geometry" x="0.0" y="-10.0" width="40.0" height="40.0"/>
</BasicBlock>
<ExplicitOutputPort id="const1:out" parent="const1" ordering="1" dataType="REAL_MATRIX" dataColumns="1" dataLines="1" initialState="0.0" style="ExplicitOutputPort;align=right;verticalAlign=middle;spacing=10.0" value=""/>
""".strip()


class DraftWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.created_sessions = []
        self.created_workflows = []
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
        server.state.drafts.clear()
        server.state.phase_plans.clear()
        server.state.workflows.clear()
        server.state.draft_to_workflow.clear()
        server.state.validation_jobs.clear()
        for task in list(server.state.validation_tasks.values()):
            task.cancel()
        server.state.validation_tasks.clear()

    def tearDown(self):
        for session_id in self.created_sessions:
            server.state.drafts.pop(session_id, None)
            server.state.phase_plans.pop(session_id, None)
            server.state.draft_to_workflow.pop(session_id, None)
        for workflow_id in self.created_workflows:
            server.state.workflows.pop(workflow_id, None)
        server.state.validation_jobs.clear()
        for task in list(server.state.validation_tasks.values()):
            task.cancel()
        server.state.validation_tasks.clear()
        for key, value in self.old_dirs.items():
            setattr(server, key, value)
        self.tempdir.cleanup()

    async def start_session(self):
        response = await server.xcos_start_draft()
        payload = json.loads(response[0].text)
        session_id = payload["session_id"]
        self.created_sessions.append(session_id)
        return session_id

    async def test_get_draft_xml_cleanup_options(self):
        session_id = await self.start_session()
        await server.xcos_add_blocks(session_id, CONST_BLOCK_XML)

        response = await server.xcos_get_draft_xml(
            session_id,
            pretty_print=True,
            strip_comments=True,
            validate=True,
        )
        text = response[0].text

        self.assertIn("<?xml version='1.0' encoding='utf-8'?>".lower(), text.lower())
        self.assertNotIn("draft comment", text)
        self.assertIn("<BasicBlock", text)

    async def test_file_path_and_file_content_tools(self):
        response = await server.xcos_start_draft(phases=["phase-1"])
        payload = json.loads(response[0].text)
        session_id = payload["session_id"]
        self.created_sessions.append(session_id)
        await server.xcos_commit_phase(session_id, "phase-1", CONST_BLOCK_XML)

        path_response = await server.xcos_get_file_path(session_id)
        path_payload = json.loads(path_response[0].text)
        session_file_path = path_payload["session_file_path"]

        self.assertTrue(os.path.exists(session_file_path))
        self.assertGreater(path_payload["session_file_size_bytes"], 0)
        self.assertTrue(path_payload["download_url"].endswith(f"/api/sessions/{session_id}/diagram.xcos"))

        content_response = await server.xcos_get_file_content(
            session_id,
            source="session",
            encoding="base64",
        )
        content_payload = json.loads(content_response[0].text)
        decoded = base64.b64decode(content_payload["content"]).decode("utf-8")

        self.assertIn("<XcosDiagram", decoded)

    async def test_verify_draft_updates_session_metadata(self):
        session_id = await self.start_session()
        await server.xcos_add_blocks(session_id, CONST_BLOCK_XML)

        mock_result = {
            "success": True,
            "task_id": "mock-task",
            "file_path": os.path.abspath("mock-verified.xcos"),
            "file_size_bytes": 123,
        }

        with patch.object(server, "run_verification", AsyncMock(return_value=mock_result)):
            verify_response = await server.xcos_verify_draft(session_id)

        verify_payload = json.loads(verify_response[0].text)
        self.assertEqual(verify_payload["task_id"], "mock-task")
        self.assertTrue(os.path.exists(verify_payload["session_file_path"]))
        self.assertTrue(verify_payload["download_url"].endswith(f"/api/sessions/{session_id}/diagram.xcos"))

        list_response = await server.xcos_list_sessions()
        sessions = json.loads(list_response[0].text)["sessions"]
        session_meta = next(item for item in sessions if item["session_id"] == session_id)

        self.assertTrue(session_meta["last_verified"]["success"])
        self.assertEqual(session_meta["last_verified"]["task_id"], "mock-task")

    async def test_block_data_includes_compact_reference_and_extra_examples(self):
        split_response = await server.get_xcos_block_data("SPLIT_f")
        split_payload = json.loads(split_response[0].text)
        self.assertIsNotNone(split_payload["info"])
        self.assertIn("SplitBlock", split_payload["compact_reference"]["template_xml"])
        self.assertFalse(split_payload["reference_xml"])

        cmscope_response = await server.get_xcos_block_data("CMSCOPE", include_extra_examples=True)
        cmscope_payload = json.loads(cmscope_response[0].text)
        self.assertIn("1 input", cmscope_payload["extra_examples"])
        self.assertIn('realParameters" height="1" width="5"', cmscope_payload["extra_examples"]["1 input"])

    async def test_build_xcos_diagram_prompt_is_listed_with_required_argument(self):
        prompts = await server.handle_list_prompts()
        build_prompt = next(item for item in prompts if item.name == server.BUILD_XCOS_DIAGRAM_PROMPT_NAME)

        self.assertEqual(build_prompt.title, server.BUILD_XCOS_DIAGRAM_PROMPT_TITLE)
        self.assertEqual(build_prompt.description, server.BUILD_XCOS_DIAGRAM_PROMPT_DESCRIPTION)
        self.assertEqual(len(build_prompt.arguments), 1)
        self.assertEqual(build_prompt.arguments[0].name, "problem_statement")
        self.assertTrue(build_prompt.arguments[0].required)

    async def test_build_xcos_diagram_prompt_substitutes_problem_statement(self):
        result = await server.handle_get_prompt(
            server.BUILD_XCOS_DIAGRAM_PROMPT_NAME,
            {"problem_statement": "simple pendulum with g=9.8, L=2m"},
        )
        prompt_text = result.messages[0].content.text

        self.assertEqual(result.description, server.BUILD_XCOS_DIAGRAM_PROMPT_RESULT_DESCRIPTION)
        self.assertIn("simple pendulum with g=9.8, L=2m", prompt_text)
        self.assertNotIn("{{problem_statement}}", prompt_text)
        self.assertIn("Stop after 3 failed repair attempts", prompt_text)
        self.assertIn("Use `xcos_get_file_content(source='last_verified')` only if the user asks", prompt_text)
        self.assertNotIn("calls in steps 9 and 10", prompt_text)

    async def test_build_xcos_diagram_prompt_requires_problem_statement(self):
        with self.assertRaises(ValueError):
            await server.handle_get_prompt(server.BUILD_XCOS_DIAGRAM_PROMPT_NAME, {})

    async def test_initialization_options_include_prompts_capability(self):
        options = server.create_server_initialization_options()

        self.assertIsNotNone(options.capabilities.prompts)
        self.assertFalse(options.capabilities.prompts.listChanged)

    async def test_workflow_widget_uses_existing_workflow_state(self):
        workflow = server.create_workflow_session("test system")
        self.created_workflows.append(workflow.workflow_id)
        workflow.phases["phase1_math_model"].status = "awaiting_approval"
        workflow.phases["phase1_math_model"].submitted_at = "2026-03-30T00:00:00"
        server.persist_workflow_session(workflow.workflow_id)

        response = await server.xcos_get_workflow_widget(workflow.workflow_id)
        payload = json.loads(response[0].text)

        self.assertEqual(payload["widget_type"], "workflow")
        self.assertEqual(payload["payload"]["workflow_id"], workflow.workflow_id)
        self.assertEqual(payload["payload"]["phases"][0]["label"], "Phase 1: Mathematical Analysis & Calculus")
        self.assertEqual(payload["payload"]["phases"][0]["status"], "awaiting_approval")

    async def test_persistence_hydrates_workflow_and_draft_state(self):
        workflow = server.create_workflow_session("persistent workflow")
        self.created_workflows.append(workflow.workflow_id)
        workflow.phases["phase1_math_model"].status = "approved"
        workflow.phases["phase2_architecture"].status = "approved"
        server.persist_workflow_session(workflow.workflow_id)

        response = await server.xcos_start_draft(workflow_id=workflow.workflow_id, session_id="persisted-session")
        payload = json.loads(response[0].text)
        session_id = payload["session_id"]
        self.created_sessions.append(session_id)
        await server.xcos_add_blocks(session_id, CONST_BLOCK_XML)

        server.state.drafts.clear()
        server.state.phase_plans.clear()
        server.state.workflows.clear()
        server.state.draft_to_workflow.clear()
        server.state.validation_jobs.clear()
        server.state.validation_tasks.clear()

        server.hydrate_persistent_state()

        self.assertIn(session_id, server.state.drafts)
        self.assertIn(workflow.workflow_id, server.state.workflows)
        self.assertEqual(server.state.drafts[session_id].workflow_id, workflow.workflow_id)
        self.assertTrue(server.state.drafts[session_id].restored_from_disk)

    async def test_xcos_start_draft_is_idempotent_with_session_id(self):
        first = json.loads((await server.xcos_start_draft(session_id="resume-me"))[0].text)
        second = json.loads((await server.xcos_start_draft(session_id="resume-me"))[0].text)

        self.created_sessions.append("resume-me")
        self.assertTrue(first["created"])
        self.assertTrue(second["resumed"])
        self.assertFalse(second["created"])
        self.assertEqual(first["session_id"], second["session_id"])

    async def test_validation_job_status_persists_and_verify_returns_running_job(self):
        session_id = await self.start_session()
        await server.xcos_add_blocks(session_id, CONST_BLOCK_XML)

        async def delayed_validation(_xml_content):
            await asyncio.sleep(1.2)
            return {
                "success": True,
                "task_id": "slow-task",
                "file_path": os.path.join(self.tempdir.name, "validated.xcos"),
                "file_size_bytes": 99,
            }

        with patch.object(server, "run_verification", side_effect=delayed_validation):
            verify_response = await server.xcos_verify_draft(session_id)
            verify_payload = json.loads(verify_response[0].text)
            self.assertEqual(verify_payload["status"], "running")
            job_id = verify_payload["job_id"]

            await asyncio.wait_for(asyncio.shield(server.state.validation_tasks[job_id]), timeout=5)

        status_response = await server.xcos_get_validation_status(job_id)
        status_payload = json.loads(status_response[0].text)
        self.assertEqual(status_payload["status"], "succeeded")
        self.assertTrue(status_payload["success"])

        server.state.validation_jobs.clear()
        server.state.validation_tasks.clear()
        server.hydrate_persistent_state()
        reloaded = await server.xcos_get_validation_status(job_id)
        reloaded_payload = json.loads(reloaded[0].text)
        self.assertEqual(reloaded_payload["status"], "succeeded")
        self.assertEqual(reloaded_payload["task_id"], "slow-task")

    async def test_workflow_summary_view_omits_full_phase_content(self):
        workflow = server.create_workflow_session("compact workflow")
        self.created_workflows.append(workflow.workflow_id)
        workflow.phases["phase1_math_model"].content = "Long derivation"
        server.persist_workflow_session(workflow.workflow_id)

        summary_response = await server.xcos_get_workflow(workflow.workflow_id)
        summary_payload = json.loads(summary_response[0].text)["workflow"]
        self.assertNotIn("content", summary_payload["phases"]["phase1_math_model"])

        full_response = await server.xcos_get_workflow(workflow.workflow_id, view="full")
        full_payload = json.loads(full_response[0].text)["workflow"]
        self.assertEqual(full_payload["phases"]["phase1_math_model"]["content"], "Long derivation")

    async def test_tool_descriptions_reflect_updated_workflow_guidance(self):
        tools = await server.handle_list_tools()
        by_name = {tool.name: tool for tool in tools}
        dumps = {name: tool.model_dump(mode="json") for name, tool in by_name.items()}

        self.assertIn("PHASE 2 (block diagram preview):", by_name["xcos_get_status_widget"].description)
        self.assertIn(
            "The host client can render the associated widget using the attached app resource.",
            by_name["xcos_get_status_widget"].description,
        )
        self.assertIn(
            "Call this after every xcos_submit_phase and xcos_review_phase call.",
            by_name["xcos_get_workflow_widget"].description,
        )
        self.assertIn(
            "The host client can render the associated widget using the attached app resource.",
            by_name["xcos_get_workflow_widget"].description,
        )
        self.assertIn(
            "The host client can render the associated widget using the attached app resource.",
            by_name["xcos_get_block_catalogue_widget"].description,
        )
        self.assertIn(
            "The host client can render the associated widget using the attached app resource.",
            by_name["xcos_get_topology_widget"].description,
        )
        self.assertIn("asynchronous validation", by_name["xcos_start_validation"].description)
        self.assertIn("meta", dumps["xcos_get_status_widget"])
        self.assertIn("meta", dumps["xcos_get_workflow_widget"])
        self.assertIn("meta", dumps["xcos_get_block_catalogue_widget"])
        self.assertIn("meta", dumps["xcos_get_topology_widget"])
        self.assertIn("annotations", dumps["xcos_get_status_widget"])
        self.assertIn(
            "phase_label='phase3_implementation'",
            by_name["xcos_commit_phase"].description,
        )

    def test_scilab_log_parser_ignores_gtk_locale_warning_when_exit_code_is_zero(self):
        parsed = server.analyze_scilab_verification_output(
            "\n".join([
                "Gtk-WARNING: Locale not supported by C library.",
                "Using the fallback 'C' locale.",
                "XCOSAI_VERIFY_INPUT_PATH:/tmp/example.xcos",
                "XCOSAI_VERIFY_TEXT_LINE_COUNT:42",
            ]),
            0,
        )
        self.assertTrue(parsed["success"])
        self.assertIsNone(parsed["warnings"])

    async def test_widget_tool_call_wrapper_uses_structured_content_and_widget_meta(self):
        response = await server.handle_call_tool("xcos_get_status_widget", {})
        self.assertIsInstance(response, server.mcp_types.CallToolResult)
        self.assertEqual(response.structuredContent["widget_type"], "status")
        self.assertIn("widget", response.meta)
        self.assertEqual(response.meta["widget"]["widget_type"], "status")

    async def test_http_post_result_accepts_control_characters_in_error_text(self):
        task_id = "task-with-control-chars"
        state_entry = {"success": None, "error": "", "details": {}, "event": asyncio.Event()}
        server.state.results[task_id] = state_entry

        class DummyRequest:
            async def body(self):
                return (
                    b'{"task_id":"task-with-control-chars","success":false,'
                    b'"error":"line1\nline2\tbad\\rvalue"}'
                )

        try:
            response = await server.http_handle_post_result(DummyRequest())
            payload = json.loads(response.body.decode("utf-8"))

            self.assertEqual(response.status_code, 200)
            self.assertEqual(payload["status"], "received")
            self.assertEqual(server.state.results[task_id]["error"], "line1\nline2\tbad\rvalue")
            self.assertTrue(server.state.results[task_id]["event"].is_set())
        finally:
            server.state.results.pop(task_id, None)


if __name__ == "__main__":
    unittest.main()
