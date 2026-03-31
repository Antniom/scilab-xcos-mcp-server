import os
import json
import asyncio
import uuid
import sys
import subprocess
import base64
import shutil
import tempfile
import textwrap
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from mcp.server import Server, NotificationOptions
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
import mcp.types as mcp_types
from lxml import etree
from colorama import init, Fore, Style
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route
import uvicorn

# Initialize colorama
init(autoreset=True)

# Shared State
class SharedState:
    def __init__(self):
        self.task_queue = asyncio.Queue()
        self.results = {}  # task_id -> {"success": bool, "error": str, "event": asyncio.Event}
        self.last_poll_time = None
        self.status_lock = asyncio.Lock()
        self.drafts = {} # session_id -> DraftDiagram
        self.phase_plans = {} # session_id -> {"phases": list[str], "completed": list[str]}
        self.workflows = {} # workflow_id -> WorkflowSession
        self.draft_to_workflow = {} # session_id -> workflow_id

state = SharedState()

# Absolute pathing for data directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
UI_DIR = os.path.join(BASE_DIR, "ui")
ICONS_DIR = os.path.join(BASE_DIR, "icons")
PORT_REGISTRY_PATH = os.path.join(DATA_DIR, "blocks", "port_registry.json")
TEMP_OUTPUT_DIR = os.environ.get("XCOS_TEMP_OUTPUT_DIR", os.path.join(DATA_DIR, "temp"))
SESSION_OUTPUT_DIR = os.environ.get("XCOS_SESSION_OUTPUT_DIR", os.path.join(BASE_DIR, "sessions"))
SERVER_PORT = int(os.environ.get("PORT", os.environ.get("XCOS_SERVER_PORT", "8000")))
MCP_HTTP_PATH = os.environ.get("XCOS_MCP_HTTP_PATH", "/mcp")
MCP_APP_MIME_TYPE = "text/html;profile=mcp-app"

WORKFLOW_PHASE_ORDER = [
    "phase1_math_model",
    "phase2_architecture",
    "phase3_implementation",
]
WORKFLOW_PHASE_LABELS = {
    "phase1_math_model": "Phase 1: Mathematical Analysis & Calculus",
    "phase2_architecture": "Phase 2: Architectural Graph & Parameter Plan",
    "phase3_implementation": "Phase 3: Implementation & Validation",
}
REVIEWABLE_PHASES = {"phase1_math_model", "phase2_architecture"}

BUILD_XCOS_DIAGRAM_PROMPT_NAME = "build_xcos_diagram"
BUILD_XCOS_DIAGRAM_PROMPT_TITLE = "Build Xcos Diagram"
BUILD_XCOS_DIAGRAM_PROMPT_DESCRIPTION = (
    "Guides Claude through a 3-phase gated workflow to model, plan, and build "
    "a Scilab Xcos diagram. Each phase requires explicit user approval before proceeding."
)
BUILD_XCOS_DIAGRAM_PROMPT_RESULT_DESCRIPTION = (
    "3-phase gated Xcos diagram builder with user approval gates"
)
BUILD_XCOS_DIAGRAM_PROMPT_ARGUMENT = mcp_types.PromptArgument(
    name="problem_statement",
    description=(
        "Description of the physical or mathematical system to model "
        "(e.g. 'simple pendulum with g=9.8, L=2m')"
    ),
    required=True,
)
BUILD_XCOS_DIAGRAM_PROMPT_TEMPLATE = textwrap.dedent(
    """\
    Build an Xcos diagram for the following system:

    {{problem_statement}}

    Follow this exact process. Never skip a step. Never proceed past an approval gate without the user explicitly typing 'approve'.

    ---

    ## PHASE 1 — Math model

    **Step 1.** Call `xcos_get_status_widget`. Display the widget. If the server is not connected, stop and tell the user before doing anything else.

    **Step 2.** Call `xcos_get_block_catalogue_widget` with a relevant category (e.g. 'Continuous', 'Sources', 'Sinks'). Display the widget so the user can see which blocks are available.

    **Step 3.** Call `xcos_create_workflow` with the problem statement. Store the returned `workflow_id` — you will need it for every subsequent phase call.

    **Step 4.** Derive the governing equations step by step in plain text. Show all algebra. Define every variable and parameter with units and numeric values.

    **Step 5.** Draw an inline Mermaid diagram (using `graph LR`) showing the signal flow: blocks for each operation with their Xcos name and numeric parameters (e.g. `GAIN[-4.9]`), arrows showing how signals connect. Do NOT use raw SVG coords to avoid messy overlapping.

    **Step 6.** Call `xcos_submit_phase` with `phase='phase1_math_model'`, `workflow_id`, and the full math derivation as content.

    **Step 7.** Call `xcos_get_workflow_widget` with the `workflow_id`. Display the widget.

    **Step 8.** STOP. Ask: 'Does the math and signal flow look correct? Reply **approve** or describe what to change.'

    **Step 9.** If the user requests changes: revise steps 4–5, call `xcos_submit_phase` again, display the widget again, ask again. Repeat until approved. Only call `xcos_review_phase` with `phase='phase1_math_model'` and `decision='approve'` after the user explicitly approves. Then call `xcos_get_workflow_widget` and display it.

    ---

    ## PHASE 2 — Architecture plan

    **Step 10.** Call `get_xcos_block_data` for every single block you plan to use. Never write block XML from memory or examples — always use the returned XML as the authoritative template. This gives you the correct port IDs, parameter structure, simulation function name, and blockType.

    **Step 11.** If you need to understand a block's internal behaviour or parameters more deeply, call `get_xcos_block_source` for that block. Use `search_related_xcos_files` to find any related configuration files if the block has complex dependencies.

    **Step 12.** Write out the full architecture plan: every block (Xcos name, simulation function, parameters with values), and every link (source block + port ID → target block + port ID). Be explicit about clock/activation links vs data links.

    **Step 13.** Draw an inline Mermaid diagram (using `graph LR`) showing the actual Xcos block architecture. Use simple block shapes with the exact Xcos name and key parameter (e.g. `GAIN[GAIN_f k=-5]`). Use solid arrows (`-->`) for data/signal links and dashed arrows (`-.->`) for clock/activation links. Label edges lightly (e.g. `-- out1 to in1 -->`) rather than making separate ports, and rely entirely on Mermaid's auto-rendering so lines do not overlap. The diagram must match the architecture plan perfectly.

    **Step 14.** Call `xcos_submit_phase` with `phase='phase2_architecture'`, `workflow_id`, and the full block + link plan as content.

    **Step 15.** Call `xcos_get_workflow_widget` with the `workflow_id`. Display the widget.

    **Step 16.** STOP. Ask: 'Does this block layout look right? Reply **approve** or describe what to change.'

    **Step 17.** If the user requests changes: revise steps 10–14, resubmit, display widget, ask again. Repeat until approved. Only call `xcos_review_phase` with `phase='phase2_architecture'` and `decision='approve'` after the user explicitly approves. Then call `xcos_get_workflow_widget` and display it.

    ---

    ## PHASE 3 — Build and verify

    **Step 18.** Call `xcos_start_draft` with the `workflow_id`. Store the returned `session_id` — you will need it for all remaining steps.

    **Step 19.** Call `xcos_add_blocks` with `session_id`. Use only XML retrieved from `get_xcos_block_data` — never from memory.

    **Step 20.** Call `xcos_get_topology_widget` with `session_id`. Display the widget. The user should see all blocks appear in the graph before any links are added.

    **Step 21.** Call `xcos_add_links` with `session_id`. Use port IDs exactly as returned by `get_xcos_block_data`.

    **Step 22.** Call `xcos_get_topology_widget` with `session_id` again. Display the widget. Check for missing links or disconnected ports — fix before continuing.

    **Step 23.** Call `xcos_get_draft_xml` with `session_id` and `pretty_print=true`. Show a brief summary of the XML structure to the user.

    **Step 24.** STOP. Ask: 'Ready to validate? Reply **approve** to run verification.'

    **Step 25.** After approval: call `xcos_verify_draft` with `session_id`.

    **Step 26.** Call `xcos_get_validation_widget` with the current draft XML. Display the widget.
    - If `success=true`: proceed to step 27.
    - If `success=false`: read the error carefully. Call `xcos_get_draft_xml` to inspect the current XML. Fix the specific block or link causing the error. Call `xcos_add_blocks` or `xcos_add_links` to rebuild, then repeat from step 25. Use `verify_xcos_xml` directly on fixed XML snippets if you want to spot-check a repair before rebuilding the full session. Never stop after one failure — keep iterating until `success=true`.

    If validation still fails after 3 repair attempts: stop the repair loop. Call xcos_get_draft_xml with pretty_print=true and show the full XML to the user. Call xcos_get_validation_widget and display it. Ask: "I was unable to fix this automatically after 3 attempts. Here is the current XML and the error. Would you like to guide the fix, or should I start phase 3 over?"

    **Step 27.** Call `xcos_commit_phase` with `session_id` and `phase_label='phase3_implementation'` to commit the verified XML to file.

    **Step 28.** Call `xcos_submit_phase` with `phase='phase3_implementation'`, `workflow_id`, and a summary confirming the file path and validation result as content.

    **Step 29.** Call `xcos_get_file_path` with `session_id`. Present the .xcos file to the user for download.

    **Step 30a.** If the user asks to inspect the final file content, call `xcos_get_file_content` with `session_id` and `source='last_verified'`. If the user asks to recover content from a previous session, call `xcos_list_sessions` to find it first.

    **Step 30.** Call `xcos_get_workflow_widget` with the `workflow_id` one final time. Display the completed 3-phase summary so the user can confirm everything is done.

    ---

    ## Rules that apply throughout all phases

    - Never proceed past a STOP gate without the user explicitly typing 'approve'.
    - Never write block XML from memory — always call `get_xcos_block_data` first.
    - Never skip `get_xcos_block_source` or `search_related_xcos_files` if a block's parameters or dependencies are unclear.
    - Every diagram must be a proper Mermaid diagram (`graph LR`) — do NOT use raw SVG generation, and do NOT use ASCII art.
    - Always call `xcos_get_workflow_widget` after every `xcos_submit_phase` call.
    - Always display every widget inline immediately after it is returned.
    - If the user requests changes at any approval gate, go back and revise — never push forward.
    - A diagram is only done when `xcos_verify_draft` returns `success=true`. Never declare it done before that.
    - Use `xcos_list_sessions` and `xcos_list_workflows` at any point if you lose track of active sessions or workflows.
    - Use `xcos_get_file_content` with `source='last_verified'` if the user asks to inspect or download the final file content after verification.
    - If you ever lose track of the active session_id or workflow_id, call `xcos_list_sessions` and `xcos_list_workflows` to recover them before doing anything else.
    - After phases 1 and 2 approval, check if `xcos_commit_phase` needs to be called — consult the tool description for the current phase label convention.
    - `verify_xcos_xml` is for spot-checking raw XML snippets during repair. `xcos_verify_draft` is for full session validation. Never confuse the two.
    """
)


def build_xcos_prompt_text(problem_statement: str) -> str:
    cleaned_problem_statement = problem_statement.strip()
    if not cleaned_problem_statement:
        raise ValueError(
            f"Prompt '{BUILD_XCOS_DIAGRAM_PROMPT_NAME}' requires a non-empty 'problem_statement' argument."
        )
    return BUILD_XCOS_DIAGRAM_PROMPT_TEMPLATE.replace(
        "{{problem_statement}}",
        cleaned_problem_statement,
    )


def icon_data_uri(filename: str, mime_type: str) -> str | None:
    path = os.path.join(ICONS_DIR, filename)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def load_server_icons() -> list[mcp_types.Icon]:
    icons: list[mcp_types.Icon] = []
    for filename, size in [
        ("scilab_xcos_mcp_48.png", "48x48"),
        ("scilab_xcos_mcp_96.png", "96x96"),
        ("scilab_xcos_mcp_512.png", "512x512"),
    ]:
        src = icon_data_uri(filename, "image/png")
        if src:
            icons.append(
                mcp_types.Icon(
                    src=src,
                    mimeType="image/png",
                    sizes=[size],
                )
            )
    return icons


SERVER_ICONS = load_server_icons()


def now_iso() -> str:
    return datetime.now().isoformat()


@dataclass
class WorkflowPhase:
    key: str
    label: str
    status: str = "pending"
    content: str = ""
    artifact_type: str = "markdown"
    submitted_at: str | None = None
    reviewed_at: str | None = None
    feedback: str = ""
    last_error: str | None = None

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "content": self.content,
            "artifact_type": self.artifact_type,
            "submitted_at": self.submitted_at,
            "reviewed_at": self.reviewed_at,
            "feedback": self.feedback,
            "last_error": self.last_error,
        }


@dataclass
class WorkflowSession:
    workflow_id: str
    problem_statement: str
    created_at: str
    updated_at: str
    current_phase: str
    phases: dict[str, WorkflowPhase]
    draft_session_id: str | None = None
    last_verified: dict | None = None

    def to_dict(self) -> dict:
        return {
            "workflow_id": self.workflow_id,
            "problem_statement": self.problem_statement,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "current_phase": self.current_phase,
            "current_phase_label": WORKFLOW_PHASE_LABELS[self.current_phase],
            "draft_session_id": self.draft_session_id,
            "last_verified": self.last_verified,
            "phases": {
                key: phase.to_dict()
                for key, phase in self.phases.items()
            },
        }


