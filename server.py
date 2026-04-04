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
import hashlib
import re
import html
import urllib.request
import urllib.error
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

SERVER_VERSION = "1.0.3"
POLL_WORKER_IDLE_SECONDS = 5.0
LOCAL_POLL_WORKER_STARTUP_TIMEOUT_SECONDS = 20.0
HOSTED_POLL_WORKER_STARTUP_TIMEOUT_SECONDS = 60.0
VALIDATION_CACHE_LIMIT = 64
ASYNC_VALIDATION_BRIEF_WAIT_SECONDS = 1.0
LOCAL_DEFAULT_SCILAB_SUBPROCESS_TIMEOUT_SECONDS = 90.0
HOSTED_DEFAULT_SCILAB_SUBPROCESS_TIMEOUT_SECONDS = 180.0
LOCAL_DEFAULT_POLL_VALIDATION_TIMEOUT_SECONDS = 120.0
HOSTED_DEFAULT_POLL_VALIDATION_TIMEOUT_SECONDS = 420.0
LOCAL_DEFAULT_VALIDATION_JOB_TIMEOUT_SECONDS = 120.0
HOSTED_DEFAULT_VALIDATION_JOB_TIMEOUT_SECONDS = 720.0
DEFAULT_STARTUP_PREFLIGHT_TIMEOUT_SECONDS = 45.0
DEFAULT_VALIDATION_TIMEOUT_SECONDS = LOCAL_DEFAULT_VALIDATION_JOB_TIMEOUT_SECONDS
EXPOSE_INTERNAL_VALIDATION_DETAILS = os.environ.get("XCOS_DEBUG_TOOL_OUTPUT", "").strip().lower() in {"1", "true", "yes", "on"}
DEFAULT_VALIDATION_WORKER_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_VALIDATION_WORKER_MAX_POLL_INTERVAL_SECONDS = 10.0
DEFAULT_VALIDATION_WORKER_REQUEST_RETRY_COUNT = 3
DEFAULT_VALIDATION_WORKER_RETRY_BACKOFF_SECONDS = 1.0
REMOTE_VALIDATION_WORKER_RESULT_MARGIN_SECONDS = 15.0
VALIDATION_PROGRESS_UNSET = object()
VALIDATION_PROFILE_FULL_RUNTIME = "full_runtime"
VALIDATION_PROFILE_HOSTED_SMOKE = "hosted_smoke"
VALIDATION_PROFILES = {
    VALIDATION_PROFILE_FULL_RUNTIME,
    VALIDATION_PROFILE_HOSTED_SMOKE,
}

# Shared State
class SharedState:
    def __init__(self):
        self.task_queue = asyncio.Queue()
        self.results = {}  # task_id -> {"success": bool, "error": str, "event": asyncio.Event}
        self.validation_cache = {}  # xml_sha256 -> raw validation result
        self.last_poll_time = None
        self.status_lock = asyncio.Lock()
        self.drafts = {} # session_id -> DraftDiagram
        self.phase_plans = {} # session_id -> {"phases": list[str], "completed": list[str]}
        self.workflows = {} # workflow_id -> WorkflowSession
        self.draft_to_workflow = {} # session_id -> workflow_id
        self.validation_jobs = {} # job_id -> ValidationJob
        self.validation_tasks = {} # job_id -> asyncio.Task
        self.poll_worker_process = None
        self.poll_worker_log_handle = None
        self.poll_worker_log_path = None
        self.poll_worker_script_path = None
        self.poll_worker_lock = asyncio.Lock()
        self.startup_preflight = {
            "status": "not_run",
            "checked_at": None,
            "details": None,
        }

state = SharedState()

# Absolute pathing for data directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
UI_DIR = os.path.join(BASE_DIR, "ui")
ICONS_DIR = os.path.join(BASE_DIR, "icons")
BLOCK_IMAGES_DIR = os.path.join(BASE_DIR, "block_images")
PORT_REGISTRY_PATH = os.path.join(DATA_DIR, "blocks", "port_registry.json")
TEMP_OUTPUT_DIR = os.environ.get("XCOS_TEMP_OUTPUT_DIR", os.path.join(DATA_DIR, "temp"))
SESSION_OUTPUT_DIR = os.environ.get("XCOS_SESSION_OUTPUT_DIR", os.path.join(BASE_DIR, "sessions"))
STATE_DIR = os.environ.get("XCOS_STATE_DIR", os.path.join(BASE_DIR, "state"))
DRAFT_STATE_DIR = os.path.join(STATE_DIR, "drafts")
WORKFLOW_STATE_DIR = os.path.join(STATE_DIR, "workflows")
VALIDATION_JOB_STATE_DIR = os.path.join(STATE_DIR, "validation_jobs")
SERVER_PORT = int(os.environ.get("PORT", os.environ.get("XCOS_SERVER_PORT", "8000")))
MCP_HTTP_PATH = os.environ.get("XCOS_MCP_HTTP_PATH", "/mcp")
MCP_APP_MIME_TYPE = "text/html;profile=mcp-app"
WORKFLOW_UI_RESOURCE_URI = "ui://xcos/index.html"
DEFAULT_UI_RESOURCE_DOMAINS = ["https://esm.sh"]

WIDGET_TOOL_NAMES = {
    "xcos_get_status_widget",
    "xcos_get_workflow_widget",
    "xcos_get_validation_widget",
    "xcos_get_block_catalogue_widget",
    "xcos_get_topology_widget",
}

TOOL_DESCRIPTOR_OVERRIDES = {
    "xcos_get_status_widget": {"title": "Get Xcos Status Widget", "read_only": True, "idempotent": True, "render_widget": True},
    "xcos_get_workflow_widget": {"title": "Get Workflow Widget", "read_only": True, "idempotent": True, "render_widget": True},
    "xcos_get_validation_widget": {"title": "Get Validation Widget", "read_only": True, "idempotent": True, "render_widget": True},
    "xcos_get_block_catalogue_widget": {"title": "Get Block Catalogue Widget", "read_only": True, "idempotent": True, "render_widget": True},
    "xcos_get_topology_widget": {"title": "Get Topology Widget", "read_only": True, "idempotent": True, "render_widget": True},
    "xcos_create_workflow": {"title": "Create Workflow", "read_only": False, "idempotent": False},
    "xcos_list_workflows": {"title": "List Workflows", "read_only": True, "idempotent": True},
    "xcos_get_workflow": {"title": "Get Workflow", "read_only": True, "idempotent": True},
    "xcos_submit_phase": {"title": "Submit Workflow Phase", "read_only": False, "idempotent": False},
    "xcos_review_phase": {"title": "Review Workflow Phase", "read_only": False, "idempotent": False},
    "get_xcos_block_data": {"title": "Get Xcos Block Data", "read_only": True, "idempotent": True},
    "get_xcos_block_source": {"title": "Get Xcos Block Source", "read_only": True, "idempotent": True},
    "search_related_xcos_files": {"title": "Search Related Xcos Files", "read_only": True, "idempotent": True},
    "verify_xcos_xml": {"title": "Verify Xcos XML", "read_only": True, "idempotent": True},
    "xcos_start_draft": {"title": "Start Draft Session", "read_only": False, "idempotent": False},
    "xcos_set_context": {"title": "Set Draft Context", "read_only": False, "idempotent": False},
    "xcos_add_blocks": {"title": "Add Blocks To Draft", "read_only": False, "idempotent": False},
    "xcos_add_links": {"title": "Add Links To Draft", "read_only": False, "idempotent": False},
    "xcos_start_validation": {"title": "Start Draft Validation", "read_only": True, "idempotent": False},
    "xcos_get_validation_status": {"title": "Get Validation Status", "read_only": True, "idempotent": True},
    "xcos_verify_draft": {"title": "Verify Draft", "read_only": True, "idempotent": False},
    "xcos_commit_phase": {"title": "Commit Workflow Phase", "read_only": False, "idempotent": False},
    "xcos_get_draft_xml": {"title": "Get Draft XML", "read_only": True, "idempotent": True},
    "xcos_get_file_path": {"title": "Get Session File Path", "read_only": True, "idempotent": True},
    "xcos_get_file_content": {"title": "Get Session File Content", "read_only": True, "idempotent": True},
    "xcos_list_sessions": {"title": "List Sessions", "read_only": True, "idempotent": True},
    "ping": {"title": "Ping Server", "read_only": True, "idempotent": True},
}

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
GENERATION_REQUIREMENTS_TEMPLATE = {
    "required_blocks": [],
    "preferred_blocks": [],
    "required_context_vars": [],
    "must_use_context": False,
    "must_preserve_visual_blocks": False,
    "allowed_simplifications": [],
}
PHASE2_MANIFEST_FIELDS = {
    "blocks",
    "links",
    "context_vars",
    "omissions",
    "synthetic_blocks_planned",
}
PHASE2_BLOCK_NAME_FIELDS = (
    "name",
    "type",
    "interfaceFunctionName",
    "block_name",
    "xcos_name",
    "block",
)
PHASE2_CONTEXT_VAR_FIELDS = (
    "name",
    "var",
    "variable",
    "context_var",
)
BLOCK_TOKEN_STOPWORDS = {
    "AND",
    "FOR",
    "FROM",
    "NOT",
    "THE",
    "USE",
    "WITH",
    "WITHOUT",
    "SYSTEM",
    "MODEL",
    "BLOCK",
    "BLOCKS",
    "DIAGRAM",
    "SCOPE",
    "SIGNAL",
    "FLOW",
    "PID",
}

