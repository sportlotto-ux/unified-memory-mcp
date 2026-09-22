"""Пункт 0 v0.4: OpenAI-протокол backend (локальный potion-сервер Hermes)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from unified_memory.config import Config
from unified_memory.embeddings import (
    EmbedServerError,
    FastembedBackend,
    OpenAIBackend,
    make_backend,
)

DIM = 6


class Handler(BaseHTTPRequestHandler):
    mode = "ok"  # ok | shuffled | http500 | garbage | ragged
    last_payload = None

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        Handler.last_payload = json.loads(self.rfile.read(n) or b"{}")
        if Handler.mode == "http500":
            self._send({"error": "boom"}, code=500)
            return
        if Handler.mode == "garbage":
            raw = b"not json{"
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        inputs = Handler.last_payload.get("input", [])
        data = [{"object": "embedding", "index": i,
                 "embedding": [float(i + 1)] * (DIM + (1 if Handler.mode == "ragged" and i else 0))}
                for i in range(len(inputs))]
        if Handler.mode == "shuffled":
            data = data[::-1]
        self._send({"object": "list", "model": "x", "data": data})


@pytest.fixture
def fake_server():
    Handler.mode = "ok"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_model_passthrough_and_dim(fake_server):
    b = OpenAIBackend(model="potion-test", base_url=fake_server, timeout=5)
    assert b.dim == DIM  # авто-детект пробой
    assert Handler.last_payload["model"] == "potion-test"


def test_order_preserved_when_shuffled(fake_server):
    Handler.mode = "shuffled"
    b = OpenAIBackend(model="m", base_url=fake_server, timeout=5, dim=DIM)
    vecs = b.embed_docs(["a", "b", "c"])
    assert [v[0] for v in vecs] == [1.0, 2.0, 3.0]


def test_unreachable_is_loud():
    b = OpenAIBackend(model="m", base_url="http://127.0.0.1:1", timeout=1)
    with pytest.raises(EmbedServerError, match="unreachable"):
        b.embed_query("x")


def test_http500_is_loud(fake_server):
    Handler.mode = "http500"
    b = OpenAIBackend(model="m", base_url=fake_server, timeout=5, dim=DIM)
    with pytest.raises(EmbedServerError, match="HTTP 500"):
        b.embed_query("x")


def test_garbage_is_loud(fake_server):
    Handler.mode = "garbage"
    b = OpenAIBackend(model="m", base_url=fake_server, timeout=5, dim=DIM)
    with pytest.raises(EmbedServerError, match="bad /v1/embeddings"):
        b.embed_query("x")


def test_ragged_is_loud(fake_server):
    Handler.mode = "ragged"
    b = OpenAIBackend(model="m", base_url=fake_server, timeout=5, dim=DIM)
    with pytest.raises(EmbedServerError, match="ragged"):
        b.embed_docs(["a", "b"])


def test_dim_override_skips_probe():
    b = OpenAIBackend(model="m", base_url="http://127.0.0.1:1", timeout=1, dim=8)
    assert b.dim == 8  # сети не касались — иначе был бы EmbedServerError


def test_make_backend_selection(tmp_path, monkeypatch):
    from unified_memory import config as cfgmod
    monkeypatch.setenv("UM_EMBEDDING_BACKEND", "openai")
    monkeypatch.setenv("UM_EMBEDDING_BASE_URL", "http://127.0.0.1:9")
    c = Config(db_path=tmp_path / "d.db")
    assert isinstance(make_backend(c), OpenAIBackend)
    monkeypatch.setenv("UM_EMBEDDING_BACKEND", "local")
    assert isinstance(make_backend(Config(db_path=tmp_path / "d.db")), FastembedBackend)
    assert cfgmod  # silence linters


def test_bad_backend_rejected(tmp_path):
    with pytest.raises(ValueError, match="UM_EMBEDDING_BACKEND"):
        Config(db_path=tmp_path / "d.db", embedding_backend="grpc")


def test_empty_base_url_rejected(tmp_path):
    with pytest.raises(ValueError, match="UM_EMBEDDING_BASE_URL"):
        Config(db_path=tmp_path / "d.db", embedding_backend="openai", embedding_base_url="")


LIVE = pytest.mark.skipif(not __import__("os").environ.get("UM_LIVE_OPENAI"),
                          reason="UM_LIVE_OPENAI=1 for the real 8127 potion server")


@LIVE
def test_live_potion_dim():
    import os
    url = os.environ.get("UM_EMBEDDING_BASE_URL", "http://127.0.0.1:8127")
    b = OpenAIBackend(model="potion-multilingual-128M-pruned-int8-ruen",
                      base_url=url, timeout=30)
    assert b.dim == 256
    v = b.embed_query("проверка связи")
    assert len(v) == 256 and any(x != 0.0 for x in v)


@LIVE
def test_live_ingest_roundtrip(tmp_path):
    import os
    from unified_memory.ingest import Ingest
    from unified_memory.store import Store
    from unified_memory.summarize import ExtractiveSummarizer
    url = os.environ.get("UM_EMBEDDING_BASE_URL", "http://127.0.0.1:8127")
    cfg = Config(db_path=tmp_path / "live.db", context_tokens=10**9,
                 embedding_backend="openai", embedding_base_url=url,
                 embedding_model="potion-multilingual-128M-pruned-int8-ruen")
    backend = make_backend(cfg)
    backend.warm()
    store = Store(cfg, embedding_dim=backend.dim, embedding_model=cfg.embedding_model)
    try:
        ing = Ingest(store, backend, ExtractiveSummarizer(), cfg)
        ing.remember_message("s", "user", "кот сидит на коврике")
        hits = ing.router().recall("где сидит кот")
        assert hits and "коврик" in hits[0].body
    finally:
        store.close()
