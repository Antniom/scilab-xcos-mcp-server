import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client


DEFAULT_MCP_URL = "https://notsn-scilab-xcos-mcp-server.hf.space/mcp"
DEFAULT_FIXTURE = "pendulo_simples_fiel_raw.xcos"
DEFAULT_SMOKE_CLK_PERIOD = "1000"
DEFAULT_WORKFLOW_STATEMENT = (
    "Modelacao do pendulo simples. Dados: g = 10 m/s^2, L = 2 m. "
    "Condicoes iniciais: theta(0)=0 rad, theta_dot(0)=1 rad/s. "
    "Modelo: theta_ddot + (g/L) sin(theta) = 0. Preserve the diagram blocks shown "
    "in the reference: CONST_m, GAIN_f, SINBLK_f, COSBLK_f, INTEGRAL_f, MUX, "
    "BARXY, CANIMXY, CLOCK_c, CMSCOPE."
)
DEFAULT_PHASE1_CONTENT = (
    "State variables:\n"
    "- theta_dot = omega\n"
    "- omega_dot = -(g/L) * sin(theta)\n"
    "- x = L * sin(theta)\n"
    "- y = -L * cos(theta)\n"
    "Initial conditions:\n"
    "- theta(0)=0\n"
    "- omega(0)=1\n"
    "The implementation preserves the requested visible blocks CONST_m, GAIN_f, "
    "SINBLK_f, COSBLK_f, INTEGRAL_f, MUX, BARXY, CANIMXY, CLOCK_c, and CMSCOPE, "
    "while allowing extra routing and summing blocks required for a valid executable "
    "Xcos topology."
)
DEFAULT_PHASE2_CONTENT = (
    "Architecture plan\n"
    "- Continuous dynamics: SIN(theta) -> GAIN(-(g/L)) -> summing node -> "
    "INTEGRAL_f(omega) -> INTEGRAL_f(theta).\n"
    "- Visualization: theta and omega to CMSCOPE; L*sin(theta) and -L*cos(theta) "
    "to BARXY and CANIMXY with CLOCK_c event refresh.\n"
    "- Practical implementation note: the executable diagram uses extra "
    "routing/summing helpers beyond the reference image, including explicit split "
    "blocks for fan-out and one summation block.\n"
    "```json\n"
    "{\"blocks\":[\"CONST_m\",\"GAIN_f\",\"GAIN_f\",\"GAIN_f\",\"SINBLK_f\","
    "\"COSBLK_f\",\"INTEGRAL_f\",\"INTEGRAL_f\",\"MUX\",\"MUX\",\"BARXY\","
    "\"CANIMXY\",\"CMSCOPE\",\"CLOCK_c\",\"BIGSOM_f\",\"SPLIT_f\","
    "\"CLKSPLIT_f\"],\"links\":[\"explicit signal links for theta/omega/sin/cos/"
    "position routing\",\"command-control links from CLOCK_c through event splitters "
    "to CANIMXY, BARXY, CMSCOPE\"],\"context_vars\":[\"g\",\"L\"],"
    "\"omissions\":[],\"synthetic_blocks_planned\":[\"SPLIT_f for fan-out "
    "normalization\",\"CLKSPLIT_f for clock event fan-out\"]}\n"
    "```"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a remote Hugging Face MCP smoke test.")
    parser.add_argument("--mcp-url", default=DEFAULT_MCP_URL)
    parser.add_argument("--fixture-path", default=DEFAULT_FIXTURE)
    parser.add_argument("--verify-timeout-seconds", type=float, default=360.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=2.0)
    parser.add_argument("--allow-degraded-runtime", action="store_true")
    return parser.parse_args()


def parse_tool_payload(result: Any) -> dict[str, Any]:
    if getattr(result, "isError", False):
        raise RuntimeError(extract_text(result) or "MCP tool call failed")
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if not isinstance(text, str):
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    raise RuntimeError(f"Unable to parse MCP tool payload: {result}")


def extract_text(result: Any) -> str:
    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts)


def load_fixture(fixture_path: Path) -> tuple[list[str], list[str], list[str]]:
    root = ET.parse(fixture_path).getroot()
    context_parent = root.find("Array[@as='context']")
    if context_parent is None:
        raise RuntimeError("Fixture is missing top-level context array")
    context_lines = [child.attrib.get("value", "").strip() for child in context_parent if child.attrib.get("value", "").strip()]

    graph_model = root.find("mxGraphModel")
    if graph_model is None:
        raise RuntimeError("Fixture is missing mxGraphModel")
    graph_root = graph_model.find("root")
    if graph_root is None:
        raise RuntimeError("Fixture is missing mxGraphModel/root")

    blocks: list[str] = []
    links: list[str] = []
    for child in graph_root:
        if child.tag == "mxCell":
            continue
        serialized = ET.tostring(child, encoding="unicode")
        if child.tag in {"ExplicitLink", "CommandControlLink"}:
            links.append(serialized)
        else:
            blocks.append(serialized)
    return context_lines, blocks, links


def normalize_smoke_context(context_lines: list[str]) -> list[str]:
    adjusted: list[str] = []
    saw_clk_period = False
    for line in context_lines:
        if line.startswith("clk_period="):
            adjusted.append(f"clk_period={DEFAULT_SMOKE_CLK_PERIOD}")
            saw_clk_period = True
        else:
            adjusted.append(line)
    if not saw_clk_period:
        adjusted.append(f"clk_period={DEFAULT_SMOKE_CLK_PERIOD}")
    return adjusted


def chunk_xml(parts: list[str], max_chars: int = 18000) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for part in parts:
        part_len = len(part) + 1
        if current and current_len + part_len > max_chars:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(part)
        current_len += part_len
    if current:
        chunks.append("\n".join(current))
    return chunks