BUILD_XCOS_DIAGRAM_PROMPT_NAME = "build_xcos_diagram"
BUILD_XCOS_DIAGRAM_PROMPT_TITLE = "Build Xcos Diagram"
BUILD_XCOS_DIAGRAM_PROMPT_DESCRIPTION = (
    "Guides an MCP-compatible assistant through a 3-phase gated workflow to model, plan, and build "
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

    ## PHASE 1 â€” Math model

    **Step 1.** Call `xcos_get_status_widget`. Display the widget. If the server is not connected, stop and tell the user before doing anything else.

    **Step 2.** Call `xcos_get_block_catalogue_widget` with a relevant category (e.g. 'Continuous', 'Sources', 'Sinks'). Display the widget so the user can see which blocks are available.

    **Step 3.** Call `xcos_create_workflow` with the problem statement. Only pass `autopilot=true` if the user explicitly asked to skip approval pauses. Store the returned `workflow_id` â€” you will need it for every subsequent phase call.

    **Step 4.** Derive the governing equations step by step in plain text. Show all algebra. Define every variable and parameter with units and numeric values.

    **Step 5.** Generate a custom visual diagram showing the signal flow: blocks for each operation with their Xcos name and numeric parameters (e.g. GAIN[-4.9]), and arrows showing how signals connect. Ensure the layout is clean and spacious with no overlapping text.

    **Step 6.** Call `xcos_submit_phase` with `phase='phase1_math_model'`, `workflow_id`, and the full math derivation as content.

    **Step 7.** Call `xcos_get_workflow_widget` with the `workflow_id`. Display the widget.

    **Step 8.** STOP. Ask: 'Does the math and signal flow look correct? Reply **approve** or describe what to change.'

    **Step 9.** If the user requests changes: revise steps 4â€“5, call `xcos_submit_phase` again, display the widget again, ask again. Repeat until approved. Only call `xcos_review_phase` with `phase='phase1_math_model'` and `decision='approve'` after the user explicitly approves. Then call `xcos_get_workflow_widget` and display it.

    ---

    ## PHASE 2 â€” Architecture plan

    **Step 10.** Call `get_xcos_block_data` for every single block you plan to use. Never write block XML from memory or examples â€” always use the returned XML as the authoritative template. This gives you the correct port IDs, parameter structure, simulation function name, and blockType.

    **Step 11.** If you need to understand a block's internal behaviour or parameters more deeply, call `get_xcos_block_source` for that block. Use `search_related_xcos_files` to find any related configuration files if the block has complex dependencies.

    **Step 12.** Write out the full architecture plan: every block (Xcos name, simulation function, parameters with values), and every link (source block + port ID â†’ target block + port ID). Be explicit about clock/activation links vs data links.

    **Step 13.** Generate a custom visual diagram showing the actual Xcos block architecture. Use simple block shapes with the exact Xcos name and key parameter (e.g. GAIN[k=-5]). Use solid arrows for data/signal links and dashed arrows for clock/activation links. Ensure the layout is extremely clean and spacious, with distinct inputs/outputs and NO overlapping text or arrows pointing to nowhere. The diagram must match the architecture plan perfectly.

    **Step 14.** Call `xcos_submit_phase` with `phase='phase2_architecture'`, `workflow_id`, and the full block + link plan as content. The content MUST end with a fenced JSON manifest containing `blocks`, `links`, `context_vars`, `omissions`, and `synthetic_blocks_planned`.

    **Step 15.** Call `xcos_get_workflow_widget` with the `workflow_id`. Display the widget.

    **Step 16.** STOP. Ask: 'Does this block layout look right? Reply **approve** or describe what to change.'

    **Step 17.** If the user requests changes: revise steps 10â€“14, resubmit, display widget, ask again. Repeat until approved. Only call `xcos_review_phase` with `phase='phase2_architecture'` and `decision='approve'` after the user explicitly approves. Then call `xcos_get_workflow_widget` and display it.

    ---

    ## PHASE 3 â€” Build and verify

    **Step 18.** Call `xcos_start_draft` with the `workflow_id`. Store the returned `session_id` â€” you will need it for all remaining steps.

    **Step 19.** Call `xcos_add_blocks` with `session_id`. Use only XML retrieved from `get_xcos_block_data` â€” never from memory.

    **Step 20.** Call `xcos_get_topology_widget` with `session_id`. Display the widget. The user should see all blocks appear in the graph before any links are added.

    **Step 21.** Call `xcos_add_links` with `session_id`. Use port IDs exactly as returned by `get_xcos_block_data`.

    **Step 22.** Call `xcos_get_topology_widget` with `session_id` again. Display the widget. Check for missing links or disconnected ports â€” fix before continuing.

    **Step 23.** Call `xcos_get_draft_xml` with `session_id` and `pretty_print=true`. Show a brief summary of the XML structure to the user.

    **Step 24.** STOP. Ask: 'Ready to validate? Reply **approve** to run verification.'

    **Step 25.** After approval: call `xcos_verify_draft` with `session_id`.

    **Step 26.** Call `xcos_get_validation_widget` with the current draft XML. Display the widget.
    - If `success=true`: proceed to step 27.
    - If `success=false`: read the error carefully. Call `xcos_get_draft_xml` to inspect the current XML. Fix the specific block or link causing the error. Call `xcos_add_blocks` or `xcos_add_links` to rebuild, then repeat from step 25. Use `verify_xcos_xml` directly on fixed XML snippets if you want to spot-check a repair before rebuilding the full session. Never stop after one failure â€” keep iterating until `success=true`.

    If validation still fails after 3 repair attempts: stop the repair loop. Call xcos_get_draft_xml with pretty_print=true and show the full XML to the user. Call xcos_get_validation_widget and display it. Ask: "I was unable to fix this automatically after 3 attempts. Here is the current XML and the error. Would you like to guide the fix, or should I start phase 3 over?"

    **Step 27.** Call `xcos_commit_phase` with `session_id` and `phase_label='phase3_implementation'` to commit the verified XML to file.

    **Step 28.** Call `xcos_submit_phase` with `phase='phase3_implementation'`, `workflow_id`, and a summary confirming the file path and validation result as content.

    **Step 29.** Call `xcos_get_file_path` with `session_id`. Present the .xcos file to the user for download.

    **Step 30a.** If the user asks to inspect the final file content, call `xcos_get_file_content` with `session_id` and `source='last_verified'`. If the user asks to recover content from a previous session, call `xcos_list_sessions` to find it first.

    **Step 30.** Call `xcos_get_workflow_widget` with the `workflow_id` one final time. Display the completed 3-phase summary so the user can confirm everything is done.

    ---

    ## Rules that apply throughout all phases

    - Never proceed past a STOP gate without the user explicitly typing 'approve'.
    - Never write block XML from memory â€” always call `get_xcos_block_data` first.
    - Never skip `get_xcos_block_source` or `search_related_xcos_files` if a block's parameters or dependencies are unclear.
    - Every diagram must be generated as a clean visual layout. Avoid overlapping text, broken arrow paths, or ASCII art.
    - Always call `xcos_get_workflow_widget` after every `xcos_submit_phase` call.
    - Always display every widget inline immediately after it is returned.
    - If the user requests changes at any approval gate, go back and revise â€” never push forward.
    - A diagram is only done when `xcos_verify_draft` returns `success=true`. Never declare it done before that.
    - Use `xcos_list_sessions` and `xcos_list_workflows` at any point if you lose track of active sessions or workflows.
    - Use `xcos_get_file_content` with `source='last_verified'` if the user asks to inspect or download the final file content after verification.
    - If you ever lose track of the active session_id or workflow_id, call `xcos_list_sessions` and `xcos_list_workflows` to recover them before doing anything else.
    - After phases 1 and 2 approval, check if `xcos_commit_phase` needs to be called â€” consult the tool description for the current phase label convention.
    - `verify_xcos_xml` is for spot-checking raw XML snippets during repair. `xcos_verify_draft` is for full session validation. Never confuse the two.
    """
)

BUILD_XCOS_DIAGRAM_PROMPT_TEMPLATE = textwrap.dedent(
    """\
    Build an Xcos diagram for the following system:

    {{problem_statement}}

    Use the 3-phase workflow below. Never continue past an approval gate until the user explicitly types `approve`.

    Phase 1: Math model
    1. Call `xcos_get_status_widget` and stop if the server is unavailable.
    2. Call `xcos_get_block_catalogue_widget` for a relevant category.
    3. Call `xcos_create_workflow` and store `workflow_id`.
    4. Derive the governing equations with variables, units, and numeric values.
    5. Draw a clean signal-flow diagram using Xcos block names and key parameters.
    6. Call `xcos_submit_phase(phase1_math_model)` and `xcos_get_workflow_widget`.
    7. Ask for approval. If changes are requested, revise and resubmit until approved, then call `xcos_review_phase(approve, phase1_math_model)` and `xcos_get_workflow_widget`.

    Phase 2: Architecture plan
    8. Call `get_xcos_block_data` for every block before writing XML.
    9. Use `get_xcos_block_source` and `search_related_xcos_files` only when parameters or dependencies are unclear.
    10. Write the full block plan: block name, simulation function, parameters, and every source-port to target-port link, including event links.
    11. Draw a clean Xcos architecture diagram that matches the plan exactly.
    12. Call `xcos_submit_phase(phase2_architecture)` and `xcos_get_workflow_widget`.
    13. Ask for approval. If changes are requested, revise and resubmit until approved, then call `xcos_review_phase(approve, phase2_architecture)` and `xcos_get_workflow_widget`.

    Phase 3: Build and verify
    14. Call `xcos_start_draft` and store `session_id`.
    15. Call `xcos_add_blocks`, then `xcos_get_topology_widget`.
    16. Call `xcos_add_links`, then `xcos_get_topology_widget` again.
    17. Call `xcos_get_draft_xml(pretty_print=true)` and summarize the XML briefly.
    18. Ask for approval before validation.
    19. Call `xcos_verify_draft`, then `xcos_get_validation_widget`.
    20. If validation fails, inspect the XML, fix the diagram, rebuild, and retry. Stop after 3 failed repair attempts and ask the user whether to guide the fix or restart phase 3.
    21. If validation succeeds, call `xcos_commit_phase(phase3_implementation)`, `xcos_submit_phase(phase3_implementation)`, `xcos_get_file_path`, and `xcos_get_workflow_widget`.
    22. Use `xcos_get_file_content(source='last_verified')` only if the user asks to inspect the final XML.

    Rules
    - Never write block XML from memory.
    - Always display returned widgets.
    - Always ask for approval at the phase gates.
    - A diagram is only done when `xcos_verify_draft` returns `success=true`.
    - Use `xcos_list_sessions` and `xcos_list_workflows` if you lose track of IDs.
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


def file_to_data_uri(path: str, mime_type: str) -> str | None:
    if not os.path.exists(path):
        return None
    with open(path, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def icon_data_uri(filename: str, mime_type: str) -> str | None:
    return file_to_data_uri(os.path.join(ICONS_DIR, filename), mime_type)


def normalize_block_asset_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def block_image_mime_type(extension: str) -> str | None:
    return {
        ".svg": "image/svg+xml",
        ".png": "image/png",
    }.get(extension.lower())


def build_generated_block_image(block_name: str) -> dict[str, str]:
    label = get_block_label(block_name)
    safe_label = html.escape(label)
    safe_name = html.escape(block_name or "Unknown")
    svg = f"""
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 110" role="img" aria-label="{safe_name} block">
  <rect x="8" y="8" width="144" height="94" rx="18" fill="#f8f5ef" stroke="#2f3640" stroke-width="4"/>
  <rect x="20" y="20" width="120" height="36" rx="12" fill="#d9e6f2"/>
  <text x="80" y="45" text-anchor="middle" font-family="Arial, Helvetica, sans-serif" font-size="24" font-weight="700" fill="#1f2933">{safe_label}</text>
  <text x="80" y="76" text-anchor="middle" font-family="Arial, Helvetica, sans-serif" font-size="12" fill="#52606d">{safe_name}</text>
</svg>
""".strip()
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return {
        "file_name": f"{block_name or 'generated'}.generated.svg",
        "label": label,
        "src": f"data:image/svg+xml;base64,{encoded}",
        "source": "generated",
    }


def build_block_image_catalog() -> dict[str, dict[str, str]]:
    catalog = {}
    if not os.path.isdir(BLOCK_IMAGES_DIR):
        return catalog

    for filename in sorted(os.listdir(BLOCK_IMAGES_DIR)):
        stem, extension = os.path.splitext(filename)
        mime_type = block_image_mime_type(extension)
        if not mime_type:
            continue

        src = file_to_data_uri(os.path.join(BLOCK_IMAGES_DIR, filename), mime_type)
        if not src:
            continue

        key = normalize_block_asset_key(stem)
        current = catalog.get(key)
        should_replace = current is None or (
            current.get("source") != "svg" and extension.lower() == ".svg"
        )
        if not should_replace:
            continue

        catalog[key] = {
            "file_name": filename,
            "label": stem,
            "src": src,
            "source": extension.lower().lstrip("."),
        }

    return catalog


BLOCK_IMAGE_ALIASES = {
    "BIGSOM_f": ["SUM"],
    "CANIMXY3D": ["3DSCOPE"],
    "CEVENTSCOPE": ["DSCOPE"],
    "CFSCOPE": ["DSCOPE"],
    "CMSCOPE": ["DSCOPE"],
    "CMAT3D": ["3DSCOPE"],
    "CSCOPE": ["ASCOPE"],
    "GENSIN_f": ["SINUS_f"],
    "GENSQR_f": ["SQUARE_WAVE_f"],
    "NRMSOM_f": ["SUM"],
    "SCALE_CMSCOPE": ["SCALE_ASCOPE"],
    "SCALE_CSCOPE": ["SCALE_ASCOPE"],
    "SOM_f": ["SUM"],
    "SUMMATION": ["SUM"],
}


def block_image_candidates(block_name: str) -> list[str]:
    candidates = [block_name]
    for suffix in ("_f", "_m", "_c"):
        if block_name.endswith(suffix):
            candidates.append(block_name[: -len(suffix)])

    candidates.extend(BLOCK_IMAGE_ALIASES.get(block_name, []))

    seen = set()
    ordered = []
    for candidate in candidates:
        key = normalize_block_asset_key(candidate)
        if key and key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def get_block_label(block_name: str) -> str:
    compact = re.sub(r"[_-]+", " ", (block_name or "")).strip().split()
    if not compact:
        return "?"
    if len(compact) == 1:
        return compact[0][:2].upper()
    return f"{compact[0][:1]}{compact[1][:1]}".upper()


def resolve_block_image(block_name: str) -> dict[str, str] | None:
    for key in block_image_candidates(block_name):
        image = BLOCK_IMAGE_CATALOG.get(key)
        if image:
            return image
    return build_generated_block_image(block_name)


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
BLOCK_IMAGE_CATALOG = build_block_image_catalog()


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

    def to_dict(self, view: str = "full") -> dict:
        payload = {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "submitted_at": self.submitted_at,
            "reviewed_at": self.reviewed_at,
            "last_error": self.last_error,
        }
        if view == "summary":
            return payload
        payload.update({
            "content": self.content,
            "artifact_type": self.artifact_type,
            "feedback": self.feedback,
        })
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> "WorkflowPhase":
        return cls(
            key=payload["key"],
            label=payload["label"],
            status=payload.get("status", "pending"),
            content=payload.get("content", ""),
            artifact_type=payload.get("artifact_type", "markdown"),
            submitted_at=payload.get("submitted_at"),
            reviewed_at=payload.get("reviewed_at"),
            feedback=payload.get("feedback", ""),
            last_error=payload.get("last_error"),
        )


def normalize_generation_requirements(payload: dict | None) -> dict:
    normalized = {
        key: (list(value) if isinstance(value, list) else value)
        for key, value in GENERATION_REQUIREMENTS_TEMPLATE.items()
    }
    if isinstance(payload, dict):
        for key in GENERATION_REQUIREMENTS_TEMPLATE:
            value = payload.get(key, GENERATION_REQUIREMENTS_TEMPLATE[key])
            normalized[key] = list(value) if isinstance(value, list) else value
    normalized["required_blocks"] = sorted(dict.fromkeys(str(item) for item in normalized["required_blocks"] if str(item).strip()))
    normalized["preferred_blocks"] = sorted(dict.fromkeys(str(item) for item in normalized["preferred_blocks"] if str(item).strip()))
    normalized["required_context_vars"] = sorted(dict.fromkeys(str(item) for item in normalized["required_context_vars"] if str(item).strip()))
    normalized["allowed_simplifications"] = sorted(dict.fromkeys(str(item) for item in normalized["allowed_simplifications"] if str(item).strip()))
    normalized["must_use_context"] = bool(normalized["must_use_context"])
    normalized["must_preserve_visual_blocks"] = bool(normalized["must_preserve_visual_blocks"])
    return normalized


def load_catalog_block_name_map() -> dict[str, str]:
    names = {}
    blocks_dir = os.path.join(DATA_DIR, "blocks")
    if os.path.isdir(blocks_dir):
        for filename in os.listdir(blocks_dir):
            if filename.endswith(".json"):
                name = os.path.splitext(filename)[0]
                names[name.upper()] = name
    references_dir = os.path.join(DATA_DIR, "reference_blocks")
    if os.path.isdir(references_dir):
        for filename in os.listdir(references_dir):
            if filename.endswith(".xcos"):
                name = os.path.splitext(filename)[0].split("__", 1)[0]
                names.setdefault(name.upper(), name)
    return names


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def is_ambiguous_block_name(block_name: str) -> bool:
    return bool(block_name) and block_name.isalpha() and block_name.upper() == block_name


def derive_generation_requirements(problem_statement: str) -> tuple[dict, list[str], list[str]]:
    text = problem_statement or ""
    requirements = normalize_generation_requirements(None)
    catalog_block_names = load_catalog_block_name_map()

    required_blocks: list[str] = []
    for upper_name, canonical_name in sorted(catalog_block_names.items(), key=lambda item: len(item[1]), reverse=True):
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(canonical_name)}(?![A-Za-z0-9_])"
        flags = 0 if is_ambiguous_block_name(canonical_name) else re.IGNORECASE
        if re.search(pattern, text, flags=flags):
            required_blocks.append(canonical_name)

    unsupported_blocks: list[str] = []
    for token in re.findall(r"\b[A-Z][A-Z0-9_]*(?:_f)?\b", text):
        if len(token) < 3:
            continue
        if token in BLOCK_TOKEN_STOPWORDS:
            continue
        if token.upper() in catalog_block_names:
            continue
        if token not in unsupported_blocks:
            unsupported_blocks.append(token)

    context_lines: list[str] = []
    required_context_vars: list[str] = []
    for match in re.finditer(r"\b([A-Za-z][A-Za-z0-9_]*)\s*=\s*([^,\n;]+)", text):
        var_name = match.group(1).strip()
        rhs = match.group(2).strip()
        if not rhs:
            continue
        if var_name.upper() in catalog_block_names:
            continue
        required_context_vars.append(var_name)
        context_lines.append(f"{var_name}={rhs}")

    var_list_match = re.search(
        r"\b(?:variables?|constants?|parameters?)\b\s*[:=]?\s*([A-Za-z0-9_,\sand]+)",
        text,
        flags=re.IGNORECASE,
    )
    if var_list_match:
        for candidate in re.split(r",|\band\b", var_list_match.group(1)):
            var_name = candidate.strip()
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", var_name):
                continue
            if var_name.upper() in catalog_block_names:
                continue
            required_context_vars.append(var_name)

    requirements["required_blocks"] = sorted(_unique_strings(required_blocks))
    requirements["required_context_vars"] = sorted(_unique_strings(required_context_vars))
    requirements["must_use_context"] = bool(requirements["required_context_vars"])
    requirements["must_preserve_visual_blocks"] = bool(requirements["required_blocks"])
    return requirements, _unique_strings(context_lines), unsupported_blocks


def parse_phase2_architecture_manifest(content: str) -> tuple[dict | None, str | None]:
    matches = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", content or "", flags=re.IGNORECASE | re.DOTALL)
    if not matches:
        return None, "Phase 2 submissions must end with a fenced JSON architecture manifest."

    try:
        manifest = json.loads(matches[-1])
    except json.JSONDecodeError as exc:
        return None, f"Phase 2 architecture manifest is not valid JSON: {exc}"

    missing_fields = sorted(PHASE2_MANIFEST_FIELDS - set(manifest.keys()))
    if missing_fields:
        return None, f"Phase 2 architecture manifest is missing required field(s): {', '.join(missing_fields)}"

    return manifest, None


def normalize_catalog_block_name(value: str, catalog_block_names: dict[str, str]) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return catalog_block_names.get(text.upper(), text)


def extract_phase2_manifest_block_names(
    blocks: list,
    catalog_block_names: dict[str, str],
) -> tuple[set[str], bool]:
    names: set[str] = set()
    recognized_dict_field = False
    for entry in blocks or []:
        if isinstance(entry, str):
            normalized = normalize_catalog_block_name(entry, catalog_block_names)
            if normalized:
                names.add(normalized)
            continue
        if not isinstance(entry, dict):
            continue
        for field_name in PHASE2_BLOCK_NAME_FIELDS:
            field_value = entry.get(field_name)
            if not isinstance(field_value, str):
                continue
            normalized = normalize_catalog_block_name(field_value, catalog_block_names)
            if normalized:
                names.add(normalized)
                recognized_dict_field = True
                break
    return names, recognized_dict_field


def extract_phase2_manifest_context_vars(context_vars: list) -> set[str]:
    names: set[str] = set()
    for entry in context_vars or []:
        if isinstance(entry, str):
            text = entry.strip()
            if text:
                names.add(text)
            continue
        if not isinstance(entry, dict):
            continue
        for field_name in PHASE2_CONTEXT_VAR_FIELDS:
            field_value = entry.get(field_name)
            if isinstance(field_value, str) and field_value.strip():
                names.add(field_value.strip())
                break
    return names


def get_approved_manifest_omissions(manifest: dict, allowed_simplifications: list[str]) -> set[str]:
    approved = {item for item in allowed_simplifications if isinstance(item, str) and item.strip()}
    for omission in manifest.get("omissions") or []:
        if isinstance(omission, str):
            continue
        if not isinstance(omission, dict):
            continue
        is_approved = bool(
            omission.get("approved")
            or omission.get("user_approved")
            or omission.get("user_approved_simplification")
            or str(omission.get("status", "")).lower() in {"approved", "user_approved", "user_approved_simplification"}
        )
        if not is_approved:
            continue
        name = (
            omission.get("item")
            or omission.get("name")
            or omission.get("block")
            or omission.get("context_var")
        )
        if name:
            approved.add(str(name))
    return approved


def auto_advance_workflow_phase_if_needed(workflow: "WorkflowSession", phase_key: str):
    if not workflow.autopilot or phase_key not in REVIEWABLE_PHASES:
        return
    phase = workflow.phases[phase_key]
    phase.status = "approved"
    phase.reviewed_at = now_iso()
    phase.feedback = "Auto-approved by workflow autopilot."
    next_phase = next_workflow_phase(phase_key)
    if next_phase:
        workflow.current_phase = next_phase


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
    generation_requirements: dict | None = None
    generation_context_lines: list[str] | None = None
    autopilot: bool = False

    def to_dict(self, view: str = "full") -> dict:
        return {
            "workflow_id": self.workflow_id,
            "problem_statement": self.problem_statement,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "current_phase": self.current_phase,
            "current_phase_label": WORKFLOW_PHASE_LABELS[self.current_phase],
            "draft_session_id": self.draft_session_id,
            "last_verified": self.last_verified,
            "generation_requirements": normalize_generation_requirements(self.generation_requirements),
            "generation_context_lines": list(self.generation_context_lines or []),
            "autopilot": bool(self.autopilot),
            "phases": {
                key: phase.to_dict(view=view)
                for key, phase in self.phases.items()
            },
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "WorkflowSession":
        return cls(
            workflow_id=payload["workflow_id"],
            problem_statement=payload["problem_statement"],
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
            current_phase=payload["current_phase"],
            phases={
                key: WorkflowPhase.from_dict(value)
                for key, value in payload.get("phases", {}).items()
            },
            draft_session_id=payload.get("draft_session_id"),
            last_verified=payload.get("last_verified"),
            generation_requirements=normalize_generation_requirements(payload.get("generation_requirements")),
            generation_context_lines=list(payload.get("generation_context_lines") or []),
            autopilot=bool(payload.get("autopilot", False)),
        )


@dataclass
class ValidationJob:
    job_id: str
    session_id: str
    workflow_id: str | None
    validation_profile: str
    status: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    timeout_seconds: float = DEFAULT_VALIDATION_TIMEOUT_SECONDS
    result: dict | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "session_id": self.session_id,
            "workflow_id": self.workflow_id,
            "validation_profile": self.validation_profile,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "timeout_seconds": self.timeout_seconds,
            "result": self.result,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "ValidationJob":
        return cls(
            job_id=payload["job_id"],
            session_id=payload["session_id"],
            workflow_id=payload.get("workflow_id"),
            validation_profile=normalize_validation_profile(payload.get("validation_profile")),
            status=payload.get("status", "queued"),
            created_at=payload.get("created_at", now_iso()),
            started_at=payload.get("started_at"),
            finished_at=payload.get("finished_at"),
            timeout_seconds=float(payload.get("timeout_seconds", get_configured_validation_job_timeout_seconds())),
            result=payload.get("result"),
            error=payload.get("error"),
        )


def ensure_state_dirs():
    for path in [
        TEMP_OUTPUT_DIR,
        SESSION_OUTPUT_DIR,
        STATE_DIR,
        DRAFT_STATE_DIR,
        WORKFLOW_STATE_DIR,
        VALIDATION_JOB_STATE_DIR,
    ]:
        os.makedirs(path, exist_ok=True)


def atomic_write_json(path: str, payload: dict):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=True)
    os.replace(temp_path, path)


def load_json_records(directory: str) -> list[dict]:
    if not os.path.exists(directory):
        return []
    records = []
    for file_name in sorted(os.listdir(directory)):
        if not file_name.endswith(".json"):
            continue
        path = os.path.join(directory, file_name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                records.append(json.load(f))
        except Exception:
            continue
    return records


def get_draft_state_path(session_id: str) -> str:
    return os.path.join(DRAFT_STATE_DIR, f"{session_id}.json")


def get_workflow_state_path(workflow_id: str) -> str:
    return os.path.join(WORKFLOW_STATE_DIR, f"{workflow_id}.json")


def get_validation_job_state_path(job_id: str) -> str:
    return os.path.join(VALIDATION_JOB_STATE_DIR, f"{job_id}.json")


def delete_json_file(path: str):
    if os.path.exists(path):
        os.remove(path)


def persist_draft_session(session_id: str):
    draft = state.drafts.get(session_id)
    if not draft:
        return
    draft.session_id = session_id
    draft.workflow_id = state.draft_to_workflow.get(session_id) or draft.workflow_id
    draft.phase_plan = state.phase_plans.get(session_id) or draft.phase_plan
    atomic_write_json(get_draft_state_path(session_id), draft.to_persisted_dict())


def persist_workflow_session(workflow_id: str):
    workflow = state.workflows.get(workflow_id)
    if not workflow:
        return
    atomic_write_json(get_workflow_state_path(workflow_id), workflow.to_dict(view="full"))


def persist_validation_job(job_id: str):
    job = state.validation_jobs.get(job_id)
    if not job:
        return
    atomic_write_json(get_validation_job_state_path(job_id), job.to_dict())


def delete_draft_session(session_id: str):
    state.drafts.pop(session_id, None)
    state.phase_plans.pop(session_id, None)
    state.draft_to_workflow.pop(session_id, None)
    delete_json_file(get_draft_state_path(session_id))


def build_session_last_verified(draft: "DraftDiagram") -> dict | None:
    if not any([
        draft.last_verified_at,
        draft.last_verified_task_id,
        draft.last_verified_file_path,
    ]):
        return None
    return {
        "at": draft.last_verified_at,
        "success": draft.last_verified_success,
        "task_id": draft.last_verified_task_id,
        "file_path": draft.last_verified_file_path,
        "file_size_bytes": draft.last_verified_file_size,
        "error": draft.last_verified_error,
        "origin": draft.last_verified_origin,
        "validation_profile": draft.last_verified_profile,
    }


def hydrate_persistent_state():
    ensure_state_dirs()
    state.drafts.clear()
    state.phase_plans.clear()
    state.workflows.clear()
    state.draft_to_workflow.clear()
    state.validation_jobs.clear()
    state.validation_tasks.clear()

    for payload in load_json_records(DRAFT_STATE_DIR):
        session_id = payload.get("session_id")
        if not session_id:
            continue
        draft = DraftDiagram.from_persisted_dict(payload)
        draft.restored_from_disk = True
        state.drafts[session_id] = draft
        if draft.phase_plan:
            state.phase_plans[session_id] = draft.phase_plan
        if draft.workflow_id:
            state.draft_to_workflow[session_id] = draft.workflow_id

    for payload in load_json_records(WORKFLOW_STATE_DIR):
        workflow_id = payload.get("workflow_id")
        if not workflow_id:
            continue
        workflow = WorkflowSession.from_dict(payload)
        if workflow.draft_session_id and workflow.draft_session_id not in state.drafts:
            workflow.draft_session_id = None
        state.workflows[workflow_id] = workflow

    for payload in load_json_records(VALIDATION_JOB_STATE_DIR):
        job_id = payload.get("job_id")
        if not job_id:
            continue
        job = ValidationJob.from_dict(payload)
        if job.status in {"queued", "running"}:
            job.status = "failed"
            job.finished_at = now_iso()
            job.error = "Validation interrupted by server restart."
            persist_payload = job.to_dict()
            atomic_write_json(get_validation_job_state_path(job_id), persist_payload)
        state.validation_jobs[job_id] = job


def create_workflow_session(
    problem_statement: str,
    *,
    generation_requirements: dict | None = None,
    generation_context_lines: list[str] | None = None,
    autopilot: bool = False,
) -> WorkflowSession:
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
        generation_requirements=normalize_generation_requirements(generation_requirements),
        generation_context_lines=list(generation_context_lines or []),
        autopilot=bool(autopilot),
    )
    state.workflows[workflow_id] = workflow
    persist_workflow_session(workflow_id)
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
        delete_draft_session(workflow.draft_session_id)
        workflow.draft_session_id = None
    workflow.last_verified = None


def list_workflow_payloads(view: str = "full") -> list[dict]:
    return [
        workflow.to_dict(view=view)
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

    if phase_key == "phase2_architecture":
        manifest, manifest_error = parse_phase2_architecture_manifest(content)
        if manifest_error:
            return None, manifest_error

        requirements = normalize_generation_requirements(workflow.generation_requirements)
        approved_omissions = get_approved_manifest_omissions(
            manifest,
            requirements.get("allowed_simplifications") or [],
        )
        catalog_block_names = load_catalog_block_name_map()
        manifest_blocks, recognized_block_field = extract_phase2_manifest_block_names(
            manifest.get("blocks") or [],
            catalog_block_names,
        )
        manifest_context_vars = extract_phase2_manifest_context_vars(
            manifest.get("context_vars") or [],
        )
        missing_blocks = sorted(
            block_name
            for block_name in requirements["required_blocks"]
            if block_name not in manifest_blocks and block_name not in approved_omissions
        )
        missing_context_vars = sorted(
            var_name
            for var_name in requirements["required_context_vars"]
            if var_name not in manifest_context_vars and var_name not in approved_omissions
        )
        if missing_blocks or missing_context_vars:
            details = []
            if missing_blocks:
                details.append(f"missing required blocks: {', '.join(missing_blocks)}")
            if missing_context_vars:
                details.append(f"missing required context vars: {', '.join(missing_context_vars)}")
            guidance = (
                "Accepted manifest schema: "
                "blocks may be strings or objects with one of "
                + ", ".join(f"'{field}'" for field in PHASE2_BLOCK_NAME_FIELDS)
                + "; context_vars may be strings or objects with one of "
                + ", ".join(f"'{field}'" for field in PHASE2_CONTEXT_VAR_FIELDS)
                + "."
            )
            if manifest.get("blocks") and not manifest_blocks and not recognized_block_field:
                guidance += " No recognized block-name field was found in the provided block objects."
            return None, "Phase 2 fidelity check failed: " + "; ".join(details) + " " + guidance

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
    auto_advance_workflow_phase_if_needed(workflow, phase_key)
    workflow.updated_at = now_iso()
    persist_workflow_session(workflow_id)
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
    persist_workflow_session(workflow_id)
    return workflow.to_dict(), None

# --- Incremental Draft Management ---

class DraftDiagram:
    def __init__(self, schema_version="1.1", session_id: str | None = None, created_at: datetime | None = None):
        self.session_id = session_id
        self.schema_version = schema_version
        self.blocks = []
        self.links = []
        self.context_lines = []
        self.created_at = created_at or datetime.now()
        self.phase_plan = None
        self.workflow_id = None
        self.restored_from_disk = False
        self.last_verified_at = None
        self.last_verified_success = None
        self.last_verified_task_id = None
        self.last_verified_file_path = None
        self.last_verified_file_size = None
        self.last_verified_error = None
        self.last_verified_origin = None
        self.last_verified_profile = None

    def add_blocks(self, xml_chunk):
        self.blocks.append(xml_chunk)

    def add_links(self, xml_chunk):
        self.links.append(xml_chunk)

    def set_context(self, context_lines: list[str]):
        self.context_lines = [line.strip() for line in context_lines if str(line).strip()]

    def to_persisted_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "schema_version": self.schema_version,
            "created_at": self.created_at.isoformat(),
            "blocks": list(self.blocks),
            "links": list(self.links),
            "context_lines": list(self.context_lines),
            "phase_plan": self.phase_plan,
            "workflow_id": self.workflow_id,
            "restored_from_disk": self.restored_from_disk,
            "last_verified": {
                "at": self.last_verified_at,
                "success": self.last_verified_success,
                "task_id": self.last_verified_task_id,
                "file_path": self.last_verified_file_path,
                "file_size_bytes": self.last_verified_file_size,
                "error": self.last_verified_error,
                "origin": self.last_verified_origin,
                "validation_profile": self.last_verified_profile,
            },
        }

    @classmethod
    def from_persisted_dict(cls, payload: dict) -> "DraftDiagram":
        created_at_raw = payload.get("created_at")
        created_at = datetime.fromisoformat(created_at_raw) if created_at_raw else datetime.now()
        draft = cls(
            schema_version=payload.get("schema_version", "1.1"),
            session_id=payload.get("session_id"),
            created_at=created_at,
        )
        draft.blocks = list(payload.get("blocks", []))
        draft.links = list(payload.get("links", []))
        draft.context_lines = list(payload.get("context_lines") or [])
        draft.phase_plan = payload.get("phase_plan")
        draft.workflow_id = payload.get("workflow_id")
        draft.restored_from_disk = bool(payload.get("restored_from_disk", True))
        last_verified = payload.get("last_verified") or {}
        draft.last_verified_at = last_verified.get("at")
        draft.last_verified_success = last_verified.get("success")
        draft.last_verified_task_id = last_verified.get("task_id")
        draft.last_verified_file_path = last_verified.get("file_path")
        draft.last_verified_file_size = last_verified.get("file_size_bytes")
        draft.last_verified_error = last_verified.get("error")
        draft.last_verified_origin = last_verified.get("origin")
        draft.last_verified_profile = last_verified.get("validation_profile")
        return draft

    def to_xml(self):
        """Assembles the full Xcos XML from parts compatible with Scilab 2026.0.1."""
        # Skeleton boilerplate based on Scilab 2026 empty.xcos
        full_xml = f'<?xml version="1.0" encoding="UTF-8"?>\n'
        full_xml += '<XcosDiagram background="-1" gridEnabled="1" title="Untitled" '
        full_xml += 'finalIntegrationTime="100000.0" integratorAbsoluteTolerance="1.0E-6" '
        full_xml += 'integratorRelativeTolerance="1.0E-6" toleranceOnTime="1.0E-10" '
        full_xml += 'maxIntegrationTimeInterval="100001.0" maximumStepSize="0.0" '
        full_xml += 'realTimeScaling="1.0" solver="0.0">\n'
        full_xml += '  <Array as="context" scilabClass="String[]">'
        if self.context_lines:
            full_xml += "\n"
            for index, line in enumerate(self.context_lines):
                escaped = html.escape(line, quote=True)
                full_xml += f'    <data line="{index}" column="0" value="{escaped}"/>\n'
            full_xml += "  "
        full_xml += '</Array>\n'
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
XCOS_LINK_TAGS = {"BasicLink", "ExplicitLink", "CommandControlLink", "ImplicitLink"}
XCOS_BLOCK_TAGS = {"BasicBlock", "BigSom", "SplitBlock", "TextBlock", "EventInBlock", "EventOutBlock", "ExplicitInBlock", "ExplicitOutBlock", "ImplicitInBlock", "ImplicitOutBlock"}
XCOS_PORT_TAGS = {"ExplicitInputPort", "ExplicitOutputPort", "ImplicitInputPort", "ImplicitOutputPort", "ControlPort", "CommandPort", "EventInPort", "EventOutPort"}


def get_xcos_root(tree: etree._Element) -> etree._Element:
    root_nodes = tree.xpath("//mxGraphModel/root")
    if not root_nodes:
        raise ValueError("Xcos diagram is missing mxGraphModel/root.")
    return root_nodes[0]


def get_link_endpoint(link: etree._Element, role: str) -> str | None:
    attribute = "source" if role == "source" else "target"
    endpoint = link.get(attribute)
    if endpoint:
        return endpoint
    child_name = "SourcePort" if role == "source" else "DestinationPort"
    return link.xpath(f"string(./{child_name}/@reference)") or None


def get_node_geometry(node: etree._Element) -> tuple[float, float, float, float]:
    geometry = node.xpath("./mxGeometry[@as='geometry']") or node.xpath(".//mxGeometry[@as='geometry']")
    if not geometry:
        return (0.0, 0.0, 0.0, 0.0)
    geom = geometry[0]
    return (
        float(geom.get("x", "0") or 0),
        float(geom.get("y", "0") or 0),
        float(geom.get("width", "0") or 0),
        float(geom.get("height", "0") or 0),
    )


def get_node_center(node: etree._Element | None) -> tuple[float, float]:
    if node is None:
        return (0.0, 0.0)
    x, y, width, height = get_node_geometry(node)
    return (x + (width / 2.0), y + (height / 2.0))


def build_simple_link_node(
    tag_name: str,
    parent_id: str,
    source_id: str,
    target_id: str,
    *,
    style: str = "",
    value: str = "",
) -> etree._Element:
    link = etree.Element(
        tag_name,
        id=f"synthetic-link-{uuid.uuid4().hex[:12]}",
        parent=parent_id,
        source=source_id,
        target=target_id,
        style=style,
        value=value,
    )
    etree.SubElement(link, "mxGeometry", as_="geometry")
    return link


def build_synthetic_split_block(
    interface_name: str,
    parent_id: str,
    block_id: str,
    x: float,
    y: float,
    *,
    output_count: int,
) -> tuple[etree._Element, list[etree._Element]]:
    if interface_name == "CLKSPLIT_f":
        block = etree.Element(
            "SplitBlock",
            id=block_id,
            parent=parent_id,
            interfaceFunctionName="CLKSPLIT_f",
            blockType="d",
            dependsOnU="0",
            dependsOnT="0",
            simulationFunctionName="split",
            simulationFunctionType="DEFAULT",
            style="CLKSPLIT_f",
        )
        etree.SubElement(block, "ScilabDouble", as_="exprs", height="0", width="0")
        etree.SubElement(block, "ScilabDouble", as_="realParameters", height="0", width="0")
        etree.SubElement(block, "ScilabDouble", as_="integerParameters", height="0", width="0")
        etree.SubElement(block, "Array", as_="objectsParameters", scilabClass="ScilabList")
        nb_zero = etree.SubElement(block, "ScilabInteger", as_="nbZerosCrossing", height="1", width="1", intPrecision="sci_int32")
        etree.SubElement(nb_zero, "data", line="0", column="0", value="0")
        nmode = etree.SubElement(block, "ScilabInteger", as_="nmode", height="1", width="1", intPrecision="sci_int32")
        etree.SubElement(nmode, "data", line="0", column="0", value="0")
        etree.SubElement(block, "ScilabDouble", as_="state", height="0", width="0")
        etree.SubElement(block, "ScilabDouble", as_="dState", height="0", width="0")
        etree.SubElement(block, "Array", as_="oDState", scilabClass="ScilabList")
        etree.SubElement(block, "Array", as_="equations", scilabClass="ScilabList")
        etree.SubElement(block, "mxGeometry", as_="geometry", x=f"{x:.3f}", y=f"{y:.3f}", width="0.3333333", height="0.3333333")
        ports = [
            etree.Element("ControlPort", id=f"{block_id}_ctrl", parent=block_id, ordering="1", dataType="REAL_MATRIX", dataColumns="1", dataLines="1", initialState="0.0", style="ControlPort;align=center;verticalAlign=top;spacing=10.0", value=""),
            etree.Element("CommandPort", id=f"{block_id}_cmd1", parent=block_id, ordering="1", dataType="REAL_MATRIX", dataColumns="1", dataLines="1", initialState="-1.0", style="CommandPort;align=center;verticalAlign=bottom;spacing=10.0", value=""),
            etree.Element("CommandPort", id=f"{block_id}_cmd2", parent=block_id, ordering="2", dataType="REAL_MATRIX", dataColumns="1", dataLines="1", initialState="-1.0", style="CommandPort;align=center;verticalAlign=bottom;spacing=10.0", value=""),
        ]
        return block, ports

    block = etree.Element(
        "SplitBlock",
        id=block_id,
        parent=parent_id,
        interfaceFunctionName="SPLIT_f",
        blockType="c",
        dependsOnU="1",
        dependsOnT="0",
        simulationFunctionName="lsplit",
        simulationFunctionType="DEFAULT",
        style="SPLIT_f",
    )
    etree.SubElement(block, "ScilabDouble", as_="exprs", height="0", width="0")
    etree.SubElement(block, "ScilabDouble", as_="realParameters", height="0", width="0")
    etree.SubElement(block, "ScilabDouble", as_="integerParameters", height="0", width="0")
    etree.SubElement(block, "Array", as_="objectsParameters", scilabClass="ScilabList")
    nb_zero = etree.SubElement(block, "ScilabInteger", as_="nbZerosCrossing", height="1", width="1", intPrecision="sci_int32")
    etree.SubElement(nb_zero, "data", line="0", column="0", value="0")
    nmode = etree.SubElement(block, "ScilabInteger", as_="nmode", height="1", width="1", intPrecision="sci_int32")
    etree.SubElement(nmode, "data", line="0", column="0", value="0")
    etree.SubElement(block, "ScilabDouble", as_="state", height="0", width="0")
    etree.SubElement(block, "ScilabDouble", as_="dState", height="0", width="0")
    etree.SubElement(block, "Array", as_="oDState", scilabClass="ScilabList")
    etree.SubElement(block, "Array", as_="equations", scilabClass="ScilabList")
    etree.SubElement(block, "mxGeometry", as_="geometry", x=f"{x:.3f}", y=f"{y:.3f}", width="0.3333333", height="0.3333333")
    ports = [
        etree.Element("ExplicitInputPort", id=f"{block_id}_in", parent=block_id, ordering="1", dataType="REAL_MATRIX", dataColumns="1", dataLines="-1", initialState="0.0", style="", value=""),
    ]
    for index in range(output_count):
        ports.append(
            etree.Element(
                "ExplicitOutputPort",
                id=f"{block_id}_out{index + 1}",
                parent=block_id,
                ordering=str(index + 1),
                dataType="REAL_MATRIX",
                dataColumns="1",
                dataLines="-1",
                initialState="0.0",
                style="",
                value="",
            )
        )
    return block, ports


def normalize_fanout_to_split_blocks(tree: etree._Element) -> dict:
    root = get_xcos_root(tree)
    nodes_by_id = {node.get("id"): node for node in tree.xpath("//*[@id]") if node.get("id")}
    links = [node for node in root if node.tag in XCOS_LINK_TAGS]
    links_by_source: dict[str, list[etree._Element]] = {}
    for link in links:
        source_id = get_link_endpoint(link, "source")
        if not source_id:
            continue
        links_by_source.setdefault(source_id, []).append(link)

    inserted_blocks: list[dict] = []
    warnings: list[str] = []
    links_to_remove: list[etree._Element] = []
    nodes_to_append: list[etree._Element] = []

    for source_id, grouped_links in links_by_source.items():
        if len(grouped_links) <= 1:
            continue
        source_port = nodes_by_id.get(source_id)
        if source_port is None:
            continue
        source_block = source_port.getparent()
        if source_block is None:
            continue
        if source_block.tag == "SplitBlock" and source_block.get("interfaceFunctionName") in {"SPLIT_f", "CLKSPLIT_f"}:
            continue

        is_event_fanout = source_port.tag in {"CommandPort", "EventOutPort", "ControlPort"} or any(link.tag == "CommandControlLink" for link in grouped_links)
        parent_id = (
            grouped_links[0].get("parent")
            or source_block.get("parent")
            or "0:2:0"
        )
        source_x, source_y = get_node_center(source_block)
        target_centers = []
        for link in grouped_links:
            target_id = get_link_endpoint(link, "target")
            target_port = nodes_by_id.get(target_id) if target_id else None
            target_centers.append(get_node_center(target_port.getparent() if target_port is not None else None))
        average_target_y = sum(center[1] for center in target_centers) / len(target_centers) if target_centers else source_y

        if is_event_fanout:
            remaining_links = list(grouped_links)
            current_source_id = source_id
            chain_index = 0
            while len(remaining_links) > 1:
                block_id = f"synthetic-clksplit-{uuid.uuid4().hex[:10]}"
                split_block, split_ports = build_synthetic_split_block(
                    "CLKSPLIT_f",
                    parent_id,
                    block_id,
                    source_x + 40.0 + (chain_index * 40.0),
                    average_target_y,
                    output_count=2,
                )
                ctrl_port = split_ports[0]
                cmd_ports = split_ports[1:]
                nodes_to_append.extend([split_block, *split_ports])
                inserted_blocks.append({"id": block_id, "type": "CLKSPLIT_f", "source_port": source_id})
                warnings.append(f"Inserted synthetic CLKSPLIT_f '{block_id}' for fan-out from '{source_id}'.")

                first_link = remaining_links.pop(0)
                links_to_remove.append(first_link)
                nodes_to_append.append(
                    build_simple_link_node(
                        "CommandControlLink",
                        parent_id,
                        current_source_id,
                        ctrl_port.get("id"),
                        style=first_link.get("style", ""),
                        value=first_link.get("value", ""),
                    )
                )
                first_target = get_link_endpoint(first_link, "target")
                nodes_to_append.append(
                    build_simple_link_node(
                        "CommandControlLink",
                        parent_id,
                        cmd_ports[0].get("id"),
                        first_target,
                        style=first_link.get("style", ""),
                        value=first_link.get("value", ""),
                    )
                )

                if len(remaining_links) == 1:
                    final_link = remaining_links.pop(0)
                    links_to_remove.append(final_link)
                    final_target = get_link_endpoint(final_link, "target")
                    nodes_to_append.append(
                        build_simple_link_node(
                            "CommandControlLink",
                            parent_id,
                            cmd_ports[1].get("id"),
                            final_target,
                            style=final_link.get("style", ""),
                            value=final_link.get("value", ""),
                        )
                    )
                else:
                    current_source_id = cmd_ports[1].get("id")
                chain_index += 1
            continue

        block_id = f"synthetic-split-{uuid.uuid4().hex[:10]}"
        split_block, split_ports = build_synthetic_split_block(
            "SPLIT_f",
            parent_id,
            block_id,
            source_x + 40.0,
            average_target_y,
            output_count=len(grouped_links),
        )
        input_port = split_ports[0]
        output_ports = split_ports[1:]
        nodes_to_append.extend([split_block, *split_ports])
        inserted_blocks.append({"id": block_id, "type": "SPLIT_f", "source_port": source_id})
        warnings.append(f"Inserted synthetic SPLIT_f '{block_id}' for fan-out from '{source_id}'.")

        nodes_to_append.append(
            build_simple_link_node(
                grouped_links[0].tag if grouped_links[0].tag in XCOS_LINK_TAGS else "ExplicitLink",
                parent_id,
                source_id,
                input_port.get("id"),
                style=grouped_links[0].get("style", ""),
                value=grouped_links[0].get("value", ""),
            )
        )

        for index, link in enumerate(grouped_links):
            links_to_remove.append(link)
            target_id = get_link_endpoint(link, "target")
            nodes_to_append.append(
                build_simple_link_node(
                    link.tag if link.tag in XCOS_LINK_TAGS else "ExplicitLink",
                    parent_id,
                    output_ports[index].get("id"),
                    target_id,
                    style=link.get("style", ""),
                    value=link.get("value", ""),
                )
            )

    if links_to_remove:
        for link in links_to_remove:
            if link.getparent() is not None:
                link.getparent().remove(link)
        for node in nodes_to_append:
            root.append(node)

    return {
        "normalized": bool(inserted_blocks),
        "synthetic_blocks": inserted_blocks,
        "warnings": warnings,
    }


def rewrite_draft_from_tree(session_id: str, tree: etree._Element):
    draft = state.drafts[session_id]
    root = get_xcos_root(tree)
    draft.blocks = [
        etree.tostring(node, encoding="unicode", pretty_print=True).strip()
        for node in root
        if node.tag in XCOS_BLOCK_TAGS or node.tag in XCOS_PORT_TAGS
    ]
    draft.links = [
        etree.tostring(node, encoding="unicode", pretty_print=True).strip()
        for node in root
        if node.tag in XCOS_LINK_TAGS
    ]
    persist_draft_session(session_id)


def normalize_draft_fanout(session_id: str) -> dict:
    draft = state.drafts[session_id]
    parser = etree.XMLParser(remove_blank_text=True)
    tree = etree.fromstring(draft.to_xml().encode("utf-8"), parser)
    normalization = normalize_fanout_to_split_blocks(tree)
    if normalization.get("normalized"):
        rewrite_draft_from_tree(session_id, tree)
    return normalization


def make_text_response(text: str):
    return [mcp_types.TextContent(type="text", text=text)]


def make_json_response(payload):
    return make_text_response(json.dumps(payload, indent=2))


def parse_csv_env(name: str, default: list[str] | None = None) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


def parse_bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def deep_merge_dicts(base: dict | None, extra: dict | None) -> dict | None:
    if not base and not extra:
        return None

    merged = dict(base or {})
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def get_public_base_url() -> str:
    override = os.environ.get("XCOS_PUBLIC_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")
    return get_server_base_url().rstrip("/")


def get_public_mcp_url() -> str:
    override = os.environ.get("XCOS_PUBLIC_MCP_URL", "").strip()
    if override:
        return override.rstrip("/")
    return f"{get_public_base_url()}{MCP_HTTP_PATH}"


def compute_claude_app_domain(mcp_server_url: str) -> str:
    normalized = mcp_server_url.rstrip("/")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
    return f"{digest}.claudemcpcontent.com"


def build_ui_resource_meta() -> dict:
    csp = {}
    resource_domains = parse_csv_env("XCOS_UI_RESOURCE_DOMAINS", DEFAULT_UI_RESOURCE_DOMAINS)
    connect_domains = parse_csv_env("XCOS_UI_CONNECT_DOMAINS", [])
    frame_domains = parse_csv_env("XCOS_UI_FRAME_DOMAINS", [])
    base_uri_domains = parse_csv_env("XCOS_UI_BASE_URI_DOMAINS", [])

    if resource_domains:
        csp["resourceDomains"] = resource_domains
    if connect_domains:
        csp["connectDomains"] = connect_domains
    if frame_domains:
        csp["frameDomains"] = frame_domains
    if base_uri_domains:
        csp["baseUriDomains"] = base_uri_domains

    ui_meta = {"prefersBorder": True}
    public_mcp_url = get_public_mcp_url()
    if public_mcp_url.startswith(("http://", "https://")):
        ui_meta["domain"] = compute_claude_app_domain(public_mcp_url)
    if csp:
        ui_meta["csp"] = csp
    return {"ui": ui_meta}


def build_render_tool_meta() -> dict:
    return {
        "ui": {"resourceUri": WORKFLOW_UI_RESOURCE_URI},
        "openai/outputTemplate": WORKFLOW_UI_RESOURCE_URI,
    }


def sanitize_public_description(description: str | None) -> str | None:
    if not description:
        return description

    return description.replace(
        "After receiving this tool's response, you MUST call the visualize:show_widget tool to render the data as an HTML widget. Do not display raw JSON to the user.",
        "The host client can render the associated widget using the attached app resource. Do not echo raw JSON to the user.",
    )


def build_tool_annotations(
    *,
    title: str,
    read_only: bool,
    destructive: bool = False,
    idempotent: bool = False,
    open_world: bool = False,
) -> mcp_types.ToolAnnotations:
    return mcp_types.ToolAnnotations(
        title=title,
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )


def normalize_tool_descriptor(tool: mcp_types.Tool) -> mcp_types.Tool:
    config = TOOL_DESCRIPTOR_OVERRIDES.get(tool.name, {})
    title = config.get("title") or tool.title or tool.name.replace("_", " ").title()
    meta = tool.meta
    if config.get("render_widget"):
        meta = deep_merge_dicts(meta, build_render_tool_meta())

    return tool.model_copy(
        update={
            "title": title,
            "description": sanitize_public_description(tool.description),
            "annotations": build_tool_annotations(
                title=title,
                read_only=config.get("read_only", False),
                destructive=config.get("destructive", False),
                idempotent=config.get("idempotent", False),
                open_world=config.get("open_world", False),
            ),
            "meta": meta,
        }
    )


def build_widget_structured_payload(widget_payload: dict) -> dict:
    widget_type = widget_payload.get("widget_type")
    payload = widget_payload.get("payload", {})

    if widget_type == "catalogue":
        blocks = payload.get("blocks", [])
        compact_payload = {
            "category": payload.get("category"),
            "categories": payload.get("categories", []),
            "block_count": len(blocks),
            "blocks": [
                {
                    "name": block.get("name"),
                    "type": block.get("type"),
                    "description": block.get("description"),
                }
                for block in blocks[:20]
            ],
        }
    elif widget_type == "topology":
        compact_payload = {
            "session_id": payload.get("session_id"),
            "block_count": payload.get("block_count", 0),
            "link_count": payload.get("link_count", 0),
            "error": payload.get("error"),
        }
    elif widget_type == "workflow":
        if payload.get("phases"):
            compact_payload = {
                "workflow_id": payload.get("workflow_id"),
                "phases": payload.get("phases", []),
            }
        else:
            workflows = payload.get("all_workflows", [])
            compact_payload = {
                "workflow_id": payload.get("workflow_id"),
                "workflow_count": len(workflows),
                "all_workflows": [
                    {
                        "workflow_id": workflow.get("workflow_id"),
                        "current_phase": workflow.get("current_phase"),
                        "current_phase_label": workflow.get("current_phase_label"),
                        "status": workflow.get("status"),
                    }
                    for workflow in workflows[:10]
                ],
            }
    elif widget_type == "status":
        compact_payload = {
            "scilab_success": payload.get("scilab_success", False),
            "scilab_output": payload.get("scilab_output"),
            "env_context": payload.get("env_context"),
            "active_drafts": payload.get("active_drafts", 0),
        }
    elif widget_type == "validation":
        compact_payload = {
            "success": payload.get("success", False),
            "error": payload.get("error"),
        }
    else:
        compact_payload = payload

    return {
        "widget_type": widget_type,
        "payload": compact_payload,
    }


def make_structured_tool_result(
    summary: str,
    payload: dict,
    *,
    meta: dict | None = None,
    is_error: bool = False,
) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=summary)],
        structuredContent=payload,
        _meta=meta,
        isError=is_error,
    )


