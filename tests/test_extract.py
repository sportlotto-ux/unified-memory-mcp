"""P2.6: mem_extract — endpoint preview триплетов, без автозаписи (red-first).

Без UM_SUMMARIZER_URL/MODEL — явный отказ, ноль сети. Сеть в тестах только
через стаб urllib.request.urlopen (прецедент test_audit2).
"""

import json

import pytest

from unified_memory.summarize import EndpointExtractor


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "server.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("UM_SUMMARIZER_URL", raising=False)
    monkeypatch.delenv("UM_SUMMARIZER_MODEL", raising=False)
    import unified_memory.server as module
    monkeypatch.setattr(module, "_backend", lambda cfg: None)
    module._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    yield module
    if module._STATE.get("store") is not None:
        module._STATE["store"].close()
    module._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)


def _resp(payload: bytes):
    class FakeResp:
        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return FakeResp()


def _choices(candidates) -> bytes:
    return json.dumps(
        {"choices": [{"message": {"content": json.dumps(candidates)}}]}
    ).encode()


def _fid(srv):
    return json.loads(srv.mem_fact("long", "capital", "Paris is capital"))["id"]


def test_no_endpoint_no_network(srv, monkeypatch):
    fid = _fid(srv)

    def _boom(req, timeout):
        raise AssertionError("network must not happen")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    with pytest.raises(ValueError):
        srv.mem_extract(f"fact:{fid}")


def test_stubbed_endpoint_returns_candidates(srv, monkeypatch):
    fid = _fid(srv)
    monkeypatch.setenv("UM_SUMMARIZER_URL", "http://x/v1")
    monkeypatch.setenv("UM_SUMMARIZER_MODEL", "m")
    want = [{"subject": "Paris", "predicate": "is_capital_of",
             "object": "France"}]
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout: _resp(_choices(want)))
    out = json.loads(srv.mem_extract(f"fact:{fid}"))
    assert out["candidates"] == want
    assert out["count"] == 1 and out["model"] == "m"


def test_garbage_from_endpoint_is_loud(srv, monkeypatch):
    fid = _fid(srv)
    monkeypatch.setenv("UM_SUMMARIZER_URL", "http://x/v1")
    monkeypatch.setenv("UM_SUMMARIZER_MODEL", "m")
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout: _resp(b"not json at all {{{"))
    with pytest.raises(ValueError):
        srv.mem_extract(f"fact:{fid}")


def test_bad_and_foreign_targets_fail_before_network(srv, monkeypatch):
    fid = json.loads(
        srv.mem_fact("long", "capital", "Paris", owner="alice"))["id"]
    monkeypatch.setenv("UM_SUMMARIZER_URL", "http://x/v1")
    monkeypatch.setenv("UM_SUMMARIZER_MODEL", "m")

    def _boom(req, timeout):
        raise AssertionError("network must not happen")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    with pytest.raises(ValueError):
        srv.mem_extract("bogus:1")
    with pytest.raises(ValueError):
        srv.mem_extract("fact:999999")
    with pytest.raises(ValueError):
        srv.mem_extract(f"fact:{fid}", owner="bob")


def test_caps_on_count_and_fields(monkeypatch):
    ext = EndpointExtractor(url="http://x/v1", model="m")
    big = [{"subject": "s" * 300, "predicate": "p", "object": "o"}
           for _ in range(30)]
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout: _resp(_choices(big)))
    out = ext.extract(["some text"])
    assert len(out) == 20
    assert all(len(c["subject"]) <= 200 for c in out)
    assert set(out[0]) == {"subject", "predicate", "object"}


def test_extract_writes_nothing(srv, monkeypatch):
    fid = _fid(srv)
    monkeypatch.setenv("UM_SUMMARIZER_URL", "http://x/v1")
    monkeypatch.setenv("UM_SUMMARIZER_MODEL", "m")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout: _resp(_choices(
            [{"subject": "Paris", "predicate": "is", "object": "capital"}])))
    store = srv._store()
    before = {t: store.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("um_facts", "um_edges", "um_links", "um_vectors",
                        "um_annotations")}
    srv.mem_extract(f"fact:{fid}")
    after = {t: store.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
             for t in before}
    assert before == after
