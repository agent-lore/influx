"""Unit tests for the inbox ``LithosClient`` task wrappers (Inbox v1 slice 1).

Verifies arg-shaping + JSON decode for ``task_list`` / ``task_claim`` /
``task_update`` and the new ``cited_nodes`` pass-through on
``task_complete`` — mirroring the existing ``task_create`` wrapper
semantics (decode via ``_result_json_dict``).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from influx.lithos_client import LithosClient


def _tool_result(payload: object, *, is_error: bool = False) -> MagicMock:
    text_content = MagicMock()
    text_content.text = payload if isinstance(payload, str) else json.dumps(payload)
    result = MagicMock()
    result.content = [text_content]
    result.isError = is_error
    return result


def _client() -> LithosClient:
    return LithosClient(url="http://localhost:1234/sse")


async def test_task_list_shapes_args_and_decodes() -> None:
    client = _client()
    client.call_tool = AsyncMock(  # type: ignore[method-assign]
        return_value=_tool_result({"tasks": [{"id": "t1"}]})
    )
    body = await client.task_list_body(tags=["influx:inbox"], status="open")
    assert body == {"tasks": [{"id": "t1"}]}
    name, args = client.call_tool.call_args.args
    assert name == "lithos_task_list"
    assert args == {"tags": ["influx:inbox"], "status": "open"}


async def test_task_claim_shapes_args_and_decodes() -> None:
    client = _client()
    client.call_tool = AsyncMock(  # type: ignore[method-assign]
        return_value=_tool_result(
            {"success": True, "expires_at": "2099-01-01T00:00:00Z"}
        )
    )
    body = await client.task_claim_body(
        task_id="t1", agent="influx-inbox", aspect="ingest"
    )
    assert body["success"] is True
    name, args = client.call_tool.call_args.args
    assert name == "lithos_task_claim"
    assert args == {
        "task_id": "t1",
        "aspect": "ingest",
        "agent": "influx-inbox",
        "ttl_minutes": 60,
    }


async def test_task_update_shapes_args_and_decodes() -> None:
    client = _client()
    client.call_tool = AsyncMock(  # type: ignore[method-assign]
        return_value=_tool_result({"success": True, "message": "updated"})
    )
    body = await client.task_update_body(
        task_id="t1", agent="influx-inbox", metadata={"inbox_result": {"x": 1}}
    )
    assert body["success"] is True
    name, args = client.call_tool.call_args.args
    assert name == "lithos_task_update"
    assert args == {
        "task_id": "t1",
        "agent": "influx-inbox",
        "metadata": {"inbox_result": {"x": 1}},
    }


async def test_task_complete_passes_cited_nodes_when_present() -> None:
    client = _client()
    client.call_tool = AsyncMock(  # type: ignore[method-assign]
        return_value=_tool_result({"status": "completed"})
    )
    await client.task_complete_body(
        task_id="t1",
        agent="influx-inbox",
        outcome="ingested into 1 profile(s): alpha",
        cited_nodes=["note-1"],
    )
    name, args = client.call_tool.call_args.args
    assert name == "lithos_task_complete"
    assert args["cited_nodes"] == ["note-1"]
    assert args["outcome"] == "ingested into 1 profile(s): alpha"


async def test_task_complete_omits_cited_nodes_when_none() -> None:
    """Scheduled-run callers (no cited_nodes) are unaffected — key omitted."""
    client = _client()
    client.call_tool = AsyncMock(  # type: ignore[method-assign]
        return_value=_tool_result({"status": "completed"})
    )
    await client.task_complete_body(task_id="t1", agent="influx", outcome="success")
    _name, args = client.call_tool.call_args.args
    assert "cited_nodes" not in args