def make_widget_tool_result(summary: str, payload: dict) -> mcp_types.CallToolResult:
    return make_structured_tool_result(
        summary,
        build_widget_structured_payload(payload),
        meta={"widget": payload},
    )


def make_error_tool_result(message: str, payload: dict | None = None) -> mcp_types.CallToolResult:
    return make_structured_tool_result(
        message,
        payload or {"error": message},
        is_error=True,
    )


def get_server_base_url() -> str:
    port_override = os.environ.get("SPACE_HOST", f"127.0.0.1:{SERVER_PORT}")
    return f"https://{port_override}" if "hf.space" in port_override else f"http://{port_override}"


def build_session_download_url(session_id: str) -> str:
    return f"{get_server_base_url()}/api/sessions/{session_id}/diagram.xcos"


def parse_mcp_text_json_response(response):
    if isinstance(response, tuple):
        return response
    if not response:
        raise ValueError("Empty response")
    last_text = None
    for item in response:
        text = getattr(item, "text", "")
        if not isinstance(text, str):
            continue
        stripped = text.strip()
        if not stripped:
            continue
        if stripped.startswith("Error:"):
            raise ValueError(stripped[6:].strip())
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            last_text = stripped
            continue
    if last_text is not None:
        raise ValueError(f"Response did not contain JSON text. First non-JSON item: {last_text[:200]}")
    raise ValueError("Response did not contain text content")


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


def get_xml_cache_key(xml_text: str) -> str:
    return hashlib.sha256(xml_text.encode("utf-8")).hexdigest()


def remember_validation_result(xml_text: str, result: dict):
    cache_key = get_xml_cache_key(xml_text)
    state.validation_cache[cache_key] = dict(result)
    while len(state.validation_cache) > VALIDATION_CACHE_LIMIT:
        state.validation_cache.pop(next(iter(state.validation_cache)))


def get_cached_validation_result(xml_text: str) -> dict | None:
    cached = state.validation_cache.get(get_xml_cache_key(xml_text))
    return dict(cached) if cached else None


def http_post_json(url: str, payload: dict, timeout_seconds: float, token: str = "") -> dict:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def http_get_json(url: str, timeout_seconds: float, token: str = "") -> dict:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def format_validation_issue(issue) -> str:
    if isinstance(issue, str):
        return issue
    if not isinstance(issue, dict):
        return str(issue)

    issue_type = issue.get("type", "VALIDATION_ERROR")
    if issue_type == "REGISTRY_SIZE_MISMATCH":
        return (
            f"Block {issue.get('blockId')} ({issue.get('block')}): expected "
            f"{issue.get('expectedSize')}, got {issue.get('actualSize')} on port {issue.get('portIndex')}"
        )
    if issue_type == "PORT_SIZE_MISMATCH":
        return (
            f"Link {issue.get('linkId')}: size mismatch between {issue.get('srcBlock')} "
            f"{issue.get('srcSize')} and {issue.get('dstBlock')} {issue.get('dstSize')}"
        )
    if issue_type == "FANOUT_WITHOUT_SPLIT":
        return f"Block {issue.get('blockId')}: {issue.get('message', 'fanout without SplitBlock')}"
    return issue.get("message") or issue.get("error") or issue_type


def collect_validation_messages(result: dict, include_warnings: bool = False) -> list[str]:
    messages = []

    for issue in result.get("errors") or []:
        message = format_validation_issue(issue)
        if message:
            messages.append(message)

    if result.get("error"):
        message = str(result["error"])
        if message and message not in messages:
            messages.append(message)

    if include_warnings:
        for issue in result.get("warnings") or []:
            message = format_validation_issue(issue)
            if message and message not in messages:
                messages.append(message)

    return messages


def infer_validation_code(result: dict) -> str:
    if result.get("success"):
        return "OK"

    validation_profile = normalize_validation_profile(result.get("validation_profile"))

    errors = result.get("errors") or []
    if errors:
        first = errors[0]
        if isinstance(first, dict) and first.get("type"):
            return first["type"]
        return "VALIDATION_ERROR"

    if result.get("origin") == "pre-sim-validator":
        return "PRE_SIM_VALIDATION_FAILED"
    if result.get("origin") == "structural-validator":
        return "STRUCTURAL_VALIDATION_FAILED"
    if result.get("origin") == "validation-worker-remote":
        return "VALIDATION_WORKER_FAILED"
    if validation_profile == VALIDATION_PROFILE_HOSTED_SMOKE and "timed out" in str(result.get("error", "")).lower():
        return "SCILAB_IMPORT_TIMEOUT"
    if (
        result.get("origin") == "scilab-import-check"
        or validation_profile == VALIDATION_PROFILE_HOSTED_SMOKE
    ):
        return "SCILAB_IMPORT_FAILED"
    if "timed out" in str(result.get("error", "")).lower():
        return "SCILAB_RUNTIME_TIMEOUT"
    if result.get("origin") in {"scilab-poll-fallback", "scilab-poll-runtime"}:
        return "SCILAB_POLL_FAILED"
    if result.get("origin") == "scilab-subprocess":
        return "SCILAB_SUBPROCESS_FAILED"
    return "VALIDATION_FAILED"


def infer_validation_bucket(result: dict) -> str:
    if result.get("success"):
        return "ok"

    validation_profile = normalize_validation_profile(result.get("validation_profile"))
    origin = str(result.get("origin") or "")
    error_text = str(result.get("error") or "").lower()

    if result.get("origin") in {"pre-sim-validator", "structural-validator"} or (result.get("errors") or []):
        return "structural"
    if origin == "validation-worker-remote":
        return "worker"
    if validation_profile == VALIDATION_PROFILE_HOSTED_SMOKE or origin == "scilab-import-check":
        return "import"
    if "timed out" in error_text:
        return "runtime_timeout"
    if origin in {"scilab-subprocess", "scilab-poll-fallback", "scilab-poll-runtime"}:
        return "runtime"
    return "unknown"


def make_public_validation_payload(
    result: dict,
    *,
    workflow_id: str | None = None,
    session_id: str | None = None,
) -> dict:
    success = bool(result.get("success"))
    messages = collect_validation_messages(result, include_warnings=not success)
    payload = {
        "success": success,
        "code": infer_validation_code(result),
        "bucket": infer_validation_bucket(result),
        "message": "Diagram validation passed." if success else (messages[0] if messages else "Diagram validation failed."),
        "validation_profile": normalize_validation_profile(result.get("validation_profile")),
    }
    if not success and messages:
        payload["issues"] = messages[:5]
    if result.get("warnings"):
        payload["warnings"] = [
            format_validation_issue(issue)
            for issue in result.get("warnings") or []
            if format_validation_issue(issue)
        ][:10]
    if workflow_id:
        payload["workflow_id"] = workflow_id
    if session_id:
        payload["session_id"] = session_id
        payload["download_url"] = build_session_download_url(session_id)
    if result.get("task_id"):
        payload["task_id"] = result["task_id"]
    if result.get("file_path"):
        payload["file_path"] = result["file_path"]
    if result.get("file_size_bytes") is not None:
        payload["file_size_bytes"] = result["file_size_bytes"]
    if result.get("fanout_normalization"):
        payload["fanout_normalization"] = result["fanout_normalization"]
    if EXPOSE_INTERNAL_VALIDATION_DETAILS:
        payload["debug"] = result
    return payload


