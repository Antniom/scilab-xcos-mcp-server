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

# SVG Icon Metadata
SVG_ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" fill="none" stroke="#D97757" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <rect x="20" y="20" width="24" height="24" rx="4"/>
    <path d="M32 25 Q32 32 25 32 Q32 32 32 39 Q32 32 39 32 Q32 32 32 25" fill="#D97757" stroke="none"/>
    <line x1="4" y1="32" x2="20" y2="32"/>
    <polyline points="14,28 20,32 14,36"/>
    <line x1="44" y1="32" x2="60" y2="32"/>
    <polyline points="54,28 60,32 54,36"/>
    <path d="M 52 32 V 52 H 12 V 32"/>
    <polyline points="8,36 12,32 16,36"/>
</svg>"""

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
PORT_REGISTRY_PATH = os.path.join(DATA_DIR, "blocks", "port_registry.json")
TEMP_OUTPUT_DIR = os.environ.get("XCOS_TEMP_OUTPUT_DIR", os.path.join(DATA_DIR, "temp"))
SESSION_OUTPUT_DIR = os.environ.get("XCOS_SESSION_OUTPUT_DIR", os.path.join(BASE_DIR, "sessions"))
SERVER_PORT = int(os.environ.get("PORT", os.environ.get("XCOS_SERVER_PORT", "8000")))
MCP_HTTP_PATH = os.environ.get("XCOS_MCP_HTTP_PATH", "/mcp")
WORKFLOW_UI_RESOURCE_URI = "ui://xcos/workflow-dashboard.html"
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
    if phase.status != "awaiting_approval":
        return None, f"{WORKFLOW_PHASE_LABELS[phase_key]} is not awaiting approval. Current status: {phase.status}."

    normalized_decision = decision.strip().lower()
    if normalized_decision not in {"approve", "request_changes"}:
        return None, "Decision must be either 'approve' or 'request_changes'."

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
    "//EventInBlock | //EventOutBlock | //ExplicitInBlock | //ExplicitOutBlock"
)
XCOS_LINK_XPATH = "//BasicLink | //ExplicitLink | //CommandControlLink"


def make_text_response(text: str):
    return [mcp_types.TextContent(type="text", text=text)]


def make_json_response(payload):
    return make_text_response(json.dumps(payload, indent=2))


def make_structured_tool_result(summary: str, payload: dict):
    return ([mcp_types.TextContent(type="text", text=summary)], payload)


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
    ui_path = os.path.join(UI_DIR, "workflow-dashboard.html")
    with open(ui_path, "r", encoding="utf-8") as f:
        return f.read()


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


def resolve_scilab_binary() -> str | None:
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

    command = [scilab_bin]
    lower_bin = scilab_bin.lower()
    if lower_bin.endswith(".bat"):
        command = [scilab_bin]
    else:
        command.extend(["-nb", "-f", verify_script_path])
        lower_bin = ""

    if not command[-1].endswith(".sce"):
        command.extend(["-nb", "-f", verify_script_path])

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=BASE_DIR,
    )
    stdout, stderr = await process.communicate()
    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")
    combined = (stdout_text + "\n" + stderr_text).strip()

    warnings = []
    success = "XCOSAI_VERIFY_OK" in combined and process.returncode == 0
    error = None

    for line in combined.splitlines():
        if line.startswith("XCOSAI_VERIFY_WARN:"):
            warnings.append(line.split(":", 1)[1].strip())
        elif line.startswith("XCOSAI_VERIFY_ERROR:"):
            error = line.split(":", 1)[1].strip()

    if not success and not error:
        error = combined[-4000:] if combined else f"Scilab exited with code {process.returncode}"

    payload = {
        "success": success,
        "origin": "subprocess-validator",
        "task_id": task_id,
        "file_path": temp_meta["path"],
        "file_size_bytes": temp_meta["size_bytes"],
        "auto_fixed_mux_to_scalar": auto_fixed,
        "validator_mode": "subprocess",
        "warnings": warnings,
    }
    if not success:
        payload["error"] = error
        payload["hint"] = "Headless Scilab validation failed. Inspect stdout/stderr and the generated .xcos file."
        payload["stdout"] = stdout_text[-4000:]
        payload["stderr"] = stderr_text[-4000:]
    return payload

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
    results = []
    
    # 1. INFO (from get_xcos_block_info logic)
    info_path = os.path.join(DATA_DIR, "blocks", f"{name}.json")
    if os.path.exists(info_path):
        with open(info_path, 'r', encoding='utf-8') as f:
            results.append(f"=== INFO ===\n{f.read()}")
    else:
        results.append(f"=== INFO ===\nError: Block info for '{name}' not found at {info_path}")

    # 2. EXAMPLE (from get_xcos_block_example logic)
    example_path = os.path.join(DATA_DIR, "reference_blocks", f"{name}.xcos")
    if os.path.exists(example_path):
        with open(example_path, 'r', encoding='utf-8') as f:
            results.append(f"=== EXAMPLE ===\n{f.read()}")
    else:
        results.append(f"=== EXAMPLE ===\nError: Reference block '{name}' not found at {example_path}")

    extra_example_prefix = f"{name}__"
    reference_dir = os.path.join(DATA_DIR, "reference_blocks")
    extra_example_files = sorted(
        file_name
        for file_name in os.listdir(reference_dir)
        if file_name.startswith(extra_example_prefix) and file_name.endswith(".xcos")
    )
    for extra_file_name in extra_example_files:
        label = os.path.splitext(extra_file_name)[0].split("__", 1)[1].replace("_", " ")
        extra_path = os.path.join(reference_dir, extra_file_name)
        with open(extra_path, "r", encoding="utf-8") as f:
            results.append(f"=== EXTRA EXAMPLE: {label} ===\n{f.read()}")

    # 3. HELP (from get_xcos_block_help logic)
    help_file = None
    search_dir = os.path.join(DATA_DIR, "help")
    for root, dirs, files in os.walk(search_dir):
        if f"{name}.xml" in files:
            help_file = os.path.join(root, f"{name}.xml")
            break
    
    if not help_file:
        results.append(f"=== HELP ===\nError: Help file for '{name}' not found in {search_dir}")
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
                results.append(f"=== HELP ===\nNo parameter sections found in {help_file}")
            else:
                results.append(f"=== HELP ===\n" + "\n\n".join(extracted_text))
        except Exception as e:
            results.append(f"=== HELP ===\nError parsing help XML: {str(e)}")

    return make_text_response("\n\n".join(results))

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
        return await run_subprocess_verification(xml_content, auto_fixed)

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


async def xcos_open_workflow_ui():
    return make_json_response({
        "workflows": list_workflow_payloads(),
        "ui_resource_uri": WORKFLOW_UI_RESOURCE_URI,
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


async def xcos_start_draft(schema_version: str = "1.1", workflow_id: str | None = None):
    workflow = None
    if workflow_id:
        workflow = get_workflow(workflow_id)
        if not workflow:
            return make_text_response(f"Error: Workflow {workflow_id} not found")
        if workflow.phases["phase2_architecture"].status != "approved":
            return make_text_response(
                "Error: Phase 2 must be approved before Phase 3 implementation can start."
            )

    session_id = str(uuid.uuid4())
    state.drafts[session_id] = DraftDiagram(schema_version)

    payload = {
        "status": "success",
        "session_id": session_id,
        "message": f"Started new Xcos draft session {session_id}",
        "critical_rule": "IMPORTANT: Any ExplicitOutputPort or EventOutPort that fanning out to multiple downstream blocks REQUIRES an intermediate SplitBlock (for data) or CLKSPLIT_f (for events)."
    }

    if workflow:
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
        sessions.append({
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
        })
    return make_json_response(sessions)

async def xcos_revert_phase(session_id: str, phase_label: str):
    if session_id not in state.drafts or session_id not in state.phase_plans:
        return make_text_response(f"Error: Session {session_id} or its phase plan not found")
    
    plan = state.phase_plans[session_id]
    if phase_label not in plan["completed"]:
        return make_text_response(f"Error: Phase '{phase_label}' has not been committed yet.")
    
    # This is a simple implementation: truncate blocks to the state BEFORE this phase was added
    # In a real system, we'd track exactly which blocks belonged to which phase.
    # For now, let's just warn that this is a placeholder or implement it properly by tracking phase->block mapping.
    return make_text_response("Error: xcos_revert_phase is not fully implemented yet. Please manually manage the draft or start a new session.")

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

async def xcos_plan_phases(session_id: str, phases: list[str]):
    if session_id not in state.drafts:
        state.drafts[session_id] = DraftDiagram()
    
    state.phase_plans[session_id] = {
        "phases": phases,
        "completed": []
    }
    return make_json_response({
        "status": "success",
        "phase_count": len(phases),
        "labels": phases
    })

async def xcos_commit_phase(session_id: str, phase_label: str, blocks_xml: str):
    if session_id not in state.drafts:
        return make_text_response(f"Error: Session {session_id} not found")
    
    if session_id not in state.phase_plans:
        return make_text_response(f"Error: No phase plan found for session {session_id}. Call xcos_plan_phases first.")
    
    plan = state.phase_plans[session_id]
    if phase_label not in plan["phases"]:
        return make_text_response(f"Error: Phase '{phase_label}' not found in the plan.")
    
    # Append blocks
    state.drafts[session_id].add_blocks(blocks_xml)
    
    # Mark as complete (avoid duplicates if re-committed)
    if phase_label not in plan["completed"]:
        plan["completed"].append(phase_label)
    
    session_meta = write_session_snapshot(session_id)
    
    completed_count = len(plan["completed"])
    total_count = len(plan["phases"])
    remaining = [p for p in plan["phases"] if p not in plan["completed"]]
    
    return make_json_response({
        "status": "success",
        "completed_count": completed_count,
        "total_count": total_count,
        "remaining_phases": remaining,
        "written_to": session_meta["path"],
        "file_size_bytes": session_meta["size_bytes"]
    })


async def xcos_get_file_path(session_id: str):
    if session_id not in state.drafts:
        return make_text_response(f"Error: Session {session_id} not found")

    session_meta = write_session_snapshot(session_id)
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
        session_meta = write_session_snapshot(session_id)
        file_path = session_meta["path"]
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
    while True:
        if detect_validation_mode() == "subprocess":
            print(
                f"{Fore.CYAN}[HEADLESS] Scilab subprocess validation enabled{Style.RESET_ALL} on port {SERVER_PORT}      ",
                end="\r",
                file=sys.stderr,
            )
            await asyncio.sleep(5)
            continue
        if state.last_poll_time:
            delta = (datetime.now() - state.last_poll_time).total_seconds()
            if delta < 5:
                print(f"{Fore.GREEN}[CONNECTED] Scilab Connected{Style.RESET_ALL} (last poll: {delta:.1f}s ago)", end="\r", file=sys.stderr)
            else:
                print(f"{Fore.RED}[DISCONNECTED] Awaiting Scilab Polling{Style.RESET_ALL} (idle for {delta:.1f}s)    ", end="\r", file=sys.stderr)
        else:
            print(f"{Fore.YELLOW}[INITIALIZING] Initializing Connection Status...{Style.RESET_ALL}            ", end="\r", file=sys.stderr)
        await asyncio.sleep(1)

# --- MCP Server Setup ---

mcp_server = Server(
    "scilab-xcos-server",
    instructions=(
        "Use the phased Xcos workflow. Phase 1 derives the mathematical model and waits for approval. "
        "Phase 2 defines block architecture, parameters, and links and waits for approval. "
        "Phase 3 starts only after approval and builds/verifies the draft."
    ),
)

streamable_http_manager = StreamableHTTPSessionManager(
    app=mcp_server,
    json_response=False,
    stateless=False,
)


def workflow_ui_meta() -> dict:
    return {
        "ui": {
            "resourceUri": WORKFLOW_UI_RESOURCE_URI,
            "prefersBorder": True,
            "csp": {
                "resourceDomains": ["https://esm.sh"],
                "connectDomains": [],
            },
        }
    }


@mcp_server.list_prompts()
async def handle_list_prompts() -> list[mcp_types.Prompt]:
    return [
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
    if name != "xcos-phased-workflow":
        raise ValueError(f"Unknown prompt: {name}")

    problem_statement = (arguments or {}).get("problem_statement", "").strip()
    prompt_text = (
        "You are an Expert Control Systems Engineer specializing in Scilab Xcos modeling.\n\n"
        "Workflow:\n"
        "1. Phase 1: derive the mathematical model, show the calculations step by step, and wait for explicit approval.\n"
        "2. Phase 2: define the block diagram architecture, list block parameters and connections, enforce SplitBlock/CLKSPLIT_f for fan-out, and wait for explicit approval.\n"
        "3. Phase 3: only after approval, create a draft, build XML, verify it, debug with block data/source when needed, and present the validated result.\n\n"
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
    return [
        mcp_types.Resource(
            name="xcos-workflow-dashboard",
            uri=WORKFLOW_UI_RESOURCE_URI,
            description="Interactive phased workflow dashboard for reviewing Xcos Phase 1 and Phase 2 before implementation.",
            mimeType=MCP_APP_MIME_TYPE,
            _meta=workflow_ui_meta(),
        )
    ]


@mcp_server.read_resource()
async def handle_read_resource(uri):
    if str(uri) != WORKFLOW_UI_RESOURCE_URI:
        raise ValueError(f"Unknown resource URI: {uri}")
    return [
        ReadResourceContents(
            content=load_ui_html(),
            mime_type=MCP_APP_MIME_TYPE,
            meta=workflow_ui_meta(),
        )
    ]

@mcp_server.list_tools()
async def handle_list_tools() -> list[mcp_types.Tool]:
    return [
        mcp_types.Tool(
            name="xcos_open_workflow_ui",
            description="Open the phased Xcos workflow dashboard UI.",
            inputSchema={"type": "object", "properties": {}},
            annotations=mcp_types.ToolAnnotations(
                title="Open Workflow Dashboard",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
            _meta=workflow_ui_meta(),
            outputSchema={
                "type": "object",
                "properties": {
                    "workflows": {"type": "array"},
                    "ui_resource_uri": {"type": "string"},
                },
                "required": ["workflows", "ui_resource_uri"],
            },
        ),
        mcp_types.Tool(
            name="xcos_create_workflow",
            description="Create a new 3-phase Xcos workflow session from a control-system problem statement.",
            inputSchema={"type": "object", "properties": {"problem_statement": {"type": "string"}}, "required": ["problem_statement"]},
            outputSchema={"type": "object", "properties": {"status": {"type": "string"}, "workflow": {"type": "object"}}, "required": ["status", "workflow"]},
        ),
        mcp_types.Tool(
            name="xcos_list_workflows",
            description="List all phased Xcos workflow sessions and their review state.",
            inputSchema={"type": "object", "properties": {}},
            annotations=mcp_types.ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
            outputSchema={"type": "object", "properties": {"workflows": {"type": "array"}}, "required": ["workflows"]},
        ),
        mcp_types.Tool(
            name="xcos_get_workflow",
            description="Get the full state of one phased Xcos workflow session.",
            inputSchema={"type": "object", "properties": {"workflow_id": {"type": "string"}}, "required": ["workflow_id"]},
            annotations=mcp_types.ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
            outputSchema={"type": "object", "properties": {"workflow": {"type": "object"}}, "required": ["workflow"]},
        ),
        mcp_types.Tool(
            name="xcos_submit_phase",
            description="Submit the content for Phase 1, Phase 2, or Phase 3 in a phased Xcos workflow session.",
            inputSchema={"type": "object", "properties": {
                "workflow_id": {"type": "string"},
                "phase": {"type": "string", "enum": WORKFLOW_PHASE_ORDER},
                "content": {"type": "string"},
                "artifact_type": {"type": "string", "default": "markdown"},
            }, "required": ["workflow_id", "phase", "content"]},
            outputSchema={"type": "object", "properties": {"status": {"type": "string"}, "workflow": {"type": "object"}}, "required": ["status", "workflow"]},
        ),
        mcp_types.Tool(
            name="xcos_review_phase",
            description="Approve Phase 1 or Phase 2, or request changes, in a phased Xcos workflow session.",
            inputSchema={"type": "object", "properties": {
                "workflow_id": {"type": "string"},
                "phase": {"type": "string", "enum": [WORKFLOW_PHASE_ORDER[0], WORKFLOW_PHASE_ORDER[1]]},
                "decision": {"type": "string", "enum": ["approve", "request_changes"]},
                "feedback": {"type": "string", "default": ""},
            }, "required": ["workflow_id", "phase", "decision"]},
            outputSchema={"type": "object", "properties": {"status": {"type": "string"}, "workflow": {"type": "object"}}, "required": ["status", "workflow"]},
        ),
        mcp_types.Tool(
            name="get_xcos_block_data",
            description="Returns annotation JSON, reference XML, and parameter help for an Xcos block.",
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
            description="Sends generated Xcos XML to an open Scilab instance for validation.",
            inputSchema={"type": "object", "properties": {"xml_content": {"type": "string"}}, "required": ["xml_content"]}
        ),
        mcp_types.Tool(
            name="xcos_start_draft",
            description="Core draft workflow: starts a new incremental Xcos diagram draft session.",
            inputSchema={"type": "object", "properties": {
                "schema_version": {"type": "string", "default": "1.1"},
                "workflow_id": {"type": "string"},
            }}
        ),
        mcp_types.Tool(
            name="xcos_add_blocks",
            description="Core draft workflow: adds block XML elements to an active draft session.",
            inputSchema={"type": "object", "properties": {
                "session_id": {"type": "string"},
                "blocks_xml": {"type": "string"}
            }, "required": ["session_id", "blocks_xml"]}
        ),
        mcp_types.Tool(
            name="xcos_add_links",
            description="Core draft workflow: adds link XML elements to an active draft session.",
            inputSchema={"type": "object", "properties": {
                "session_id": {"type": "string"},
                "links_xml": {"type": "string"}
            }, "required": ["session_id", "links_xml"]}
        ),
        mcp_types.Tool(
            name="xcos_verify_draft",
            description="Core draft workflow: assembles the current draft session, validates it in Scilab, and returns both the verified temp file path and the saved session snapshot path.",
            inputSchema={"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]}
        ),
        mcp_types.Tool(
            name="xcos_plan_phases",
            description="Call this first before generating any XML. Splits the diagram into named phases. Claude must complete one phase at a time and call xcos_commit_phase after each before proceeding to the next.",
            inputSchema={"type": "object", "properties": {
                "session_id": {"type": "string"},
                "phases": {"type": "array", "items": {"type": "string"}}
            }, "required": ["session_id", "phases"]}
        ),
        mcp_types.Tool(
            name="xcos_commit_phase",
            description="Call this after finishing each phase. Commits the generated XML to the file and signals that the next phase can begin. Do not start the next phase until this returns successfully.",
            inputSchema={"type": "object", "properties": {
                "session_id": {"type": "string"},
                "phase_label": {"type": "string"},
                "blocks_xml": {"type": "string"}
            }, "required": ["session_id", "phase_label", "blocks_xml"]}
        ),
        mcp_types.Tool(
            name="xcos_get_draft_xml",
            description="Core draft workflow: returns the full accumulated XML of the current draft session, with optional pretty-printing, comment stripping, and XML validation.",
            inputSchema={"type": "object", "properties": {
                "session_id": {"type": "string"},
                "pretty_print": {"type": "boolean", "default": False},
                "strip_comments": {"type": "boolean", "default": False},
                "validate": {"type": "boolean", "default": False}
            }, "required": ["session_id"]}
        ),
        mcp_types.Tool(
            name="xcos_get_file_path",
            description="Returns the saved draft session file path plus the latest verified file path/size metadata for download or export workflows.",
            inputSchema={"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]}
        ),
        mcp_types.Tool(
            name="xcos_get_file_content",
            description="Returns the current draft, saved session file, or last verified .xcos file content as text or base64.",
            inputSchema={"type": "object", "properties": {
                "session_id": {"type": "string"},
                "source": {"type": "string", "enum": ["draft", "session", "last_verified"], "default": "session"},
                "encoding": {"type": "string", "enum": ["text", "base64"], "default": "text"}
            }, "required": ["session_id"]}
        ),
        mcp_types.Tool(
            name="xcos_list_sessions",
            description="Lists all active Xcos draft sessions with block/link counts, saved file metadata, and last verification status.",
            inputSchema={"type": "object", "properties": {}}
        ),
        mcp_types.Tool(
            name="xcos_revert_phase",
            description="[UNIMPLEMENTED] Safety tool to roll back a draft to a previous phase.",
            inputSchema={"type": "object", "properties": {
                "session_id": {"type": "string"},
                "phase_label": {"type": "string"}
            }, "required": ["session_id", "phase_label"]}
        ),
    ]

@mcp_server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None):
    # Standardize empty arguments to empty dict
    if arguments is None:
        arguments = {}
    
    if name == "xcos_open_workflow_ui":
        payload = parse_mcp_text_json_response(await xcos_open_workflow_ui())
        return make_structured_tool_result("Opened the phased Xcos workflow dashboard.", payload)
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
        payload = parse_mcp_text_json_response(await xcos_start_draft(arguments.get("schema_version", "1.1"), arguments.get("workflow_id")))
        return make_structured_tool_result(
            f"Started draft session {payload['session_id']}.",
            payload,
        )
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
    elif name == "xcos_plan_phases":
        return await xcos_plan_phases(arguments["session_id"], arguments["phases"])
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
    elif name == "xcos_revert_phase":
        return await xcos_revert_phase(arguments["session_id"], arguments["phase_label"])
    else:
        return [mcp_types.TextContent(type="text", text=f"Unknown tool: {name}")]

async def main():
    mode = os.environ.get("XCOS_SERVER_MODE", "both").strip().lower()
    if mode not in {"both", "http", "stdio"}:
        raise RuntimeError("XCOS_SERVER_MODE must be one of: both, http, stdio")

    if mode in {"both", "http"}:
        await cleanup_port(SERVER_PORT)

    if mode == "stdio":
        async with stdio_server() as (read_stream, write_stream):
            await asyncio.gather(
                mcp_server.run(read_stream, write_stream, mcp_server.create_initialization_options()),
                telemetry_loop(),
            )
        return

    if mode == "http":
        await asyncio.gather(run_http_server(), telemetry_loop())
        return

    async with stdio_server() as (read_stream, write_stream):
        await asyncio.gather(
            mcp_server.run(read_stream, write_stream, mcp_server.create_initialization_options()),
            run_http_server(),
            telemetry_loop(),
        )

if __name__ == "__main__":
    asyncio.run(main())
