"""CopilotKit / AG-UI backend for the harness, with an MCP server wired in.

The browser talks to a CopilotKit Next.js app, which proxies each request to this
endpoint as an AG-UI ``RunAgentInput``. ``harness.agui_stream`` resolves the
request's ``threadId`` to a persistent conversation and streams AG-UI events back.

The demo ``sales`` MCP server (``mcp_server.py``) is attached per request — see
``_sales_mcp`` for why a fresh instance per request is the right pattern.

Run (with the agui extra installed and OPENAI_API_KEY in your environment/.env):
    uv sync --prerelease=allow --extra agui
    uv run uvicorn examples.copilotkit.backend.server:app --reload --port 8000
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from ag_ui.encoder import EventEncoder
from agent_framework import MCPStdioTool
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from harness import Harness, HarnessConfig

app = FastAPI()

# Put session/sandbox output in a temp dir, NOT the default ./.harness under the repo.
# Why: the agent's run_python writes scripts into the session dir; with `uvicorn --reload`
# watching the repo, those writes would hot-reload the server mid-conversation, wiping the
# in-memory SessionManager. The next message (same threadId) would then rebuild a fresh
# conversation, and the replayed history — which contains a backend tool *call* but not its
# output — gets rejected by the model ("No tool output found for function call").
_SESSIONS = Path(tempfile.gettempdir()) / "harness-copilotkit-sessions"
harness = Harness(HarnessConfig(root_dir=_SESSIONS))  # default OpenAI client (needs OPENAI_API_KEY)

_MCP_SERVER = Path(__file__).with_name("mcp_server.py")


def _sales_mcp() -> MCPStdioTool:
    """A fresh MCP client per request.

    ``agui_stream`` opens one persistent ``Conversation`` per ``threadId``, and that
    conversation connects the MCP server and owns its lifecycle (closing it on
    teardown). A new thread's first message connects this instance; reused threads
    return the existing conversation and ignore the (still-unconnected) fresh object,
    which is simply garbage-collected. Sharing one connected instance across threads
    would let the first thread to finish close the server out from under the others.
    """
    return MCPStdioTool(name="sales", command=sys.executable, args=[str(_MCP_SERVER)])


def _tool_call_ids(m: dict) -> list[str]:
    return [tc.get("id") for tc in (m.get("toolCalls") or m.get("tool_calls") or []) if tc.get("id")]


def _result_id(m: dict) -> str | None:
    return m.get("toolCallId") or m.get("tool_call_id")


def _heal_history(input_data: dict) -> None:
    """Drop orphan tool calls from the replayed history so OpenAI doesn't 400.

    CopilotKit replays the conversation each turn but does NOT round-trip the *results* of
    backend (server-side) tool calls — so on turn 2 the assistant's turn-1 `function_call`
    arrives with no matching `function_call_output`, and OpenAI rejects it ("No tool output
    found for function call …"). We make the history self-consistent: keep only tool calls
    that have a matching tool-result message, and drop any orphan tool-result messages. The
    assistant's final text answer is preserved, so the model keeps the conversation context
    and simply re-calls the tool on this turn if it needs fresh data.
    """
    msgs = input_data.get("messages") or []
    result_ids = {_result_id(m) for m in msgs if m.get("role") == "tool"}

    healed: list[dict] = []
    dropped_calls: list[str] = []
    for m in msgs:
        if m.get("role") == "assistant" and _tool_call_ids(m):
            keep = [tc for tc in (m.get("toolCalls") or m.get("tool_calls"))
                    if tc.get("id") in result_ids]
            dropped_calls += [i for i in _tool_call_ids(m) if i not in result_ids]
            if keep:
                m = {**m}
                m["toolCalls" if "toolCalls" in m else "tool_calls"] = keep
            elif m.get("content"):
                m = {k: v for k, v in m.items() if k not in ("toolCalls", "tool_calls")}
            else:
                continue  # an assistant message that was *only* orphan tool calls
        healed.append(m)

    kept_ids = {i for m in healed if m.get("role") == "assistant" for i in _tool_call_ids(m)}
    final = [m for m in healed if not (m.get("role") == "tool" and _result_id(m) not in kept_ids)]
    dropped_results = [_result_id(m) for m in healed
                       if m.get("role") == "tool" and _result_id(m) not in kept_ids]

    input_data["messages"] = final
    print(f"[agui] messages in={len(msgs)} out={len(final)} "
          f"dropped_orphan_calls={dropped_calls} dropped_orphan_results={dropped_results}")


@app.post("/agent")
async def agent(request: Request) -> StreamingResponse:
    input_data = await request.json()      # AG-UI RunAgentInput (messages, threadId, runId, tools, state)
    _heal_history(input_data)               # make the replayed history self-consistent (see above)
    encoder = EventEncoder()

    async def sse():
        async for event in harness.agui_stream(input_data, tools=[_sales_mcp()]):
            yield encoder.encode(event)    # -> "data: {json}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream")