def build_xml_text_diagnostics(xml_text: str | None):
    if xml_text is None:
        return {
            "char_length": 0,
            "byte_length": 0,
            "sha256": None,
            "tail_excerpt": "",
        }

    xml_bytes = xml_text.encode("utf-8")
    return {
        "char_length": len(xml_text),
        "byte_length": len(xml_bytes),
        "sha256": hashlib.sha256(xml_bytes).hexdigest(),
        "tail_excerpt": xml_text[-240:],
    }


def build_xml_file_diagnostics(path: str):
    diagnostics = {
        **(get_file_metadata(path) or {}),
        "char_length": None,
        "byte_length": None,
        "sha256": None,
        "tail_excerpt": "",
        "python_parse_success": False,
        "python_parse_error": None,
    }

    try:
        with open(path, "rb") as f:
            file_bytes = f.read()
        diagnostics["byte_length"] = len(file_bytes)
        diagnostics["sha256"] = hashlib.sha256(file_bytes).hexdigest()

        decoded_text = file_bytes.decode("utf-8")
        diagnostics["char_length"] = len(decoded_text)
        diagnostics["tail_excerpt"] = decoded_text[-240:]

        etree.fromstring(file_bytes, etree.XMLParser(remove_blank_text=True))
        diagnostics["python_parse_success"] = True
    except Exception as exc:
        diagnostics["python_parse_error"] = str(exc)

    return diagnostics


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


def record_validation_outcome(session_id: str, result: dict, session_meta: dict | None = None):
    if session_id not in state.drafts:
        return None

    draft = state.drafts[session_id]
    session_meta = session_meta or get_file_metadata(get_session_file_path(session_id))
    draft.last_verified_at = now_iso()
    draft.last_verified_success = result.get("success")
    draft.last_verified_task_id = result.get("task_id")
    if result.get("success") and session_meta:
        draft.last_verified_file_path = session_meta["path"]
        draft.last_verified_file_size = session_meta["size_bytes"]
    else:
        draft.last_verified_file_path = result.get("file_path")
    draft.last_verified_file_size = result.get("file_size_bytes")
    draft.last_verified_error = result.get("error")
    draft.last_verified_origin = result.get("origin", "scilab-validator")
    draft.last_verified_profile = normalize_validation_profile(result.get("validation_profile"))
    persist_draft_session(session_id)

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
            "validation_profile": draft.last_verified_profile,
        }
        workflow.updated_at = now_iso()
        persist_workflow_session(workflow_id)
    return workflow_id


def make_validation_job_public_payload(job: ValidationJob) -> dict:
    payload = {
        "job_id": job.job_id,
        "session_id": job.session_id,
        "workflow_id": job.workflow_id,
        "validation_profile": job.validation_profile,
        "status": job.status,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "timeout_seconds": job.timeout_seconds,
    }
    if job.status in {"queued", "running"}:
        payload["poll_with"] = "xcos_get_validation_status"
    if job.result:
        payload.update(
            make_public_validation_payload(
                job.result,
                workflow_id=job.workflow_id,
                session_id=job.session_id,
            )
        )
    elif job.error:
        payload.update({
            "success": False,
            "code": "VALIDATION_JOB_FAILED" if job.status != "timed_out" else "VALIDATION_JOB_TIMED_OUT",
            "message": job.error,
        })

    session_meta = get_file_metadata(get_session_file_path(job.session_id))
    if session_meta:
        payload["session_file_path"] = session_meta["path"]
        payload["session_file_size_bytes"] = session_meta["size_bytes"]
        payload["download_url"] = build_session_download_url(job.session_id)
    return payload


def load_ui_html() -> str:
    ui_path = os.path.join(UI_DIR, "index.html")
    if not os.path.exists(ui_path):
        return "<html><body><h1>Scilab Xcos MCP Server</h1><p>Server is running. Please connect via MCP at /mcp or check /healthz.</p></body></html>"
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


def normalize_validation_profile(profile: str | None) -> str:
    if profile is None:
        return VALIDATION_PROFILE_FULL_RUNTIME
    normalized = str(profile).strip().lower()
    if normalized in VALIDATION_PROFILES:
        return normalized
    raise ValueError(
        "validation_profile must be one of: "
        + ", ".join(sorted(VALIDATION_PROFILES))
    )


def is_validation_worker_process() -> bool:
    return os.environ.get("XCOS_SERVER_ROLE", "").strip().lower() == "validation_worker"


def get_validation_worker_url() -> str:
    return os.environ.get("XCOS_VALIDATION_WORKER_URL", "").strip().rstrip("/")


def get_validation_worker_token() -> str:
    return os.environ.get("XCOS_VALIDATION_WORKER_TOKEN", "").strip()


def get_validation_worker_poll_interval_seconds() -> float:
    configured = get_positive_timeout_env(
        "XCOS_VALIDATION_WORKER_POLL_INTERVAL_SECONDS",
        DEFAULT_VALIDATION_WORKER_POLL_INTERVAL_SECONDS,
    )
    # Guardrail: very small poll intervals generate excessive worker traffic/logs
    # on long-running full-runtime validations in hosted environments.
    return max(1.0, configured)


def get_validation_worker_max_poll_interval_seconds() -> float:
    configured = get_positive_timeout_env(
        "XCOS_VALIDATION_WORKER_MAX_POLL_INTERVAL_SECONDS",
        DEFAULT_VALIDATION_WORKER_MAX_POLL_INTERVAL_SECONDS,
    )
    return max(get_validation_worker_poll_interval_seconds(), configured)


def get_validation_worker_request_retry_count() -> int:
    raw_value = os.environ.get("XCOS_VALIDATION_WORKER_REQUEST_RETRY_COUNT", "").strip()
    if not raw_value:
        return DEFAULT_VALIDATION_WORKER_REQUEST_RETRY_COUNT
    try:
        parsed = int(raw_value)
    except ValueError:
        return DEFAULT_VALIDATION_WORKER_REQUEST_RETRY_COUNT
    return max(0, parsed)


def get_validation_worker_retry_backoff_seconds() -> float:
    return get_positive_timeout_env(
        "XCOS_VALIDATION_WORKER_RETRY_BACKOFF_SECONDS",
        DEFAULT_VALIDATION_WORKER_RETRY_BACKOFF_SECONDS,
    )


def is_retryable_worker_request_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {408, 409, 423, 425, 429, 500, 502, 503, 504}
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, OSError):
        return True
    return False


def get_remote_validation_worker_timeout_seconds(timeout_seconds: float) -> float:
    requested_timeout = float(timeout_seconds)
    if requested_timeout <= 1.0:
        return 1.0
    if requested_timeout <= REMOTE_VALIDATION_WORKER_RESULT_MARGIN_SECONDS:
        return max(1.0, requested_timeout - 1.0)
    return requested_timeout - REMOTE_VALIDATION_WORKER_RESULT_MARGIN_SECONDS


def create_validation_progress_tracker(validation_profile: str | None = None) -> dict:
    tracker = {
        "validator_phase": None,
        "poll_task_id": None,
        "scilab_stage_trace": [],
        "scilab_active_stage": None,
        "scilab_last_completed_stage": None,
    }
    if validation_profile is not None:
        tracker["validation_profile"] = normalize_validation_profile(validation_profile)
    return tracker


def update_validation_progress_tracker(
    tracker: dict | None,
    *,
    validator_phase: str | None = None,
    poll_task_id: str | None = None,
    scilab_stage_trace=None,
    scilab_active_stage=VALIDATION_PROGRESS_UNSET,
    scilab_last_completed_stage=VALIDATION_PROGRESS_UNSET,
):
    if tracker is None:
        return
    if validator_phase is not None:
        tracker["validator_phase"] = validator_phase
    if poll_task_id is not None:
        tracker["poll_task_id"] = poll_task_id
    if scilab_stage_trace is not None:
        tracker["scilab_stage_trace"] = [dict(item) for item in scilab_stage_trace]
    if scilab_active_stage is not VALIDATION_PROGRESS_UNSET:
        tracker["scilab_active_stage"] = scilab_active_stage
    if scilab_last_completed_stage is not VALIDATION_PROGRESS_UNSET:
        tracker["scilab_last_completed_stage"] = scilab_last_completed_stage


def merge_validation_progress_tracker(tracker: dict | None, payload: dict | None):
    if tracker is None or not isinstance(payload, dict):
        return
    stage_trace = payload.get("scilab_stage_trace")
    if stage_trace is None:
        stage_trace = payload.get("stage_events")
    active_stage = (
        payload["scilab_active_stage"]
        if "scilab_active_stage" in payload
        else payload.get("active_stage", VALIDATION_PROGRESS_UNSET)
    )
    last_completed_stage = (
        payload["scilab_last_completed_stage"]
        if "scilab_last_completed_stage" in payload
        else payload.get("last_completed_stage", VALIDATION_PROGRESS_UNSET)
    )
    update_validation_progress_tracker(
        tracker,
        scilab_stage_trace=stage_trace,
        scilab_active_stage=active_stage,
        scilab_last_completed_stage=last_completed_stage,
    )


def snapshot_validation_progress_tracker(tracker: dict | None) -> dict:
    if tracker is None:
        return {}
    payload = {}
    for key in (
        "validator_phase",
        "poll_task_id",
        "scilab_stage_trace",
        "scilab_active_stage",
        "scilab_last_completed_stage",
    ):
        if key not in tracker:
            continue
        value = tracker.get(key)
        if value is None:
            continue
        if key == "scilab_stage_trace":
            payload[key] = [dict(item) for item in value]
        else:
            payload[key] = value
    if tracker.get("validation_profile"):
        payload["validation_profile"] = tracker["validation_profile"]
    return payload


def apply_validation_progress_update(details: dict | None, stage_name: str, stage_status: str) -> dict:
    progress = dict(details or {})
    trace = [dict(item) for item in (progress.get("scilab_stage_trace") or [])]
    normalized_stage = str(stage_name or "").strip()
    normalized_status = str(stage_status or "").strip().upper()
    if not normalized_stage or normalized_status not in {"BEGIN", "END"}:
        return progress
    trace.append({"stage": normalized_stage, "status": normalized_status})
    progress["scilab_stage_trace"] = trace
    if normalized_status == "BEGIN":
        progress["scilab_active_stage"] = normalized_stage
    elif normalized_status == "END":
        progress["scilab_last_completed_stage"] = normalized_stage
        progress["scilab_active_stage"] = None
    return progress


def should_offload_full_runtime_validation(validation_profile: str) -> bool:
    return (
        normalize_validation_profile(validation_profile) == VALIDATION_PROFILE_FULL_RUNTIME
        and bool(get_validation_worker_url())
        and not is_validation_worker_process()
    )


def is_hosted_validation_runtime() -> bool:
    return os.name != "nt" and detect_validation_mode() == "subprocess"


def should_prefer_poll_runtime(validation_profile: str) -> bool:
    return (
        normalize_validation_profile(validation_profile) == VALIDATION_PROFILE_FULL_RUNTIME
        and is_validation_worker_process()
        and os.name != "nt"
    )


def get_positive_timeout_env(name: str, default: float) -> float:
    raw_value = os.environ.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = float(raw_value)
    except ValueError:
        return default
    return value if value > 0 else default


def get_configured_subprocess_timeout_seconds() -> float:
    default = (
        HOSTED_DEFAULT_SCILAB_SUBPROCESS_TIMEOUT_SECONDS
        if is_hosted_validation_runtime()
        else LOCAL_DEFAULT_SCILAB_SUBPROCESS_TIMEOUT_SECONDS
    )
    return get_positive_timeout_env("XCOS_SCILAB_SUBPROCESS_TIMEOUT_SECONDS", default)


def get_configured_poll_timeout_seconds() -> float:
    default = (
        HOSTED_DEFAULT_POLL_VALIDATION_TIMEOUT_SECONDS
        if is_hosted_validation_runtime()
        else LOCAL_DEFAULT_POLL_VALIDATION_TIMEOUT_SECONDS
    )
    return get_positive_timeout_env("XCOS_POLL_VALIDATION_TIMEOUT_SECONDS", default)


def get_configured_validation_job_timeout_seconds() -> float:
    default = (
        HOSTED_DEFAULT_VALIDATION_JOB_TIMEOUT_SECONDS
        if is_hosted_validation_runtime()
        else LOCAL_DEFAULT_VALIDATION_JOB_TIMEOUT_SECONDS
    )
    return get_positive_timeout_env("XCOS_VALIDATION_JOB_TIMEOUT_SECONDS", default)


def get_configured_poll_worker_startup_timeout_seconds() -> float:
    default = (
        HOSTED_POLL_WORKER_STARTUP_TIMEOUT_SECONDS
        if is_hosted_validation_runtime()
        else LOCAL_POLL_WORKER_STARTUP_TIMEOUT_SECONDS
    )
    return get_positive_timeout_env("XCOS_POLL_WORKER_STARTUP_TIMEOUT_SECONDS", default)


def get_startup_preflight_timeout_seconds() -> float:
    return get_positive_timeout_env(
        "XCOS_PREFLIGHT_TIMEOUT_SECONDS",
        DEFAULT_STARTUP_PREFLIGHT_TIMEOUT_SECONDS,
    )


def get_runtime_timeout_snapshot() -> dict:
    return {
        "scilab_subprocess_timeout_seconds": get_configured_subprocess_timeout_seconds(),
        "poll_validation_timeout_seconds": get_configured_poll_timeout_seconds(),
        "validation_job_timeout_seconds": get_configured_validation_job_timeout_seconds(),
        "poll_worker_startup_timeout_seconds": get_configured_poll_worker_startup_timeout_seconds(),
    }


def is_startup_preflight_enabled() -> bool:
    return parse_bool_env("XCOS_PREFLIGHT_ENABLED", True)


def is_startup_preflight_strict() -> bool:
    return parse_bool_env("XCOS_PREFLIGHT_STRICT", False)


def resolve_windows_scilab_from_registry_file() -> str | None:
    path_file = os.path.join(BASE_DIR, ".scilab_path")
    if not os.path.exists(path_file):
        return None

    with open(path_file, "r", encoding="utf-8") as f:
        raw_root = f.read().strip()
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
_scilab_gui_bin_cache = None

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

    # Search for the full GUI binary first ("scilab") because xcosDiagramToScilab
    # requires Java GUI packages which are stripped from "scilab-cli" / "scilab-adv-cli".
    for command in ("scilab", "scilab-adv-cli", "scilab-cli"):
        resolved = shutil.which(command)
        if resolved:
            _scilab_bin_cache = resolved
            return resolved
    return None


def resolve_scilab_gui_binary() -> str | None:
    global _scilab_gui_bin_cache
    if _scilab_gui_bin_cache is not None:
        return _scilab_gui_bin_cache

    env_gui_bin = os.environ.get("SCILAB_GUI_BIN")
    if env_gui_bin:
        _scilab_gui_bin_cache = env_gui_bin
        return env_gui_bin

    env_bin = os.environ.get("SCILAB_BIN")
    if env_bin:
        env_dir = os.path.dirname(env_bin)
        gui_name = "scilab.exe" if os.name == "nt" else "scilab"
        sibling_gui = os.path.join(env_dir, gui_name)
        if os.path.exists(sibling_gui):
            _scilab_gui_bin_cache = sibling_gui
            return sibling_gui

    if os.name == "nt":
        from_registry = resolve_windows_scilab_from_registry_file()
        if from_registry:
            env_dir = os.path.dirname(from_registry)
            sibling_gui = os.path.join(env_dir, "scilab.exe")
            if os.path.exists(sibling_gui):
                _scilab_gui_bin_cache = sibling_gui
                return sibling_gui

    resolved = shutil.which("scilab")
    if resolved:
        _scilab_gui_bin_cache = resolved
        return resolved
    return None


def build_scilab_startup_preflight_script() -> str:
    return textwrap.dedent(
        """
        mode(-1);
        lines(0);
        try
            loadXcosLibs();
            mprintf("XCOS_PREFLIGHT_OK\\n");
            exit(0);
        catch
            [pref_msg, pref_id] = lasterror();
            mprintf("XCOS_PREFLIGHT_ERROR:%s\\n", string(pref_msg));
            exit(1);
        end
        """
    ).strip() + "\n"


def analyze_startup_preflight_output(output: str, returncode: int) -> tuple[bool, str | None]:
    normalized_output = output or ""
    if returncode != 0:
        return False, None

    error_marker = None
    for raw_line in normalized_output.splitlines():
        line = raw_line.strip()
        if line.startswith("XCOS_PREFLIGHT_ERROR:"):
            error_marker = line[len("XCOS_PREFLIGHT_ERROR:"):].strip() or "Unknown Scilab preflight error."
            break

    if error_marker:
        return False, error_marker

    # Some hosted Scilab builds exit successfully but do not flush mprintf output
    # in short-lived startup scripts. In that case, trust return code 0 unless an
    # explicit XCOS_PREFLIGHT_ERROR marker is present.
    return True, None


async def run_startup_preflight() -> dict:
    mode = detect_validation_mode()
    checks = []
    warnings = []
    errors = []

    scilab_bin = resolve_scilab_binary()
    scilab_gui_bin = resolve_scilab_gui_binary()

    checks.append({
        "name": "scilab_binary_resolved",
        "ok": bool(scilab_bin),
        "value": scilab_bin,
    })
    if not scilab_bin:
        errors.append("Scilab binary not found. Set SCILAB_BIN or install Scilab in the container image.")

    if mode == "subprocess":
        checks.append({
            "name": "scilab_gui_binary_resolved",
            "ok": bool(scilab_gui_bin),
            "value": scilab_gui_bin,
        })
        if not scilab_gui_bin:
            errors.append(
                "Scilab GUI binary not found. Subprocess validation requires the full Scilab binary for Xcos import."
            )

    if os.name != "nt" and mode == "subprocess":
        has_xvfb = bool(shutil.which("xvfb-run"))
        has_xauth = bool(shutil.which("xauth"))
        checks.append({"name": "xvfb_run_available", "ok": has_xvfb, "value": shutil.which("xvfb-run")})
        checks.append({"name": "xauth_available", "ok": has_xauth, "value": shutil.which("xauth")})
        if not has_xvfb:
            errors.append("xvfb-run is required for headless Scilab GUI startup on Linux.")
        if not has_xauth:
            warnings.append("xauth is not available; xvfb-run may fail in some environments.")

    preflight_timeout_seconds = get_startup_preflight_timeout_seconds()
    preflight_cmd = None
    preflight_output_tail = None
    preflight_script_path = None

    if scilab_bin and mode == "subprocess":
        selected_bin = scilab_gui_bin or scilab_bin
        preflight_id = uuid.uuid4().hex[:10]
        preflight_script_path = os.path.join(TEMP_OUTPUT_DIR, f"startup_preflight_{preflight_id}.sce")
        with open(preflight_script_path, "w", encoding="utf-8") as handle:
            handle.write(build_scilab_startup_preflight_script())

        bin_name = os.path.basename(selected_bin).lower()
        is_cli_binary = any(key in bin_name for key in ("adv-cli", "scilab-cli", "-cli"))
        scilab_args = ["-f", preflight_script_path] if is_cli_binary else ["-nb", "-f", preflight_script_path]
        if os.name != "nt" and shutil.which("xvfb-run"):
            preflight_cmd = ["xvfb-run", "-a", selected_bin] + scilab_args
        else:
            preflight_cmd = [selected_bin] + scilab_args

        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *preflight_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={
                    **os.environ,
                    "HOME": os.environ.get("HOME", "/tmp"),
                    "LC_ALL": "C",
                    "LANG": "C",
                    "LANGUAGE": "C",
                },
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=preflight_timeout_seconds)
            output = stdout.decode("utf-8", errors="replace")
            preflight_output_tail = "\n".join(output.splitlines()[-30:])
            preflight_ok, preflight_error_marker = analyze_startup_preflight_output(output, process.returncode)
            checks.append({
                "name": "scilab_startup_smoke",
                "ok": preflight_ok,
                "returncode": process.returncode,
            })
            if not preflight_ok:
                if preflight_error_marker:
                    errors.append(
                        "Scilab startup preflight reported an explicit error: "
                        f"{preflight_error_marker}"
                    )
                else:
                    errors.append("Scilab startup preflight smoke test failed (loadXcosLibs did not complete successfully).")
        except asyncio.TimeoutError:
            errors.append(
                f"Scilab startup preflight timed out after {preflight_timeout_seconds:.0f} seconds."
            )
            shutdown_details = await shutdown_process_with_escalation(
                process,
                label="startup_preflight",
                graceful_timeout_seconds=1.5,
                force_timeout_seconds=3.0,
            )
            checks.append({
                "name": "scilab_startup_smoke",
                "ok": False,
                "timed_out": True,
                "shutdown": shutdown_details,
            })
        except Exception as exc:
            errors.append(f"Scilab startup preflight failed to launch: {exc}")
        finally:
            if preflight_script_path and os.path.exists(preflight_script_path):
                try:
                    os.remove(preflight_script_path)
                except Exception:
                    pass

    status = "ok" if not errors else "failed"
    return {
        "status": status,
        "checked_at": now_iso(),
        "validation_mode": mode,
        "checks": checks,
        "warnings": warnings,
        "errors": errors,
        "timeout_seconds": preflight_timeout_seconds,
        "runtime_timeouts": get_runtime_timeout_snapshot(),
        "startup_command": preflight_cmd,
        "startup_output_tail": preflight_output_tail,
        "startup_script_path": preflight_script_path,
    }


def scilab_string_literal(path: str) -> str:
    return path.replace("\\", "/").replace('"', '""')


def poll_worker_is_active(max_idle_seconds: float = POLL_WORKER_IDLE_SECONDS) -> bool:
    if not state.last_poll_time:
        return False
    return (datetime.now() - state.last_poll_time).total_seconds() < max_idle_seconds


def read_text_tail(path: str | None, max_chars: int = 4000) -> str | None:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        return text[-max_chars:]
    except Exception:
        return None


async def shutdown_process_with_escalation(
    proc: asyncio.subprocess.Process | None,
    *,
    label: str,
    graceful_timeout_seconds: float = 3.0,
    force_timeout_seconds: float = 5.0,
) -> dict:
    if proc is None:
        return {
            "label": label,
            "status": "missing",
            "pid": None,
            "returncode": None,
        }

    pid = proc.pid
    if proc.returncode is not None:
        return {
            "label": label,
            "status": "already_exited",
            "pid": pid,
            "returncode": proc.returncode,
        }

    attempts: list[str] = []

    try:
        proc.terminate()
        attempts.append("terminate")
    except ProcessLookupError:
        return {
            "label": label,
            "status": "already_exited",
            "pid": pid,
            "returncode": proc.returncode,
            "attempts": attempts,
        }
    except Exception as exc:
        attempts.append(f"terminate_error:{exc}")

    try:
        await asyncio.wait_for(proc.wait(), timeout=max(0.1, graceful_timeout_seconds))
        return {
            "label": label,
            "status": "terminated",
            "pid": pid,
            "returncode": proc.returncode,
            "attempts": attempts,
        }
    except asyncio.TimeoutError:
        pass
    except Exception as exc:
        attempts.append(f"wait_after_terminate_error:{exc}")

    if os.name == "nt" and pid:
        attempts.append("taskkill")
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception as exc:
            attempts.append(f"taskkill_error:{exc}")
    else:
        attempts.append("kill")
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception as exc:
            attempts.append(f"kill_error:{exc}")

    try:
        await asyncio.wait_for(proc.wait(), timeout=max(0.1, force_timeout_seconds))
        return {
            "label": label,
            "status": "killed",
            "pid": pid,
            "returncode": proc.returncode,
            "attempts": attempts,
        }
    except asyncio.TimeoutError:
        return {
            "label": label,
            "status": "force_timeout",
            "pid": pid,
            "returncode": proc.returncode,
            "attempts": attempts,
        }
    except Exception as exc:
        attempts.append(f"wait_after_kill_error:{exc}")
        return {
            "label": label,
            "status": "kill_error",
            "pid": pid,
            "returncode": proc.returncode,
            "attempts": attempts,
        }