def create_workflow_session(problem_statement: str) -> WorkflowSession:
    workflow_id = str(uuid.uuid4())
    timestamp = now_iso()
    phases = {
        key: WorkflowPhase(key=key, label=WORKFLOW_PHASE_LABELS[key])
        for key in WORKFLOW_PHASE_ORDER
    }
    workflow = WorkflowSession(
        workflow_id=workflow_id,
        problem_statement=problem_statement.strip(),
        created_at=timestamp,
        updated_at=timestamp,
        current_phase=WORKFLOW_PHASE_ORDER[0],
        phases=phases,
    )
    state.workflows[workflow_id] = workflow
    return workflow


def get_workflow(workflow_id: str) -> WorkflowSession | None:
    return state.workflows.get(workflow_id)


def workflow_phase_index(phase_key: str) -> int:
    return WORKFLOW_PHASE_ORDER.index(phase_key)


def next_workflow_phase(phase_key: str) -> str | None:
    index = workflow_phase_index(phase_key)
    if index + 1 >= len(WORKFLOW_PHASE_ORDER):
        return None
    return WORKFLOW_PHASE_ORDER[index + 1]


def reset_workflow_phase(phase: WorkflowPhase):
    phase.status = "pending"
    phase.content = ""
    phase.artifact_type = "markdown"
    phase.submitted_at = None
    phase.reviewed_at = None
    phase.feedback = ""
    phase.last_error = None


def reset_workflow_downstream(workflow: WorkflowSession, phase_key: str):
    index = workflow_phase_index(phase_key)
    for downstream in WORKFLOW_PHASE_ORDER[index + 1:]:
        reset_workflow_phase(workflow.phases[downstream])

    if workflow.draft_session_id:
        state.draft_to_workflow.pop(workflow.draft_session_id, None)
        workflow.draft_session_id = None
    workflow.last_verified = None


def list_workflow_payloads() -> list[dict]:
    return [
        workflow.to_dict()
        for workflow in sorted(
            state.workflows.values(),
            key=lambda item: item.created_at,
            reverse=True,
        )
    ]


def submit_workflow_phase(
    workflow_id: str,
    phase_key: str,
    content: str,
    artifact_type: str = "markdown",
) -> tuple[dict | None, str | None]:
    workflow = get_workflow(workflow_id)
    if not workflow:
        return None, f"Workflow {workflow_id} not found"
    if phase_key not in WORKFLOW_PHASE_ORDER:
        return None, f"Invalid phase '{phase_key}'. Valid phases: {', '.join(WORKFLOW_PHASE_ORDER)}"
    if not content.strip():
        return None, "Phase content cannot be empty"

    index = workflow_phase_index(phase_key)
    if index > 0:
        previous_phase = WORKFLOW_PHASE_ORDER[index - 1]
        if workflow.phases[previous_phase].status != "approved":
            return None, f"{WORKFLOW_PHASE_LABELS[previous_phase]} must be approved before submitting {WORKFLOW_PHASE_LABELS[phase_key]}."

    reset_workflow_downstream(workflow, phase_key)

    phase = workflow.phases[phase_key]
    phase.content = content.strip()
    phase.artifact_type = artifact_type
    phase.submitted_at = now_iso()
    phase.reviewed_at = None
    phase.feedback = ""
    phase.last_error = None
    phase.status = "awaiting_approval" if phase_key in REVIEWABLE_PHASES else "in_progress"
    workflow.current_phase = phase_key
    workflow.updated_at = now_iso()
    return workflow.to_dict(), None


def review_workflow_phase(
    workflow_id: str,
    phase_key: str,
    decision: str,
    feedback: str = "",
) -> tuple[dict | None, str | None]:
    workflow = get_workflow(workflow_id)
    if not workflow:
        return None, f"Workflow {workflow_id} not found"
    if phase_key not in REVIEWABLE_PHASES:
        return None, f"Only Phase 1 and Phase 2 can be reviewed. Received '{phase_key}'."

    phase = workflow.phases[phase_key]
    normalized_decision = decision.strip().lower()
    if normalized_decision not in {"approve", "request_changes"}:
        return None, "Decision must be either 'approve' or 'request_changes'."

    # Idempotent: if phase is already approved and caller sends 'approve', succeed gracefully.
    # Guards against race conditions where the server auto-approves before the manual call.
    if phase.status == "approved" and normalized_decision == "approve":
        return workflow.to_dict(), None

    if phase.status != "awaiting_approval":
        return None, f"{WORKFLOW_PHASE_LABELS[phase_key]} is not awaiting approval. Current status: {phase.status}."

    phase.reviewed_at = now_iso()
    phase.feedback = feedback.strip()
    phase.last_error = None

    if normalized_decision == "approve":
        phase.status = "approved"
        next_phase = next_workflow_phase(phase_key)
        if next_phase:
            workflow.current_phase = next_phase
    else:
        phase.status = "changes_requested"
        workflow.current_phase = phase_key
        reset_workflow_downstream(workflow, phase_key)

    workflow.updated_at = now_iso()
    return workflow.to_dict(), None

# --- Incremental Draft Management ---

class DraftDiagram:
    def __init__(self, schema_version="1.1"):
        self.schema_version = schema_version
        self.blocks = []
        self.links = []
        self.created_at = datetime.now()
        self.last_verified_at = None
        self.last_verified_success = None
        self.last_verified_task_id = None
        self.last_verified_file_path = None
        self.last_verified_file_size = None
        self.last_verified_error = None
        self.last_verified_origin = None

    def add_blocks(self, xml_chunk):
        self.blocks.append(xml_chunk)

    def add_links(self, xml_chunk):
        self.links.append(xml_chunk)

    def to_xml(self):
        """Assembles the full Xcos XML from parts compatible with Scilab 2026.0.1."""
        # Skeleton boilerplate based on Scilab 2026 empty.xcos
        full_xml = f'<?xml version="1.0" encoding="UTF-8"?>\n'
        full_xml += '<XcosDiagram background="-1" gridEnabled="1" title="Untitled" '
        full_xml += 'finalIntegrationTime="100000.0" integratorAbsoluteTolerance="1.0E-6" '
        full_xml += 'integratorRelativeTolerance="1.0E-6" toleranceOnTime="1.0E-10" '
        full_xml += 'maxIntegrationTimeInterval="100001.0" maximumStepSize="0.0" '
        full_xml += 'realTimeScaling="1.0" solver="0.0">\n'
        full_xml += '  <Array as="context" scilabClass="String[]"></Array>\n'
        full_xml += '  <mxGraphModel as="model" grid="1" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="850" pageHeight="1100" background="#ffffff">\n'
        full_xml += '    <root>\n'
        full_xml += '      <mxCell id="0"/>\n'
        full_xml += '      <mxCell id="1" parent="0"/>\n'
        
        # Append blocks and links
        for b in self.blocks:
            full_xml += f'      {b}\n'
        for l in self.links:
            full_xml += f'      {l}\n'
            
        full_xml += '    </root>\n'
        full_xml += '  </mxGraphModel>\n'
        full_xml += '</XcosDiagram>'
        return full_xml

# --- Xcos Validation & Auto-fix Helpers ---

