"""v0.7.3 C11: поведенческие хинты у всех 26 тулов (mcp ToolAnnotations)."""

import asyncio

import unified_memory.server as srv

# name → (read_only, destructive, idempotent)
_EXPECTED = {
    "mem_remember": (False, False, False),
    "mem_fact": (False, False, False),
    "mem_annotate": (False, False, True),
    "mem_link": (False, False, True),
    "mem_graph_query": (True, False, True),
    "mem_recall": (True, False, True),
    "mem_expand": (True, False, True),
    "mem_get": (True, False, True),
    "mem_inspect": (True, False, True),
    "mem_load_session": (True, False, True),
    "mem_update": (False, False, False),
    "mem_compact": (False, False, True),
    "mem_assemble": (True, False, True),
    "mem_forget": (False, True, True),
    "mem_reindex": (False, False, True),
    "mem_recent": (True, False, True),
    "mem_evidence": (True, False, True),
    "mem_validate": (True, False, True),
    "mem_task": (False, False, False),
    "mem_persona": (False, False, False),
    "mem_extract": (True, False, False),
    "mem_bank_share": (False, False, True),
    "mem_bank_unshare": (False, False, True),
    "mem_batch": (False, True, False),
    "mem_status": (True, False, True),
    "mem_doctor": (False, True, False),
}


def _tools():
    return {t.name: t.annotations for t in asyncio.run(srv.mcp.list_tools())}


def test_all_tools_annotated():
    ann = _tools()
    assert set(ann) == set(_EXPECTED)
    for name, a in ann.items():
        assert a is not None, name
        assert a.open_world_hint is False, name  # локальный стор


def test_hint_values_match_table():
    ann = _tools()
    for name, (ro, destr, idem) in _EXPECTED.items():
        a = ann[name]
        assert (a.read_only_hint, a.destructive_hint, a.idempotent_hint) == \
            (ro, destr, idem), name