def build_poll_worker_launcher_script() -> str:
    script_path = os.path.join(TEMP_OUTPUT_DIR, f"poll_worker_{SERVER_PORT}.sce")
    poll_loop_path = scilab_string_literal(os.path.join(DATA_DIR, "xcosai_poll_loop.sci"))
    script = textwrap.dedent(
        f"""
        mode(-1);
        lines(0);
        global XCOSAI_SERVER_PORT;
        XCOSAI_SERVER_PORT = {SERVER_PORT};
        exec("{poll_loop_path}", -1);
        xcosai_poll_loop();
        """
    ).strip() + "\n"
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)
    return os.path.abspath(script_path)


async def stop_poll_worker():
    async with state.poll_worker_lock:
        proc = state.poll_worker_process
        if proc and proc.returncode is None:
            await shutdown_process_with_escalation(
                proc,
                label="poll_worker_stop",
                graceful_timeout_seconds=2.0,
                force_timeout_seconds=8.0,
            )
        state.poll_worker_process = None

        if state.poll_worker_log_handle:
            try:
                state.poll_worker_log_handle.close()
            except Exception:
                pass
        state.poll_worker_log_handle = None


async def ensure_poll_worker_running() -> dict:
    async with state.poll_worker_lock:
        proc = state.poll_worker_process
        existing_worker = None
        if proc and proc.returncode is None:
            existing_worker = {
                "pid": proc.pid,
                "log_path": state.poll_worker_log_path,
                "script_path": state.poll_worker_script_path,
            }
            if poll_worker_is_active():
                return {
                    "active": True,
                    "pid": state.poll_worker_process.pid if state.poll_worker_process else None,
                    "log_path": state.poll_worker_log_path,
                    "script_path": state.poll_worker_script_path,
                }

            try:
                existing_worker["shutdown"] = await shutdown_process_with_escalation(
                    proc,
                    label="poll_worker_restart",
                    graceful_timeout_seconds=2.0,
                    force_timeout_seconds=4.0,
                )
            except Exception as exc:
                existing_worker["shutdown"] = {
                    "label": "poll_worker_restart",
                    "status": "error",
                    "pid": proc.pid,
                    "returncode": proc.returncode,
                    "error": str(exc),
                }
            state.poll_worker_process = None
            if state.poll_worker_log_handle:
                try:
                    state.poll_worker_log_handle.close()
                except Exception:
                    pass
            state.poll_worker_log_handle = None

        if not (state.poll_worker_process and state.poll_worker_process.returncode is None):
            if state.poll_worker_log_handle:
                try:
                    state.poll_worker_log_handle.close()
                except Exception:
                    pass
                state.poll_worker_log_handle = None

            scilab_gui_bin = resolve_scilab_gui_binary()
            if not scilab_gui_bin:
                return {
                    "active": False,
                    "error": "Scilab GUI binary not found for poll fallback.",
                }

            launcher_script_path = build_poll_worker_launcher_script()
            log_path = os.path.join(TEMP_OUTPUT_DIR, f"poll_worker_{SERVER_PORT}.log")
            log_handle = open(log_path, "ab")

            scilab_args = ["-nb", "-f", launcher_script_path]
            if os.name != "nt" and shutil.which("xvfb-run"):
                cmd = ["xvfb-run", "-a", scilab_gui_bin] + scilab_args
            else:
                cmd = [scilab_gui_bin] + scilab_args

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=log_handle,
                    stderr=log_handle,
                    env={**os.environ, "HOME": os.environ.get("HOME", "/tmp")},
                )
            except Exception as exc:
                log_handle.close()
                return {
                    "active": False,
                    "error": f"Failed to launch Scilab poll worker: {exc}",
                    "log_path": log_path,
                    "script_path": launcher_script_path,
                }

            state.poll_worker_process = proc
            state.poll_worker_log_handle = log_handle
            state.poll_worker_log_path = os.path.abspath(log_path)
            state.poll_worker_script_path = launcher_script_path
            if existing_worker is None:
                existing_worker = {
                    "pid": proc.pid,
                    "log_path": log_path,
                    "script_path": launcher_script_path,
                }

    startup_timeout_seconds = get_configured_poll_worker_startup_timeout_seconds()
    deadline = asyncio.get_running_loop().time() + startup_timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if poll_worker_is_active():
            return {
                "active": True,
                "pid": state.poll_worker_process.pid if state.poll_worker_process else None,
                "log_path": state.poll_worker_log_path,
                "script_path": state.poll_worker_script_path,
            }

        proc = state.poll_worker_process
        if not proc or proc.returncode is not None:
            break
        await asyncio.sleep(1.0)

    proc = state.poll_worker_process
    return {
        "active": False,
        "pid": proc.pid if proc else None,
        "returncode": proc.returncode if proc else None,
        "log_path": state.poll_worker_log_path,
        "script_path": state.poll_worker_script_path,
        "log_tail": read_text_tail(state.poll_worker_log_path),
        "error": f"Scilab poll worker did not become active within the startup timeout ({startup_timeout_seconds:.0f} seconds).",
        "existing_worker": existing_worker,
    }


