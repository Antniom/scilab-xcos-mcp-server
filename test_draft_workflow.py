import asyncio
import base64
import json
import os
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

    def tearDown(self):
        for session_id in self.created_sessions:
            server.state.drafts.pop(session_id, None)
            server.state.phase_plans.pop(session_id, None)

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

        list_response = await server.xcos_list_sessions()
        sessions = json.loads(list_response[0].text)["sessions"]
        session_meta = next(item for item in sessions if item["session_id"] == session_id)

        self.assertTrue(session_meta["last_verified"]["success"])
        self.assertEqual(session_meta["last_verified"]["task_id"], "mock-task")

    async def test_block_data_includes_split_and_extra_examples(self):
        split_response = await server.get_xcos_block_data("SPLIT_f")
        split_payload = json.loads(split_response[0].text)
        self.assertIsNotNone(split_payload["info"])
        self.assertIn("SplitBlock", json.dumps(split_payload["info"]))

        cmscope_response = await server.get_xcos_block_data("CMSCOPE")
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
        self.assertIn("If validation still fails after 3 repair attempts", prompt_text)
        self.assertIn("Step 30a.", prompt_text)

    async def test_build_xcos_diagram_prompt_requires_problem_statement(self):
        with self.assertRaises(ValueError):
            await server.handle_get_prompt(server.BUILD_XCOS_DIAGRAM_PROMPT_NAME, {})

    async def test_initialization_options_include_prompts_capability(self):
        options = server.create_server_initialization_options()

        self.assertIsNotNone(options.capabilities.prompts)
        self.assertFalse(options.capabilities.prompts.listChanged)


if __name__ == "__main__":
    unittest.main()