async def call_tool_json(session: ClientSession, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await session.call_tool(name, arguments or {})
    return parse_tool_payload(result)


async def wait_for_validation(
    session: ClientSession,
    session_id: str,
    verify_timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    payload = await call_tool_json(session, "xcos_verify_draft", {"session_id": session_id})
    if payload.get("status") not in {"running", "queued"}:
        return payload

    job_id = payload.get("job_id")
    if not job_id:
        raise RuntimeError(f"Validation started without job_id: {payload}")

    deadline = asyncio.get_running_loop().time() + verify_timeout_seconds
    while True:
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"Validation job {job_id} timed out after {verify_timeout_seconds} seconds")
        await asyncio.sleep(poll_interval_seconds)
        status = await call_tool_json(session, "xcos_get_validation_status", {"job_id": job_id})
        if status.get("status") in {"running", "queued"}:
            continue
        return status


def is_degraded_runtime_timeout(validation: dict[str, Any], allow_degraded_runtime: bool) -> bool:
    if not allow_degraded_runtime:
        return False
    if validation.get("code") != "SCILAB_RUNTIME_TIMEOUT":
        return False
    debug = validation.get("debug") or {}
    structural = debug.get("structural_check") or {}
    return bool(structural.get("success"))


def ensure_validation_succeeded(validation: dict[str, Any], allow_degraded_runtime: bool) -> bool:
    degraded_runtime_timeout = is_degraded_runtime_timeout(validation, allow_degraded_runtime)
    if not validation.get("success") and not degraded_runtime_timeout:
        raise RuntimeError(json.dumps(validation, indent=2))
    return degraded_runtime_timeout


async def run_smoke_test(args: argparse.Namespace) -> dict[str, Any]:
    fixture_path = Path(args.fixture_path)
    if not fixture_path.is_absolute():
        fixture_path = Path.cwd() / fixture_path
    if not fixture_path.exists():
        raise FileNotFoundError(f"Fixture not found: {fixture_path}")

    context_lines, blocks, links = load_fixture(fixture_path)
    context_lines = normalize_smoke_context(context_lines)
    block_chunks = chunk_xml(blocks)
    link_chunks = chunk_xml(links)

    http_client = create_mcp_http_client()
    async with streamable_http_client(args.mcp_url, http_client=http_client) as (read_stream, write_stream, _):
        session = ClientSession(read_stream, write_stream)
        async with session:
            await session.initialize()

            ping = await call_tool_json(session, "ping")
            workflow = await call_tool_json(
                session,
                "xcos_create_workflow",
                {"problem_statement": DEFAULT_WORKFLOW_STATEMENT},
            )
            workflow_id = workflow["workflow_id"]

            await call_tool_json(
                session,
                "xcos_submit_phase",
                {
                    "workflow_id": workflow_id,
                    "phase": "phase1_math_model",
                    "content": DEFAULT_PHASE1_CONTENT,
                    "artifact_type": "markdown",
                },
            )
            await call_tool_json(
                session,
                "xcos_review_phase",
                {"workflow_id": workflow_id, "phase": "phase1_math_model", "decision": "approve"},
            )
            await call_tool_json(
                session,
                "xcos_submit_phase",
                {
                    "workflow_id": workflow_id,
                    "phase": "phase2_architecture",
                    "content": DEFAULT_PHASE2_CONTENT,
                    "artifact_type": "markdown",
                },
            )
            await call_tool_json(
                session,
                "xcos_review_phase",
                {"workflow_id": workflow_id, "phase": "phase2_architecture", "decision": "approve"},
            )

            draft = await call_tool_json(
                session,
                "xcos_start_draft",
                {"workflow_id": workflow_id, "phases": ["phase3_implementation"]},
            )
            session_id = draft["session_id"]

            await call_tool_json(
                session,
                "xcos_set_context",
                {"session_id": session_id, "context_lines": context_lines},
            )

            for index, chunk in enumerate(block_chunks, start=1):
                await call_tool_json(
                    session,
                    "xcos_add_blocks",
                    {"session_id": session_id, "blocks_xml": chunk},
                )

            for index, chunk in enumerate(link_chunks, start=1):
                await call_tool_json(
                    session,
                    "xcos_add_links",
                    {"session_id": session_id, "links_xml": chunk},
                )

            validation = await wait_for_validation(
                session,
                session_id,
                args.verify_timeout_seconds,
                args.poll_interval_seconds,
            )
            degraded_runtime_timeout = ensure_validation_succeeded(validation, args.allow_degraded_runtime)

            file_info = await call_tool_json(session, "xcos_get_file_path", {"session_id": session_id})
            file_content = await call_tool_json(
                session,
                "xcos_get_file_content",
                {"session_id": session_id, "source": "session", "encoding": "text"},
            )
            commit = None
            if validation.get("success"):
                commit = await call_tool_json(
                    session,
                    "xcos_commit_phase",
                    {"session_id": session_id, "phase_label": "phase3_implementation", "blocks_xml": ""},
                )

            return {
                "ping": ping,
                "workflow_id": workflow_id,
                "session_id": session_id,
                "block_chunk_count": len(block_chunks),
                "link_chunk_count": len(link_chunks),
                "degraded_runtime_timeout": degraded_runtime_timeout,
                "validation": validation,
                "commit": commit,
                "file_info": file_info,
                "file_size_bytes": file_content.get("size_bytes"),
            }


def main() -> int:
    args = parse_args()
    try:
        summary = asyncio.run(run_smoke_test(args))
    except Exception as exc:
        print(f"REMOTE_SMOKE_TEST_FAILED: {exc!r}", file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