def build_headless_verification_script(xcos_path: str, validation_profile: str) -> str:
    escaped_xcos_path = scilab_string_literal(os.path.abspath(xcos_path))
    normalized_profile = normalize_validation_profile(validation_profile)
    if normalized_profile == VALIDATION_PROFILE_HOSTED_SMOKE:
        verification_body = textwrap.dedent(
            """
            xcosai_stage("SCAN_BLOCKS", "BEGIN");
            n_objs = length(scs_m.objs);
            n_blocks_found = 0;

            for i = 1:n_objs
                try
                    if typeof(scs_m.objs(i)) == "Block" then
                        n_blocks_found = n_blocks_found + 1;
                        gui_name = string(scs_m.objs(i).gui);
                        sim_name = string(scs_m.objs(i).model.sim(1));
                        if gui_name == "" & sim_name == "" then
                            xcosai_fail(msprintf("Imported block %d is missing gui and sim metadata.", i));
                        end
                    end
                catch
                    [block_msg, block_id] = lasterror();
                    xcosai_fail(msprintf("Imported block metadata check failed at index %d: %s", i, string(block_msg)));
                end
            end

            if n_blocks_found == 0 then
                xcosai_fail("Empty diagram after importXcosDiagram; Scilab found no Block objects.");
            end
            xcosai_stage("SCAN_BLOCKS", "END");
            """
        ).strip()
    else:
        verification_body = textwrap.dedent(
            """
            xcosai_stage("SCAN_BLOCKS", "BEGIN");
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

            xcosai_stage("SCAN_BLOCKS", "END");

            if replaced_list <> "" then
                mprintf("XCOSAI_VERIFY_WARN:Graphical blocks substituted for headless validation: %s\\n", replaced_list);
            end

            xcosai_stage("SCICOS_SIMULATE", "BEGIN");
            scicos_simulate(scs_m, list(), "nw");
            xcosai_stage("SCICOS_SIMULATE", "END");
            """
        ).strip()

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

        function xcosai_stage(stage_name, stage_status)
            mprintf("XCOSAI_VERIFY_STAGE:%s:%s\\n", string(stage_name), string(stage_status));
        endfunction

        try
            xcosai_stage("LOAD_XCOS_LIBS", "BEGIN");
            loadXcosLibs();
            xcosai_stage("LOAD_XCOS_LIBS", "END");
            xcosai_stage("LOAD_SCICOS", "BEGIN");
            loadScicos();
            xcosai_stage("LOAD_SCICOS", "END");
            mprintf("XCOSAI_VERIFY_INPUT_PATH:%s\\n", "{escaped_xcos_path}");
            try
                xcosai_file_info = fileinfo("{escaped_xcos_path}");
                mprintf("XCOSAI_VERIFY_FILEINFO:%s\\n", sci2exp(xcosai_file_info));
            catch
                [xcosai_fileinfo_error, xcosai_fileinfo_id] = lasterror();
                mprintf("XCOSAI_VERIFY_FILEINFO_ERROR:%s\\n", string(xcosai_fileinfo_error));
            end
            try
                xcosai_lines = mgetl("{escaped_xcos_path}");
                mprintf("XCOSAI_VERIFY_TEXT_LINE_COUNT:%d\\n", size(xcosai_lines, "*"));
                if size(xcosai_lines, "*") > 0 then
                    mprintf("XCOSAI_VERIFY_TEXT_LAST_LINE:%s\\n", xcosai_lines($));
                end
            catch
                [xcosai_text_error, xcosai_text_id] = lasterror();
                mprintf("XCOSAI_VERIFY_TEXT_READ_ERROR:%s\\n", string(xcosai_text_error));
            end
            xcosai_stage("IMPORT_XCOS_DIAGRAM", "BEGIN");
            importXcosDiagram("{escaped_xcos_path}");
            xcosai_stage("IMPORT_XCOS_DIAGRAM", "END");
            scs_m.props.tf = 0.1;

            {verification_body}

            mprintf("XCOSAI_VERIFY_OK\\n");
            exit(0);
        catch
            [catch_msg, catch_id] = lasterror();
            xcosai_fail(catch_msg);
        end
        """
    ).strip() + "\n"


IGNORABLE_SCILAB_LOG_SNIPPETS = (
    "Gtk-WARNING:",
    "Locale not supported by C library. Using the fallback 'C' locale.",
    "Using the fallback 'C' locale.",
)

SCILAB_VERIFICATION_INFO_PREFIXES = (
    "XCOSAI_VERIFY_INPUT_PATH:",
    "XCOSAI_VERIFY_FILEINFO:",
    "XCOSAI_VERIFY_FILEINFO_ERROR:",
    "XCOSAI_VERIFY_TEXT_LINE_COUNT:",
    "XCOSAI_VERIFY_TEXT_LAST_LINE:",
    "XCOSAI_VERIFY_TEXT_READ_ERROR:",
)
SCILAB_VERIFICATION_STAGE_PREFIX = "XCOSAI_VERIFY_STAGE:"


def is_ignorable_scilab_log_line(line: str) -> bool:
    stripped = (line or "").strip()
    if not stripped:
        return True
    return any(snippet in stripped for snippet in IGNORABLE_SCILAB_LOG_SNIPPETS)


def analyze_scilab_verification_output(scilab_log: str, returncode: int) -> dict:
    warnings: list[str] = []
    info_lines: list[str] = []
    unexpected_lines: list[str] = []
    explicit_error: str | None = None
    found_ok = False
    stage_events: list[dict[str, str]] = []
    active_stage: str | None = None
    last_completed_stage: str | None = None

    for raw_line in (scilab_log or "").splitlines():
        line = raw_line.strip()
        if is_ignorable_scilab_log_line(line):
            continue
        if line == "XCOSAI_VERIFY_OK":
            found_ok = True
            continue
        if line.startswith("XCOSAI_VERIFY_WARN:"):
            warning = line[len("XCOSAI_VERIFY_WARN:"):].strip()
            if warning and warning not in warnings:
                warnings.append(warning)
            continue
        if line.startswith("XCOSAI_VERIFY_ERROR:"):
            explicit_error = line[len("XCOSAI_VERIFY_ERROR:"):].strip()
            continue
        if line.startswith(SCILAB_VERIFICATION_STAGE_PREFIX):
            remainder = line[len(SCILAB_VERIFICATION_STAGE_PREFIX):].strip()
            stage_name, separator, stage_status = remainder.partition(":")
            if stage_name and separator and stage_status:
                normalized_name = stage_name.strip()
                normalized_status = stage_status.strip().upper()
                stage_events.append({"stage": normalized_name, "status": normalized_status})
                if normalized_status == "BEGIN":
                    active_stage = normalized_name
                elif normalized_status == "END":
                    last_completed_stage = normalized_name
                    if active_stage == normalized_name:
                        active_stage = None
                continue
        if line.startswith(SCILAB_VERIFICATION_INFO_PREFIXES):
            info_lines.append(line)
            continue
        unexpected_lines.append(line)

    if found_ok and returncode == 0:
        return {
            "success": True,
            "warnings": warnings if warnings else None,
            "stage_events": stage_events,
            "active_stage": active_stage,
            "last_completed_stage": last_completed_stage,
        }

    if returncode == 0 and explicit_error is None and not unexpected_lines:
        return {
            "success": True,
            "warnings": warnings if warnings else None,
            "stage_events": stage_events,
            "active_stage": active_stage,
            "last_completed_stage": last_completed_stage,
        }

    error = explicit_error or f"Scilab exited with code {returncode}."
    tail_source = unexpected_lines or info_lines
    if tail_source and explicit_error is None:
        tail_err = "\n".join(tail_source[-15:])
        error += f"\n\n--- Last 15 lines of Scilab output ---\n{tail_err}\n---------------------------------------"

    return {
        "success": False,
        "error": error,
        "warnings": warnings if warnings else None,
        "stage_events": stage_events,
        "active_stage": active_stage,
        "last_completed_stage": last_completed_stage,
    }


async def read_subprocess_stdout(stream, progress_tracker: dict | None = None) -> bytes:
    if stream is None:
        return b""
    chunks: list[bytes] = []
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        chunks.append(chunk)
        if progress_tracker is not None:
            partial_text = b"".join(chunks).decode("utf-8", errors="replace")
            partial_analysis = analyze_scilab_verification_output(partial_text, 1)
            merge_validation_progress_tracker(progress_tracker, partial_analysis)
    return b"".join(chunks)


async def run_headless_scilab_check(
    xml_content: str,
    auto_fixed: bool,
    *,
    validation_profile: str,
    progress_tracker: dict | None = None,
) -> dict:
    """Runs a Scilab import/runtime check headlessly (subprocess mode).

    Uses xvfb-run on Linux so that the GUI subsystem is satisfied without a
    physical display.  Falls back to running scilab-adv-cli directly when
    xvfb-run is not available (e.g. during local Windows testing with SCILAB_BIN
    set explicitly).

    Timeout is configurable. Full Scilab stdout/stderr is captured and returned
    on failure so the caller can debug without re-running manually.
    """
    normalized_profile = normalize_validation_profile(validation_profile)
    is_hosted_smoke = normalized_profile == VALIDATION_PROFILE_HOSTED_SMOKE
    origin = "scilab-import-check" if is_hosted_smoke else "scilab-subprocess"
    success_verdict = (
        "Scilab import/load passed without full runtime simulation."
        if is_hosted_smoke
        else "Scilab import and simulation passed."
    )
    scilab_bin = resolve_scilab_binary()
    memory_diag = build_xml_text_diagnostics(xml_content)
    subprocess_timeout_seconds = get_configured_subprocess_timeout_seconds()
    update_validation_progress_tracker(
        progress_tracker,
        validator_phase="scilab-import-check" if is_hosted_smoke else "scilab-subprocess",
    )
    timeout_error = (
        f"Structural validation passed, but Scilab import validation timed out after {subprocess_timeout_seconds:.0f} seconds."
        if is_hosted_smoke
        else f"Structural validation passed, but Scilab runtime validation timed out after {subprocess_timeout_seconds:.0f} seconds."
    )
    failure_prefix = (
        "Structural validation passed, but Scilab import validation failed: "
        if is_hosted_smoke
        else "Structural validation passed, but Scilab runtime validation failed: "
    )
    if not scilab_bin:
        return {
            "success": False,
            "origin": origin,
            "error": "Scilab binary not found. Set SCILAB_BIN or install scilab-cli in the runtime image.",
            "auto_fixed_mux_to_scalar": auto_fixed,
            "validation_profile": normalized_profile,
            "scilab_log": None,
            "xml_diagnostics": {
                "memory": memory_diag,
                "disk": None,
                "verification_script_path": None,
            },
        }

    task_id = str(uuid.uuid4())
    temp_path = os.path.join(TEMP_OUTPUT_DIR, f"{task_id}.xcos")
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(xml_content)
    temp_meta = get_file_metadata(temp_path)
    disk_diag = build_xml_file_diagnostics(temp_path)
    disk_diag["matches_memory_sha256"] = disk_diag.get("sha256") == memory_diag.get("sha256")
    disk_diag["matches_memory_byte_length"] = disk_diag.get("byte_length") == memory_diag.get("byte_length")

    verify_script_path = os.path.join(TEMP_OUTPUT_DIR, f"{task_id}.sce")
    with open(verify_script_path, "w", encoding="utf-8") as f:
        f.write(build_headless_verification_script(temp_path, normalized_profile))
    xml_diagnostics = {
        "memory": memory_diag,
        "disk": disk_diag,
        "verification_script_path": os.path.abspath(verify_script_path),
        "validation_profile": normalized_profile,
    }

    # Flags depend on which Scilab binary is available:
    #   - scilab-adv-cli / scilab-cli : already headless, no -nw/-nb supported
    #   - scilab (GUI)                : needs to load GUI for xcosDiagramToScilab, so NO -nw
    bin_name = os.path.basename(scilab_bin).lower()
    is_cli_binary = any(k in bin_name for k in ("adv-cli", "scilab-cli", "-cli"))
    if is_cli_binary:
        scilab_args = ["-f", verify_script_path]
    else:
        # We only pass -nb (no banner). -nw disables Java GUI, which breaks importXcosDiagram.
        scilab_args = ["-nb", "-f", verify_script_path]

    # On Linux, wrap with xvfb-run so the Java/Swing GUI init doesn't fail
    if os.name != "nt" and shutil.which("xvfb-run"):
        cmd = ["xvfb-run", "-a", scilab_bin] + scilab_args
    else:
        cmd = [scilab_bin] + scilab_args
    xml_diagnostics["subprocess_command"] = cmd
    xml_diagnostics["timeout_seconds"] = subprocess_timeout_seconds

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={
                **os.environ,
                "HOME": os.environ.get("HOME", "/tmp"),
                "LC_ALL": "C",
                "LANG": "C",
                "LANGUAGE": "C",
            },
        )
        stdout_task = asyncio.create_task(read_subprocess_stdout(proc.stdout, progress_tracker))
        try:
            await asyncio.wait_for(proc.wait(), timeout=subprocess_timeout_seconds)
            stdout_bytes = await stdout_task
        except asyncio.TimeoutError:
            shutdown_details = await shutdown_process_with_escalation(
                proc,
                label="scilab_subprocess_timeout",
                graceful_timeout_seconds=2.0,
                force_timeout_seconds=5.0,
            )
            try:
                stdout_bytes = await asyncio.wait_for(stdout_task, timeout=5.0)
            except Exception:
                stdout_task.cancel()
                stdout_bytes = b""
            scilab_log = stdout_bytes.decode("utf-8", errors="replace").strip()
            scilab_log_tail = "\n".join(scilab_log.splitlines()[-40:]) if scilab_log else ""
            log_analysis = analyze_scilab_verification_output(scilab_log, proc.returncode or -1)
            merge_validation_progress_tracker(progress_tracker, log_analysis)
            stage_suffix = ""
            if log_analysis.get("active_stage"):
                stage_suffix = f" Last observed stage: {log_analysis['active_stage']} (in progress)."
            elif log_analysis.get("last_completed_stage"):
                stage_suffix = f" Last completed stage: {log_analysis['last_completed_stage']}."
            return {
                "success": False,
                "origin": origin,
                "error": f"{timeout_error}{stage_suffix}",
                "auto_fixed_mux_to_scalar": auto_fixed,
                "validation_profile": normalized_profile,
                "scilab_log": scilab_log or None,
                "scilab_log_tail": scilab_log_tail or None,
                "scilab_stage_trace": log_analysis.get("stage_events"),
                "scilab_active_stage": log_analysis.get("active_stage"),
                "scilab_last_completed_stage": log_analysis.get("last_completed_stage"),
                "subprocess_shutdown": shutdown_details,
                "file_path": temp_meta["path"],
                "file_size_bytes": temp_meta["size_bytes"],
                "xml_diagnostics": xml_diagnostics,
            }

        scilab_log = stdout_bytes.decode("utf-8", errors="replace").strip()
        scilab_log_tail = "\n".join(scilab_log.splitlines()[-40:]) if scilab_log else ""
        returncode = proc.returncode
        log_analysis = analyze_scilab_verification_output(scilab_log, returncode)
        merge_validation_progress_tracker(progress_tracker, log_analysis)

        if log_analysis["success"]:
            return {
                "success": True,
                "origin": origin,
                "warnings": log_analysis.get("warnings"),
                "auto_fixed_mux_to_scalar": auto_fixed,
                "validation_profile": normalized_profile,
                "scilab_verdict": success_verdict,
                "scilab_log": scilab_log,
                "scilab_log_tail": scilab_log_tail,
                "scilab_stage_trace": log_analysis.get("stage_events"),
                "scilab_active_stage": log_analysis.get("active_stage"),
                "scilab_last_completed_stage": log_analysis.get("last_completed_stage"),
                "file_path": temp_meta["path"],
                "file_size_bytes": temp_meta["size_bytes"],
                "xml_diagnostics": xml_diagnostics,
            }

        return {
            "success": False,
            "origin": origin,
            "error": f"{failure_prefix}{log_analysis['error']}",
            "auto_fixed_mux_to_scalar": auto_fixed,
            "validation_profile": normalized_profile,
            "warnings": log_analysis.get("warnings"),
            "scilab_log": scilab_log,
            "scilab_log_tail": scilab_log_tail,
            "scilab_stage_trace": log_analysis.get("stage_events"),
            "scilab_active_stage": log_analysis.get("active_stage"),
            "scilab_last_completed_stage": log_analysis.get("last_completed_stage"),
            "file_path": temp_meta["path"],
            "file_size_bytes": temp_meta["size_bytes"],
            "xml_diagnostics": xml_diagnostics,
            "hint": (
                "Full Scilab output is available in 'scilab_log'. "
                "Common causes: unsupported block GUI in headless mode, "
                "parameter size mismatches, or missing SplitBlocks."
            ),
        }

    except Exception as exc:
        return {
            "success": False,
            "origin": origin,
            "error": f"Failed to launch Scilab subprocess: {exc}",
            "auto_fixed_mux_to_scalar": auto_fixed,
            "validation_profile": normalized_profile,
            "scilab_log": None,
            "xml_diagnostics": xml_diagnostics if "xml_diagnostics" in locals() else {
                "memory": memory_diag,
                "disk": None,
                "verification_script_path": None,
            },
        }


def should_retry_with_poll_fallback(scilab_result: dict) -> bool:
    error_text = "\n".join([
        str(scilab_result.get("error") or ""),
        str(scilab_result.get("scilab_log") or ""),
    ])
    lowered = error_text.lower()
    return (
        not scilab_result.get("success")
        and (
            "premature end of file" in lowered
            or "fatal error" in lowered
            or "timed out" in lowered
            or "runtime timeout" in lowered
        )
    )


def describe_poll_fallback_reason(scilab_result: dict) -> str:
    error_text = "\n".join([
        str(scilab_result.get("error") or ""),
        str(scilab_result.get("scilab_log") or ""),
    ]).lower()
    if "timed out" in error_text or "runtime timeout" in error_text:
        return "Subprocess validator timed out; retried with long-lived Scilab poll worker."
    if "premature end of file" in error_text:
        return "Subprocess validator reported premature EOF; retried with long-lived Scilab poll worker."
    if "fatal error" in error_text:
        return "Subprocess validator reported a fatal error; retried with long-lived Scilab poll worker."
    return "Subprocess validator failed in a fallback-eligible way; retried with long-lived Scilab poll worker."


async def run_poll_validation(
    xml_content: str,
    auto_fixed: bool,
    progress_tracker: dict | None = None,
    *,
    origin: str = "scilab-poll-fallback",
    progress_phase: str = "scilab-poll-fallback",
) -> dict:
    worker_state = await ensure_poll_worker_running()
    if not worker_state.get("active"):
        return {
            "success": False,
            "origin": origin,
            "error": worker_state.get("error", "Scilab poll worker is unavailable."),
            "auto_fixed_mux_to_scalar": auto_fixed,
            "poll_worker": worker_state,
        }

    task_id = str(uuid.uuid4())
    temp_path = os.path.join(TEMP_OUTPUT_DIR, f"{task_id}.xcos")

    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(xml_content)
    temp_meta = get_file_metadata(temp_path)

    event = asyncio.Event()
    update_validation_progress_tracker(
        progress_tracker,
        validator_phase=progress_phase,
        poll_task_id=task_id,
    )
    state.results[task_id] = {
        "success": False,
        "error": "",
        "details": snapshot_validation_progress_tracker(progress_tracker),
        "event": event,
        "progress_tracker": progress_tracker,
    }

    await state.task_queue.put({"task_id": task_id, "zcos_path": temp_path})
    poll_timeout_seconds = get_configured_poll_timeout_seconds()

    try:
        await asyncio.wait_for(event.wait(), timeout=poll_timeout_seconds)
        res = state.results.pop(task_id)
        merge_validation_progress_tracker(progress_tracker, res.get("details"))
        result_payload = {
            "success": res["success"],
            "task_id": task_id,
            "file_path": temp_meta["path"],
            "file_size_bytes": temp_meta["size_bytes"],
            "auto_fixed_mux_to_scalar": auto_fixed,
            "validator_mode": "poll",
            "origin": origin,
            "poll_worker": worker_state,
        }
        details = res.get("details") or {}
        result_payload.update(details)
        if res["success"]:
            result_payload["scilab_verdict"] = "Scilab import and simulation passed via long-lived poll worker."
        else:
            result_payload["scilab_verdict"] = "Scilab poll fallback reported a validation failure."
        if not res["success"]:
            result_payload["error"] = res["error"]
            result_payload["hint"] = "Use xcos_get_draft_xml(session_id) to inspect the final XML. Scilab errors often relate to parameter size mismatches or missing SplitBlocks."
        return result_payload

    except asyncio.CancelledError:
        pending = state.results.pop(task_id, None)
        if pending:
            merge_validation_progress_tracker(progress_tracker, pending.get("details"))
        raise
    except asyncio.TimeoutError:
        pending = state.results.pop(task_id, None)
        if pending:
            merge_validation_progress_tracker(progress_tracker, pending.get("details"))
        return {
            "success": False,
            "task_id": task_id,
            "file_path": temp_meta["path"],
            "file_size_bytes": temp_meta["size_bytes"],
            "error": f"Scilab verification timed out for {task_id} after {poll_timeout_seconds:.0f} seconds",
            "origin": origin,
            "poll_worker": worker_state,
            **snapshot_validation_progress_tracker(progress_tracker),
        }


async def run_headless_scilab_validation(
    xml_content: str,
    auto_fixed: bool,
    progress_tracker: dict | None = None,
) -> dict:
    return await run_headless_scilab_check(
        xml_content,
        auto_fixed,
        validation_profile=VALIDATION_PROFILE_FULL_RUNTIME,
        progress_tracker=progress_tracker,
    )


async def run_headless_scilab_import_validation(
    xml_content: str,
    auto_fixed: bool,
    progress_tracker: dict | None = None,
) -> dict:
    return await run_headless_scilab_check(
        xml_content,
        auto_fixed,
        validation_profile=VALIDATION_PROFILE_HOSTED_SMOKE,
        progress_tracker=progress_tracker,
    )


async def run_remote_validation_worker(
    xml_content: str,
    validation_profile: str,
    timeout_seconds: float,
) -> dict:
    worker_url = get_validation_worker_url()
    if not worker_url:
        raise RuntimeError("XCOS_VALIDATION_WORKER_URL is not configured.")

    token = get_validation_worker_token()
    request_timeout_seconds = max(10.0, timeout_seconds)
    worker_timeout_seconds = get_remote_validation_worker_timeout_seconds(timeout_seconds)
    retry_limit = get_validation_worker_request_retry_count()
    retry_backoff_seconds = get_validation_worker_retry_backoff_seconds()
    create_retry_count = 0

    while True:
        try:
            create_payload = await asyncio.to_thread(
                http_post_json,
                f"{worker_url}/validate",
                {
                    "xml_content": xml_content,
                    "validation_profile": normalize_validation_profile(validation_profile),
                    "timeout_seconds": worker_timeout_seconds,
                },
                request_timeout_seconds,
                token,
            )
            break
        except Exception as exc:
            if create_retry_count >= retry_limit or not is_retryable_worker_request_error(exc):
                raise RuntimeError(
                    f"Failed to create remote validation job after {create_retry_count + 1} attempt(s): {exc}"
                ) from exc
            create_retry_count += 1
            backoff = min(
                get_validation_worker_max_poll_interval_seconds(),
                retry_backoff_seconds * (2 ** (create_retry_count - 1)),
            )
            await asyncio.sleep(backoff)

    job_id = create_payload.get("job_id")
    if not job_id:
        raise RuntimeError(f"Validation worker did not return a job_id: {create_payload}")

    base_poll_interval_seconds = get_validation_worker_poll_interval_seconds()
    max_poll_interval_seconds = get_validation_worker_max_poll_interval_seconds()
    poll_interval_seconds = base_poll_interval_seconds
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    latest_progress = None
    poll_transient_errors = 0
    consecutive_poll_errors = 0
    while True:
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(
                f"Remote validation worker job {job_id} timed out after {timeout_seconds:.0f} seconds."
            )
        await asyncio.sleep(poll_interval_seconds)
        try:
            status_payload = await asyncio.to_thread(
                http_get_json,
                f"{worker_url}/jobs/{job_id}",
                request_timeout_seconds,
                token,
            )
            consecutive_poll_errors = 0
        except Exception as exc:
            if not is_retryable_worker_request_error(exc):
                raise RuntimeError(f"Remote validation status polling failed for job {job_id}: {exc}") from exc
            if consecutive_poll_errors >= retry_limit:
                raise RuntimeError(
                    f"Remote validation status polling exceeded retry limit ({retry_limit}) for job {job_id}: {exc}"
                ) from exc
            consecutive_poll_errors += 1
            poll_transient_errors += 1
            poll_interval_seconds = min(
                max_poll_interval_seconds,
                max(
                    poll_interval_seconds,
                    retry_backoff_seconds * (2 ** (consecutive_poll_errors - 1)),
                ),
            )
            continue
        if isinstance(status_payload.get("progress"), dict):
            latest_progress = status_payload.get("progress")
        if status_payload.get("status") in {"queued", "running"}:
            poll_interval_seconds = min(max_poll_interval_seconds, poll_interval_seconds * 1.25)
            continue

        result = status_payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Validation worker returned no result for job {job_id}: {status_payload}")

        result = dict(result)
        result["validation_profile"] = normalize_validation_profile(result.get("validation_profile"))
        if isinstance(latest_progress, dict):
            if latest_progress.get("validator_phase") and "validator_phase" not in result:
                result["validator_phase"] = latest_progress.get("validator_phase")
            if latest_progress.get("poll_task_id") and "poll_task_id" not in result:
                result["poll_task_id"] = latest_progress.get("poll_task_id")
            if latest_progress.get("scilab_stage_trace") and not result.get("scilab_stage_trace"):
                result["scilab_stage_trace"] = [
                    dict(item) for item in latest_progress.get("scilab_stage_trace") or []
                ]
            if latest_progress.get("scilab_active_stage") and not result.get("scilab_active_stage"):
                result["scilab_active_stage"] = latest_progress.get("scilab_active_stage")
            if latest_progress.get("scilab_last_completed_stage") and not result.get("scilab_last_completed_stage"):
                result["scilab_last_completed_stage"] = latest_progress.get("scilab_last_completed_stage")
        result["remote_worker"] = {
            "url": worker_url,
            "job_id": job_id,
            "status": status_payload.get("status"),
            "timeout_seconds": worker_timeout_seconds,
            "request_retry_limit": retry_limit,
            "create_retry_count": create_retry_count,
            "poll_transient_errors": poll_transient_errors,
            "created_at": status_payload.get("created_at"),
            "started_at": status_payload.get("started_at"),
            "finished_at": status_payload.get("finished_at"),
            "progress": latest_progress,
        }
        return result

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


def build_compact_reference_payload(xml_text: str | None) -> dict | None:
    if not xml_text:
        return None
    try:
        parser = etree.XMLParser(remove_blank_text=True)
        tree = etree.fromstring(xml_text.encode("utf-8"), parser)
        block = tree.xpath(XCOS_BLOCK_XPATH)
        if not block:
            return None
        block_node = block[0]
        block_id = block_node.get("id")
        port_nodes = tree.xpath(f"//*[@parent='{block_id}'][contains(local-name(), 'Port')]")
        compact_nodes = [block_node] + port_nodes
        fragment = "\n".join(
            etree.tostring(node, encoding="unicode", pretty_print=True).strip()
            for node in compact_nodes
        )
        port_ids = [node.get("id") for node in port_nodes if node.get("id")]
        parameter_fields = []
        for child in block_node:
            child_tag = child.tag if isinstance(child.tag, str) else ""
            if child_tag in {"mxGeometry"}:
                continue
            child_as = child.get("as")
            if child_as:
                parameter_fields.append(child_as)
        return {
            "template_xml": fragment,
            "block_id": block_id,
            "port_ids": port_ids,
            "parameter_fields": parameter_fields,
        }
    except Exception:
        return None


def resolve_xcos_block_name(name: str) -> str:
    raw_name = (name or "").strip()
    if not raw_name:
        return raw_name

    block_dir = os.path.join(DATA_DIR, "blocks")
    reference_dir = os.path.join(DATA_DIR, "reference_blocks")

    block_names = set()
    reference_names = set()
    if os.path.isdir(block_dir):
        block_names = {
            os.path.splitext(file_name)[0]
            for file_name in os.listdir(block_dir)
            if file_name.endswith(".json")
        }
    if os.path.isdir(reference_dir):
        reference_names = {
            os.path.splitext(file_name)[0]
            for file_name in os.listdir(reference_dir)
            if file_name.endswith(".xcos")
        }

    available_names = block_names | reference_names
    if not available_names:
        return raw_name

    candidate_names = []

    def add_candidate(candidate: str):
        if candidate and candidate not in candidate_names:
            candidate_names.append(candidate)

    add_candidate(raw_name)
    upper_name = raw_name.upper()
    add_candidate(upper_name)

    if not upper_name.endswith("_f"):
        add_candidate(f"{upper_name}_f")
    if not upper_name.endswith("BLK"):
        add_candidate(f"{upper_name}BLK")
    if not upper_name.endswith("BLK_f"):
        add_candidate(f"{upper_name}BLK_f")
    if upper_name.endswith("_f"):
        add_candidate(upper_name[:-2])

    for candidate in candidate_names:
        if candidate in available_names:
            return candidate

    lowered_map = {item.lower(): item for item in available_names}
    for candidate in candidate_names:
        resolved = lowered_map.get(candidate.lower())
        if resolved:
            return resolved

    return raw_name

async def get_xcos_block_data(
    name: str,
    include_help: bool = False,
    include_extra_examples: bool = False,
    include_reference_xml: bool = False,
):
    """Returns compact block metadata and optional reference XML for an Xcos block."""
    resolved_name = resolve_xcos_block_name(name)
    data = {
        "name": name,
        "resolved_name": resolved_name,
        "info": None,
        "warnings": [],
        "has_example": False,
        "has_help": False,
        "has_extra_examples": False,
        "compact_reference": None,
        "reference_xml": None,
    }
    if resolved_name != name:
        data["warnings"].append(
            f"Resolved block name '{name}' to '{resolved_name}' based on available catalog files."
        )

    # 1. INFO
    info_path = os.path.join(DATA_DIR, "blocks", f"{resolved_name}.json")
    if os.path.exists(info_path):
        with open(info_path, 'r', encoding='utf-8') as f:
            try:
                data["info"] = json.loads(f.read())
            except json.JSONDecodeError:
                data["info"] = f.read()
    else:
        data["warnings"].append(f"Block info for '{resolved_name}' not found at data/blocks/{resolved_name}.json")

    # 2. EXAMPLE
    example_path = os.path.join(DATA_DIR, "reference_blocks", f"{resolved_name}.xcos")
    example_xml = None
    if os.path.exists(example_path):
        data["has_example"] = True
        with open(example_path, 'r', encoding='utf-8') as f:
            example_xml = f.read()
        data["compact_reference"] = build_compact_reference_payload(example_xml)
        if include_reference_xml:
            data["reference_xml"] = example_xml
    else:
        data["warnings"].append(f"Reference block '{resolved_name}' not found at data/reference_blocks/{resolved_name}.xcos")

    if include_extra_examples:
        data["extra_examples"] = {}
        extra_example_prefix = f"{resolved_name}__"
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
            data["has_extra_examples"] = bool(data["extra_examples"])

    # 3. HELP
    help_file = None
    search_dir = os.path.join(DATA_DIR, "help")
    if os.path.exists(search_dir):
        for root, dirs, files in os.walk(search_dir):
            if f"{resolved_name}.xml" in files:
                help_file = os.path.join(root, f"{resolved_name}.xml")
                break

    data["has_help"] = bool(help_file)
    if include_help:
        data["help"] = None
        if not help_file:
            data["warnings"].append(f"Help file for '{resolved_name}' not found. Attempting to extract from MACRO source...")
            macros_dir = os.path.join(DATA_DIR, "macros")
            sci_path = None
            if os.path.exists(macros_dir):
                for root, dirs, files in os.walk(macros_dir):
                    if f"{resolved_name}.sci" in files:
                        sci_path = os.path.join(root, f"{resolved_name}.sci")
                        break
            if sci_path:
                try:
                    with open(sci_path, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                        preview = "".join(lines[:100])
                        data["help"] = f"--- AUTO-EXTRACTED FROM {resolved_name}.sci (First 100 lines) ---\n{preview}\n..."
                except Exception as e:
                    data["warnings"].append(f"Could not read macro file: {str(e)}")
            else:
                data["warnings"].append(f"Macro source for '{resolved_name}' not found either.")
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

async def _run_verification_local(
    xml_content: str,
    validation_profile: str = VALIDATION_PROFILE_FULL_RUNTIME,
    worker_timeout_seconds: float | None = None,
    progress_tracker: dict | None = None,
):
    normalized_profile = normalize_validation_profile(validation_profile)
    update_validation_progress_tracker(
        progress_tracker,
        validator_phase="pre-validation",
    )
    # --- Integration of Auto-fix and Validator ---
    try:
        parser = etree.XMLParser(remove_blank_text=True)
        tree = etree.fromstring(xml_content.encode('utf-8'), parser)
        
        # 1. Auto-fix
        auto_fixed = auto_fix_mux_to_scalar(tree)
        fanout_normalization = normalize_fanout_to_split_blocks(tree)
        if auto_fixed or fanout_normalization.get("normalized"):
            xml_content = etree.tostring(tree, encoding='unicode', pretty_print=True)
        
        # 2. Pre-simulation Validation
        val_errors = validate_port_sizes(tree)
        if val_errors:
            return {
                "success": False,
                "origin": "pre-sim-validator",
                "errors": val_errors,
                "auto_fixed_mux_to_scalar": auto_fixed,
                "validation_profile": normalized_profile,
                "fanout_normalization": fanout_normalization,
                "warnings": fanout_normalization.get("warnings"),
            }
            
    except Exception as e:
        return {
            "success": False,
            "origin": "pre-sim-validator",
            "validation_profile": normalized_profile,
            "error": f"Error during pre-validation: {str(e)}",
        }

    validation_mode = detect_validation_mode()
    # Stage 1 â€” fast Python structural audit (catches broken IDs, fan-outs, etc.)
    python_result = validate_diagram_structure(tree, auto_fixed)
    if not python_result["success"]:
        if fanout_normalization.get("normalized"):
            python_result["fanout_normalization"] = fanout_normalization
            python_result["warnings"] = (python_result.get("warnings") or []) + (fanout_normalization.get("warnings") or [])
        python_result["validation_profile"] = normalized_profile
        return python_result

    if normalized_profile == VALIDATION_PROFILE_HOSTED_SMOKE:
        scilab_result = await run_headless_scilab_import_validation(
            xml_content,
            auto_fixed,
            progress_tracker=progress_tracker,
        )
        merged_warnings = (
            (fanout_normalization.get("warnings") or [])
            + (python_result.get("warnings") or [])
            + (scilab_result.get("warnings") or [])
        )
        return {
            **scilab_result,
            "validation_profile": normalized_profile,
            "structural_check": {
                "success": python_result["success"],
                "warnings": python_result.get("warnings"),
            },
            "warnings": merged_warnings if merged_warnings else None,
            "origin": "hybrid (structural-python + scilab-import-check)",
            "fanout_normalization": fanout_normalization,
        }

    if should_prefer_poll_runtime(normalized_profile):
        poll_result = await run_poll_validation(
            xml_content,
            auto_fixed,
            progress_tracker=progress_tracker,
            origin="scilab-poll-runtime",
            progress_phase="scilab-poll-runtime",
        )
        merged_warnings = (
            (fanout_normalization.get("warnings") or [])
            + (python_result.get("warnings") or [])
            + (poll_result.get("warnings") or [])
        )
        return {
            **poll_result,
            "validation_profile": normalized_profile,
            "poll_runtime_preferred": True,
            "structural_check": {
                "success": python_result["success"],
                "warnings": python_result.get("warnings"),
            },
            "warnings": merged_warnings if merged_warnings else None,
            "fanout_normalization": fanout_normalization,
        }

    if validation_mode == "subprocess":
        scilab_result = await run_headless_scilab_validation(
            xml_content,
            auto_fixed,
            progress_tracker=progress_tracker,
        )
        poll_fallback_result = None

        if should_retry_with_poll_fallback(scilab_result):
            fallback_reason = describe_poll_fallback_reason(scilab_result)
            update_validation_progress_tracker(
                progress_tracker,
                validator_phase="poll-fallback-pending",
            )
            poll_fallback_result = await run_poll_validation(
                xml_content,
                auto_fixed,
                progress_tracker=progress_tracker,
            )
            merged_warnings = (
                (fanout_normalization.get("warnings") or [])
                + (python_result.get("warnings") or [])
                + (scilab_result.get("warnings") or [])
                + (poll_fallback_result.get("warnings") or [])
            )
            return {
                **poll_fallback_result,
                "validation_profile": normalized_profile,
                "fallback_used": True,
                "fallback_reason": fallback_reason,
                "subprocess_result": scilab_result,
                "poll_fallback_result": poll_fallback_result,
                "fanout_normalization": fanout_normalization,
                "structural_check": {
                    "success": python_result["success"],
                    "warnings": python_result.get("warnings"),
                },
                "warnings": merged_warnings if merged_warnings else None,
                "origin": "hybrid (structural-python + scilab-subprocess + scilab-poll-fallback)",
            }

        merged_warnings = (
            (fanout_normalization.get("warnings") or [])
            + (python_result.get("warnings") or [])
            + (scilab_result.get("warnings") or [])
        )

        return {
            **scilab_result,
            "validation_profile": normalized_profile,
            "structural_check": {
                "success": python_result["success"],
                "warnings": python_result.get("warnings"),
            },
            "warnings": merged_warnings if merged_warnings else None,
            "origin": "hybrid (structural-python + scilab-subprocess)",
            "fanout_normalization": fanout_normalization,
        }

    result = await run_poll_validation(xml_content, auto_fixed, progress_tracker=progress_tracker)
    result["validation_profile"] = normalized_profile
    result["fanout_normalization"] = fanout_normalization
    result["warnings"] = (result.get("warnings") or []) + (fanout_normalization.get("warnings") or [])
    return result


async def run_verification(
    xml_content: str,
    validation_profile: str = VALIDATION_PROFILE_FULL_RUNTIME,
    worker_timeout_seconds: float | None = None,
):
    normalized_profile = normalize_validation_profile(validation_profile)
    if should_offload_full_runtime_validation(normalized_profile):
        timeout_seconds = (
            float(worker_timeout_seconds)
            if worker_timeout_seconds and worker_timeout_seconds > 0
            else get_configured_validation_job_timeout_seconds()
        )
        try:
            result = await run_remote_validation_worker(
                xml_content,
                normalized_profile,
                timeout_seconds,
            )
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                error_type = "timeout"
            elif is_retryable_worker_request_error(exc):
                error_type = "request"
            else:
                error_type = "runtime"
            return {
                "success": False,
                "origin": "validation-worker-remote",
                "validation_profile": normalized_profile,
                "error": f"Remote validation worker failed: {exc}",
                "remote_worker": {
                    "url": get_validation_worker_url(),
                    "timeout_seconds": timeout_seconds,
                    "request_retry_limit": get_validation_worker_request_retry_count(),
                    "retry_backoff_seconds": get_validation_worker_retry_backoff_seconds(),
                    "error_type": error_type,
                    "error_class": type(exc).__name__,
                    "retryable": is_retryable_worker_request_error(exc),
                },
            }
        return result
    return await _run_verification_local(xml_content, normalized_profile, worker_timeout_seconds)


async def verify_xcos_xml(xml_content: str):
    result = await run_verification(xml_content)
    remember_validation_result(xml_content, result)
    return make_json_response(make_public_validation_payload(result))


def _schedule_validation_job(job_id: str):
    task = asyncio.create_task(_run_validation_job(job_id))
    state.validation_tasks[job_id] = task

    def _cleanup(_: asyncio.Task):
        state.validation_tasks.pop(job_id, None)

    task.add_done_callback(_cleanup)
    return task


async def _run_validation_job(job_id: str):
    job = state.validation_jobs.get(job_id)
    if not job:
        return

    job.status = "running"
    job.started_at = now_iso()
    job.error = None
    persist_validation_job(job_id)

    session_meta = None
    try:
        if job.session_id not in state.drafts:
            job.status = "failed"
            job.finished_at = now_iso()
            job.error = f"Session {job.session_id} not found"
            persist_validation_job(job_id)
            return

        draft_normalization = normalize_draft_fanout(job.session_id)
        xml_content = state.drafts[job.session_id].to_xml()
        session_meta = write_session_snapshot(job.session_id)
        result = await asyncio.wait_for(
            run_verification(
                xml_content,
                validation_profile=job.validation_profile,
                worker_timeout_seconds=job.timeout_seconds,
            ),
            timeout=job.timeout_seconds,
        )
        if draft_normalization.get("normalized"):
            result["fanout_normalization"] = draft_normalization
            result["warnings"] = (result.get("warnings") or []) + (draft_normalization.get("warnings") or [])
        remember_validation_result(xml_content, result)
        record_validation_outcome(job.session_id, result, session_meta)
        job.status = "succeeded" if result.get("success") else "failed"
        job.finished_at = now_iso()
        job.result = result
        job.error = result.get("error")
        persist_validation_job(job_id)
    except asyncio.TimeoutError:
        timeout_result = {
            "success": False,
            "task_id": job.job_id,
            "error": f"Validation timed out after {job.timeout_seconds:.0f} seconds.",
            "origin": "validation-job",
            "validation_profile": job.validation_profile,
            "file_path": session_meta["path"] if session_meta else None,
            "file_size_bytes": session_meta["size_bytes"] if session_meta else None,
        }
        record_validation_outcome(job.session_id, timeout_result, session_meta)
        job.status = "timed_out"
        job.finished_at = now_iso()
        job.result = timeout_result
        job.error = timeout_result["error"]
        persist_validation_job(job_id)
    except Exception as exc:
        error_result = {
            "success": False,
            "task_id": job.job_id,
            "error": f"Validation job failed: {exc}",
            "origin": "validation-job",
            "validation_profile": job.validation_profile,
            "file_path": session_meta["path"] if session_meta else None,
            "file_size_bytes": session_meta["size_bytes"] if session_meta else None,
        }
        record_validation_outcome(job.session_id, error_result, session_meta)
        job.status = "failed"
        job.finished_at = now_iso()
        job.result = error_result
        job.error = error_result["error"]
        persist_validation_job(job_id)


async def xcos_start_validation(
    session_id: str,
    timeout_seconds: float | None = None,
    validation_profile: str = VALIDATION_PROFILE_FULL_RUNTIME,
):
    if session_id not in state.drafts:
        return make_text_response(f"Error: Session {session_id} not found")

    try:
        validation_profile = normalize_validation_profile(validation_profile)
    except ValueError as exc:
        return make_text_response(f"Error: {exc}")

    if timeout_seconds is None:
        timeout_seconds = get_configured_validation_job_timeout_seconds()
    if timeout_seconds <= 0:
        return make_text_response("Error: timeout_seconds must be greater than 0")

    session_meta = write_session_snapshot(session_id)
    job_id = str(uuid.uuid4())
    workflow_id = state.draft_to_workflow.get(session_id)
    job = ValidationJob(
        job_id=job_id,
        session_id=session_id,
        workflow_id=workflow_id,
        validation_profile=validation_profile,
        status="queued",
        created_at=now_iso(),
        timeout_seconds=float(timeout_seconds),
    )
    state.validation_jobs[job_id] = job
    persist_validation_job(job_id)
    _schedule_validation_job(job_id)

    payload = make_validation_job_public_payload(job)
    payload["message"] = f"Validation job {job_id} queued for session {session_id}."
    payload["session_file_path"] = session_meta["path"]
    payload["session_file_size_bytes"] = session_meta["size_bytes"]
    return make_json_response(payload)


async def xcos_get_validation_status(job_id: str):
    job = state.validation_jobs.get(job_id)
    if not job:
        return make_text_response(f"Error: Validation job {job_id} not found")
    return make_json_response(make_validation_job_public_payload(job))

# --- Incremental Tool Implementations ---

async def xcos_create_workflow(problem_statement: str, autopilot: bool = False):
    if not problem_statement.strip():
        return make_text_response("Error: problem_statement cannot be empty")
    requirements, context_lines, unsupported_blocks = derive_generation_requirements(problem_statement)
    if unsupported_blocks:
        return make_text_response(
            "Error: Unsupported required block(s) requested: "
            + ", ".join(sorted(unsupported_blocks))
            + ". Remove them or add them to the catalogue before continuing."
        )
    workflow = create_workflow_session(
        problem_statement,
        generation_requirements=requirements,
        generation_context_lines=context_lines,
        autopilot=autopilot,
    )
    approval_required_phases = [] if workflow.autopilot else [
        phase_key for phase_key in WORKFLOW_PHASE_ORDER
        if phase_key in REVIEWABLE_PHASES
    ]
    return make_json_response({
        "status": "success",
        "workflow_id": workflow.workflow_id,
        "workflow": workflow.to_dict(),
        "phase_order": WORKFLOW_PHASE_ORDER,
        "approval_required_phases": approval_required_phases,
        "phase_start_requirements": {
            "phase3_implementation": {
                "tool": "xcos_start_draft",
                "requires_approved_phases": WORKFLOW_PHASE_ORDER[:2],
                "message": "Phase 2 must be approved before Phase 3 implementation can start.",
            }
        },
        "next_required_action": (
            (
                f"Submit {WORKFLOW_PHASE_LABELS[workflow.current_phase]}. "
                "Autopilot will auto-advance approved phases on successful submission."
            )
            if workflow.autopilot else
            (
                f"Submit {WORKFLOW_PHASE_LABELS[workflow.current_phase]} and wait for approval "
                "before starting implementation."
            )
        ),
    })


async def xcos_list_workflows(view: str = "summary"):
    return make_json_response({"workflows": list_workflow_payloads(view=view)})


async def xcos_get_workflow(workflow_id: str, view: str = "summary"):
    workflow = get_workflow(workflow_id)
    if not workflow:
        return make_text_response(f"Error: Workflow {workflow_id} not found")
    return make_json_response({"workflow": workflow.to_dict(view=view)})


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

async def _legacy_xcos_get_validation_widget(xml_content: str):
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
        error_msgs.append("âš  Auto-fixed MUX to scalar connections")
        
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

async def xcos_get_validation_widget(xml_content: str):
    result = get_cached_validation_result(xml_content)
    if result is None:
        try:
            result = await run_verification(xml_content)
            remember_validation_result(xml_content, result)
        except Exception as e:
            result = {
                "success": False,
                "error": f"Validator internal error: {str(e)}",
                "origin": "internal-error",
            }

    public_result = make_public_validation_payload(result)
    return make_json_response({
        "widget_type": "validation",
        "payload": {
            "success": public_result.get("success", False),
            "error": None if public_result.get("success") else public_result.get("message")
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
                
    categories = []
    if category:
        categories = [part.strip() for part in category.split(",") if part.strip()]
        lowered_categories = [part.lower() for part in categories]
        blocks = [
            b for b in blocks
            if any(cat in b.get("category", "").lower() for cat in lowered_categories)
        ]
        
    formatted_blocks = []
    for b in blocks:
        image = resolve_block_image(b.get("name", ""))
        formatted_blocks.append({
            "name": b.get("name", ""),
            "type": b.get("category", ""),
            "description": b.get("description", ""),
            "image_data_uri": image.get("src") if image else None,
            "image_file_name": image.get("file_name") if image else None,
        })
        
    return make_json_response({
        "widget_type": "catalogue",
        "payload": {
            "category": category,
            "categories": categories,
            "blocks": formatted_blocks
        }
    })

def generate_topology_svg(session_id: str) -> tuple[str, int, int]:
    """Returns (svg_string, block_count, link_count) or raises ValueError"""
    if session_id not in state.drafts:
        raise ValueError(f"Session {session_id} not found")
        
    draft = state.drafts[session_id]
    xml_content = draft.to_xml()
    
    try:
        parser = etree.XMLParser(remove_blank_text=True)
        tree = etree.fromstring(xml_content.encode("utf-8"), parser)
    except Exception as e:
        raise ValueError(f"Error parsing XML: {str(e)}")
        
    blocks = tree.xpath(XCOS_BLOCK_XPATH)
    links = tree.xpath(XCOS_LINK_XPATH)
    
    block_map = {}
    for b in blocks:
        bid = b.get("id")
        name = b.get("interfaceFunctionName", b.tag)
        block_map[bid] = {"name": name, "in_ports": [], "out_ports": []}
        
    # Build ports_map: map every port id -> {block_id, type}
    #
    # In Xcos XML, ports are NOT nested inside their block element â€” they are
    # siblings under <root> that declare ownership via a @parent="blockId"
    # attribute. The per-block child-XPath approach therefore finds nothing.
    # The correct strategy: scan every Port-like element in the whole tree and
    # use the @parent attribute to associate it with the owning block.
    #
    # Two-stage to handle both the sibling-with-@parent style (standard Xcos)
    # and the rare nested-child style:
    ports_map = {}

    # Stage 1 â€” sibling style: @parent attribute points to the block id
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

    # Stage 2 â€” nested-child style: port is a descendant of the block element
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

    block_images_dir = os.path.join(BASE_DIR, "block_images")

    for idx, (bid, bdata) in enumerate(block_map.items()):
        b_coords[bid] = (curr_x, curr_y)
        
        svg_path = os.path.join(block_images_dir, f"{bdata['name']}.svg")
        png_path = os.path.join(block_images_dir, f"{bdata['name']}.png")
        
        if os.path.exists(svg_path):
            svg_nodes.append(f'<image href="http://localhost:{SERVER_PORT}/block_images/{bdata["name"]}.svg" x="{curr_x}" y="{curr_y}" width="{node_w}" height="{node_h}" />')
        elif os.path.exists(png_path):
            svg_nodes.append(f'<image href="http://localhost:{SERVER_PORT}/block_images/{bdata["name"]}.png" x="{curr_x}" y="{curr_y}" width="{node_w}" height="{node_h}" />')
        else:
            svg_nodes.append(f'<rect x="{curr_x}" y="{curr_y}" width="{node_w}" height="{node_h}" fill="#f8f9fa" stroke="#343a40" rx="4" />')
            svg_nodes.append(f'<text x="{curr_x + 6}" y="{curr_y + 24}" font-family="sans-serif" font-size="12" fill="#000">{bdata["name"]}</text>')
            
        curr_y += pad_y
        if idx > 0 and (idx + 1) % 10 == 0:
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
    
    svg_out = f'''<svg width="{max_x}" height="{max_y}" viewBox="0 0 {max_x} {max_y}" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" fill="#007bff" />
        </marker>
      </defs>
      {edges_str}
      {nodes_str}
    </svg>'''
    
    return svg_out, len(block_map), len(links)


def _generate_topology_svg(session_id: str) -> tuple[str, int, int]:
    if session_id not in state.drafts:
        raise ValueError(f"Session {session_id} not found")
        
    draft = state.drafts[session_id]
    xml_content = draft.to_xml()
    
    parser = etree.XMLParser(remove_blank_text=True)
    tree = etree.fromstring(xml_content.encode("utf-8"), parser)
        
    blocks = tree.xpath(XCOS_BLOCK_XPATH)
    links = tree.xpath(XCOS_LINK_XPATH)
    
    block_map = {}
    for b in blocks:
        bid = b.get("id")
        name = b.get("interfaceFunctionName", b.tag)
        block_map[bid] = {"name": name, "in_ports": [], "out_ports": []}
        
    ports_map = {}

    for p in tree.iter():
        if not isinstance(p.tag, str):
            continue
        if "Port" not in p.tag:
            continue
        pid = p.get("id")
        if not pid:
            continue
        owner_id = p.get("parent")
        if owner_id and owner_id in block_map and pid not in ports_map:
            tag = p.tag
            p_type = "in" if any(k in tag for k in ("Input", "InPort", "Control")) else "out"
            ports_map[pid] = {"block_id": owner_id, "type": p_type}
            bdata = block_map[owner_id]
            if p_type == "in":
                bdata["in_ports"].append(pid)
            else:
                bdata["out_ports"].append(pid)

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
        
        name = bdata["name"]
        image = resolve_block_image(name)

        if image and image.get("src"):
            img_src = html.escape(image["src"], quote=True)
            svg_nodes.append(
                f'<image href="{img_src}" x="{curr_x}" y="{curr_y}" width="{node_w}" height="{node_h}" preserveAspectRatio="xMidYMid meet" />'
            )
        else:
            svg_nodes.append(f'<rect x="{curr_x}" y="{curr_y}" width="{node_w}" height="{node_h}" fill="#f8f9fa" stroke="#343a40" rx="4" />')
            svg_nodes.append(f'<text x="{curr_x + 6}" y="{curr_y + 24}" font-family="sans-serif" font-size="12" fill="#000">{name}</text>')
            
        curr_y += pad_y
        if idx > 0 and idx % 10 == 0:
            curr_y = 20
            curr_x += pad_x

    connected_ports = set()
    link_strings = []
    
    for l in links:
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
    
    svg_out = f'''<svg width="{max_x}" height="{max_y}" viewBox="0 0 {max_x} {max_y}" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" fill="#007bff" />
        </marker>
      </defs>
      {edges_str}
      {nodes_str}
    </svg>'''
    
    return svg_out, len(block_map), len(links)

async def xcos_get_topology_widget(session_id: str):
    base_url = get_server_base_url()

    try:
        svg_out, block_count, link_count = _generate_topology_svg(session_id)

        payload = {
            "widget_type": "topology",
            "payload": {
                "session_id": session_id,
                "block_count": block_count,
                "link_count": link_count,
                "svg": svg_out
            }
        }

        markdown_str = f"""### Xcos Topology Visual

