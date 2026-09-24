"""v0.8 C12: in-process smoke — 15 тулов + recall round-trip через in-memory MCP.

Транспорт `mcp.client._memory.InMemoryTransport` сверен с установленным mcp
(поднимает низкоуровневый Server в фоновой задаче; MCPServer разворачивается сам).
Без сети и подпроцессов; backend → None (FTS-only).
"""

import asyncio
import json

import pytest

TOOLS = {
    "mem_remember", "mem_fact", "mem_annotate", "mem_link", "mem_graph_query",
    "mem_recall", "mem_expand",
    "mem_update", "mem_compact", "mem_assemble", "mem_forget", "mem_reindex",
    "mem_recent", "mem_evidence", "mem_validate", "mem_batch", "mem_get", "mem_inspect",
    "mem_load_session", "mem_status", "mem_doctor",
}


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "smoke.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import unified_memory.server as m
    monkeypatch.setattr(m, "_backend", lambda c: None)
    monkeypatch.setattr(m, "_maybe_maintenance", lambda *a, **k: None)
    m._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    yield m
    if m._STATE.get("store") is not None:
        m._STATE["store"].close()
    m._STATE.update(ingest=None, store=None, cfg=None)


def test_inmemory_smoke(srv):
    from mcp import ClientSession
    from mcp.client._memory import InMemoryTransport

    async def run():
        async with InMemoryTransport(srv.mcp) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = {t.name for t in (await client.list_tools()).tools}
                assert tools == TOOLS

                made = await client.call_tool(
                    "mem_fact", {"category": "smoke", "name": "ping",
                                 "body": "дым-тест прошёл"})
                fid = json.loads(made.content[0].text)["id"]
                assert isinstance(fid, int)

                graph = json.loads((await client.call_tool(
                    "mem_graph_query", {"subject": "missing"}
                )).content[0].text)
                assert graph["edges"] == [] and graph["links"] == []

                detail = json.loads((await client.call_tool(
                    "mem_get", {"kind": "fact", "id": fid}
                )).content[0].text)
                assert detail["found"] and "дым-тест" in detail["body"]
                made_message = await client.call_tool(
                    "mem_remember", {"session_id": "smoke", "role": "user",
                                     "content": "сообщение для inspect"}
                )
                mid = json.loads(made_message.content[0].text)["id"]
                message_detail = json.loads((await client.call_tool(
                    "mem_get", {"kind": "message", "id": mid}
                )).content[0].text)
                assert message_detail["metadata"]["session_id"] == "smoke"
                inspected = json.loads((await client.call_tool(
                    "mem_inspect", {"session_id": "smoke", "message_id": mid}
                )).content[0].text)
                assert "store" in inspected and "archive" in inspected
                assert inspected["session"]["pressure"]["messages"] == 1
                assert inspected["message"]["metadata"]["session_id"] == "smoke"
                transcript = json.loads((await client.call_tool(
                    "mem_load_session", {"session_id": "smoke", "limit": 10}
                )).content[0].text)
                assert transcript["items"][0]["body"] == "сообщение для inspect"

                rec = await client.call_tool(
                    "mem_recall", {"query": "дым-тест", "scope": "facts"})
                hits = json.loads(rec.content[0].text)      # JSON-парс круг замкнул
                assert any("дым-тест" in h["body"] for h in hits)

                st = json.loads((await client.call_tool("mem_status", {})).content[0].text)
                assert st["um_facts"] >= 1

    asyncio.run(run())
