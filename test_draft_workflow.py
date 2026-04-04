import asyncio
import base64
import json
import os
import tempfile
import urllib.error
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

MINIMAL_DIAGRAM_XML = "<XcosDiagram><mxGraphModel><root /></mxGraphModel></XcosDiagram>"


def build_phase2_content(blocks, context_vars, omissions=None, synthetic_blocks=None):
    manifest = {
        "blocks": blocks,
        "links": [],
        "context_vars": context_vars,
        "omissions": omissions or [],
        "synthetic_blocks_planned": synthetic_blocks or [],
    }
    return "Architecture plan\n```json\n" + json.dumps(manifest) + "\n```"


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
            "validation_profile": server.VALIDATION_PROFILE_HOSTED_SMOKE,
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
        self.assertEqual(
            session_meta["last_verified"]["validation_profile"],
            server.VALIDATION_PROFILE_HOSTED_SMOKE,
        )

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

    async def test_workflow_creation_derives_generation_requirements(self):
        response = await server.xcos_create_workflow(
            "Build a pendulum with MUX, BARXY, CMSCOPE, CANIMXY and g=10, L=1, theta0=0, omega0=0",
            autopilot=True,
        )
        payload = json.loads(response[0].text)
        workflow = payload["workflow"]

        self.created_workflows.append(workflow["workflow_id"])
        self.assertTrue(workflow["autopilot"])
        self.assertEqual(
            workflow["generation_requirements"]["required_blocks"],
            ["BARXY", "CANIMXY", "CMSCOPE", "MUX"],
        )
        self.assertEqual(
            workflow["generation_requirements"]["required_context_vars"],
            ["L", "g", "omega0", "theta0"],
        )
        self.assertTrue(workflow["generation_requirements"]["must_use_context"])
        self.assertEqual(
            workflow["generation_context_lines"],
            ["g=10", "L=1", "theta0=0", "omega0=0"],
        )

    async def test_workflow_creation_rejects_unsupported_required_blocks(self):
        response = await server.xcos_create_workflow("Use MUX and TOTALLYUNSUPPORTEDBLOCK.")
        self.assertIn("Unsupported required block", response[0].text)

    async def test_phase2_manifest_enforces_required_blocks_and_context_vars(self):
        create_response = await server.xcos_create_workflow(
            "Build with MUX, BARXY, CMSCOPE and g=10, L=1.",
            autopilot=True,
        )
        create_payload = json.loads(create_response[0].text)
        workflow_id = create_payload["workflow"]["workflow_id"]
        self.created_workflows.append(workflow_id)

        await server.xcos_submit_phase(workflow_id, "phase1_math_model", "Math derivation")

        rejected = await server.xcos_submit_phase(
            workflow_id,
            "phase2_architecture",
            build_phase2_content(["MUX"], ["g"]),
        )
        self.assertIn("missing required blocks", rejected[0].text)
        self.assertIn("missing required context vars", rejected[0].text)

        accepted = await server.xcos_submit_phase(
            workflow_id,
            "phase2_architecture",
            build_phase2_content(["MUX", "BARXY", "CMSCOPE"], ["g", "L"]),
        )
        accepted_payload = json.loads(accepted[0].text)
        self.assertEqual(
            accepted_payload["workflow"]["phases"]["phase2_architecture"]["status"],
            "approved",
        )

    async def test_phase2_manifest_accepts_block_objects_with_documented_fields(self):
        create_response = await server.xcos_create_workflow(
            "Build with MUX, BARXY, CMSCOPE and g=10, L=1.",
            autopilot=True,
        )
        create_payload = json.loads(create_response[0].text)
        workflow_id = create_payload["workflow"]["workflow_id"]
        self.created_workflows.append(workflow_id)

        await server.xcos_submit_phase(workflow_id, "phase1_math_model", "Math derivation")

        manifest = {
            "blocks": [
                {"interfaceFunctionName": "MUX"},
                {"type": "BARXY"},
                {"block_name": "CMSCOPE"},
            ],
            "links": [],
            "context_vars": [{"name": "g"}, {"variable": "L"}],
            "omissions": [],
            "synthetic_blocks_planned": [],
        }
        accepted = await server.xcos_submit_phase(
            workflow_id,
            "phase2_architecture",
            "Architecture plan\n```json\n" + json.dumps(manifest) + "\n```",
        )
        accepted_payload = json.loads(accepted[0].text)
        self.assertEqual(
            accepted_payload["workflow"]["phases"]["phase2_architecture"]["status"],
            "approved",
        )

    async def test_phase2_manifest_error_documents_expected_block_fields(self):
        create_response = await server.xcos_create_workflow(
            "Build with MUX, BARXY, CMSCOPE and g=10, L=1.",
            autopilot=True,
        )
        create_payload = json.loads(create_response[0].text)
        workflow_id = create_payload["workflow"]["workflow_id"]
        self.created_workflows.append(workflow_id)

        await server.xcos_submit_phase(workflow_id, "phase1_math_model", "Math derivation")

        manifest = {
            "blocks": [{"unexpected": "MUX"}],
            "links": [],
            "context_vars": ["g", "L"],
            "omissions": [],
            "synthetic_blocks_planned": [],
        }
        rejected = await server.xcos_submit_phase(
            workflow_id,
            "phase2_architecture",
            "Architecture plan\n```json\n" + json.dumps(manifest) + "\n```",
        )
        self.assertIn("Accepted manifest schema:", rejected[0].text)
        self.assertIn("interfaceFunctionName", rejected[0].text)

    async def test_xcos_set_context_injects_top_level_context(self):
        session_id = await self.start_session()
        set_response = await server.xcos_set_context(session_id, ["g=10", "L=1", "theta0=0"])
        set_payload = json.loads(set_response[0].text)

        self.assertEqual(set_payload["context_line_count"], 3)

        xml_response = await server.xcos_get_draft_xml(session_id, pretty_print=True)
        xml_text = xml_response[0].text
        self.assertIn('<Array as="context" scilabClass="String[]">', xml_text)
        self.assertIn('value="g=10"', xml_text)
        self.assertIn('value="theta0=0"', xml_text)

    async def test_start_draft_inherits_required_context_lines(self):
        create_response = await server.xcos_create_workflow(
            "Pendulum with MUX and g=10, L=2, theta0=0, omega0=0",
            autopilot=True,
        )
        create_payload = json.loads(create_response[0].text)
        workflow_id = create_payload["workflow"]["workflow_id"]
        self.created_workflows.append(workflow_id)

        await server.xcos_submit_phase(workflow_id, "phase1_math_model", "Math derivation")
        await server.xcos_submit_phase(
            workflow_id,
            "phase2_architecture",
            build_phase2_content(["MUX"], ["g", "L", "theta0", "omega0"]),
        )

        draft_response = await server.xcos_start_draft(workflow_id=workflow_id)
        draft_payload = json.loads(draft_response[0].text)
        session_id = draft_payload["session_id"]
        self.created_sessions.append(session_id)

        xml_response = await server.xcos_get_draft_xml(session_id, pretty_print=True)
        xml_text = xml_response[0].text
        self.assertIn('value="g=10"', xml_text)
        self.assertIn('value="L=2"', xml_text)
        self.assertIn('value="omega0=0"', xml_text)

    async def test_requirement_extraction_does_not_treat_lowercase_from_as_FROM_block(self):
        response = await server.xcos_create_workflow(
            "Build the same pendulum from the attached reference image with MUX and BARXY.",
            autopilot=True,
        )
        payload = json.loads(response[0].text)
        workflow = payload["workflow"]

        self.created_workflows.append(workflow["workflow_id"])
        self.assertEqual(
            workflow["generation_requirements"]["required_blocks"],
            ["BARXY", "MUX"],
        )
        self.assertNotIn("FROM", workflow["generation_requirements"]["required_blocks"])

    def test_normalize_fanout_to_split_blocks_inserts_explicit_split(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<XcosDiagram background="-1" gridEnabled="1" title="Test">
  <Array as="context" scilabClass="String[]"></Array>
  <mxGraphModel as="model">
    <root>
      <mxCell id="0"/>
      <mxCell id="1" parent="0"/>
      <BasicBlock id="src" parent="0:2:0" interfaceFunctionName="CONST_m">
        <mxGeometry as="geometry" x="0" y="0" width="40" height="40"/>
      </BasicBlock>
      <ExplicitOutputPort id="src:out" parent="src" ordering="1"/>
      <BasicBlock id="dst1" parent="0:2:0" interfaceFunctionName="GAINBLK_f">
        <mxGeometry as="geometry" x="100" y="0" width="40" height="40"/>
      </BasicBlock>
      <ExplicitInputPort id="dst1:in" parent="dst1" ordering="1"/>
      <BasicBlock id="dst2" parent="0:2:0" interfaceFunctionName="GAINBLK_f">
        <mxGeometry as="geometry" x="100" y="100" width="40" height="40"/>
      </BasicBlock>
      <ExplicitInputPort id="dst2:in" parent="dst2" ordering="1"/>
      <ExplicitLink id="l1" parent="0:2:0" source="src:out" target="dst1:in" style="" value=""><mxGeometry as="geometry"/></ExplicitLink>
      <ExplicitLink id="l2" parent="0:2:0" source="src:out" target="dst2:in" style="" value=""><mxGeometry as="geometry"/></ExplicitLink>
    </root>
  </mxGraphModel>
</XcosDiagram>"""
        tree = server.etree.fromstring(xml.encode("utf-8"), server.etree.XMLParser(remove_blank_text=True))

        normalization = server.normalize_fanout_to_split_blocks(tree)
        validation = server.validate_diagram_structure(tree, auto_fixed=False)

        self.assertTrue(normalization["normalized"])
        self.assertIn("SPLIT_f", normalization["warnings"][0])
        self.assertTrue(validation["success"])
        self.assertEqual(len(tree.xpath("//SplitBlock[@interfaceFunctionName='SPLIT_f']")), 1)

    def test_normalize_fanout_to_split_blocks_inserts_event_split(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<XcosDiagram background="-1" gridEnabled="1" title="Event Test">
  <Array as="context" scilabClass="String[]"></Array>
  <mxGraphModel as="model">
    <root>
      <mxCell id="0"/>
      <mxCell id="1" parent="0"/>
      <BasicBlock id="clk" parent="0:2:0" interfaceFunctionName="CLOCK_c">
        <mxGeometry as="geometry" x="0" y="0" width="40" height="40"/>
      </BasicBlock>
      <CommandPort id="clk:cmd" parent="clk" ordering="1"/>
      <BasicBlock id="scope1" parent="0:2:0" interfaceFunctionName="CMSCOPE">
        <mxGeometry as="geometry" x="120" y="0" width="40" height="40"/>
      </BasicBlock>
      <ControlPort id="scope1:ctrl" parent="scope1" ordering="1"/>
      <BasicBlock id="scope2" parent="0:2:0" interfaceFunctionName="BARXY">
        <mxGeometry as="geometry" x="120" y="100" width="40" height="40"/>
      </BasicBlock>
      <ControlPort id="scope2:ctrl" parent="scope2" ordering="1"/>
      <CommandControlLink id="e1" parent="0:2:0" source="clk:cmd" target="scope1:ctrl" style="" value=""><mxGeometry as="geometry"/></CommandControlLink>
      <CommandControlLink id="e2" parent="0:2:0" source="clk:cmd" target="scope2:ctrl" style="" value=""><mxGeometry as="geometry"/></CommandControlLink>
    </root>
  </mxGraphModel>
</XcosDiagram>"""
        tree = server.etree.fromstring(xml.encode("utf-8"), server.etree.XMLParser(remove_blank_text=True))

        normalization = server.normalize_fanout_to_split_blocks(tree)
        validation = server.validate_diagram_structure(tree, auto_fixed=False)

        self.assertTrue(normalization["normalized"])
        self.assertIn("CLKSPLIT_f", normalization["warnings"][0])
        self.assertTrue(validation["success"])
        self.assertEqual(len(tree.xpath("//SplitBlock[@interfaceFunctionName='CLKSPLIT_f']")), 1)

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

        async def delayed_validation(
            _xml_content,
            validation_profile=server.VALIDATION_PROFILE_FULL_RUNTIME,
            worker_timeout_seconds=None,
        ):
            await asyncio.sleep(1.2)
            return {
                "success": True,
                "task_id": "slow-task",
                "file_path": os.path.join(self.tempdir.name, "validated.xcos"),
                "file_size_bytes": 99,
                "validation_profile": validation_profile,
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

    def test_validation_timeout_config_defaults_and_env_overrides(self):
        with patch.object(server, "detect_validation_mode", return_value="poll"), patch.object(server.os, "name", "posix"):
            self.assertEqual(server.get_configured_subprocess_timeout_seconds(), 90.0)
            self.assertEqual(server.get_configured_poll_timeout_seconds(), 120.0)
            self.assertEqual(server.get_configured_validation_job_timeout_seconds(), 120.0)
            self.assertEqual(server.get_configured_poll_worker_startup_timeout_seconds(), 20.0)

        with patch.object(server, "detect_validation_mode", return_value="subprocess"), patch.object(server.os, "name", "posix"):
            self.assertEqual(server.get_configured_subprocess_timeout_seconds(), 180.0)
            self.assertEqual(server.get_configured_poll_timeout_seconds(), 420.0)
            self.assertEqual(server.get_configured_validation_job_timeout_seconds(), 720.0)
            self.assertEqual(server.get_configured_poll_worker_startup_timeout_seconds(), 60.0)

        with (
            patch.dict(
                server.os.environ,
                {
                    "XCOS_SCILAB_SUBPROCESS_TIMEOUT_SECONDS": "222",
                    "XCOS_POLL_VALIDATION_TIMEOUT_SECONDS": "333",
                    "XCOS_VALIDATION_JOB_TIMEOUT_SECONDS": "444",
                    "XCOS_POLL_WORKER_STARTUP_TIMEOUT_SECONDS": "77",
                },
                clear=False,
            ),
            patch.object(server, "detect_validation_mode", return_value="subprocess"),
            patch.object(server.os, "name", "posix"),
        ):
            self.assertEqual(server.get_configured_subprocess_timeout_seconds(), 222.0)
            self.assertEqual(server.get_configured_poll_timeout_seconds(), 333.0)
            self.assertEqual(server.get_configured_validation_job_timeout_seconds(), 444.0)
            self.assertEqual(server.get_configured_poll_worker_startup_timeout_seconds(), 77.0)

    def test_should_retry_with_poll_fallback_on_runtime_timeout(self):
        self.assertTrue(
            server.should_retry_with_poll_fallback(
                {
                    "success": False,
                    "error": "Structural validation passed, but Scilab runtime validation timed out after 180 seconds.",
                }
            )
        )
        self.assertFalse(
            server.should_retry_with_poll_fallback(
                {"success": False, "error": "Structural validation passed, but Scilab reported a parameter mismatch."}
            )
        )

    async def test_run_verification_retries_poll_fallback_after_subprocess_timeout_and_returns_success(self):
        subprocess_result = {
            "success": False,
            "origin": "scilab-subprocess",
            "error": "Structural validation passed, but Scilab runtime validation timed out after 180 seconds.",
        }
        poll_result = {
            "success": True,
            "origin": "scilab-poll-fallback",
            "warnings": ["poll worker warning"],
            "scilab_verdict": "Scilab import and simulation passed via long-lived poll worker.",
        }
        python_result = {"success": True, "warnings": ["structural warning"]}

        with (
            patch.object(server, "detect_validation_mode", return_value="subprocess"),
            patch.object(server, "auto_fix_mux_to_scalar", return_value=False),
            patch.object(server, "normalize_fanout_to_split_blocks", return_value={"normalized": False, "warnings": ["fanout warning"]}),
            patch.object(server, "validate_port_sizes", return_value=[]),
            patch.object(server, "validate_diagram_structure", return_value=python_result),
            patch.object(server, "run_headless_scilab_validation", AsyncMock(return_value=subprocess_result)),
            patch.object(server, "run_poll_validation", AsyncMock(return_value=poll_result)),
        ):
            result = await server.run_verification(MINIMAL_DIAGRAM_XML)

        self.assertTrue(result["success"])
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["subprocess_result"], subprocess_result)
        self.assertEqual(result["poll_fallback_result"], poll_result)
        self.assertIn("timed out", result["fallback_reason"].lower())
        self.assertEqual(result["origin"], "hybrid (structural-python + scilab-subprocess + scilab-poll-fallback)")

    async def test_run_verification_returns_poll_failure_as_top_level_after_subprocess_timeout(self):
        subprocess_result = {
            "success": False,
            "origin": "scilab-subprocess",
            "error": "Structural validation passed, but Scilab runtime validation timed out after 180 seconds.",
        }
        poll_result = {
            "success": False,
            "origin": "scilab-poll-fallback",
            "error": "Scilab verification timed out for fallback-task after 180 seconds",
            "file_path": os.path.join(self.tempdir.name, "fallback.xcos"),
            "file_size_bytes": 1234,
        }
        python_result = {"success": True, "warnings": []}

        with (
            patch.object(server, "detect_validation_mode", return_value="subprocess"),
            patch.object(server, "auto_fix_mux_to_scalar", return_value=False),
            patch.object(server, "normalize_fanout_to_split_blocks", return_value={"normalized": False, "warnings": []}),
            patch.object(server, "validate_port_sizes", return_value=[]),
            patch.object(server, "validate_diagram_structure", return_value=python_result),
            patch.object(server, "run_headless_scilab_validation", AsyncMock(return_value=subprocess_result)),
            patch.object(server, "run_poll_validation", AsyncMock(return_value=poll_result)),
        ):
            result = await server.run_verification(MINIMAL_DIAGRAM_XML)

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], poll_result["error"])
        self.assertEqual(result["file_path"], poll_result["file_path"])
        self.assertEqual(result["subprocess_result"], subprocess_result)
        self.assertEqual(result["poll_fallback_result"], poll_result)
        self.assertTrue(result["fallback_used"])
        self.assertEqual(server.infer_validation_code(result), "SCILAB_RUNTIME_TIMEOUT")
        self.assertEqual(server.infer_validation_bucket(result), "runtime_timeout")

    async def test_run_verification_hosted_smoke_uses_import_only_validation(self):
        import_result = {
            "success": True,
            "origin": "scilab-import-check",
            "warnings": ["import warning"],
            "validation_profile": server.VALIDATION_PROFILE_HOSTED_SMOKE,
        }
        python_result = {"success": True, "warnings": ["structural warning"]}

        with (
            patch.object(server, "auto_fix_mux_to_scalar", return_value=False),
            patch.object(server, "normalize_fanout_to_split_blocks", return_value={"normalized": False, "warnings": ["fanout warning"]}),
            patch.object(server, "validate_port_sizes", return_value=[]),
            patch.object(server, "validate_diagram_structure", return_value=python_result),
            patch.object(server, "run_headless_scilab_import_validation", AsyncMock(return_value=import_result)) as import_mock,
            patch.object(server, "run_headless_scilab_validation", AsyncMock()) as runtime_mock,
            patch.object(server, "run_poll_validation", AsyncMock()) as poll_mock,
        ):
            result = await server.run_verification(
                MINIMAL_DIAGRAM_XML,
                validation_profile=server.VALIDATION_PROFILE_HOSTED_SMOKE,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["validation_profile"], server.VALIDATION_PROFILE_HOSTED_SMOKE)
        self.assertEqual(result["origin"], "hybrid (structural-python + scilab-import-check)")
        import_mock.assert_awaited_once()
        runtime_mock.assert_not_awaited()
        poll_mock.assert_not_awaited()

    async def test_run_verification_hosted_smoke_returns_import_failure_code(self):
        import_result = {
            "success": False,
            "origin": "scilab-import-check",
            "error": "Structural validation passed, but Scilab import validation failed: broken import",
            "validation_profile": server.VALIDATION_PROFILE_HOSTED_SMOKE,
        }
        python_result = {"success": True, "warnings": []}

        with (
            patch.object(server, "auto_fix_mux_to_scalar", return_value=False),
            patch.object(server, "normalize_fanout_to_split_blocks", return_value={"normalized": False, "warnings": []}),
            patch.object(server, "validate_port_sizes", return_value=[]),
            patch.object(server, "validate_diagram_structure", return_value=python_result),
            patch.object(server, "run_headless_scilab_import_validation", AsyncMock(return_value=import_result)),
        ):
            result = await server.run_verification(
                MINIMAL_DIAGRAM_XML,
                validation_profile=server.VALIDATION_PROFILE_HOSTED_SMOKE,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["validation_profile"], server.VALIDATION_PROFILE_HOSTED_SMOKE)
        self.assertEqual(server.infer_validation_code(result), "SCILAB_IMPORT_FAILED")
        self.assertEqual(server.infer_validation_bucket(result), "import")

    async def test_run_verification_validation_worker_prefers_long_lived_poll_runtime(self):
        poll_result = {
            "success": True,
            "origin": "scilab-poll-runtime",
            "warnings": ["poll worker warning"],
            "validator_mode": "poll",
        }
        python_result = {"success": True, "warnings": ["structural warning"]}

        with (
            patch.dict(server.os.environ, {"XCOS_SERVER_ROLE": "validation_worker"}, clear=False),
            patch.object(server.os, "name", "posix"),
            patch.object(server, "detect_validation_mode", return_value="subprocess"),
            patch.object(server, "auto_fix_mux_to_scalar", return_value=False),
            patch.object(server, "normalize_fanout_to_split_blocks", return_value={"normalized": False, "warnings": ["fanout warning"]}),
            patch.object(server, "validate_port_sizes", return_value=[]),
            patch.object(server, "validate_diagram_structure", return_value=python_result),
            patch.object(server, "run_headless_scilab_validation", AsyncMock()) as runtime_mock,
            patch.object(server, "run_poll_validation", AsyncMock(return_value=poll_result)) as poll_mock,
        ):
            result = await server.run_verification(
                MINIMAL_DIAGRAM_XML,
                validation_profile=server.VALIDATION_PROFILE_FULL_RUNTIME,
            )

        self.assertTrue(result["success"])
        self.assertTrue(result["poll_runtime_preferred"])
        self.assertEqual(result["origin"], "scilab-poll-runtime")
        self.assertEqual(
            result["warnings"],
            ["fanout warning", "structural warning", "poll worker warning"],
        )
        runtime_mock.assert_not_awaited()
        poll_mock.assert_awaited_once_with(
            MINIMAL_DIAGRAM_XML,
            False,
            progress_tracker=None,
            origin="scilab-poll-runtime",
            progress_phase="scilab-poll-runtime",
        )

    async def test_run_verification_offloads_full_runtime_to_remote_worker_when_configured(self):
        worker_result = {
            "success": True,
            "origin": "scilab-subprocess",
            "validation_profile": server.VALIDATION_PROFILE_FULL_RUNTIME,
        }
        with (
            patch.dict(server.os.environ, {"XCOS_VALIDATION_WORKER_URL": "https://worker.example"}, clear=False),
            patch.object(server, "run_remote_validation_worker", AsyncMock(return_value=worker_result)) as remote_mock,
            patch.object(server, "_run_verification_local", AsyncMock()) as local_mock,
        ):
            result = await server.run_verification(
                MINIMAL_DIAGRAM_XML,
                validation_profile=server.VALIDATION_PROFILE_FULL_RUNTIME,
                worker_timeout_seconds=123.0,
            )

        self.assertEqual(result, worker_result)
        remote_mock.assert_awaited_once_with(
            MINIMAL_DIAGRAM_XML,
            server.VALIDATION_PROFILE_FULL_RUNTIME,
            123.0,
        )
        local_mock.assert_not_awaited()

    async def test_run_verification_keeps_hosted_smoke_local_even_with_worker_configured(self):
        local_result = {
            "success": True,
            "origin": "hybrid (structural-python + scilab-import-check)",
            "validation_profile": server.VALIDATION_PROFILE_HOSTED_SMOKE,
        }
        with (
            patch.dict(server.os.environ, {"XCOS_VALIDATION_WORKER_URL": "https://worker.example"}, clear=False),
            patch.object(server, "run_remote_validation_worker", AsyncMock()) as remote_mock,
            patch.object(server, "_run_verification_local", AsyncMock(return_value=local_result)) as local_mock,
        ):
            result = await server.run_verification(
                MINIMAL_DIAGRAM_XML,
                validation_profile=server.VALIDATION_PROFILE_HOSTED_SMOKE,
            )

        self.assertEqual(result, local_result)
        remote_mock.assert_not_awaited()
        local_mock.assert_awaited_once()

    async def test_run_verification_remote_worker_timeout_includes_failure_metadata(self):
        with (
            patch.dict(server.os.environ, {"XCOS_VALIDATION_WORKER_URL": "https://worker.example"}, clear=False),
            patch.object(
                server,
                "run_remote_validation_worker",
                AsyncMock(side_effect=TimeoutError("worker timed out")),
            ),
            patch.object(server, "get_configured_validation_job_timeout_seconds", return_value=321.0),
        ):
            result = await server.run_verification(
                MINIMAL_DIAGRAM_XML,
                validation_profile=server.VALIDATION_PROFILE_FULL_RUNTIME,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["origin"], "validation-worker-remote")
        self.assertEqual(result["validation_profile"], server.VALIDATION_PROFILE_FULL_RUNTIME)
        self.assertEqual(result["remote_worker"]["url"], "https://worker.example")
        self.assertEqual(result["remote_worker"]["timeout_seconds"], 321.0)
        self.assertEqual(result["remote_worker"]["error_type"], "timeout")
        self.assertTrue(result["remote_worker"]["retryable"])
        self.assertEqual(server.infer_validation_bucket(result), "worker")

    async def test_run_verification_remote_worker_non_retryable_failure_metadata(self):
        with (
            patch.dict(server.os.environ, {"XCOS_VALIDATION_WORKER_URL": "https://worker.example"}, clear=False),
            patch.object(
                server,
                "run_remote_validation_worker",
                AsyncMock(side_effect=ValueError("worker payload parsing failed")),
            ),
        ):
            result = await server.run_verification(
                MINIMAL_DIAGRAM_XML,
                validation_profile=server.VALIDATION_PROFILE_FULL_RUNTIME,
                worker_timeout_seconds=222.0,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["origin"], "validation-worker-remote")
        self.assertEqual(result["remote_worker"]["error_type"], "runtime")
        self.assertFalse(result["remote_worker"]["retryable"])
        self.assertEqual(result["remote_worker"]["timeout_seconds"], 222.0)
        self.assertEqual(result["remote_worker"]["error_class"], "ValueError")

    def test_get_remote_validation_worker_timeout_seconds_reserves_margin(self):
        self.assertEqual(
            server.get_remote_validation_worker_timeout_seconds(720.0),
            705.0,
        )
        self.assertEqual(
            server.get_remote_validation_worker_timeout_seconds(15.0),
            14.0,
        )
        self.assertEqual(
            server.get_remote_validation_worker_timeout_seconds(1.0),
            1.0,
        )

    def test_validation_worker_retry_config_defaults_and_env_overrides(self):
        with patch.dict(server.os.environ, {}, clear=True):
            self.assertEqual(
                server.get_validation_worker_request_retry_count(),
                server.DEFAULT_VALIDATION_WORKER_REQUEST_RETRY_COUNT,
            )
            self.assertEqual(
                server.get_validation_worker_retry_backoff_seconds(),
                server.DEFAULT_VALIDATION_WORKER_RETRY_BACKOFF_SECONDS,
            )

        with patch.dict(
            server.os.environ,
            {
                "XCOS_VALIDATION_WORKER_REQUEST_RETRY_COUNT": "6",
                "XCOS_VALIDATION_WORKER_RETRY_BACKOFF_SECONDS": "2.5",
            },
            clear=True,
        ):
            self.assertEqual(server.get_validation_worker_request_retry_count(), 6)
            self.assertEqual(server.get_validation_worker_retry_backoff_seconds(), 2.5)

        with patch.dict(
            server.os.environ,
            {
                "XCOS_VALIDATION_WORKER_REQUEST_RETRY_COUNT": "invalid",
                "XCOS_VALIDATION_WORKER_RETRY_BACKOFF_SECONDS": "invalid",
            },
            clear=True,
        ):
            self.assertEqual(
                server.get_validation_worker_request_retry_count(),
                server.DEFAULT_VALIDATION_WORKER_REQUEST_RETRY_COUNT,
            )
            self.assertEqual(
                server.get_validation_worker_retry_backoff_seconds(),
                server.DEFAULT_VALIDATION_WORKER_RETRY_BACKOFF_SECONDS,
            )

    def test_is_retryable_worker_request_error(self):
        self.assertTrue(server.is_retryable_worker_request_error(urllib.error.URLError("network")))
        self.assertTrue(server.is_retryable_worker_request_error(TimeoutError("timeout")))
        self.assertTrue(server.is_retryable_worker_request_error(OSError("socket")))
        self.assertTrue(
            server.is_retryable_worker_request_error(
                urllib.error.HTTPError("https://worker.example", 503, "busy", {}, None)
            )
        )
        self.assertFalse(
            server.is_retryable_worker_request_error(
                urllib.error.HTTPError("https://worker.example", 400, "bad request", {}, None)
            )
        )

    async def test_run_remote_validation_worker_uses_shorter_inner_timeout(self):
        posted_payloads = []

        def fake_post(url, payload, timeout_seconds, token):
            posted_payloads.append((url, payload, timeout_seconds, token))
            return {"job_id": "worker-job"}

        def fake_get(_url, _timeout_seconds, _token):
            return {
                "status": "timed_out",
                "created_at": "2026-04-04T00:00:00",
                "started_at": "2026-04-04T00:00:01",
                "finished_at": "2026-04-04T00:11:45",
                "progress": {
                    "validator_phase": "scilab-poll-fallback",
                    "scilab_active_stage": "SCICOS_SIMULATE",
                    "scilab_last_completed_stage": "VALIDATE_LINK_CONNECTIVITY",
                },
                "result": {
                    "success": False,
                    "origin": "validation-worker",
                    "validation_profile": server.VALIDATION_PROFILE_FULL_RUNTIME,
                    "error": "Validation worker timed out after 705 seconds.",
                },
            }

        with (
            patch.dict(
                server.os.environ,
                {
                    "XCOS_VALIDATION_WORKER_URL": "https://worker.example",
                    "XCOS_VALIDATION_WORKER_TOKEN": "secret-token",
                },
                clear=False,
            ),
            patch.object(server, "http_post_json", side_effect=fake_post),
            patch.object(server, "http_get_json", side_effect=fake_get),
            patch.object(server.asyncio, "sleep", AsyncMock()),
        ):
            result = await server.run_remote_validation_worker(
                MINIMAL_DIAGRAM_XML,
                server.VALIDATION_PROFILE_FULL_RUNTIME,
                720.0,
            )

        self.assertEqual(posted_payloads[0][0], "https://worker.example/validate")
        self.assertEqual(posted_payloads[0][1]["timeout_seconds"], 705.0)
        self.assertEqual(posted_payloads[0][3], "secret-token")
        self.assertFalse(result["success"])
        self.assertEqual(result["validator_phase"], "scilab-poll-fallback")
        self.assertEqual(result["scilab_active_stage"], "SCICOS_SIMULATE")
        self.assertEqual(result["remote_worker"]["timeout_seconds"], 705.0)

    async def test_run_remote_validation_worker_retries_create_on_transient_error(self):
        post_calls = []

        def fake_post(url, payload, timeout_seconds, token):
            post_calls.append((url, payload, timeout_seconds, token))
            if len(post_calls) == 1:
                raise urllib.error.URLError("temporary create failure")
            return {"job_id": "worker-job"}

        def fake_get(_url, _timeout_seconds, _token):
            return {
                "status": "succeeded",
                "result": {
                    "success": True,
                    "origin": "validation-worker",
                    "validation_profile": server.VALIDATION_PROFILE_FULL_RUNTIME,
                },
            }

        with (
            patch.dict(server.os.environ, {"XCOS_VALIDATION_WORKER_URL": "https://worker.example"}, clear=False),
            patch.object(server, "http_post_json", side_effect=fake_post),
            patch.object(server, "http_get_json", side_effect=fake_get),
            patch.object(server.asyncio, "sleep", AsyncMock()),
        ):
            result = await server.run_remote_validation_worker(
                MINIMAL_DIAGRAM_XML,
                server.VALIDATION_PROFILE_FULL_RUNTIME,
                120.0,
            )

        self.assertTrue(result["success"])
        self.assertEqual(len(post_calls), 2)
        self.assertEqual(result["remote_worker"]["create_retry_count"], 1)

    async def test_run_remote_validation_worker_retries_poll_on_transient_error(self):
        poll_calls = []

        def fake_post(_url, _payload, _timeout_seconds, _token):
            return {"job_id": "worker-job"}

        def fake_get(_url, _timeout_seconds, _token):
            poll_calls.append(1)
            if len(poll_calls) == 1:
                raise urllib.error.URLError("temporary poll failure")
            return {
                "status": "succeeded",
                "result": {
                    "success": True,
                    "origin": "validation-worker",
                    "validation_profile": server.VALIDATION_PROFILE_FULL_RUNTIME,
                },
            }

        with (
            patch.dict(server.os.environ, {"XCOS_VALIDATION_WORKER_URL": "https://worker.example"}, clear=False),
            patch.object(server, "http_post_json", side_effect=fake_post),
            patch.object(server, "http_get_json", side_effect=fake_get),
            patch.object(server.asyncio, "sleep", AsyncMock()),
        ):
            result = await server.run_remote_validation_worker(
                MINIMAL_DIAGRAM_XML,
                server.VALIDATION_PROFILE_FULL_RUNTIME,
                120.0,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["remote_worker"]["poll_transient_errors"], 1)

    async def test_xcos_start_validation_uses_configured_timeout_by_default(self):
        session_id = await self.start_session()
        with (
            patch.object(server, "get_configured_validation_job_timeout_seconds", return_value=321.0),
            patch.object(server, "_schedule_validation_job", return_value=None),
        ):
            response = await server.xcos_start_validation(session_id)

        payload = json.loads(response[0].text)
        self.assertEqual(payload["timeout_seconds"], 321.0)
        self.assertEqual(payload["validation_profile"], server.VALIDATION_PROFILE_FULL_RUNTIME)
        self.assertEqual(server.state.validation_jobs[payload["job_id"]].timeout_seconds, 321.0)
        self.assertEqual(
            server.state.validation_jobs[payload["job_id"]].validation_profile,
            server.VALIDATION_PROFILE_FULL_RUNTIME,
        )

    async def test_xcos_start_validation_accepts_hosted_smoke_profile(self):
        session_id = await self.start_session()
        with patch.object(server, "_schedule_validation_job", return_value=None):
            response = await server.xcos_start_validation(
                session_id,
                validation_profile=server.VALIDATION_PROFILE_HOSTED_SMOKE,
            )

        payload = json.loads(response[0].text)
        self.assertEqual(payload["validation_profile"], server.VALIDATION_PROFILE_HOSTED_SMOKE)
        self.assertEqual(
            server.state.validation_jobs[payload["job_id"]].validation_profile,
            server.VALIDATION_PROFILE_HOSTED_SMOKE,
        )

    async def test_xcos_verify_draft_uses_configured_timeout_by_default(self):
        session_id = await self.start_session()
        start_response = server.make_json_response({"job_id": "job-123", "status": "queued"})
        status_response = server.make_json_response({"job_id": "job-123", "status": "queued"})

        with (
            patch.object(server, "get_configured_validation_job_timeout_seconds", return_value=654.0),
            patch.object(server, "xcos_start_validation", AsyncMock(return_value=start_response)) as start_mock,
            patch.object(server, "xcos_get_validation_status", AsyncMock(return_value=status_response)),
        ):
            response = await server.xcos_verify_draft(session_id)

        payload = json.loads(response[0].text)
        self.assertEqual(payload["status"], "queued")
        start_mock.assert_awaited_once_with(
            session_id,
            654.0,
            server.VALIDATION_PROFILE_FULL_RUNTIME,
        )

    async def test_xcos_verify_draft_accepts_hosted_smoke_profile(self):
        session_id = await self.start_session()
        start_response = server.make_json_response({"job_id": "job-456", "status": "queued"})
        status_response = server.make_json_response({"job_id": "job-456", "status": "queued"})

        with (
            patch.object(server, "get_configured_validation_job_timeout_seconds", return_value=654.0),
            patch.object(server, "xcos_start_validation", AsyncMock(return_value=start_response)) as start_mock,
            patch.object(server, "xcos_get_validation_status", AsyncMock(return_value=status_response)),
        ):
            response = await server.xcos_verify_draft(
                session_id,
                validation_profile=server.VALIDATION_PROFILE_HOSTED_SMOKE,
            )

        payload = json.loads(response[0].text)
        self.assertEqual(payload["status"], "queued")
        start_mock.assert_awaited_once_with(
            session_id,
            654.0,
            server.VALIDATION_PROFILE_HOSTED_SMOKE,
        )

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
        self.assertIn("validation_profile defaults to 'full_runtime'", by_name["xcos_start_validation"].description)
        self.assertIn("use 'hosted_smoke' for deploy-safe", by_name["xcos_verify_draft"].description)
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

    def test_scilab_log_parser_tracks_stage_trace_on_success(self):
        parsed = server.analyze_scilab_verification_output(
            "\n".join([
                "XCOSAI_VERIFY_STAGE:LOAD_XCOS_LIBS:BEGIN",
                "XCOSAI_VERIFY_STAGE:LOAD_XCOS_LIBS:END",
                "XCOSAI_VERIFY_STAGE:LOAD_SCICOS:BEGIN",
                "XCOSAI_VERIFY_STAGE:LOAD_SCICOS:END",
                "XCOSAI_VERIFY_STAGE:IMPORT_XCOS_DIAGRAM:BEGIN",
                "XCOSAI_VERIFY_STAGE:IMPORT_XCOS_DIAGRAM:END",
                "XCOSAI_VERIFY_STAGE:SCAN_BLOCKS:BEGIN",
                "XCOSAI_VERIFY_STAGE:SCAN_BLOCKS:END",
                "XCOSAI_VERIFY_STAGE:SCICOS_SIMULATE:BEGIN",
                "XCOSAI_VERIFY_STAGE:SCICOS_SIMULATE:END",
                "XCOSAI_VERIFY_OK",
            ]),
            0,
        )
        self.assertTrue(parsed["success"])
        self.assertEqual(parsed["last_completed_stage"], "SCICOS_SIMULATE")
        self.assertIsNone(parsed["active_stage"])
        self.assertEqual(parsed["stage_events"][-1], {"stage": "SCICOS_SIMULATE", "status": "END"})

    def test_scilab_log_parser_tracks_active_stage_on_failure(self):
        parsed = server.analyze_scilab_verification_output(
            "\n".join([
                "XCOSAI_VERIFY_STAGE:LOAD_XCOS_LIBS:BEGIN",
                "XCOSAI_VERIFY_STAGE:LOAD_XCOS_LIBS:END",
                "XCOSAI_VERIFY_STAGE:SCICOS_SIMULATE:BEGIN",
                "XCOSAI_VERIFY_ERROR:sim stalled",
            ]),
            1,
        )
        self.assertFalse(parsed["success"])
        self.assertEqual(parsed["active_stage"], "SCICOS_SIMULATE")
        self.assertEqual(parsed["last_completed_stage"], "LOAD_XCOS_LIBS")
        self.assertEqual(parsed["stage_events"][0], {"stage": "LOAD_XCOS_LIBS", "status": "BEGIN"})

    def test_apply_validation_progress_update_tracks_stage_transitions(self):
        details = server.apply_validation_progress_update({}, "LOAD_XCOS_LIBS", "BEGIN")
        self.assertEqual(details["scilab_active_stage"], "LOAD_XCOS_LIBS")
        self.assertEqual(
            details["scilab_stage_trace"],
            [{"stage": "LOAD_XCOS_LIBS", "status": "BEGIN"}],
        )

        details = server.apply_validation_progress_update(details, "LOAD_XCOS_LIBS", "END")
        self.assertIsNone(details["scilab_active_stage"])
        self.assertEqual(details["scilab_last_completed_stage"], "LOAD_XCOS_LIBS")
        self.assertEqual(
            details["scilab_stage_trace"][-1],
            {"stage": "LOAD_XCOS_LIBS", "status": "END"},
        )

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

    async def test_http_post_progress_updates_task_details_and_tracker(self):
        task_id = "task-progress"
        progress_tracker = server.create_validation_progress_tracker(server.VALIDATION_PROFILE_FULL_RUNTIME)
        state_entry = {
            "success": None,
            "error": "",
            "details": {},
            "event": asyncio.Event(),
            "progress_tracker": progress_tracker,
        }
        server.state.results[task_id] = state_entry

        class DummyRequest:
            async def body(self):
                return b'{"task_id":"task-progress","stage":"SCICOS_SIMULATE","status":"BEGIN"}'

        try:
            response = await server.http_handle_post_progress(DummyRequest())
            payload = json.loads(response.body.decode("utf-8"))

            self.assertEqual(response.status_code, 200)
            self.assertEqual(payload["status"], "received")
            self.assertEqual(
                server.state.results[task_id]["details"]["scilab_active_stage"],
                "SCICOS_SIMULATE",
            )
            self.assertEqual(progress_tracker["scilab_active_stage"], "SCICOS_SIMULATE")
            self.assertEqual(
                progress_tracker["scilab_stage_trace"][-1],
                {"stage": "SCICOS_SIMULATE", "status": "BEGIN"},
            )
        finally:
            server.state.results.pop(task_id, None)


if __name__ == "__main__":
    unittest.main()