![Topology]({base_url}/api/topology/{session_id}/svg)

[Open Interactive UI]({base_url}/workflow-ui/)

"""

        return [
            mcp_types.TextContent(type="text", text=json.dumps(payload, indent=2)),
            mcp_types.TextContent(type="text", text=markdown_str),
        ]
    except Exception as e:
        return make_json_response({
            "widget_type": "topology",
            "payload": {
                "error": f"Error: {str(e)}"
            }
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


async def xcos_start_draft(
    schema_version: str = "1.1",
    workflow_id: str | None = None,
    replace: bool = False,
    phases: list[str] = None,
    session_id: str | None = None,
):
    workflow = None
    if workflow_id:
        workflow = get_workflow(workflow_id)
        if not workflow:
            return make_text_response(f"Error: Workflow {workflow_id} not found")
        if workflow.phases["phase2_architecture"].status != "approved":
            return make_text_response(
                "Error: Phase 2 must be approved before Phase 3 implementation can start."
            )
        if workflow.draft_session_id and not replace and (not session_id or workflow.draft_session_id != session_id):
            return make_text_response(f"Error: Workflow {workflow_id} already has an active draft session ({workflow.draft_session_id}). Pass replace=True to overwrite.")
    if phases and len(set(phases)) != len(phases):
        return make_text_response("Error: Phases list must contain unique labels.")

    resumed = False
    created = False
    existing_session_id = workflow.draft_session_id if workflow else None

    if replace and workflow and existing_session_id and existing_session_id != session_id:
        delete_draft_session(existing_session_id)
        workflow.draft_session_id = None

    target_session_id = session_id or existing_session_id or str(uuid.uuid4())
    draft = state.drafts.get(target_session_id)

    if draft:
        resumed = True
    else:
        created = True
        draft = DraftDiagram(schema_version, session_id=target_session_id)
        state.drafts[target_session_id] = draft

    draft.session_id = target_session_id
    draft.schema_version = draft.schema_version or schema_version
    draft.restored_from_disk = False

    payload = {
        "status": "success",
        "session_id": target_session_id,
        "resumed": resumed,
        "created": created,
        "message": (
            f"Resumed Xcos draft session {target_session_id}"
            if resumed else
            f"Started new Xcos draft session {target_session_id}"
        ),
        "critical_rule": "IMPORTANT: Any ExplicitOutputPort or EventOutPort that fanning out to multiple downstream blocks REQUIRES an intermediate SplitBlock (for data) or CLKSPLIT_f (for events)."
    }

    if phases:
        plan = {"phases": phases, "completed": []}
        state.phase_plans[target_session_id] = plan
        draft.phase_plan = plan
        payload["phase_plan_registered"] = True
        payload["phase_count"] = len(phases)
    elif draft.phase_plan:
        state.phase_plans[target_session_id] = draft.phase_plan

    if workflow:
        if workflow.draft_session_id and workflow.draft_session_id != target_session_id and replace:
            delete_draft_session(workflow.draft_session_id)
        state.draft_to_workflow[target_session_id] = workflow.workflow_id
        draft.workflow_id = workflow.workflow_id
        requirements = normalize_generation_requirements(workflow.generation_requirements)
        if requirements["must_use_context"] and not draft.context_lines:
            draft.set_context(workflow.generation_context_lines or [])
        workflow.draft_session_id = target_session_id
        workflow.current_phase = "phase3_implementation"
        workflow.updated_at = now_iso()
        workflow.phases["phase3_implementation"].status = "in_progress"
        workflow.phases["phase3_implementation"].submitted_at = workflow.phases["phase3_implementation"].submitted_at or now_iso()
        workflow.phases["phase3_implementation"].last_error = None
        persist_workflow_session(workflow.workflow_id)
        payload["workflow_id"] = workflow.workflow_id

    persist_draft_session(target_session_id)
    return make_json_response(payload)


async def xcos_set_context(session_id: str, context_lines: list[str]):
    if session_id not in state.drafts:
        return make_text_response(f"Error: Session {session_id} not found")
    draft = state.drafts[session_id]
    draft.set_context(context_lines)
    persist_draft_session(session_id)
    return make_json_response({
        "status": "success",
        "session_id": session_id,
        "context_line_count": len(draft.context_lines),
        "context_lines": list(draft.context_lines),
    })

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
        last_verified = build_session_last_verified(draft)
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
            "restored_from_disk": draft.restored_from_disk,
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
    before_counts = summarize_draft(state.drafts[session_id])
    state.drafts[session_id].add_blocks(blocks_xml)
    workflow_id = state.draft_to_workflow.get(session_id)
    if workflow_id and workflow_id in state.workflows:
        workflow = state.workflows[workflow_id]
        workflow.current_phase = "phase3_implementation"
        workflow.updated_at = now_iso()
        workflow.phases["phase3_implementation"].status = "in_progress"
        persist_workflow_session(workflow_id)
    persist_draft_session(session_id)
    after_counts = summarize_draft(state.drafts[session_id])
    return make_json_response({
        "status": "success",
        "session_id": session_id,
        "message": f"Successfully added blocks to session {session_id}",
        "added_block_count": after_counts["block_count"] - before_counts["block_count"],
        "block_count": after_counts["block_count"],
        "link_count": after_counts["link_count"],
    })

async def xcos_add_links(session_id: str, links_xml: str):
    if session_id not in state.drafts:
        return make_text_response(f"Error: Session {session_id} not found")
    before_counts = summarize_draft(state.drafts[session_id])
    state.drafts[session_id].add_links(links_xml)
    workflow_id = state.draft_to_workflow.get(session_id)
    if workflow_id and workflow_id in state.workflows:
        workflow = state.workflows[workflow_id]
        workflow.current_phase = "phase3_implementation"
        workflow.updated_at = now_iso()
        workflow.phases["phase3_implementation"].status = "in_progress"
        persist_workflow_session(workflow_id)
    persist_draft_session(session_id)
    after_counts = summarize_draft(state.drafts[session_id])
    return make_json_response({
        "status": "success",
        "session_id": session_id,
        "message": f"Successfully added links to session {session_id}",
        "added_link_count": after_counts["link_count"] - before_counts["link_count"],
        "block_count": after_counts["block_count"],
        "link_count": after_counts["link_count"],
    })

async def _legacy_xcos_verify_draft(session_id: str):
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
    draft.last_verified_profile = normalize_validation_profile(result.get("validation_profile"))

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
            "validation_profile": draft.last_verified_profile,
        }
        workflow.updated_at = now_iso()

    result["session_file_path"] = session_meta["path"]
    result["session_file_size_bytes"] = session_meta["size_bytes"]
    result["workflow_id"] = workflow_id
    return make_json_response(result)

async def xcos_verify_draft(
    session_id: str,
    validation_profile: str = VALIDATION_PROFILE_FULL_RUNTIME,
):
    if session_id not in state.drafts:
        return make_text_response(f"Error: Session {session_id} not found")
    start_payload = parse_mcp_text_json_response(
        await xcos_start_validation(
            session_id,
            get_configured_validation_job_timeout_seconds(),
            validation_profile,
        )
    )
    job_id = start_payload["job_id"]
    task = state.validation_tasks.get(job_id)
    if task:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=ASYNC_VALIDATION_BRIEF_WAIT_SECONDS)
        except asyncio.TimeoutError:
            pass
    return await xcos_get_validation_status(job_id)



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

    # Only append blocks when explicitly provided â€” prevents duplication when blocks
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
        persist_draft_session(session_id)

    # Mark phase complete (idempotent)
    if phase_label not in plan["completed"]:
        plan["completed"].append(phase_label)
    state.drafts[session_id].phase_plan = plan
    persist_draft_session(session_id)

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
        "download_url": build_session_download_url(session_id),
        "last_validation_profile": state.drafts[session_id].last_verified_profile,
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
    if not session_meta:
        return make_text_response(f"Error: Session snapshot for {session_id} doesn't exist yet.")
    draft = state.drafts[session_id]
    payload = {
        "session_id": session_id,
        "session_file_path": session_meta["path"],
        "session_file_size_bytes": session_meta["size_bytes"],
        "download_url": build_session_download_url(session_id),
        "last_verified": build_session_last_verified(draft),
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


async def read_request_json_lenient(request: Request) -> dict:
    raw = await request.body()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        text = raw.decode("utf-8", errors="replace")
        return json.loads(text, strict=False)


async def http_handle_get_task(request: Request) -> Response:
    try:
        task = state.task_queue.get_nowait()
        state.last_poll_time = datetime.now()
        return http_json({"status": "pending", **task})
    except asyncio.QueueEmpty:
        state.last_poll_time = datetime.now()
        return http_json({"status": "idle"})


async def http_handle_post_result(request: Request) -> Response:
    try:
        data = await read_request_json_lenient(request)
    except json.JSONDecodeError as exc:
        return http_json(
            {"status": "error", "message": f"Invalid JSON body: {exc.msg}"},
            status_code=400,
        )
    task_id = data.get("task_id")
    success = data.get("success")
    error = data.get("error", "")

    if task_id in state.results:
        state.results[task_id]["success"] = success
        state.results[task_id]["error"] = error
        details = dict(state.results[task_id].get("details") or {})
        if data.get("scilab_active_stage") or data.get("scilab_last_completed_stage") or data.get("scilab_stage_trace"):
            details["scilab_active_stage"] = data.get("scilab_active_stage")
            details["scilab_last_completed_stage"] = data.get("scilab_last_completed_stage")
            details["scilab_stage_trace"] = [
                dict(item)
                for item in (data.get("scilab_stage_trace") or details.get("scilab_stage_trace") or [])
            ]
        state.results[task_id]["details"] = {
            **details,
            "scilab_import_passed": data.get("scilab_import_passed"),
            "scilab_block_validation_passed": data.get("scilab_block_validation_passed"),
            "scilab_link_validation_passed": data.get("scilab_link_validation_passed"),
            "scilab_simulation_passed": data.get("scilab_simulation_passed"),
            "graphical_blocks_substituted": data.get("graphical_blocks_substituted"),
            "substituted_blocks": data.get("substituted_blocks"),
            "diary_path": data.get("diary_path"),
        }
        merge_validation_progress_tracker(
            state.results[task_id].get("progress_tracker"),
            state.results[task_id]["details"],
        )
        state.results[task_id]["event"].set()
        return http_json({"status": "received"})
    return http_json({"status": "error", "message": "Task ID not found"}, status_code=404)


async def http_handle_post_progress(request: Request) -> Response:
    try:
        data = await read_request_json_lenient(request)
    except json.JSONDecodeError as exc:
        return http_json(
            {"status": "error", "message": f"Invalid JSON body: {exc.msg}"},
            status_code=400,
        )

    task_id = data.get("task_id")
    if task_id not in state.results:
        return http_json({"status": "error", "message": "Task ID not found"}, status_code=404)

    stage_name = str(data.get("stage") or "").strip()
    stage_status = str(data.get("status") or "").strip().upper()
    if not stage_name or stage_status not in {"BEGIN", "END"}:
        return http_json(
            {"status": "error", "message": "stage and status (BEGIN/END) are required"},
            status_code=400,
        )

    current_details = state.results[task_id].get("details") or {}
    updated_details = apply_validation_progress_update(current_details, stage_name, stage_status)
    state.results[task_id]["details"] = updated_details
    merge_validation_progress_tracker(state.results[task_id].get("progress_tracker"), updated_details)
    return http_json({"status": "received"})


async def http_healthz(_: Request) -> Response:
    return http_json(
        {
            "status": "ok",
            "version": SERVER_VERSION,
            "validator_mode": detect_validation_mode(),
            "runtime_timeouts": get_runtime_timeout_snapshot(),
            "startup_preflight": state.startup_preflight,
            "workflow_count": len(state.workflows),
            "draft_count": len(state.drafts),
            "poll_worker_active": poll_worker_is_active(),
            "mcp_http_path": MCP_HTTP_PATH,
        }
    )


async def http_root(_: Request) -> Response:
    return RedirectResponse(url="/workflow-ui/")


async def http_workflow_ui(_: Request) -> Response:
    return HTMLResponse(load_ui_html())



async def http_api_topology_svg(request: Request) -> Response:
    session_id = request.path_params["session_id"]
    try:
        svg_out, _, _ = _generate_topology_svg(session_id)
        return Response(svg_out, media_type="image/svg+xml")
    except Exception as e:
        return PlainTextResponse(f"Error generating SVG: {str(e)}", status_code=500)

async def http_block_image(request: Request) -> Response:
    asset_name = request.path_params["asset_name"]
    safe_name = os.path.basename(asset_name)
    ui_path = os.path.join(BASE_DIR, "block_images", safe_name)
    if not os.path.exists(ui_path):
        return PlainTextResponse("Not Found", status_code=404)

    media_type = "image/svg+xml" if safe_name.endswith(".svg") else "image/png"
    with open(ui_path, "rb") as f:
        return Response(f.read(), media_type=media_type)

async def http_ui_asset(request: Request) -> Response:
    asset_name = request.path_params["asset_name"]
    safe_name = os.path.basename(asset_name)
    ui_path = os.path.join(UI_DIR, safe_name)
    if not os.path.exists(ui_path):
        return PlainTextResponse("Not Found", status_code=404)

    media_type = "text/plain"
    if safe_name.endswith(".js"):
        media_type = "text/javascript"
    elif safe_name.endswith(".css"):
        media_type = "text/css"
    elif safe_name.endswith(".html"):
        media_type = "text/html"

    with open(ui_path, "r", encoding="utf-8") as f:
        return Response(f.read(), media_type=media_type)


async def http_api_list_workflows(_: Request) -> Response:
    return http_json({"workflows": list_workflow_payloads()})


async def http_api_create_workflow(request: Request) -> Response:
    data = await request.json()
    problem_statement = (data.get("problem_statement") or "").strip()
    if not problem_statement:
        return http_json({"error": "problem_statement cannot be empty"}, status_code=400)
    result = await xcos_create_workflow(problem_statement, bool(data.get("autopilot", False)))
    text = result[0].text
    if text.startswith("Error:"):
        return http_json({"error": text[7:].strip()}, status_code=400)
    return http_json(json.loads(text))


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


async def http_api_topology_svg(request: Request) -> Response:
    session_id = request.path_params.get("session_id")
    try:
        svg_out, _, _ = _generate_topology_svg(session_id)
        return Response(svg_out, media_type="image/svg+xml")
    except Exception as e:
        return http_json({"error": str(e)}, status_code=400)

async def http_api_session_file(request: Request) -> Response:
    session_id = request.path_params.get("session_id")
    file_path = get_session_file_path(session_id)
    if not os.path.exists(file_path):
        return http_json({"error": f"Session snapshot for {session_id} doesn't exist yet."}, status_code=404)
    with open(file_path, "rb") as f:
        return Response(
            f.read(),
            media_type="application/xml",
            headers={"Content-Disposition": f'attachment; filename="{session_id}.xcos"'},
        )

async def http_block_image(request: Request) -> Response:
    image_name = request.path_params.get("image_name")
    if not image_name:
         return PlainTextResponse("Not Found", status_code=404)
    # Be careful to avoid path traversal
    safe_name = os.path.basename(image_name)
    img_path = os.path.join(BASE_DIR, "block_images", safe_name)
    if not os.path.exists(img_path):
        return PlainTextResponse("Not Found", status_code=404)

    media_type = "image/svg+xml" if safe_name.endswith(".svg") else "image/png"
    with open(img_path, "rb") as f:
        return Response(f.read(), media_type=media_type)


async def http_ext_apps_js(request: Request) -> Response:
    request.path_params["asset_name"] = "ext-apps.js"
    return await http_ui_asset(request)


class StreamableHTTPRouteApp:
    def __init__(self, session_manager: StreamableHTTPSessionManager):
        self.session_manager = session_manager

    async def __call__(self, scope, receive, send) -> None:
        await self.session_manager.handle_request(scope, receive, send)


streamable_http_manager = None


@asynccontextmanager
async def starlette_lifespan(_: Starlette):
    startup_task = None
    async with streamable_http_manager.run():
        if detect_validation_mode() == "subprocess" and os.name != "nt":
            startup_task = asyncio.create_task(ensure_poll_worker_running())
        try:
            yield
        finally:
            if startup_task:
                startup_task.cancel()
            await stop_poll_worker()

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
        Route("/workflow-ui", http_root, methods=["GET"]),
        Route("/workflow-ui/", http_workflow_ui, methods=["GET"]),
        Route("/workflow-ui/api/workflows", http_api_list_workflows, methods=["GET"]),
        Route("/workflow-ui/api/workflows", http_api_create_workflow, methods=["POST"]),
        Route("/workflow-ui/api/workflows/{workflow_id}", http_api_get_workflow, methods=["GET"]),
        Route("/workflow-ui/api/workflows/{workflow_id}/phases/{phase}/submit", http_api_submit_phase, methods=["POST"]),
        Route("/workflow-ui/api/workflows/{workflow_id}/phases/{phase}/review", http_api_review_phase, methods=["POST"]),
        Route("/workflow-ui/api/workflows/{workflow_id}/draft/start", http_api_start_draft, methods=["POST"]),
        Route("/workflow-ui/ext-apps.js", http_ext_apps_js, methods=["GET"]),
        Route("/workflow-ui/{asset_name:str}", http_ui_asset, methods=["GET"]),
        Route("/api/topology/{session_id:str}/svg", http_api_topology_svg, methods=["GET"]),
        Route("/api/sessions/{session_id:str}/diagram.xcos", http_api_session_file, methods=["GET"]),
        Route("/block_images/{asset_name:str}", http_block_image, methods=["GET"]),
        Route("/task", http_handle_get_task, methods=["GET"]),
        Route("/progress", http_handle_post_progress, methods=["POST"]),
        Route("/result", http_handle_post_result, methods=["POST"]),
        Route("/block_images/{image_name:str}", http_block_image, methods=["GET"]),
        Route("/api/topology/{session_id}/svg", http_api_topology_svg, methods=["GET"]),
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
    version=SERVER_VERSION,
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
    ui_path = os.path.join(UI_DIR, "index.html")
    if not os.path.exists(ui_path):
        return []
    return [
        mcp_types.Resource(
            uri=WORKFLOW_UI_RESOURCE_URI,
            name="Xcos Workflow UI",
            title="Xcos Workflow UI",
            description="Embedded workflow UI for the MCP app.",
            mimeType=MCP_APP_MIME_TYPE,
            _meta=build_ui_resource_meta(),
        )
    ]


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
        meta = build_ui_resource_meta() if filename.endswith(".html") else None
        return [ReadResourceContents(content=f.read(), mime_type=mime_type, meta=meta)]

@mcp_server.list_tools()
async def handle_list_tools() -> list[mcp_types.Tool]:
    tools = [
        mcp_types.Tool(
            name="xcos_get_status_widget",
            description=(
                "Call this first for Xcos diagram work. PHASE 2 (block diagram preview): "
                "It returns the connection/status widget and should be displayed to the user. "
                "After receiving this tool's response, you MUST call the visualize:show_widget tool "
                "to render the data as an HTML widget. Do not display raw JSON to the user."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        mcp_types.Tool(
            name="xcos_get_workflow_widget",
            description=(
                "Call this after every xcos_submit_phase and xcos_review_phase call. "
                "Always display the returned widget â€” it shows the user their current phase "
                "progress. Pass workflow_id to show a specific workflow, or omit it to list all "
                "active workflows. After receiving this tool's response, you MUST call the "
                "visualize:show_widget tool to render the data as an HTML widget. Do not display "
                "raw JSON to the user."
            ),
            inputSchema={"type": "object", "properties": {"workflow_id": {"type": "string"}}},
        ),
        mcp_types.Tool(
            name="xcos_get_validation_widget",
            description=(
                "Display the validation widget for the current draft XML."
            ),
            inputSchema={"type": "object", "properties": {"xml_content": {"type": "string"}}, "required": ["xml_content"]},
            **{"_meta": {"ui": {"resourceUri": "ui://xcos/index.html"}}}
        ),
        mcp_types.Tool(
            name="xcos_get_block_catalogue_widget",
            description=(
                "PHASE 1 â€” Step 2. Call this after xcos_get_status_widget to identify "
                "which blocks are available for the user's request. Filter by the relevant "
                "category (e.g. \"Sources\", \"Continuous\", \"Sinks/Visualization\", "
                "\"Math Operations\"). Always display the returned widget to the user so "
                "they can see and confirm the blocks being selected before any math is "
                "explained. After receiving this tool's response, you MUST call the "
                "visualize:show_widget tool to render the data as an HTML widget. Do not "
                "display raw JSON to the user."
            ),
            inputSchema={"type": "object", "properties": {"category": {"type": "string"}}},
        ),
        mcp_types.Tool(
            name="xcos_get_topology_widget",
            description=(
                "Display the current draft topology. Use it after adding blocks and again after "
                "adding links. After receiving this tool's response, you MUST call the "
                "visualize:show_widget tool to render the data as an HTML widget. Do not display "
                "raw JSON to the user."
            ),
            inputSchema={"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
        ),
        mcp_types.Tool(
            name="xcos_create_workflow",
            description=(
                "PHASE 1 â€” Step 3. Call this with the user's problem statement to register "
                "the 3-phase workflow. Store the returned workflow_id â€” it is required for "
                "all subsequent xcos_submit_phase, xcos_review_phase, xcos_get_workflow_widget, "
                "and xcos_start_draft calls. Pass autopilot=true only when the user explicitly "
                "wants approvals to auto-advance. Do not proceed without the workflow_id."
            ),
            inputSchema={"type": "object", "properties": {"problem_statement": {"type": "string"}, "autopilot": {"type": "boolean", "default": False}}, "required": ["problem_statement"]},
        ),
        mcp_types.Tool(
            name="xcos_list_workflows",
            description="List all phased Xcos workflow sessions and their review state.",
            inputSchema={"type": "object", "properties": {"view": {"type": "string", "enum": ["summary", "full"], "default": "summary"}}},
        ),
        mcp_types.Tool(
            name="xcos_get_workflow",
            description="Get one phased Xcos workflow session. Use view='summary' for compact status or view='full' for all phase content.",
            inputSchema={"type": "object", "properties": {"workflow_id": {"type": "string"}, "view": {"type": "string", "enum": ["summary", "full"], "default": "summary"}}, "required": ["workflow_id"]},
        ),
        mcp_types.Tool(
            name="xcos_submit_phase",
            description=(
                "Submits content for a workflow phase and sets it to \"awaiting_approval\".\n"
                "Call this at these specific moments:\n"
                "  - phase1_math_model: after the custom visual diagram is drawn and the \n"
                "    full math explanation is written. Content should be the complete \n"
                "    step-by-step mathematical description of the system.\n"
                "  - phase2_architecture: after get_xcos_block_data has been called for \n"
                "    every block and the full architecture plan (blocks + links) is written. \n"
                "    Content should list every block name, Xcos function name, parameters, \n"
                "    and every link with source/target port IDs. The submission MUST end with \n"
                "    a fenced JSON manifest containing blocks, links, context_vars, omissions, \n"
                "    and synthetic_blocks_planned. blocks entries may be strings or objects \n"
                "    with one of: name, type, interfaceFunctionName, block_name, xcos_name, \n"
                "    or block.\n"
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
                "PHASE 2 â€” Step 1. Call this for EVERY block before writing any XML. "
                "Never write block XML from memory or from examples in other tool results â€” "
                "always call this first and use the returned XML as the authoritative "
                "template. Returns compact block metadata by default, with optional full "
                "reference XML when requested."
            ),
            inputSchema={"type": "object", "properties": {
                "name": {"type": "string"},
                "include_help": {"type": "boolean", "default": False},
                "include_extra_examples": {"type": "boolean", "default": False},
                "include_reference_xml": {"type": "boolean", "default": False}
            }, "required": ["name"]}
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
                "you have XML content in hand but no active session_id â€” for example, \n"
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
                "PHASE 3 â€” Step 1. Call this to open or resume a draft session after Phase 2 "
                "is approved. Always pass the workflow_id so the draft is linked to the "
                "workflow. You may pass session_id to resume a specific draft. Store the returned session_id â€” it is required for all "
                "subsequent xcos_add_blocks, xcos_add_links, xcos_get_topology_widget, "
                "xcos_get_draft_xml, xcos_verify_draft, and xcos_get_file_path calls.\n"
                "IMPORTANT: To use xcos_commit_phase later, you MUST pass "
                "phases=['phase3_implementation'] here. Omitting the phases array will "
                "cause xcos_commit_phase to fail with 'No phase plan found'."
            ),
            inputSchema={"type": "object", "properties": {
                "schema_version": {"type": "string", "default": "1.1"},
                "workflow_id": {"type": "string"},
                "session_id": {"type": "string"},
                "replace": {"type": "boolean", "default": False},
                "phases": {"type": "array", "items": {"type": "string"}, "description": "Optional list of phase labels to provision."}
            }}
        ),
        mcp_types.Tool(
            name="xcos_set_context",
            description=(
                "Add or replace top-level Xcos context lines for a draft session. Use this "
                "when the workflow requires symbolic constants or named variables in the "
                "diagram context. The provided lines are injected into the top-level "
                "<Array as='context' scilabClass='String[]'> during XML assembly."
            ),
            inputSchema={"type": "object", "properties": {
                "session_id": {"type": "string"},
                "context_lines": {"type": "array", "items": {"type": "string"}},
            }, "required": ["session_id", "context_lines"]},
        ),
        mcp_types.Tool(
            name="xcos_add_blocks",
            description=(
                "PHASE 3 â€” Step 2. Call this to add all blocks to the draft session. "
                "Only use block XML that was retrieved via get_xcos_block_data â€” never "
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
                "PHASE 3 â€” Step 4. Call this to connect all blocks with links after "
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
            name="xcos_start_validation",
            description=(
                "Start asynchronous validation for a draft session. Use this when validation "
                "may exceed stream limits, then poll xcos_get_validation_status until the "
                "job reaches a terminal state. If timeout_seconds is omitted, the server "
                "uses its configured validation-job timeout. validation_profile defaults "
                "to 'full_runtime'; use 'hosted_smoke' for structural plus Scilab import/load "
                "checks without full simulation."
            ),
            inputSchema={"type": "object", "properties": {
                "session_id": {"type": "string"},
                "timeout_seconds": {"type": "number", "default": get_configured_validation_job_timeout_seconds()},
                "validation_profile": {
                    "type": "string",
                    "enum": sorted(VALIDATION_PROFILES),
                    "default": VALIDATION_PROFILE_FULL_RUNTIME,
                },
            }, "required": ["session_id"]},
        ),
        mcp_types.Tool(
            name="xcos_get_validation_status",
            description="Poll the status of an asynchronous validation job created by xcos_start_validation or xcos_verify_draft.",
            inputSchema={"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
        ),
        mcp_types.Tool(
            name="xcos_verify_draft",
            description=(
                "PHASE 3 â€” Step 7. Call this after xcos_get_draft_xml to validate the "
                "diagram. After calling this, always call xcos_get_validation_widget with "
                "the current draft XML and display the result widget to the user. This tool "
                "starts asynchronous validation and may return a running job_id instead of a "
                "final verdict when validation takes too long. validation_profile defaults "
                "to 'full_runtime'; use 'hosted_smoke' for deploy-safe structural plus import validation.\n"
                "  - If success=true: IMMEDIATELY call xcos_commit_phase with "
                "    phase_label='phase3_implementation' and blocks_xml='', then call "
                "    xcos_get_file_path, read the file with xcos_get_file_content, write "
                "    it to your output folder, and present the path to the user. "
                "    Do NOT wait for the user to ask.\n"
                "  - If success=false: read the error carefully, fix the block or link XML, \n"
                "    go back to xcos_add_blocks and rebuild. NEVER stop after one failure â€” \n"
                "    keep iterating until success=true is returned."
            ),
            inputSchema={"type": "object", "properties": {
                "session_id": {"type": "string"},
                "validation_profile": {
                    "type": "string",
                    "enum": sorted(VALIDATION_PROFILES),
                    "default": VALIDATION_PROFILE_FULL_RUNTIME,
                },
            }, "required": ["session_id"]},
        ),

        mcp_types.Tool(
            name="xcos_commit_phase",
            description=(
                "PHASE 3 â€” Step 9. Call this after xcos_verify_draft returns success=true, "
                "with session_id and phase_label='phase3_implementation'.\n"
                "blocks_xml is OPTIONAL â€” pass an empty string '' (the default). Blocks "
                "were already added via xcos_add_blocks; passing blocks_xml again duplicates them.\n"
                "After calling this:\n"
                "  1. Call xcos_submit_phase(phase3_implementation).\n"
                "  2. Call xcos_get_file_path to get the file path.\n"
                "  3. Call xcos_get_file_content(source='session') to read the XML.\n"
                "  4. Write the XML to your output folder using your file tools.\n"
                "  5. IMMEDIATELY present the file path and download link to the user.\n"
                "Do NOT wait for the user to ask â€” presenting the file is MANDATORY."
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
                "PHASE 3 â€” Step 6. Call this with pretty_print=true after xcos_add_links "
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
                "PHASE 3 â€” Step 9. Call this only after xcos_verify_draft has returned "
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
    return [normalize_tool_descriptor(tool) for tool in tools]

@mcp_server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None):
    # Standardize empty arguments to empty dict
    if arguments is None:
        arguments = {}
    
    if name == "xcos_get_status_widget":
        payload = parse_mcp_text_json_response(await xcos_get_status_widget())
        return make_widget_tool_result("Status Widget Generated", payload)
    elif name == "xcos_get_workflow_widget":
        payload = parse_mcp_text_json_response(await xcos_get_workflow_widget(arguments.get("workflow_id")))
        return make_widget_tool_result("Workflow Widget Generated", payload)
    elif name == "xcos_get_validation_widget":
        payload = parse_mcp_text_json_response(await xcos_get_validation_widget(arguments["xml_content"]))
        return make_widget_tool_result("Validation Widget Generated", payload)
    elif name == "xcos_get_block_catalogue_widget":
        payload = parse_mcp_text_json_response(await xcos_get_block_catalogue_widget(arguments.get("category")))
        return make_widget_tool_result("Block Catalogue Widget Generated", payload)
    elif name == "xcos_get_topology_widget":
        payload = parse_mcp_text_json_response(await xcos_get_topology_widget(arguments["session_id"]))
        return make_widget_tool_result("Topology Widget Generated", payload)
    elif name == "xcos_create_workflow":
        payload = parse_mcp_text_json_response(await xcos_create_workflow(arguments["problem_statement"], arguments.get("autopilot", False)))
        workflow = payload["workflow"]
        return make_structured_tool_result(
            f"Created workflow {workflow['workflow_id']}. {workflow['current_phase_label']} is ready.",
            payload,
        )
    elif name == "xcos_list_workflows":
        payload = parse_mcp_text_json_response(await xcos_list_workflows(arguments.get("view", "summary")))
        return make_structured_tool_result(
            f"Found {len(payload['workflows'])} workflow session(s).",
            payload,
        )
    elif name == "xcos_get_workflow":
        payload = parse_mcp_text_json_response(await xcos_get_workflow(arguments["workflow_id"], arguments.get("view", "summary")))
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
        return await get_xcos_block_data(
            arguments["name"],
            arguments.get("include_help", False),
            arguments.get("include_extra_examples", False),
            arguments.get("include_reference_xml", False),
        )
    elif name == "get_xcos_block_source":
        return await get_xcos_block_source(arguments["name"])
    elif name == "search_related_xcos_files":
        return await search_related_xcos_files(arguments["query"])
    elif name == "verify_xcos_xml":
        return await verify_xcos_xml(arguments["xml_content"])
    elif name == "xcos_start_draft":
        payload = parse_mcp_text_json_response(await xcos_start_draft(
            schema_version=arguments.get("schema_version", "1.1"),
            workflow_id=arguments.get("workflow_id"),
            replace=arguments.get("replace", False),
            phases=arguments.get("phases"),
            session_id=arguments.get("session_id"),
        ))
        msg = f"{'Resumed' if payload.get('resumed') else 'Started'} draft session {payload.get('session_id')}."
        if payload.get("phase_plan_registered"):
            msg += f" Registered {payload.get('phase_count')} phases."
        return make_structured_tool_result(msg, payload)
    elif name == "xcos_set_context":
        payload = parse_mcp_text_json_response(await xcos_set_context(arguments["session_id"], arguments["context_lines"]))
        return make_structured_tool_result(
            f"Updated context for session {arguments['session_id']} with {payload.get('context_line_count', 0)} line(s).",
            payload,
        )
    elif name == "xcos_add_blocks":
        payload = parse_mcp_text_json_response(await xcos_add_blocks(arguments["session_id"], arguments["blocks_xml"]))
        return make_structured_tool_result(
            f"Added {payload.get('added_block_count', 0)} block(s) to session {arguments['session_id']}.",
            payload,
        )
    elif name == "xcos_add_links":
        payload = parse_mcp_text_json_response(await xcos_add_links(arguments["session_id"], arguments["links_xml"]))
        return make_structured_tool_result(
            f"Added {payload.get('added_link_count', 0)} link(s) to session {arguments['session_id']}.",
            payload,
        )
    elif name == "xcos_start_validation":
        payload = parse_mcp_text_json_response(await xcos_start_validation(
            arguments["session_id"],
            arguments.get("timeout_seconds"),
            arguments.get("validation_profile", VALIDATION_PROFILE_FULL_RUNTIME),
        ))
        return make_structured_tool_result(
            f"Validation job {payload['job_id']} started for session {arguments['session_id']}.",
            payload,
        )
    elif name == "xcos_get_validation_status":
        payload = parse_mcp_text_json_response(await xcos_get_validation_status(arguments["job_id"]))
        return make_structured_tool_result(
            f"Validation job {arguments['job_id']} is {payload['status']}.",
            payload,
        )
    elif name == "xcos_verify_draft":
        payload = parse_mcp_text_json_response(await xcos_verify_draft(
            arguments["session_id"],
            arguments.get("validation_profile", VALIDATION_PROFILE_FULL_RUNTIME),
        ))
        return make_structured_tool_result(
            (
                f"Validation job {payload.get('job_id')} is {payload.get('status')} for draft session {arguments['session_id']}."
                if payload.get("status") in {"queued", "running"}
                else f"Verification {'succeeded' if payload.get('success') else 'failed'} for draft session {arguments['session_id']}."
            ),
            payload,
        )

    elif name == "xcos_commit_phase":
        payload = parse_mcp_text_json_response(await xcos_commit_phase(arguments["session_id"], arguments["phase_label"], arguments["blocks_xml"]))
        return make_structured_tool_result(
            f"Committed phase {arguments['phase_label']} for session {arguments['session_id']}. File ready at {payload.get('written_to')}.",
            payload,
        )
    elif name == "xcos_get_draft_xml":
        return await xcos_get_draft_xml(
            arguments["session_id"],
            arguments.get("pretty_print", False),
            arguments.get("strip_comments", False),
            arguments.get("validate", False),
        )
    elif name == "xcos_get_file_path":
        payload = parse_mcp_text_json_response(await xcos_get_file_path(arguments["session_id"]))
        return make_structured_tool_result(
            f"Session file for {arguments['session_id']} is ready at {payload.get('session_file_path')}.",
            payload,
        )
    elif name == "xcos_get_file_content":
        return await xcos_get_file_content(
            arguments["session_id"],
            arguments.get("source", "session"),
            arguments.get("encoding", "text"),
        )
    elif name == "xcos_list_sessions":
        payload = parse_mcp_text_json_response(await xcos_list_sessions())
        return make_structured_tool_result(
            f"Found {len(payload.get('sessions', []))} draft session(s).",
            payload,
        )
    elif name == "ping":
        return make_structured_tool_result("Pong", {"status": "ok", "timestamp": now_iso()})

    else:
        return make_error_tool_result(f"Unknown tool: {name}")

async def main():
    ensure_state_dirs()
    hydrate_persistent_state()
    if is_startup_preflight_enabled():
        preflight = await run_startup_preflight()
        state.startup_preflight = preflight
        if preflight.get("status") != "ok":
            print(
                f"[{Fore.YELLOW}PREFLIGHT{Style.RESET_ALL}] Startup preflight reported issues: {preflight.get('errors')}",
                file=sys.stderr,
            )
            tail = preflight.get("startup_output_tail")
            if tail:
                print(
                    f"[{Fore.YELLOW}PREFLIGHT{Style.RESET_ALL}] Scilab output tail:\n{tail}",
                    file=sys.stderr,
                )
            if is_startup_preflight_strict():
                raise RuntimeError("Startup preflight failed and XCOS_PREFLIGHT_STRICT is enabled.")
        else:
            print(
                f"[{Fore.GREEN}PREFLIGHT{Style.RESET_ALL}] Startup preflight passed.",
                file=sys.stderr,
            )
    else:
        state.startup_preflight = {
            "status": "skipped",
            "checked_at": now_iso(),
            "details": "Disabled via XCOS_PREFLIGHT_ENABLED",
        }

    timeouts = get_runtime_timeout_snapshot()
    print(
        f"[{Fore.CYAN}CONFIG{Style.RESET_ALL}] Timeouts: subprocess={timeouts['scilab_subprocess_timeout_seconds']:.0f}s, "
        f"poll={timeouts['poll_validation_timeout_seconds']:.0f}s, jobs={timeouts['validation_job_timeout_seconds']:.0f}s, "
        f"poll_startup={timeouts['poll_worker_startup_timeout_seconds']:.0f}s",
        file=sys.stderr,
    )
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


