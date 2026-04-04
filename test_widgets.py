import json
import os
import tempfile
import unittest

import server


BLOCKS_XML = """
<BasicBlock id="b1" parent="0:2:0" interfaceFunctionName="CONST_m" style="CONST_m">
  <mxGeometry x="0" y="0" width="40" height="40" as="geometry"/>
</BasicBlock>
<ExplicitOutputPort id="p1" parent="b1" ordering="1" dataType="REAL_MATRIX" dataColumns="1" dataLines="1" value=""/>
<BasicBlock id="b2" parent="0:2:0" interfaceFunctionName="CMSCOPE" style="CMSCOPE">
  <mxGeometry x="200" y="0" width="40" height="40" as="geometry"/>
</BasicBlock>
<ExplicitInputPort id="p2" parent="b2" ordering="1" dataType="REAL_MATRIX" dataColumns="1" dataLines="1" value=""/>
""".strip()


LINKS_XML = """
<ExplicitLink id="l1" source="p1" target="p2" parent="0:2:0" style="ExplicitLink" />
""".strip()


class WidgetTests(unittest.IsolatedAsyncioTestCase):
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
        server.state.drafts.clear()
        server.state.phase_plans.clear()
        server.state.workflows.clear()
        server.state.draft_to_workflow.clear()
        server.state.validation_jobs.clear()
        for task in list(server.state.validation_tasks.values()):
            task.cancel()
        server.state.validation_tasks.clear()

    def tearDown(self):
        for task in list(server.state.validation_tasks.values()):
            task.cancel()
        server.state.validation_tasks.clear()
        for key, value in self.old_dirs.items():
            setattr(server, key, value)
        self.tempdir.cleanup()

    async def test_topology_widget_reports_counts_after_blocks_and_links(self):
        start_response = await server.xcos_start_draft(session_id="widget-session")
        start_payload = json.loads(start_response[0].text)
        self.assertTrue(start_payload["created"])

        add_blocks_response = await server.xcos_add_blocks("widget-session", BLOCKS_XML)
        add_blocks_payload = json.loads(add_blocks_response[0].text)
        self.assertEqual(add_blocks_payload["added_block_count"], 2)
        self.assertEqual(add_blocks_payload["block_count"], 2)
        self.assertEqual(add_blocks_payload["link_count"], 0)

        blocks_response = await server.xcos_get_topology_widget("widget-session")
        blocks_payload = json.loads(blocks_response[0].text)
        self.assertEqual(blocks_payload["payload"]["session_id"], "widget-session")
        self.assertEqual(blocks_payload["payload"]["block_count"], 2)
        self.assertEqual(blocks_payload["payload"]["link_count"], 0)
        self.assertIn("<svg", blocks_payload["payload"]["svg"])
        self.assertIn("data:image/", blocks_payload["payload"]["svg"])
        self.assertIn("/api/topology/widget-session/svg", blocks_response[1].text)

        add_links_response = await server.xcos_add_links("widget-session", LINKS_XML)
        add_links_payload = json.loads(add_links_response[0].text)
        self.assertEqual(add_links_payload["added_link_count"], 1)
        self.assertEqual(add_links_payload["block_count"], 2)
        self.assertEqual(add_links_payload["link_count"], 1)

        links_response = await server.xcos_get_topology_widget("widget-session")
        links_payload = json.loads(links_response[0].text)
        self.assertEqual(links_payload["payload"]["block_count"], 2)
        self.assertEqual(links_payload["payload"]["link_count"], 1)
        self.assertIn('marker-end="url(#arrow)"', links_payload["payload"]["svg"])

    async def test_parse_mcp_text_json_response_scans_for_json_text(self):
        response = [
            server.mcp_types.TextContent(type="text", text="Summary only"),
            server.mcp_types.TextContent(type="text", text='{"ok": true}')
        ]
        payload = server.parse_mcp_text_json_response(response)
        self.assertTrue(payload["ok"])

    async def test_block_catalogue_widget_accepts_multiple_categories(self):
        response = await server.xcos_get_block_catalogue_widget("Sources, Continuous")
        payload = json.loads(response[0].text)
        self.assertEqual(payload["payload"]["categories"], ["Sources", "Continuous"])
        names = {block["name"] for block in payload["payload"]["blocks"]}
        self.assertIn("CONST_m", names)
        self.assertIn("INTEGRAL_m", names)


if __name__ == "__main__":
    unittest.main()