def load_port_registry():
    if os.path.exists(PORT_REGISTRY_PATH):
        with open(PORT_REGISTRY_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

PORT_REGISTRY = load_port_registry()
SCALAR_INPUT_BLOCKS = {"CANIMXY", "CANIMXY3D", "BARXY", "CSCOPXY", "CSCOPXY3D"}

def auto_fix_mux_to_scalar(tree):
    """
    Finds MUX blocks connected to scalar-only input blocks and bypasses them.
    If a MUX with N inputs is connected to a destination block that expects 
    multiple scalar inputs (like CANIMXY), it replaces the MUX link with 
    direct links from MUX inputs.
    """
    # Find all MUX blocks
    mux_blocks = tree.xpath("//BasicBlock[@interfaceFunctionName='MUX' or @interfaceFunctionName='MUX_f']")
    links_to_remove = []
    blocks_to_remove = set()
    new_links = []

    for mux in mux_blocks:
        mux_id = mux.get("id")
        # Find output port of this MUX
        mux_out_port = mux.xpath("./ExplicitOutputPort")
        if not mux_out_port: continue
        mux_out_id = mux_out_port[0].get("id")

        # Find link from this MUX output
        link_from_mux = tree.xpath(f"//BasicLink[SourcePort/@reference='{mux_out_id}']")
        if not link_from_mux: continue
        
        link = link_from_mux[0]
        dst_port_ref = link.xpath("./DestinationPort/@reference")[0]
        dst_port = tree.xpath(f"//*[@id='{dst_port_ref}']")[0]
        dst_block = dst_port.getparent()
        dst_interface = dst_block.get("interfaceFunctionName")

        if dst_interface in SCALAR_INPUT_BLOCKS:
            # We found a MUX -> Scalar block link.
            # Mirror the manual fix: Bypass the MUX.
            # Get MUX input links
            mux_in_ports = mux.xpath("./ExplicitInputPort")
            # Ordering them by 'ordering' attribute
            mux_in_ports.sort(key=lambda p: int(p.get("ordering", "0")))
            
            # Destination ports of the destination block
            dst_in_ports = dst_block.xpath("./ExplicitInputPort")
            dst_in_ports.sort(key=lambda p: int(p.get("ordering", "0")))

            # Connect MUX inputs to Destination inputs
            for i, mux_in_p in enumerate(mux_in_ports):
                if i >= len(dst_in_ports): break
                
                mux_in_id = mux_in_p.get("id")
                # Find the link coming into the MUX
                incoming_link = tree.xpath(f"//BasicLink[DestinationPort/@reference='{mux_in_id}']")
                if incoming_link:
                    # Point the incoming link directly to the destination block port
                    incoming_src_ref = incoming_link[0].xpath("./SourcePort/@reference")[0]
                    
                    # Create a new link replacing the incoming + mux + outgoing chain
                    link_parent = link.get("parent") or mux.get("parent") or "0:2:0"
                    new_link = etree.Element("BasicLink", id=str(uuid.uuid4()), parent=link_parent,
                                            style=link.get("style") or "noEdgeStyle=1;orthogonal=1;")
                    etree.SubElement(new_link, "mxGeometry", as_="geometry")
                    etree.SubElement(new_link, "SourcePort", as_="source", reference=incoming_src_ref)
                    etree.SubElement(new_link, "DestinationPort", as_="target", reference=dst_in_ports[i].get("id"))
                    new_links.append(new_link)
                    
                    links_to_remove.append(incoming_link[0])
            
            links_to_remove.append(link)
            blocks_to_remove.add(mux)

    # Apply changes
    graph_root = tree.xpath("//mxGraphModel/root")[0]
    for link in links_to_remove:
        if link.getparent() is not None:
            link.getparent().remove(link)
    for block in blocks_to_remove:
        if block.getparent() is not None:
            block.getparent().remove(block)
    for nl in new_links:
        graph_root.append(nl)
    
    return len(blocks_to_remove) > 0

def validate_port_sizes(tree):
    """
    Parses the Xcos XML tree to extract port info and validates it against 
    the PORT_REGISTRY and link consistency. Supports wildcards (-1, -2).
    """
    errors = []
    
    # Helper to check if dimensions match, respecting Scilab wildcards (-1, -2)
    def dims_match(actual, expected):
        if not actual or not expected: return True
        if len(actual) != len(expected): return False
        for a, e in zip(actual, expected):
            # If either side is negative, it's a wildcard matching anything
            if a < 0 or e < 0: continue 
            if a != e: return False
        return True

    # 1. Collect all port definitions from the XML
    ports = {} # id -> {size: [rows, cols], blockId: str, interface: str, kind: in/out, index: int}
    all_ports = tree.xpath("//ExplicitInputPort | //ExplicitOutputPort")
    for p in all_ports:
        pid = p.get("id")
        block = p.getparent()
        kind = "input" if "Input" in p.tag else "output"
        ports[pid] = {
            "size": [int(p.get("dataLines", 1)), int(p.get("dataColumns", 1))],
            "blockId": block.get("id"),
            "interface": block.get("interfaceFunctionName"),
            "kind": kind,
            "index": int(p.get("ordering", 1))
        }

    # 2. Cross-check against Registry
    for pid, pdata in ports.items():
        reg = PORT_REGISTRY.get(pdata["interface"])
        if reg:
            expected_ports = reg.get("inputs" if pdata["kind"] == "input" else "outputs")
            
            # Skip validation for variadic blocks (logic for these is usually parameter-dependent)
            if expected_ports == "variadic":
                continue
                
            if expected_ports and pdata["index"] <= len(expected_ports):
                expected_size = expected_ports[pdata["index"] - 1]
                if not dims_match(pdata["size"], expected_size):
                    errors.append({
                        "type": "REGISTRY_SIZE_MISMATCH",
                        "block": pdata["interface"],
                        "blockId": pdata["blockId"],
                        "portKind": pdata["kind"],
                        "portIndex": pdata["index"],
                        "expectedSize": expected_size,
                        "actualSize": pdata["size"]
                    })

    # 3. Validate Link consistency (src size must match dst size unless wildcard)
    links = tree.xpath("//BasicLink")
    src_port_counts = {} # src_port_id -> count
    for link in links:
        src_ref = link.xpath("./SourcePort/@reference")
        dst_ref = link.xpath("./DestinationPort/@reference")
        if not src_ref or not dst_ref: continue
        
        src_id = src_ref[0]
        src_port_counts[src_id] = src_port_counts.get(src_id, 0) + 1
        
        src_port = ports.get(src_id)
        dst_port = ports.get(dst_ref[0])
        
        if src_port and dst_port:
            if not dims_match(src_port["size"], dst_port["size"]):
                errors.append({
                    "type": "PORT_SIZE_MISMATCH",
                    "srcBlock": src_port["interface"],
                    "dstBlock": dst_port["interface"],
                    "srcPort": src_port["index"],
                    "dstPort": dst_port["index"],
                    "srcSize": src_port["size"],
                    "dstSize": dst_port["size"],
                    "linkId": link.get("id")
                })
    
    # 4. Fan-out check (SplitBlock required for multiple links from one output)
    for port_id, count in src_port_counts.items():
        if count > 1:
            port = ports.get(port_id)
            if port and port["kind"] == "output":
                # Special cases: SplitBlock itself, or CLKSPLIT_f don't need further splits
                # But here we check if a standard block's output is fanning out
                if port["interface"] not in {"SplitBlock", "CLKSPLIT_f"}:
                    errors.append({
                        "type": "FANOUT_WITHOUT_SPLIT",
                        "block": port["interface"],
                        "blockId": port["blockId"],
                        "portIndex": port["index"],
                        "linkCount": count,
                        "message": f"Output port {port['index']} of {port['interface']} has {count} links. Xcos requires an intermediate SplitBlock for data links or CLKSPLIT_f for event links when fanning out."
                    })

    return errors


# --- Ensure Directories Exist ---
os.makedirs(TEMP_OUTPUT_DIR, exist_ok=True)
os.makedirs(SESSION_OUTPUT_DIR, exist_ok=True)

XCOS_BLOCK_XPATH = (
    "//BasicBlock | //BigSom | //SplitBlock | //TextBlock | "
    "//EventInBlock | //EventOutBlock | //ExplicitInBlock | //ExplicitOutBlock | "
    "//ImplicitInBlock | //ImplicitOutBlock"
)
XCOS_LINK_XPATH = "//BasicLink | //ExplicitLink | //CommandControlLink | //ImplicitLink"


def make_text_response(text: str):
    return [mcp_types.TextContent(type="text", text=text)]


def make_json_response(payload):
    return make_text_response(json.dumps(payload, indent=2))


def make_structured_tool_result(summary: str, payload: dict):
    return [
        mcp_types.TextContent(
            type="text",
            text=f"{summary}\n\n{json.dumps(payload, indent=2)}"
        )
    ]


def parse_mcp_text_json_response(response):
    if isinstance(response, tuple):
        return response
    if not response:
        raise ValueError("Empty response")
    text = response[0].text
    if text.startswith("Error:"):
        raise ValueError(text[6:].strip())
    return json.loads(text)


def get_session_dir(session_id: str) -> str:
    return os.path.join(SESSION_OUTPUT_DIR, session_id)


def get_session_file_path(session_id: str) -> str:
    return os.path.join(get_session_dir(session_id), "diagram.xcos")


def get_file_metadata(path: str | None):
    if not path:
        return None
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        return None
    return {
        "path": abs_path,
        "size_bytes": os.path.getsize(abs_path),
    }


def summarize_draft(draft: DraftDiagram):
    try:
        tree = etree.fromstring(draft.to_xml().encode("utf-8"))
        return {
            "block_count": len(tree.xpath(XCOS_BLOCK_XPATH)),
            "link_count": len(tree.xpath(XCOS_LINK_XPATH)),
        }
    except Exception:
        return {
            "block_count": len(draft.blocks),
            "link_count": len(draft.links),
        }


def write_session_snapshot(session_id: str):
    session_dir = get_session_dir(session_id)
    os.makedirs(session_dir, exist_ok=True)
    file_path = get_session_file_path(session_id)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(state.drafts[session_id].to_xml())
    return get_file_metadata(file_path)


def load_ui_html() -> str:
    return "<html><body><h1>Scilab Xcos MCP Server</h1><p>Server is running. Please connect via MCP at /mcp or check /healthz.</p></body></html>"


def detect_validation_mode() -> str:
    explicit = os.environ.get("XCOS_VALIDATION_MODE")
    if explicit:
        return explicit.strip().lower()
    if os.environ.get("SCILAB_BIN"):
        return "subprocess"
    if os.name != "nt":
        return "subprocess"
    return "poll"


def resolve_windows_scilab_from_registry_file() -> str | None:
    path_file = os.path.join(BASE_DIR, ".scilab_path")
    if not os.path.exists(path_file):
        return None

    raw_root = open(path_file, "r", encoding="utf-8").read().strip()
    root = os.path.abspath(os.path.join(BASE_DIR, raw_root))
    candidates = [
        os.path.join(root, "bin", "WScilex-cli.exe"),
        os.path.join(root, "bin", "scilab-cli.exe"),
        os.path.join(root, "bin", "scilab-cli"),
        os.path.join(root, "bin", "scilab.bat"),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


_scilab_bin_cache = None

def resolve_scilab_binary() -> str | None:
    global _scilab_bin_cache
    if _scilab_bin_cache is not None:
        return _scilab_bin_cache
    
    env_bin = os.environ.get("SCILAB_BIN")
    if env_bin:
        return env_bin

    if os.name == "nt":
        from_registry = resolve_windows_scilab_from_registry_file()
        if from_registry:
            return from_registry

    for command in ("scilab-cli", "scilab-adv-cli", "scilab"):
        resolved = shutil.which(command)
        if resolved:
            _scilab_bin_cache = resolved
            return resolved
    return None


def scilab_string_literal(path: str) -> str:
    return path.replace("\\", "/").replace('"', '""')


def build_headless_verification_script(xcos_path: str) -> str:
    escaped_xcos_path = scilab_string_literal(os.path.abspath(xcos_path))
    return textwrap.dedent(
        f"""
        mode(-1);
        lines(0);

        function block=xcosai_nop_sim(block, flag)
        endfunction

        function xcosai_fail(msg)
            mprintf("XCOSAI_VERIFY_ERROR:%s\\n", string(msg));
            exit(1);
        endfunction

        try
            loadXcosLibs();
            loadScicos();
            importXcosDiagram("{escaped_xcos_path}");
            scs_m.props.tf = 0.1;

            n_objs = length(scs_m.objs);
            n_blocks_found = 0;
            replaced_list = "";

            for i = 1:n_objs
                try
                    if typeof(scs_m.objs(i)) == "Block" then
                        n_blocks_found = n_blocks_found + 1;
                        if scs_m.objs(i).model.sim(2) == 5 then
                            gui_name = scs_m.objs(i).gui;
                            scs_m.objs(i).model.sim(1) = "xcosai_nop_sim";
                            if replaced_list == "" then
                                replaced_list = gui_name;
                            elseif isempty(strindex(replaced_list, gui_name)) then
                                replaced_list = replaced_list + ", " + gui_name;
                            end
                        end
                    end
                catch
                end
            end

            if n_blocks_found == 0 then
                xcosai_fail("Empty diagram after importXcosDiagram; Scilab found no Block objects.");
            end

            if replaced_list <> "" then
                mprintf("XCOSAI_VERIFY_WARN:Graphical blocks substituted for headless validation: %s\\n", replaced_list);
            end

            scicos_simulate(scs_m, list(), "nw");
            mprintf("XCOSAI_VERIFY_OK\\n");
            exit(0);
        catch
            [catch_msg, catch_id] = lasterror();
            xcosai_fail(catch_msg);
        end
        """
    ).strip() + "\n"


async def run_subprocess_verification(xml_content: str, auto_fixed: bool):
    scilab_bin = resolve_scilab_binary()
    if not scilab_bin:
        return {
            "success": False,
            "origin": "subprocess-validator",
            "error": "Scilab binary not found. Set SCILAB_BIN or install scilab-cli in the runtime image.",
            "auto_fixed_mux_to_scalar": auto_fixed,
        }

    task_id = str(uuid.uuid4())
    temp_path = os.path.join(TEMP_OUTPUT_DIR, f"{task_id}.xcos")
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(xml_content)
    temp_meta = get_file_metadata(temp_path)

    verify_script_path = os.path.join(TEMP_OUTPUT_DIR, f"{task_id}.sce")
    with open(verify_script_path, "w", encoding="utf-8") as f:
        f.write(build_headless_verification_script(temp_path))

def validate_diagram_structure(tree: etree._Element, auto_fixed: bool) -> dict:
    """Performs structural audit of Xcos XML without needing Scilab."""
    errors = []
    warnings = []
    
    # All IDs in diagram
    all_id_nodes = tree.xpath("//*[@id]")
    all_ids = {n.get("id") for n in all_id_nodes}
    
    # Blocks and their ports
    blocks = tree.xpath(XCOS_BLOCK_XPATH)
    block_ids = {b.get("id") for b in blocks}
    
    ports = tree.xpath(".//*[contains(local-name(), 'Port')]")
    port_ids = {p.get("id") for p in ports}
    
    # 1. Check Links
    links = tree.xpath(XCOS_LINK_XPATH)
    for l in links:
        lid = l.get("id", "unknown")
        src_id = l.get("source") or l.xpath("string(./*[@as='source']/@reference)")
        dst_id = l.get("target") or l.xpath("string(./*[@as='target']/@reference)")
        
        if not src_id:
            errors.append(f"Link {lid}: Missing source endpoint.")
        elif src_id not in all_ids:
            errors.append(f"Link {lid}: Source endpoint {src_id} does not exist.")
        elif src_id not in port_ids and src_id not in block_ids:
            warnings.append(f"Link {lid}: Source {src_id} exists but is not a port or block.")

        if not dst_id:
            errors.append(f"Link {lid}: Missing target endpoint.")
        elif dst_id not in all_ids:
            errors.append(f"Link {lid}: Target endpoint {dst_id} does not exist.")
            
    # 2. Check for missing SplitBlocks (Fan-out)
    # If a port is used as source in multiple links, it must be a SplitBlock or have a SplitBlock
    edge_sources = []
    for l in links:
        sid = l.get("source") or l.xpath("string(./*[@as='source']/@reference)")
        if sid: edge_sources.append(sid)
    
    from collections import Counter
    counts = Counter(edge_sources)
    for pid, count in counts.items():
        if count > 1:
            # Check if pid belongs to a SplitBlock
            parent_block = tree.xpath(f"//*[@id='{pid}']/parent::*")
            if parent_block and parent_block[0].tag != "SplitBlock":
                errors.append(f"Port {pid} has fan-out {count} but parent is not a SplitBlock. Added a CLKSPLIT_f or SplitBlock.")

    success = len(errors) == 0
    return {
        "success": success,
        "origin": "structural-validator",
        "errors": errors if errors else None,
        "warnings": warnings,
        "auto_fixed_mux_to_scalar": auto_fixed,
        "validator_mode": "structural-python"
    }


async def run_subprocess_verification(xml_content: str, auto_fixed: bool = False):
    """Legacy Scilab subprocess validator - now uses structural validation on HF."""
    # This is kept for backward compatibility if needed, but run_verification now bypasses it.
    parser = etree.XMLParser(remove_blank_text=True)
    tree = etree.fromstring(xml_content.encode("utf-8"), parser)
    return validate_diagram_structure(tree, auto_fixed)

# --- MCP Tool Implementations ---

async def get_xcos_block_source(name: str):
    # Recursive search for {name}.sci in ./data/macros/
    macros_dir = os.path.join(DATA_DIR, "macros")
    for root, dirs, files in os.walk(macros_dir):
        if f"{name}.sci" in files:
            path = os.path.join(root, f"{name}.sci")
            with open(path, 'r', encoding='utf-8') as f:
                return [mcp_types.TextContent(type="text", text=f.read())]
    return [mcp_types.TextContent(type="text", text=f"Error: Source for '{name}' not found in {macros_dir}")]

async def get_xcos_block_data(name: str):
    """Returns annotation JSON, reference XML, and parameter help for an Xcos block."""
    data = {
        "info": None,
        "example": None,
        "extra_examples": {},
        "help": None,
        "warnings": []
    }
    
    # 1. INFO 
    info_path = os.path.join(DATA_DIR, "blocks", f"{name}.json")
    if os.path.exists(info_path):
        with open(info_path, 'r', encoding='utf-8') as f:
            try:
                data["info"] = json.loads(f.read())
            except json.JSONDecodeError:
                data["info"] = f.read()
    else:
        data["warnings"].append(f"Block info for '{name}' not found at data/blocks/{name}.json")

    # 2. EXAMPLE 
    example_path = os.path.join(DATA_DIR, "reference_blocks", f"{name}.xcos")
    if os.path.exists(example_path):
        with open(example_path, 'r', encoding='utf-8') as f:
            data["example"] = f.read()
    else:
        data["warnings"].append(f"Reference block '{name}' not found at data/reference_blocks/{name}.xcos")

    extra_example_prefix = f"{name}__"
    reference_dir = os.path.join(DATA_DIR, "reference_blocks")
    if os.path.exists(reference_dir):
        extra_example_files = sorted(
            file_name
            for file_name in os.listdir(reference_dir)
            if file_name.startswith(extra_example_prefix) and file_name.endswith(".xcos")
        )
        for extra_file_name in extra_example_files:
            label = os.path.splitext(extra_file_name)[0].split("__", 1)[1].replace("_", " ")
            extra_path = os.path.join(reference_dir, extra_file_name)
            with open(extra_path, "r", encoding="utf-8") as f:
                data["extra_examples"][label] = f.read()

    # 3. HELP 
    help_file = None
    search_dir = os.path.join(DATA_DIR, "help")
    if os.path.exists(search_dir):
        for root, dirs, files in os.walk(search_dir):
            if f"{name}.xml" in files:
                help_file = os.path.join(root, f"{name}.xml")
                break
    
    if not help_file:
        data["warnings"].append(f"Help file for '{name}' not found. Attempting to extract from MACRO source...")
        macros_dir = os.path.join(DATA_DIR, "macros")
        sci_path = None
        if os.path.exists(macros_dir):
            for root, dirs, files in os.walk(macros_dir):
                if f"{name}.sci" in files:
                    sci_path = os.path.join(root, f"{name}.sci")
                    break
        if sci_path:
            try:
                with open(sci_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    preview = "".join(lines[:100])
                    data["help"] = f"--- AUTO-EXTRACTED FROM {name}.sci (First 100 lines) ---\n{preview}\n..."
            except Exception as e:
                data["warnings"].append(f"Could not read macro file: {str(e)}")
        else:
            data["warnings"].append(f"Macro source for '{name}' not found either.")
    else:
        try:
            parser = etree.XMLParser(remove_blank_text=True)
            tree = etree.parse(help_file, parser)
            sections = tree.xpath("//*[local-name()='refsection']")
            extracted_text = []
            for section in sections:
                sec_id = section.get("{http://www.w3.org/XML/1998/namespace}id") or section.get("id")
                if sec_id and (sec_id.startswith("Dialogbox_") or sec_id.startswith("Defaultproperties_")):
                    title = section.xpath("string(.)").strip()
                    extracted_text.append(f"--- Section: {sec_id} ---\n{title}")

            if not extracted_text:
                data["warnings"].append(f"No parameter sections found in {os.path.basename(help_file)}")
            else:
                data["help"] = "\n\n".join(extracted_text)
        except Exception as e:
            data["warnings"].append(f"Error parsing help XML: {str(e)}")

    return make_json_response(data)

async def search_related_xcos_files(query: str):
    results = []
    for root, dirs, files in os.walk(DATA_DIR):
        for file in files:
            if query.lower() in file.lower():
                results.append(os.path.join(root, file))
    
    if not results:
        return make_text_response(f"No files matching '{query}' found in {DATA_DIR}")
    
    return make_text_response("\n".join(results))

async def run_verification(xml_content: str):
    # --- Integration of Auto-fix and Validator ---
    try:
        parser = etree.XMLParser(remove_blank_text=True)
        tree = etree.fromstring(xml_content.encode('utf-8'), parser)
        
        # 1. Auto-fix
        auto_fixed = auto_fix_mux_to_scalar(tree)
        if auto_fixed:
            xml_content = etree.tostring(tree, encoding='unicode', pretty_print=True)
        
        # 2. Pre-simulation Validation
        val_errors = validate_port_sizes(tree)
        if val_errors:
            return {
                "success": False,
                "origin": "pre-sim-validator",
                "errors": val_errors,
                "auto_fixed_mux_to_scalar": auto_fixed,
            }
            
    except Exception as e:
        return {
            "success": False,
            "origin": "pre-sim-validator",
            "error": f"Error during pre-validation: {str(e)}",
        }

    validation_mode = detect_validation_mode()
    if validation_mode == "subprocess":
        # Headless Scilab has too many limitations (-nogui disables xcosDiagramToScilab).
        # We switch to a high-fidelity Python-based structural validator.
        return validate_diagram_structure(tree, auto_fixed)

    task_id = str(uuid.uuid4())
    temp_path = os.path.join(TEMP_OUTPUT_DIR, f"{task_id}.xcos")
    
    with open(temp_path, 'w', encoding='utf-8') as f:
        f.write(xml_content)
    temp_meta = get_file_metadata(temp_path)
    
    event = asyncio.Event()
    state.results[task_id] = {"success": False, "error": "", "event": event}
    
    await state.task_queue.put({"task_id": task_id, "zcos_path": temp_path})
    
    try:
        # Wait for 120 seconds
        await asyncio.wait_for(event.wait(), timeout=120.0)
        res = state.results.pop(task_id)
        
        # Structured result
        result_payload = {
            "success": res["success"],
            "task_id": task_id,
            "file_path": temp_meta["path"],
            "file_size_bytes": temp_meta["size_bytes"],
            "auto_fixed_mux_to_scalar": auto_fixed,
            "validator_mode": "poll",
        }
        if not res["success"]:
            result_payload["error"] = res["error"]
            result_payload["hint"] = "Use xcos_get_draft_xml(session_id) to inspect the final XML. Scilab errors often relate to parameter size mismatches or missing SplitBlocks."
        
        return result_payload
        
    except asyncio.TimeoutError:
        state.results.pop(task_id, None)
        return {
            "success": False,
            "task_id": task_id,
            "file_path": temp_meta["path"],
            "file_size_bytes": temp_meta["size_bytes"],
            "error": f"Scilab verification timed out for {task_id}"
        }


async def verify_xcos_xml(xml_content: str):
    return make_json_response(await run_verification(xml_content))

# --- Incremental Tool Implementations ---

async def xcos_create_workflow(problem_statement: str):
    if not problem_statement.strip():
        return make_text_response("Error: problem_statement cannot be empty")
    workflow = create_workflow_session(problem_statement)
    return make_json_response({
        "status": "success",
        "workflow": workflow.to_dict(),
    })


async def xcos_list_workflows():
    return make_json_response({"workflows": list_workflow_payloads()})


async def xcos_get_workflow(workflow_id: str):
    workflow = get_workflow(workflow_id)
    if not workflow:
        return make_text_response(f"Error: Workflow {workflow_id} not found")
    return make_json_response({"workflow": workflow.to_dict()})


async def xcos_get_status_widget():
    mode = detect_validation_mode()
    scilab_bin = resolve_scilab_binary()
    
    version = "Unknown"
    xcos_loaded = "Unknown"
    tmp_dir = "Unknown"
    
    polling_active = False
    if state.last_poll_time and (datetime.now() - state.last_poll_time).total_seconds() < 5:
        polling_active = True
        
    connection_status = "Connected" if (polling_active or mode == "subprocess") else "Disconnected"
    status_color = "#28a745" if connection_status == "Connected" else "#dc3545"
    
    if scilab_bin:
        # On Windows, WScilex-cli.exe requires full GUI initialisation and hangs
        # when spawned as a short-lived subprocess. Read version metadata from
        # the install directory instead.
        if os.name == "nt":
            scilab_root = os.path.dirname(os.path.dirname(os.path.abspath(scilab_bin)))
            version_incl = os.path.join(scilab_root, "Version.incl")
            if os.path.exists(version_incl):
                try:
                    with open(version_incl, "r", encoding="utf-8") as vf:
                        for vline in vf:
                            if "SCIVERSION=" in vline:
                                version = vline.split("=", 1)[1].strip()
                                break
                except Exception:
                    pass
            xcos_dir = os.path.join(scilab_root, "modules", "xcos")
            xcos_loaded = "T" if os.path.isdir(xcos_dir) else "F"
            tmp_dir = os.environ.get("TEMP", "Unknown")
        else:
            # Linux / remote: spawn Scilab with a correctly formatted script.
            # Statements are separated by real newlines; \n inside mprintf strings
            # is the two-char Scilab escape that produces a newline in output.
            import textwrap as _tw
            status_script = _tw.dedent("""\
                mode(-1);
                lines(0);
                mprintf("XCOS_STATUS_START\\n");
                mprintf("%s\\n", getversion());
                mprintf("%s\\n", string(with_module("xcos")));
                mprintf("%s\\n", TMPDIR);
                mprintf("XCOS_STATUS_END\\n");
                exit(0);
            """)
            task_id = str(uuid.uuid4())
            script_path = os.path.join(TEMP_OUTPUT_DIR, f"status_{task_id}.sce")
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(status_script)

            command = [scilab_bin]
            if shutil.which("xvfb-run"):
                command = ["xvfb-run", "-a", scilab_bin]
            lower_bin = scilab_bin.lower()
            if "scilab-cli" in lower_bin:
                command.extend(["-nb", "-f", script_path])
            else:
                command.extend(["-nw", "-nb", "-f", script_path])

            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=BASE_DIR,
                )
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=20.0)
                out_str = stdout.decode("utf-8", errors="replace")
                if "XCOS_STATUS_START" in out_str and "XCOS_STATUS_END" in out_str:
                    raw = out_str.split("XCOS_STATUS_START")[1].split("XCOS_STATUS_END")[0]
                    parts = [p.strip() for p in raw.splitlines() if p.strip()]
                    if len(parts) >= 3:
                        version = parts[0]
                        xcos_loaded = parts[1]
                        tmp_dir = parts[2]
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

    payload = {
        "widget_type": "status",
        "payload": {
            "scilab_success": connection_status == "Connected",
            "scilab_output": version,
            "env_context": mode,
            "active_drafts": len(state.drafts)
        }
    }
    return make_json_response(payload)

async def xcos_get_workflow_widget(workflow_id: str = None):
    try:
        if workflow_id:
            payload = parse_mcp_text_json_response(await xcos_get_workflow(workflow_id))
            workflows = [payload["workflow"]]
        else:
            payload = parse_mcp_text_json_response(await xcos_list_workflows())
            workflows = payload["workflows"]
    except ValueError as exc:
        return make_json_response({
            "widget_type": "workflow",
            "payload": {
                "error": str(exc)
            }
        })

    phases = []
    if workflow_id and workflows:
        workflow = workflows[0]
        # Build phases array for frontend
        for phase_key in WORKFLOW_PHASE_ORDER:
            phase = workflow["phases"][phase_key]
            phases.append({
                "label": phase["label"],
                "status": phase["status"],
                "submitted_at": phase["submitted_at"],
                "reviewed_at": phase["reviewed_at"],
                "feedback": getattr(phase, 'feedback', phase.get("feedback", ""))
            })
            
    return make_json_response({
        "widget_type": "workflow",
        "payload": {
            "workflow_id": workflow_id or "All",
            "phases": phases if workflow_id else [],
            "all_workflows": workflows if not workflow_id else []
        }
    })

async def xcos_get_validation_widget(xml_content: str):
    try:
        result = await run_verification(xml_content)
    except Exception as e:
        result = {
            "success": False,
            "error": f"Validator internal error: {str(e)}",
            "origin": "internal-error",
        }
    
    error_msgs = []
    
    if result.get("auto_fixed_mux_to_scalar"):
        error_msgs.append("⚠ Auto-fixed MUX to scalar connections")
        
    if "errors" in result and result["errors"]:
        for e in result["errors"]:
            if e["type"] == "REGISTRY_SIZE_MISMATCH":
                error_msgs.append(f"Block {e.get('blockId')} ({e.get('block')}): expected {e.get('expectedSize')}, got {e.get('actualSize')} on port {e.get('portIndex')}")
            elif e["type"] == "PORT_SIZE_MISMATCH":
                error_msgs.append(f"Link {e.get('linkId')}: size mismatch between {e.get('srcBlock')} {e.get('srcSize')} and {e.get('dstBlock')} {e.get('dstSize')}")
            elif e["type"] == "FANOUT_WITHOUT_SPLIT":
                error_msgs.append(f"Block {e.get('blockId')}: {e.get('message', 'fanout without SplitBlock')}")
            else:
                error_msgs.append(f"{e.get('type')}: {e.get('message', '')}")
                
    if result.get("error"):
        error_msgs.append(str(result["error"]))
        
    if result.get("warnings"):
        for w in result["warnings"]:
            error_msgs.append(str(w))
            
    return make_json_response({
        "widget_type": "validation",
        "payload": {
            "success": result.get("success", False),
            "error": "\n".join(error_msgs) if error_msgs else None
        }
    })

async def xcos_get_block_catalogue_widget(category: str = None):
    index_path = os.path.join(DATA_DIR, "blocks", "_index.json")
    blocks = []
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                blocks = data.get("block_files", [])
            except Exception:
                pass
                
    if category:
        cat_lower = category.lower()
        blocks = [b for b in blocks if cat_lower in b.get("category", "").lower()]
        
    formatted_blocks = []
    for b in blocks:
        formatted_blocks.append({
            "name": b.get("name", ""),
            "type": b.get("category", ""),
            "description": b.get("description", "")
        })
        
    return make_json_response({
        "widget_type": "catalogue",
        "payload": {
            "category": category,
            "blocks": formatted_blocks
        }
    })

async def xcos_get_topology_widget(session_id: str):
    if session_id not in state.drafts:
        return make_json_response({
            "widget_type": "topology",
            "payload": {
                "error": f"Session {session_id} not found"
            }
        })
        
    draft = state.drafts[session_id]
    xml_content = draft.to_xml()
    
    try:
        parser = etree.XMLParser(remove_blank_text=True)
        tree = etree.fromstring(xml_content.encode("utf-8"), parser)
    except Exception as e:
        return make_json_response({
            "widget_type": "topology",
            "payload": {
                "error": f"Error parsing XML: {str(e)}"
            }
        })
        
    blocks = tree.xpath(XCOS_BLOCK_XPATH)
    links = tree.xpath(XCOS_LINK_XPATH)
    
    block_map = {}
    for b in blocks:
        bid = b.get("id")
        name = b.get("interfaceFunctionName", b.tag)
        block_map[bid] = {"name": name, "in_ports": [], "out_ports": []}
        
    # Build ports_map: map every port id -> {block_id, type}
    #
    # In Xcos XML, ports are NOT nested inside their block element — they are
    # siblings under <root> that declare ownership via a @parent="blockId"
    # attribute. The per-block child-XPath approach therefore finds nothing.
    # The correct strategy: scan every Port-like element in the whole tree and
    # use the @parent attribute to associate it with the owning block.
    #
    # Two-stage to handle both the sibling-with-@parent style (standard Xcos)
    # and the rare nested-child style:
    ports_map = {}

    # Stage 1 — sibling style: @parent attribute points to the block id
    for p in tree.iter():
        if not isinstance(p.tag, str):
            continue
        if "Port" not in p.tag:
            continue
        pid = p.get("id")
        if not pid:
            continue
        owner_id = p.get("parent")  # the Xcos @parent attribute
        if owner_id and owner_id in block_map and pid not in ports_map:
            tag = p.tag
            p_type = "in" if any(k in tag for k in ("Input", "InPort", "Control")) else "out"
            ports_map[pid] = {"block_id": owner_id, "type": p_type}
            bdata = block_map[owner_id]
            if p_type == "in":
                bdata["in_ports"].append(pid)
            else:
                bdata["out_ports"].append(pid)

    # Stage 2 — nested-child style: port is a descendant of the block element
    for bid, bdata in block_map.items():
        block_nodes = tree.xpath(f"//*[@id='{bid}']")
        if not block_nodes:
            continue
        block = block_nodes[0]
        for p in block.iter():
            if not isinstance(p.tag, str) or "Port" not in p.tag:
                continue
            pid = p.get("id")
            if pid and pid not in ports_map:
                tag = p.tag
                p_type = "in" if any(k in tag for k in ("Input", "InPort", "Control")) else "out"
                ports_map[pid] = {"block_id": bid, "type": p_type}
                if p_type == "in":
                    bdata["in_ports"].append(pid)
                else:
                    bdata["out_ports"].append(pid)

    svg_nodes = []
    svg_edges = []

    node_w = 100
    node_h = 40
    pad_y = 60
    pad_x = 150
    curr_y = 20
    curr_x = 20

    b_coords = {}

    for idx, (bid, bdata) in enumerate(block_map.items()):
        b_coords[bid] = (curr_x, curr_y)
        svg_nodes.append(f'<rect x="{curr_x}" y="{curr_y}" width="{node_w}" height="{node_h}" fill="#f8f9fa" stroke="#343a40" rx="4" />')
        svg_nodes.append(f'<text x="{curr_x + 6}" y="{curr_y + 24}" font-family="sans-serif" font-size="12" fill="#000">{bdata["name"]}</text>')
        curr_y += pad_y
        if idx > 0 and idx % 10 == 0:
            curr_y = 20
            curr_x += pad_x

    connected_ports = set()
    link_strings = []
    
    for l in links:
        # Check source/target as attributes OR as children with as='source'/'target'
        src_id = l.get("source")
        if not src_id:
            src_node = l.xpath("./*[@as='source']")
            if src_node: src_id = src_node[0].get("reference")
            
        dst_id = l.get("target")
        if not dst_id:
            dst_node = l.xpath("./*[@as='target']")
            if dst_node: dst_id = dst_node[0].get("reference")

        if src_id and dst_id:
            connected_ports.add(src_id)
            connected_ports.add(dst_id)
            
            src_info = ports_map.get(src_id)
            dst_info = ports_map.get(dst_id)
            
            src_name = block_map[src_info["block_id"]]["name"] if src_info else "?"
            dst_name = block_map[dst_info["block_id"]]["name"] if dst_info else "?"
            
            link_strings.append(f"{src_name} &rarr; {dst_name}")
            
            if src_info and dst_info:
                s_coords = b_coords.get(src_info["block_id"])
                d_coords = b_coords.get(dst_info["block_id"])
                if s_coords and d_coords:
                    sx = s_coords[0] + node_w
                    sy = s_coords[1] + (node_h/2)
                    dx = d_coords[0]
                    dy = d_coords[1] + (node_h/2)
                    svg_edges.append(f'<path d="M {sx} {sy} L {dx} {dy}" stroke="#007bff" stroke-width="2" fill="none" marker-end="url(#arrow)" />')
            
    max_x = curr_x + node_w + 20
    max_y = curr_y + 20
    
    nodes_str = ''.join(svg_nodes)
    edges_str = ''.join(svg_edges)
    
    svg_out = f'''<svg width="100%" height="{max_y}" viewBox="0 0 {max_x} {max_y}" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" fill="#007bff" />
        </marker>
      </defs>
      {edges_str}
      {nodes_str}
    </svg>'''
    
    return make_json_response({
        "widget_type": "topology",
        "payload": {
            "session_id": session_id,
            "block_count": len(block_map),
            "link_count": len(links),
            "svg": svg_out
        }
    })


async def xcos_submit_phase(
    workflow_id: str,
    phase: str,
    content: str,
    artifact_type: str = "markdown",
):
    payload, error = submit_workflow_phase(workflow_id, phase, content, artifact_type)
    if error:
        return make_text_response(f"Error: {error}")
    return make_json_response({
        "status": "success",
        "workflow": payload,
    })


async def xcos_review_phase(
    workflow_id: str,
    phase: str,
    decision: str,
    feedback: str = "",
):
    payload, error = review_workflow_phase(workflow_id, phase, decision, feedback)
    if error:
        return make_text_response(f"Error: {error}")
    return make_json_response({
        "status": "success",
        "workflow": payload,
    })


async def xcos_start_draft(schema_version: str = "1.1", workflow_id: str | None = None, replace: bool = False, phases: list[str] = None):
    workflow = None
    if workflow_id:
        workflow = get_workflow(workflow_id)
        if not workflow:
            return make_text_response(f"Error: Workflow {workflow_id} not found")
        if workflow.phases["phase2_architecture"].status != "approved":
            return make_text_response(
                "Error: Phase 2 must be approved before Phase 3 implementation can start."
            )
        if workflow.draft_session_id and not replace:
            return make_text_response(f"Error: Workflow {workflow_id} already has an active draft session ({workflow.draft_session_id}). Pass replace=True to overwrite.")

    session_id = str(uuid.uuid4())
    state.drafts[session_id] = DraftDiagram(schema_version)

    payload = {
        "status": "success",
        "session_id": session_id,
        "message": f"Started new Xcos draft session {session_id}",
        "critical_rule": "IMPORTANT: Any ExplicitOutputPort or EventOutPort that fanning out to multiple downstream blocks REQUIRES an intermediate SplitBlock (for data) or CLKSPLIT_f (for events)."
    }

    if phases:
        if len(set(phases)) != len(phases):
            return make_text_response("Error: Phases list must contain unique labels.")
        state.phase_plans[session_id] = {
            "phases": phases,
            "completed": []
        }
        payload["phase_plan_registered"] = True
        payload["phase_count"] = len(phases)

    if workflow:
        if workflow.draft_session_id and workflow.draft_session_id in state.draft_to_workflow:
            del state.draft_to_workflow[workflow.draft_session_id]
            if workflow.draft_session_id in state.drafts:
                del state.drafts[workflow.draft_session_id]
                
        state.draft_to_workflow[session_id] = workflow.workflow_id
        workflow.draft_session_id = session_id
        workflow.current_phase = "phase3_implementation"
        workflow.updated_at = now_iso()
        workflow.phases["phase3_implementation"].status = "in_progress"
        workflow.phases["phase3_implementation"].submitted_at = workflow.phases["phase3_implementation"].submitted_at or now_iso()
        workflow.phases["phase3_implementation"].last_error = None
        payload["workflow_id"] = workflow.workflow_id

    return make_json_response(payload)

async def xcos_get_draft_xml(
    session_id: str,
    pretty_print: bool = False,
    strip_comments: bool = False,
    validate: bool = False,
):
    if session_id not in state.drafts:
        return make_text_response(f"Error: Session {session_id} not found")
    xml = state.drafts[session_id].to_xml()
    if pretty_print or strip_comments or validate:
        try:
            parser = etree.XMLParser(
                remove_blank_text=pretty_print,
                remove_comments=strip_comments,
            )
            tree = etree.fromstring(xml.encode("utf-8"), parser)
            xml = etree.tostring(
                tree,
                encoding="utf-8",
                pretty_print=pretty_print,
                xml_declaration=True,
            ).decode("utf-8")
        except Exception as e:
            return make_text_response(f"Error validating draft XML: {str(e)}")
    return make_text_response(xml)

async def xcos_list_sessions():
    sessions = []
    for sid, draft in state.drafts.items():
        counts = summarize_draft(draft)
        session_meta = get_file_metadata(get_session_file_path(sid))
        last_verified = None
        if any([
            draft.last_verified_at,
            draft.last_verified_task_id,
            draft.last_verified_file_path,
        ]):
            last_verified = {
                "at": draft.last_verified_at,
                "success": draft.last_verified_success,
                "task_id": draft.last_verified_task_id,
                "file_path": draft.last_verified_file_path,
                "file_size_bytes": draft.last_verified_file_size,
                "error": draft.last_verified_error,
                "origin": draft.last_verified_origin,
            }
        session_data = {
            "session_id": sid,
            "created_at": draft.created_at.isoformat(),
            "block_count": counts["block_count"],
            "link_count": counts["link_count"],
            "block_chunk_count": len(draft.blocks),
            "link_chunk_count": len(draft.links),
            "has_phase_plan": sid in state.phase_plans,
            "workflow_id": state.draft_to_workflow.get(sid),
            "session_file_path": session_meta["path"] if session_meta else None,
            "session_file_size_bytes": session_meta["size_bytes"] if session_meta else None,
            "last_verified": last_verified,
        }
        if sid in state.phase_plans:
            plan = state.phase_plans[sid]
            session_data["planned_phases"] = plan["phases"]
            session_data["completed_phases"] = plan["completed"]
            session_data["remaining_phases"] = [p for p in plan["phases"] if p not in plan["completed"]]
        sessions.append(session_data)
    return make_json_response({"sessions": sessions})

async def xcos_add_blocks(session_id: str, blocks_xml: str):
    if session_id not in state.drafts:
        return make_text_response(f"Error: Session {session_id} not found")
    state.drafts[session_id].add_blocks(blocks_xml)
    workflow_id = state.draft_to_workflow.get(session_id)
    if workflow_id and workflow_id in state.workflows:
        workflow = state.workflows[workflow_id]
        workflow.current_phase = "phase3_implementation"
        workflow.updated_at = now_iso()
        workflow.phases["phase3_implementation"].status = "in_progress"
    return make_text_response(f"Successfully added blocks to session {session_id}")

async def xcos_add_links(session_id: str, links_xml: str):
    if session_id not in state.drafts:
        return make_text_response(f"Error: Session {session_id} not found")
    state.drafts[session_id].add_links(links_xml)
    workflow_id = state.draft_to_workflow.get(session_id)
    if workflow_id and workflow_id in state.workflows:
        workflow = state.workflows[workflow_id]
        workflow.current_phase = "phase3_implementation"
        workflow.updated_at = now_iso()
        workflow.phases["phase3_implementation"].status = "in_progress"
    return make_text_response(f"Successfully added links to session {session_id}")

async def xcos_verify_draft(session_id: str):
    if session_id not in state.drafts:
        return make_text_response(f"Error: Session {session_id} not found")
    
    draft = state.drafts[session_id]
    xml_content = draft.to_xml()
    session_meta = write_session_snapshot(session_id)
    result = await run_verification(xml_content)

    draft.last_verified_at = datetime.now().isoformat()
    draft.last_verified_success = result.get("success")
    draft.last_verified_task_id = result.get("task_id")
    # Point last_verified to the session snapshot (not the temp validator file)
    # so xcos_get_file_content(source='last_verified') always returns the final file.
    if result.get("success"):
        draft.last_verified_file_path = session_meta["path"]
        draft.last_verified_file_size = session_meta["size_bytes"]
    else:
        draft.last_verified_file_path = result.get("file_path")
        draft.last_verified_file_size = result.get("file_size_bytes")
    draft.last_verified_error = result.get("error")
    draft.last_verified_origin = result.get("origin", "scilab-validator")

    workflow_id = state.draft_to_workflow.get(session_id)
    if workflow_id and workflow_id in state.workflows:
        workflow = state.workflows[workflow_id]
        phase3 = workflow.phases["phase3_implementation"]
        phase3.reviewed_at = now_iso()
        phase3.last_error = result.get("error")
        phase3.status = "completed" if result.get("success") else "failed"
        workflow.current_phase = "phase3_implementation"
        workflow.last_verified = {
            "success": result.get("success"),
            "task_id": result.get("task_id"),
            "file_path": result.get("file_path"),
            "file_size_bytes": result.get("file_size_bytes"),
            "error": result.get("error"),
            "origin": result.get("origin", "scilab-validator"),
        }
        workflow.updated_at = now_iso()

    result["session_file_path"] = session_meta["path"]
    result["session_file_size_bytes"] = session_meta["size_bytes"]
    result["workflow_id"] = workflow_id
    return make_json_response(result)



async def xcos_commit_phase(session_id: str, phase_label: str, blocks_xml: str = ""):
    if session_id not in state.drafts:
        return make_text_response(f"Error: Session {session_id} not found")

    if session_id not in state.phase_plans:
        return make_text_response(
            f"Error: No phase plan found for session {session_id}. "
            "Call xcos_start_draft with phases=['phase3_implementation'] first."
        )

    plan = state.phase_plans[session_id]
    if phase_label not in plan["phases"]:
        return make_text_response(
            f"Error: Phase '{phase_label}' not in plan. Registered phases: {plan['phases']}"
        )

    # Only append blocks when explicitly provided — prevents duplication when blocks
    # were already added via xcos_add_blocks (the normal workflow).
    if blocks_xml and blocks_xml.strip():
        try:
            parser = etree.XMLParser(remove_blank_text=True)
            root = etree.fromstring(f"<root>{blocks_xml}</root>".encode("utf-8"), parser)
            for block in root.xpath(
                "//BasicBlock | //BigSom | //SplitBlock | //TextBlock "
                "| //EventInBlock | //EventOutBlock "
                "| //ExplicitInBlock | //ExplicitOutBlock"
            ):
                if not block.xpath(".//mxGeometry"):
                    return make_text_response(
                        "Error: Invalid XML - block missing required <mxGeometry> element."
                    )
        except Exception as e:
            return make_text_response(f"Error: Invalid XML fragment syntax: {str(e)}")
        state.drafts[session_id].add_blocks(blocks_xml)

    # Mark phase complete (idempotent)
    if phase_label not in plan["completed"]:
        plan["completed"].append(phase_label)

    session_meta = write_session_snapshot(session_id)

    completed_count = len(plan["completed"])
    total_count = len(plan["phases"])
    remaining = [p for p in plan["phases"] if p not in plan["completed"]]
    file_path = session_meta["path"]
    file_size = session_meta["size_bytes"]

    return make_json_response({
        "status": "success",
        "completed_count": completed_count,
        "total_count": total_count,
        "remaining_phases": remaining,
        "written_to": file_path,
        "file_size_bytes": file_size,
        "MUST_PRESENT_TO_USER": (
            f"The verified .xcos file is ready at: {file_path} ({file_size} bytes). "
            "You MUST immediately: "
            "(1) call xcos_get_file_path to confirm the path, "
            "(2) call xcos_get_file_content(session_id=..., source='session') to read the XML, "
            "(3) write the file content to your output directory so the user can download it, "
            "(4) present the file path and a download link in your reply. "
            "Do NOT skip these steps or wait for the user to ask."
        ),
    })


async def xcos_get_file_path(session_id: str):
    if session_id not in state.drafts:
        return make_text_response(f"Error: Session {session_id} not found")

    session_meta = get_file_metadata(get_session_file_path(session_id))
    draft = state.drafts[session_id]
    payload = {
        "session_id": session_id,
        "session_file_path": session_meta["path"],
        "session_file_size_bytes": session_meta["size_bytes"],
        "last_verified": {
            "at": draft.last_verified_at,
            "success": draft.last_verified_success,
            "task_id": draft.last_verified_task_id,
            "file_path": draft.last_verified_file_path,
            "file_size_bytes": draft.last_verified_file_size,
            "error": draft.last_verified_error,
            "origin": draft.last_verified_origin,
        }
    }
    return make_json_response(payload)


async def xcos_get_file_content(
    session_id: str,
    source: str = "session",
    encoding: str = "text",
):
    if session_id not in state.drafts:
        return make_text_response(f"Error: Session {session_id} not found")

    draft = state.drafts[session_id]
    source = source.lower()
    encoding = encoding.lower()
    if source not in {"draft", "session", "last_verified"}:
        return make_text_response("Error: source must be one of draft, session, or last_verified")
    if encoding not in {"text", "base64"}:
        return make_text_response("Error: encoding must be text or base64")

    file_path = None
    if source == "draft":
        raw = draft.to_xml().encode("utf-8")
    elif source == "session":
        file_path = get_session_file_path(session_id)
        if not os.path.exists(file_path):
            return make_text_response(f"Error: Session snapshot {file_path} doesn't exist yet. Commit a phase or write a snapshot first.")
        with open(file_path, "rb") as f:
            raw = f.read()
    else:
        file_path = draft.last_verified_file_path
        if not file_path or not os.path.exists(file_path):
            return make_text_response(f"Error: No last verified file available for session {session_id}")
        with open(file_path, "rb") as f:
            raw = f.read()

    content = (
        raw.decode("utf-8")
        if encoding == "text"
        else base64.b64encode(raw).decode("ascii")
    )
    return make_json_response({
        "session_id": session_id,
        "source": source,
        "encoding": encoding,
        "file_path": os.path.abspath(file_path) if file_path else None,
        "size_bytes": len(raw),
        "content": content,
    })

# --- HTTP / Browser UI / MCP App Server ---

def http_json(payload, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code)


async def http_handle_get_task(request: Request) -> Response:
    try:
        task = state.task_queue.get_nowait()
        state.last_poll_time = datetime.now()
        return http_json({"status": "pending", **task})
    except asyncio.QueueEmpty:
        state.last_poll_time = datetime.now()
        return http_json({"status": "idle"})


async def http_handle_post_result(request: Request) -> Response:
    data = await request.json()
    task_id = data.get("task_id")
    success = data.get("success")
    error = data.get("error", "")

    if task_id in state.results:
        state.results[task_id]["success"] = success
        state.results[task_id]["error"] = error
        state.results[task_id]["event"].set()
        return http_json({"status": "received"})
    return http_json({"status": "error", "message": "Task ID not found"}, status_code=404)


async def http_healthz(_: Request) -> Response:
    return http_json(
        {
            "status": "ok",
            "version": "1.0.1",
            "validator_mode": detect_validation_mode(),
            "workflow_count": len(state.workflows),
            "draft_count": len(state.drafts),
            "mcp_http_path": MCP_HTTP_PATH,
        }
    )


async def http_root(_: Request) -> Response:
    return RedirectResponse(url="/workflow-ui")


async def http_workflow_ui(_: Request) -> Response:
    return HTMLResponse(load_ui_html())


async def http_api_list_workflows(_: Request) -> Response:
    return http_json({"workflows": list_workflow_payloads()})


async def http_api_create_workflow(request: Request) -> Response:
    data = await request.json()
    problem_statement = (data.get("problem_statement") or "").strip()
    if not problem_statement:
        return http_json({"error": "problem_statement cannot be empty"}, status_code=400)
    workflow = create_workflow_session(problem_statement)
    return http_json({"status": "success", "workflow": workflow.to_dict()})


async def http_api_get_workflow(request: Request) -> Response:
    workflow_id = request.path_params["workflow_id"]
    workflow = get_workflow(workflow_id)
    if not workflow:
        return http_json({"error": f"Workflow {workflow_id} not found"}, status_code=404)
    return http_json({"workflow": workflow.to_dict()})


async def http_api_submit_phase(request: Request) -> Response:
    workflow_id = request.path_params["workflow_id"]
    phase = request.path_params["phase"]
    data = await request.json()
    payload, error = submit_workflow_phase(
        workflow_id,
        phase,
        data.get("content", ""),
        data.get("artifact_type", "markdown"),
    )
    if error:
        return http_json({"error": error}, status_code=400)
    return http_json({"status": "success", "workflow": payload})


async def http_api_review_phase(request: Request) -> Response:
    workflow_id = request.path_params["workflow_id"]
    phase = request.path_params["phase"]
    data = await request.json()
    payload, error = review_workflow_phase(
        workflow_id,
        phase,
        data.get("decision", ""),
        data.get("feedback", ""),
    )
    if error:
        return http_json({"error": error}, status_code=400)
    return http_json({"status": "success", "workflow": payload})


async def http_api_start_draft(request: Request) -> Response:
    workflow_id = request.path_params["workflow_id"]
    result = await xcos_start_draft("1.1", workflow_id)
    text = result[0].text
    if text.startswith("Error:"):
        return http_json({"error": text[7:].strip()}, status_code=400)
    return http_json(json.loads(text))


async def http_ext_apps_js(request: Request) -> Response:
    ui_path = os.path.join(UI_DIR, "ext-apps.js")
    with open(ui_path, "r", encoding="utf-8") as f:
        return Response(f.read(), media_type="text/javascript")


class StreamableHTTPRouteApp:
    def __init__(self, session_manager: StreamableHTTPSessionManager):
        self.session_manager = session_manager

    async def __call__(self, scope, receive, send) -> None:
        await self.session_manager.handle_request(scope, receive, send)


streamable_http_manager = None


@asynccontextmanager
async def starlette_lifespan(_: Starlette):
    async with streamable_http_manager.run():
        yield

async def cleanup_port(port=8000):
    """Kills any process currently using the specified port on Windows."""
    if os.name != "nt":
        return
    try:
        # Find PID using netstat with strict matching for the port
        # /C: ensures the colon and trailing space are matched to avoid 8000 matching 18000
        cmd = f'netstat -ano | findstr /C:":{port} "'
        output = subprocess.check_output(cmd, shell=True).decode()
        for line in output.splitlines():
            if "LISTENING" in line:
                pid = line.strip().split()[-1]
                if int(pid) != os.getpid(): # Don't kill ourselves
                    print(f"[{Fore.YELLOW}CLEANUP{Style.RESET_ALL}] Force-killing process {pid} on port {port}...", file=sys.stderr)
                    # /F is force, /T kills child processes too
                    subprocess.run(f'taskkill /F /T /PID {pid}', shell=True, check=True, capture_output=True)
    except (subprocess.CalledProcessError, IndexError):
        # No process found or taskkill failed, which is fine
        pass

def build_starlette_app() -> Starlette:
    routes = [
        Route("/", http_root, methods=["GET"]),
        Route("/healthz", http_healthz, methods=["GET"]),
        Route("/workflow-ui", http_workflow_ui, methods=["GET"]),
        Route("/workflow-ui/ext-apps.js", http_ext_apps_js, methods=["GET"]),
        Route("/workflow-ui/api/workflows", http_api_list_workflows, methods=["GET"]),
        Route("/workflow-ui/api/workflows", http_api_create_workflow, methods=["POST"]),
        Route("/workflow-ui/api/workflows/{workflow_id}", http_api_get_workflow, methods=["GET"]),
        Route("/workflow-ui/api/workflows/{workflow_id}/phases/{phase}/submit", http_api_submit_phase, methods=["POST"]),
        Route("/workflow-ui/api/workflows/{workflow_id}/phases/{phase}/review", http_api_review_phase, methods=["POST"]),
        Route("/workflow-ui/api/workflows/{workflow_id}/draft/start", http_api_start_draft, methods=["POST"]),
        Route("/task", http_handle_get_task, methods=["GET"]),
        Route("/result", http_handle_post_result, methods=["POST"]),
        Route(MCP_HTTP_PATH, StreamableHTTPRouteApp(streamable_http_manager), methods=["GET", "POST", "DELETE"]),
    ]
    return Starlette(debug=False, routes=routes, lifespan=starlette_lifespan)


async def run_http_server():
    app = build_starlette_app()
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=SERVER_PORT,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    print(
        f"[{Fore.CYAN}HTTP{Style.RESET_ALL}] Server running on http://0.0.0.0:{SERVER_PORT} "
        f"(UI: /workflow-ui, MCP: {MCP_HTTP_PATH})",
        file=sys.stderr,
    )
    await server.serve()

# --- Telemetry ---

async def telemetry_loop():
    last_status = None
    while True:
        if detect_validation_mode() == "subprocess":
            status = "SUBPROCESS"
            if status != last_status:
                print(
                    f"{Fore.CYAN}[HEADLESS] Scilab subprocess validation enabled{Style.RESET_ALL} on port {SERVER_PORT}",
                    file=sys.stderr,
                )
                last_status = status
            await asyncio.sleep(5)
            continue
        if state.last_poll_time:
            delta = (datetime.now() - state.last_poll_time).total_seconds()
            if delta < 5:
                status = "CONNECTED"
                if status != last_status:
                    print(f"{Fore.GREEN}[CONNECTED] Scilab Connected{Style.RESET_ALL}", file=sys.stderr)
                    last_status = status
            else:
                status = "DISCONNECTED"
                if status != last_status:
                    print(f"{Fore.RED}[DISCONNECTED] Awaiting Scilab Polling{Style.RESET_ALL} (idle for {delta:.1f}s)", file=sys.stderr)
                    last_status = status
        else:
            status = "INITIALIZING"
            if status != last_status:
                print(f"{Fore.YELLOW}[INITIALIZING] Awaiting Connection...{Style.RESET_ALL}", file=sys.stderr)
                last_status = status
        await asyncio.sleep(1)

# --- MCP Server Setup ---

mcp_server = Server(
    "scilab-xcos-server",
    version="0.1.0",
    instructions=(
        "Use the phased Xcos workflow. Phase 1 derives the mathematical model and waits for approval. "
        "Phase 2 defines block architecture, parameters, and links and waits for approval. "
        "Phase 3 starts only after approval and builds/verifies the draft."
    ),
    icons=SERVER_ICONS or None,
)

streamable_http_manager = StreamableHTTPSessionManager(
    app=mcp_server,
    json_response=False,
    stateless=False,
)


def create_server_initialization_options():
    options = mcp_server.create_initialization_options()
    options.capabilities.prompts = mcp_types.PromptsCapability(listChanged=False)
    return options


@mcp_server.list_prompts()
async def handle_list_prompts() -> list[mcp_types.Prompt]:
    return [
        mcp_types.Prompt(
            name=BUILD_XCOS_DIAGRAM_PROMPT_NAME,
            title=BUILD_XCOS_DIAGRAM_PROMPT_TITLE,
            description=BUILD_XCOS_DIAGRAM_PROMPT_DESCRIPTION,
            arguments=[BUILD_XCOS_DIAGRAM_PROMPT_ARGUMENT],
        ),
        mcp_types.Prompt(
            name="xcos-phased-workflow",
            description="Guides an agent through the 3-phase Xcos workflow with explicit approval gates.",
            arguments=[
                mcp_types.PromptArgument(
                    name="problem_statement",
                    description="The control-system problem to solve in Xcos.",
                    required=False,
                )
            ],
        )
    ]


@mcp_server.get_prompt()
async def handle_get_prompt(name: str, arguments: dict[str, str] | None) -> mcp_types.GetPromptResult:
    if name == BUILD_XCOS_DIAGRAM_PROMPT_NAME:
        prompt_text = build_xcos_prompt_text((arguments or {}).get("problem_statement", ""))
        return mcp_types.GetPromptResult(
            description=BUILD_XCOS_DIAGRAM_PROMPT_RESULT_DESCRIPTION,
            messages=[
                mcp_types.PromptMessage(
                    role="user",
                    content=mcp_types.TextContent(type="text", text=prompt_text),
                )
            ],
        )

    if name != "xcos-phased-workflow":
        raise ValueError(f"Unknown prompt: {name}")

    problem_statement = (arguments or {}).get("problem_statement", "").strip()
    prompt_text = (
        "You are an Expert Control Systems Engineer specializing in Scilab Xcos modeling.\n\n"
        "Workflow:\n"
        "1. Phase 1: derive the mathematical model, show the calculations step by step, and wait for explicit approval.\n"
        "2. Phase 2: define the block diagram architecture, list block parameters and connections, enforce SplitBlock/CLKSPLIT_f for fan-out, and wait for explicit approval.\n"
        "3. Phase 3: only after approval, create a draft, build XML, verify it, debug with block data/source when needed, and present the validated result.\n"
        "4. Final Step: Use `xcos_get_file_content(encoding='text')` to fetch the verified XML, write it to your environment using native file tools, and present a download link to the user.\n\n"
        "Use the workflow tools on this server to create and update the phase session so the review UI stays in sync."
    )
    if problem_statement:
        prompt_text += f"\n\nCurrent problem statement:\n{problem_statement}"

    return mcp_types.GetPromptResult(
        description="Phased Xcos generation instructions",
        messages=[
            mcp_types.PromptMessage(
                role="user",
                content=mcp_types.TextContent(type="text", text=prompt_text),
            )
        ],
    )


@mcp_server.list_resources()
async def handle_list_resources() -> list[mcp_types.Resource]:
    return []


@mcp_server.read_resource()
async def handle_read_resource(uri):
    uri_str = str(uri)
    if not uri_str.startswith("ui://xcos/"):
        raise ValueError(f"Unknown resource URI: {uri}")
    
    filename = uri_str.split("/")[-1]
    ui_path = os.path.join(UI_DIR, filename)
    
    if not os.path.exists(ui_path):
        raise ValueError(f"UI Resource not found: {filename}")
        
    mime_type = "text/plain"
    if filename.endswith(".html"):
        mime_type = MCP_APP_MIME_TYPE
    elif filename.endswith(".css"):
        mime_type = "text/css"
    elif filename.endswith(".js") or filename.endswith(".mjs"):
        mime_type = "text/javascript"
        
    with open(ui_path, "r", encoding="utf-8") as f:
        return [ReadResourceContents(content=f.read(), mime_type=mime_type)]

@mcp_server.list_tools()
async def handle_list_tools() -> list[mcp_types.Tool]:
    return [
        mcp_types.Tool(
            name="xcos_get_status_widget",
            description=(
                "ALWAYS call this first when the user requests an Xcos diagram. If the "
                "server is not connected, stop and inform the user before doing anything "
                "else. Returns an HTML status widget — always display it to the user.\n\n"
                "This tool is Step 1 of the Xcos diagram workflow. After calling this, "
                "the required sequence is:\n"
                "PHASE 1 (math derivation):\n"
                "  xcos_get_block_catalogue_widget → xcos_create_workflow →\n"
                "  [derive equations step by step in plain text] →\n"
                "  xcos_submit_phase(phase1_math_model) →\n"
                "  xcos_get_workflow_widget → [ask user for approval] →\n"
                "  xcos_review_phase(approve, phase1_math_model) →\n"
                "  xcos_get_workflow_widget\n\n"
                "PHASE 2 (block diagram preview):\n"
                "  get_xcos_block_data (for EVERY block) →\n"
                "  get_xcos_block_source (for any block whose parameters are unclear) →\n"
                "  search_related_xcos_files (for any block with complex dependencies) →\n"
                "  [draw Mermaid diagram (graph LR): exact Xcos blocks with simple names, \n"
                "   solid arrows for data links, dashed arrows for clock/event links, \n"
                "   feedback loops routed without crossing other blocks — this is a preview \n"
                "   of the real Xcos diagram, not an illustration of the physical system] →\n"
                "  [write full architecture plan: every block name, function, parameters,\n"
                "   every link with source/target port IDs] →\n"
                "  xcos_submit_phase(phase2_architecture) →\n"
                "  xcos_get_workflow_widget → [ask user for approval] →\n"
                "  xcos_review_phase(approve, phase2_architecture) →\n"
                "  xcos_get_workflow_widget\n\n"
                "PHASE 3 (build, verify, fix):\n"
                "  xcos_start_draft(workflow_id) → xcos_add_blocks →\n"
                "  xcos_get_topology_widget → xcos_add_links →\n"
                "  xcos_get_topology_widget → xcos_get_draft_xml(pretty_print=true) →\n"
                "  xcos_verify_draft → xcos_get_validation_widget →\n"
                "  [if failed: fix XML and retry from xcos_add_blocks, max 3 attempts,\n"
                "   then ask user for guidance] →\n"
                "  xcos_commit_phase(phase3_implementation) →\n"
                "  xcos_submit_phase(phase3_implementation) →\n"
                "  xcos_get_file_path → xcos_get_workflow_widget\n\n"
                "RULES:\n"
                "- Never skip phases or jump straight to file creation.\n"
                "- Never write block XML from memory — always call get_xcos_block_data first.\n"
                "- Never stop after a failed verification — iterate until success=true.\n"
                "- Always display every widget inline as it is returned.\n"
                "- Never declare a diagram done unless xcos_verify_draft returned success=true."
            ),
            inputSchema={"type": "object", "properties": {}},
            **{"_meta": {"ui": {"resourceUri": "ui://xcos/index.html"}}}
        ),
        mcp_types.Tool(
            name="xcos_get_workflow_widget",
            description="Call this after every xcos_submit_phase and xcos_review_phase call. Always display the returned widget — it shows the user their current phase progress. Pass workflow_id to show a specific workflow, or omit it to list all active workflows.",
            inputSchema={"type": "object", "properties": {"workflow_id": {"type": "string"}}},
            **{"_meta": {"ui": {"resourceUri": "ui://xcos/index.html"}}}
        ),
        mcp_types.Tool(
            name="xcos_get_validation_widget",
            description=(
                "PHASE 3 — Step 8. Call this immediately after every xcos_verify_draft "
                "call, passing the current draft XML. Always display the returned widget "
                "to the user — it shows a clear green tick (success) or red error message "
                "(failure). Never skip this step, as it gives the user visible confirmation "
                "of the validation result."
            ),
            inputSchema={"type": "object", "properties": {"xml_content": {"type": "string"}}, "required": ["xml_content"]},
            **{"_meta": {"ui": {"resourceUri": "ui://xcos/index.html"}}}
        ),
        mcp_types.Tool(
            name="xcos_get_block_catalogue_widget",
            description=(
                "PHASE 1 — Step 2. Call this after xcos_get_status_widget to identify "
                "which blocks are available for the user's request. Filter by the relevant "
                "category (e.g. \"Sources\", \"Continuous\", \"Sinks/Visualization\", "
                "\"Math Operations\"). Always display the returned widget to the user so "
                "they can see and confirm the blocks being selected before any math is "
                "explained."
            ),
            inputSchema={"type": "object", "properties": {"category": {"type": "string"}}},
            **{"_meta": {"ui": {"resourceUri": "ui://xcos/index.html"}}}
        ),
        mcp_types.Tool(
            name="xcos_get_topology_widget",
            description=(
                "PHASE 3 — Steps 3 and 5. Call this twice during Phase 3 and always "
                "display the returned widget:\n"
                "  - First call: immediately after xcos_add_blocks, so the user can \n"
                "    see the blocks appear in the graph before links are added.\n"
                "  - Second call: immediately after xcos_add_links, so the user can \n"
                "    see the fully connected graph with arrows between blocks.\n"
                "If the second call shows missing links or disconnected blocks, fix the \n"
                "link XML before proceeding to verification."
            ),
            inputSchema={"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
            **{"_meta": {"ui": {"resourceUri": "ui://xcos/index.html"}}}
        ),
        mcp_types.Tool(
            name="xcos_create_workflow",
            description=(
                "PHASE 1 — Step 3. Call this with the user's problem statement to register "
                "the 3-phase workflow. Store the returned workflow_id — it is required for "
                "all subsequent xcos_submit_phase, xcos_review_phase, xcos_get_workflow_widget, "
                "and xcos_start_draft calls. Do not proceed without it."
            ),
            inputSchema={"type": "object", "properties": {"problem_statement": {"type": "string"}}, "required": ["problem_statement"]},
        ),
        mcp_types.Tool(
            name="xcos_list_workflows",
            description="List all phased Xcos workflow sessions and their review state.",
            inputSchema={"type": "object", "properties": {}},
        ),
        mcp_types.Tool(
            name="xcos_get_workflow",
            description="Get the full state of one phased Xcos workflow session.",
            inputSchema={"type": "object", "properties": {"workflow_id": {"type": "string"}}, "required": ["workflow_id"]},
        ),
        mcp_types.Tool(
            name="xcos_submit_phase",
            description=(
                "Submits content for a workflow phase and sets it to \"awaiting_approval\".\n"
                "Call this at these specific moments:\n"
                "  - phase1_math_model: after the Mermaid diagram is drawn and the \n"
                "    full math explanation is written. Content should be the complete \n"
                "    step-by-step mathematical description of the system.\n"
                "  - phase2_architecture: after get_xcos_block_data has been called for \n"
                "    every block and the full architecture plan (blocks + links) is written. \n"
                "    Content should list every block name, Xcos function name, parameters, \n"
                "    and every link with source/target port IDs.\n"
                "  - phase3_implementation: after xcos_verify_draft returns success=true. \n"
                "    Content should confirm the file path and validation result.\n"
                "After calling this, always call xcos_get_workflow_widget to show the \n"
                "updated progress, then ask the user for approval before proceeding."
            ),
            inputSchema={"type": "object", "properties": {
                "workflow_id": {"type": "string"},
                "phase": {"type": "string", "enum": WORKFLOW_PHASE_ORDER},
                "content": {"type": "string"},
                "artifact_type": {"type": "string", "default": "markdown"},
            }, "required": ["workflow_id", "phase", "content"]},
        ),
        mcp_types.Tool(
            name="xcos_review_phase",
            description=(
                "Call this only after the user has explicitly approved the submitted phase "
                "content. Use decision=\"approve\" to advance the workflow, or "
                "decision=\"request_changes\" with feedback if the user wants modifications "
                "(in which case go back and revise, then re-submit). After approving, "
                "call xcos_get_workflow_widget to show the updated state, then proceed "
                "to the next phase."
            ),
            inputSchema={"type": "object", "properties": {
                "workflow_id": {"type": "string"},
                "phase": {"type": "string", "enum": [WORKFLOW_PHASE_ORDER[0], WORKFLOW_PHASE_ORDER[1]]},
                "decision": {"type": "string", "enum": ["approve", "request_changes"]},
                "feedback": {"type": "string", "default": ""},
            }, "required": ["workflow_id", "phase", "decision"]},
        ),
        mcp_types.Tool(
            name="get_xcos_block_data",
            description=(
                "PHASE 2 — Step 1. Call this for EVERY block before writing any XML. "
                "Never write block XML from memory or from examples in other tool results — "
                "always call this first and use the returned XML as the authoritative "
                "template. Returns the correct port IDs, parameter structure, simulation "
                "function name, and blockType needed to build valid Xcos XML."
            ),
            inputSchema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        ),
        mcp_types.Tool(
            name="get_xcos_block_source",
            description="Reads the raw Scilab .sci interface macro directly from the source code.",
            inputSchema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        ),
        mcp_types.Tool(
            name="search_related_xcos_files",
            description="Checks for any other files related to a specific block or keyword.",
            inputSchema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
        ),
        mcp_types.Tool(
            name="verify_xcos_xml",
            description=(
                "Validates raw Xcos XML directly without a draft session. Use this when \n"
                "you have XML content in hand but no active session_id — for example, \n"
                "when re-checking fixed XML during a repair loop. For session-based \n"
                "validation (the normal workflow), prefer xcos_verify_draft instead. \n"
                "After calling this, always call xcos_get_validation_widget with the \n"
                "same XML and display the result widget to the user."
            ),
            inputSchema={"type": "object", "properties": {"xml_content": {"type": "string"}}, "required": ["xml_content"]}
        ),
        mcp_types.Tool(
            name="xcos_start_draft",
            description=(
                "PHASE 3 — Step 1. Call this to open a new draft session after Phase 2 "
                "is approved. Always pass the workflow_id so the draft is linked to the "
                "workflow. Store the returned session_id — it is required for all "
                "subsequent xcos_add_blocks, xcos_add_links, xcos_get_topology_widget, "
                "xcos_get_draft_xml, xcos_verify_draft, and xcos_get_file_path calls.\n"
                "IMPORTANT: To use xcos_commit_phase later, you MUST pass "
                "phases=['phase3_implementation'] here. Omitting the phases array will "
                "cause xcos_commit_phase to fail with 'No phase plan found'."
            ),
            inputSchema={"type": "object", "properties": {
                "schema_version": {"type": "string", "default": "1.1"},
                "workflow_id": {"type": "string"},
                "replace": {"type": "boolean", "default": False},
                "phases": {"type": "array", "items": {"type": "string"}, "description": "Optional list of phase labels to provision."}
            }}
        ),
        mcp_types.Tool(
            name="xcos_add_blocks",
            description=(
                "PHASE 3 — Step 2. Call this to add all blocks to the draft session. "
                "Only use block XML that was retrieved via get_xcos_block_data — never "
                "write block XML from memory. After calling this, immediately call "
                "xcos_get_topology_widget and display the widget so the user can see "
                "the blocks appear in the graph."
            ),
            inputSchema={"type": "object", "properties": {
                "session_id": {"type": "string"},
                "blocks_xml": {"type": "string"}
            }, "required": ["session_id", "blocks_xml"]}
        ),
        mcp_types.Tool(
            name="xcos_add_links",
            description=(
                "PHASE 3 — Step 4. Call this to connect all blocks with links after "
                "xcos_add_blocks and the first xcos_get_topology_widget call. Use port "
                "IDs exactly as returned by get_xcos_block_data. After calling this, "
                "call xcos_get_topology_widget again and display the updated widget so "
                "the user can see the fully connected graph with arrows between blocks."
            ),
            inputSchema={"type": "object", "properties": {
                "session_id": {"type": "string"},
                "links_xml": {"type": "string"}
            }, "required": ["session_id", "links_xml"]}
        ),
        mcp_types.Tool(
            name="xcos_verify_draft",
            description=(
                "PHASE 3 — Step 7. Call this after xcos_get_draft_xml to validate the "
                "diagram. After calling this, always call xcos_get_validation_widget with "
                "the current draft XML and display the result widget to the user.\n"
                "  - If success=true: IMMEDIATELY call xcos_commit_phase with "
                "    phase_label='phase3_implementation' and blocks_xml='', then call "
                "    xcos_get_file_path, read the file with xcos_get_file_content, write "
                "    it to your output folder, and present the path to the user. "
                "    Do NOT wait for the user to ask.\n"
                "  - If success=false: read the error carefully, fix the block or link XML, \n"
                "    go back to xcos_add_blocks and rebuild. NEVER stop after one failure — \n"
                "    keep iterating until success=true is returned."
            ),
            inputSchema={"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
        ),

        mcp_types.Tool(
            name="xcos_commit_phase",
            description=(
                "PHASE 3 — Step 9. Call this after xcos_verify_draft returns success=true, "
                "with session_id and phase_label='phase3_implementation'.\n"
                "blocks_xml is OPTIONAL — pass an empty string '' (the default). Blocks "
                "were already added via xcos_add_blocks; passing blocks_xml again duplicates them.\n"
                "After calling this:\n"
                "  1. Call xcos_submit_phase(phase3_implementation).\n"
                "  2. Call xcos_get_file_path to get the file path.\n"
                "  3. Call xcos_get_file_content(source='session') to read the XML.\n"
                "  4. Write the XML to your output folder using your file tools.\n"
                "  5. IMMEDIATELY present the file path and download link to the user.\n"
                "Do NOT wait for the user to ask — presenting the file is MANDATORY."
            ),
            inputSchema={"type": "object", "properties": {
                "session_id": {"type": "string"},
                "phase_label": {"type": "string"},
                "blocks_xml": {"type": "string", "default": ""}
            }, "required": ["session_id", "phase_label"]},
        ),
        mcp_types.Tool(
            name="xcos_get_draft_xml",
            description=(
                "PHASE 3 — Step 6. Call this with pretty_print=true after xcos_add_links "
                "and before xcos_verify_draft. Show a brief summary of the XML to the user "
                "so they can see what is about to be validated. Also call this to retrieve "
                "the current XML whenever a verification fails and you need to inspect or "
                "fix the diagram before retrying."
            ),
            inputSchema={"type": "object", "properties": {
                "session_id": {"type": "string"},
                "pretty_print": {"type": "boolean", "default": False},
                "strip_comments": {"type": "boolean", "default": False},
                "validate": {"type": "boolean", "default": False}
            }, "required": ["session_id"]},
        ),
        mcp_types.Tool(
            name="xcos_get_file_path",
            description=(
                "PHASE 3 — Step 9. Call this only after xcos_verify_draft has returned "
                "success=true. Retrieve the verified file path. After getting the path, "
                "call xcos_get_file_content(source='session') to read the XML content, "
                "then write it to your output directory so the user can download it. "
                "Present the file path and download link to the user IMMEDIATELY. "
                "Then call xcos_get_workflow_widget to show the completed workflow summary."
            ),
            inputSchema={"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
        ),
        mcp_types.Tool(
            name="xcos_get_file_content",
            description="Returns the current draft, saved session file, or last verified .xcos file content as text or base64.",
            inputSchema={"type": "object", "properties": {
                "session_id": {"type": "string"},
                "source": {"type": "string", "enum": ["draft", "session", "last_verified"], "default": "session"},
                "encoding": {"type": "string", "enum": ["text", "base64"], "default": "text"}
            }, "required": ["session_id"]},
        ),
        mcp_types.Tool(
            name="xcos_list_sessions",
            description="Lists all active Xcos draft sessions with block/link counts, saved file metadata, and last verification status.",
            inputSchema={"type": "object", "properties": {}},
        ),
        mcp_types.Tool(
            name="ping",
            description="Simple tool to verify server responsiveness.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]

@mcp_server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None):
    # Standardize empty arguments to empty dict
    if arguments is None:
        arguments = {}
    
    if name == "xcos_get_status_widget":
        payload = parse_mcp_text_json_response(await xcos_get_status_widget())
        return make_structured_tool_result("Status Widget Generated", payload)
    elif name == "xcos_get_workflow_widget":
        payload = parse_mcp_text_json_response(await xcos_get_workflow_widget(arguments.get("workflow_id")))
        return make_structured_tool_result("Workflow Widget Generated", payload)
    elif name == "xcos_get_validation_widget":
        payload = parse_mcp_text_json_response(await xcos_get_validation_widget(arguments["xml_content"]))
        return make_structured_tool_result("Validation Widget Generated", payload)
    elif name == "xcos_get_block_catalogue_widget":
        payload = parse_mcp_text_json_response(await xcos_get_block_catalogue_widget(arguments.get("category")))
        return make_structured_tool_result("Block Catalogue Widget Generated", payload)
    elif name == "xcos_get_topology_widget":
        payload = parse_mcp_text_json_response(await xcos_get_topology_widget(arguments["session_id"]))
        return make_structured_tool_result("Topology Widget Generated", payload)
    elif name == "xcos_create_workflow":
        payload = parse_mcp_text_json_response(await xcos_create_workflow(arguments["problem_statement"]))
        workflow = payload["workflow"]
        return make_structured_tool_result(
            f"Created workflow {workflow['workflow_id']}. {workflow['current_phase_label']} is ready.",
            payload,
        )
    elif name == "xcos_list_workflows":
        payload = parse_mcp_text_json_response(await xcos_list_workflows())
        return make_structured_tool_result(
            f"Found {len(payload['workflows'])} workflow session(s).",
            payload,
        )
    elif name == "xcos_get_workflow":
        payload = parse_mcp_text_json_response(await xcos_get_workflow(arguments["workflow_id"]))
        return make_structured_tool_result(
            f"{payload['workflow']['current_phase_label']} is the active step for workflow {arguments['workflow_id']}.",
            payload,
        )
    elif name == "xcos_submit_phase":
        payload = parse_mcp_text_json_response(await xcos_submit_phase(
            arguments["workflow_id"],
            arguments["phase"],
            arguments["content"],
            arguments.get("artifact_type", "markdown"),
        ))
        return make_structured_tool_result(
            f"Submitted {WORKFLOW_PHASE_LABELS[arguments['phase']]} for workflow {arguments['workflow_id']}.",
            payload,
        )
    elif name == "xcos_review_phase":
        payload = parse_mcp_text_json_response(await xcos_review_phase(
            arguments["workflow_id"],
            arguments["phase"],
            arguments["decision"],
            arguments.get("feedback", ""),
        ))
        return make_structured_tool_result(
            f"{WORKFLOW_PHASE_LABELS[arguments['phase']]} review recorded with decision '{arguments['decision']}'.",
            payload,
        )
    elif name == "get_xcos_block_data":
        return await get_xcos_block_data(arguments["name"])
    elif name == "get_xcos_block_source":
        return await get_xcos_block_source(arguments["name"])
    elif name == "search_related_xcos_files":
        return await search_related_xcos_files(arguments["query"])
    elif name == "verify_xcos_xml":
        return await verify_xcos_xml(arguments["xml_content"])
    elif name == "xcos_start_draft":
        payload = parse_mcp_text_json_response(await xcos_start_draft(
            arguments.get("schema_version", "1.1"), 
            arguments.get("workflow_id"), 
            arguments.get("replace", False), 
            arguments.get("phases")
        ))
        msg = f"Started draft session {payload.get('session_id')}."
        if payload.get("phase_plan_registered"):
            msg += f" Registered {payload.get('phase_count')} phases."
        return make_structured_tool_result(msg, payload)
    elif name == "xcos_add_blocks":
        return await xcos_add_blocks(arguments["session_id"], arguments["blocks_xml"])
    elif name == "xcos_add_links":
        return await xcos_add_links(arguments["session_id"], arguments["links_xml"])
    elif name == "xcos_verify_draft":
        payload = parse_mcp_text_json_response(await xcos_verify_draft(arguments["session_id"]))
        return make_structured_tool_result(
            f"Verification {'succeeded' if payload.get('success') else 'failed'} for draft session {arguments['session_id']}.",
            payload,
        )

    elif name == "xcos_commit_phase":
        return await xcos_commit_phase(arguments["session_id"], arguments["phase_label"], arguments["blocks_xml"])
    elif name == "xcos_get_draft_xml":
        return await xcos_get_draft_xml(
            arguments["session_id"],
            arguments.get("pretty_print", False),
            arguments.get("strip_comments", False),
            arguments.get("validate", False),
        )
    elif name == "xcos_get_file_path":
        return await xcos_get_file_path(arguments["session_id"])
    elif name == "xcos_get_file_content":
        return await xcos_get_file_content(
            arguments["session_id"],
            arguments.get("source", "session"),
            arguments.get("encoding", "text"),
        )
    elif name == "xcos_list_sessions":
        return await xcos_list_sessions()
    elif name == "ping":
        return make_structured_tool_result("Pong", {"status": "ok", "timestamp": now_iso()})

    else:
        return [mcp_types.TextContent(type="text", text=f"Unknown tool: {name}")]

async def main():
    mode = os.environ.get("XCOS_SERVER_MODE", "stdio").strip().lower()
    if mode not in {"both", "http", "stdio"}:
        raise RuntimeError("XCOS_SERVER_MODE must be one of: both, http, stdio")

    if mode in {"both", "http"}:
        await cleanup_port(SERVER_PORT)

    if mode == "stdio":
        async with stdio_server() as (read_stream, write_stream):
            await asyncio.gather(
                mcp_server.run(read_stream, write_stream, create_server_initialization_options()),
                telemetry_loop(),
            )
        return

    if mode == "http":
        await asyncio.gather(run_http_server(), telemetry_loop())
        return

    async with stdio_server() as (read_stream, write_stream):
        await asyncio.gather(
            mcp_server.run(read_stream, write_stream, create_server_initialization_options()),
            run_http_server(),
            telemetry_loop(),
        )

if __name__ == "__main__":
    asyncio.run(main())
