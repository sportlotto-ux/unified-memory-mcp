#!/usr/bin/env python3
"""Захват сессий OpenCode в unified-memory (этап 1).

Читает сессии и сообщения через HTTP API OpenCode (Basic auth) и пишет в память
тем же путём, что MCP (`Ingest`), с исходным штампом времени и id сообщения в
`source_ref` — поэтому повторный прогон идемпотентен даже при потере состояния.

Что берём:
  * `user`      — текст запроса;
  * `assistant` — только части `type=text` (reasoning и вызовы инструментов
                  пропускаем: это не знание, а шум).

Что не берём никогда:
  * `synthetic` — инъекция инструкций (AGENTS.md и прочее): иначе память
                  забьётся текстом инструкций;
  * `system`, `compaction`, `idle` — служебные записи;
  * пустые сообщения.

Границы заданы явно, а не «как получится»: длина одного сообщения (CAP_CHARS,
обрезка с маркером), сообщений на сессию (CAP_PER_SESSION), сессий за прогон
(CAP_SESSIONS), страниц сообщений (CAP_PAGES).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CAP_CHARS = 4000
CAP_PER_SESSION = 200
CAP_SESSIONS = 50
CAP_PAGES = 3
TRUNC_MARK = "\n[…обрезано при захвате]"

HERE = Path(__file__).resolve().parent
# Состояние и отчёт — runtime-артефакты: держим их в каталоге памяти, а не в
# дереве репозитория (репозиторий версионируется и пушится на origin).
STATE_PATH = Path(os.environ.get(
    "UM_CAPTURE_STATE",
    str(Path.home() / ".config/opencode/memory/capture-state.json")))
REPORT_PATH = Path(os.environ.get(
    "UM_CAPTURE_REPORT",
    str(Path.home() / ".config/opencode/memory/capture-report.json")))
SERVICE_JSON = Path("/home/user/.config/opencode/service.json")
API_URL = os.environ.get("OPENCODE_API_URL", "http://127.0.0.1:49374")
USERNAME = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")

SKIP_TYPES = {"synthetic", "system", "compaction", "idle"}


# ---------- чистые функции: их и проверяем тестами ----------

def extract_text(message: dict) -> str:
    """Текст сообщения, пригодный для памяти. Пустая строка = не берём."""
    mtype = message.get("type")
    if mtype in SKIP_TYPES:
        return ""
    if mtype == "user":
        return (message.get("text") or "").strip()
    if mtype == "assistant":
        parts = []
        for part in message.get("content") or []:
            if part.get("type") == "text":
                text = (part.get("text") or "").strip()
                if text:
                    parts.append(text)
        return "\n\n".join(parts)
    return ""


def clip(text: str, cap: int = CAP_CHARS) -> str:
    if len(text) <= cap:
        return text
    return text[:cap].rstrip() + TRUNC_MARK


def created_ms(message: dict) -> int:
    return int((message.get("time") or {}).get("created") or 0)


def to_messages(raw: list[dict], *, since_ms: int = 0,
                known: set[str] | None = None,
                cap: int = CAP_PER_SESSION) -> list[dict]:
    """Отфильтровать страницу API (новейшие первыми) в хронологический список.

    Возвращает элементы {ref, role, content, created_ms, order} — готовые к записи.
    """
    known = known or set()
    picked = []
    for msg in raw:
        ref = str(msg.get("id") or "")
        if not ref or ref in known:
            continue
        stamp = created_ms(msg)
        if stamp <= since_ms:
            continue
        text = extract_text(msg)
        if not text:
            continue
        picked.append({
            "ref": ref,
            "role": "user" if msg.get("type") == "user" else "assistant",
            "content": clip(text),
            "created_ms": stamp,
        })
    picked.sort(key=lambda m: (m["created_ms"], m["ref"]))
    picked = picked[-cap:]
    for i, item in enumerate(picked):
        item["order"] = i
    return picked


# ---------- доступ к API ----------

def _auth() -> str:
    password = ""
    if SERVICE_JSON.exists():
        try:
            password = json.loads(SERVICE_JSON.read_text()).get("password", "")
        except (OSError, ValueError):
            password = ""
    password = os.environ.get("OPENCODE_SERVER_PASSWORD", password)
    raw = f"{USERNAME}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def api_get(path: str, auth: str, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(API_URL.rstrip("/") + path,
                                headers={"authorization": auth})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def list_sessions(auth: str, limit: int = CAP_SESSIONS) -> list[dict]:
    payload = api_get("/api/session", auth)
    rows = payload.get("data") or []
    return rows[:limit]


def session_messages(auth: str, session_id: str,
                     pages: int = CAP_PAGES) -> list[dict]:
    """Сообщения сессии, новейшие первыми; при нехватке — следующая страница."""
    collected: list[dict] = []
    cursor = ""
    for _ in range(pages):
        path = f"/api/session/{session_id}/message"
        if cursor:
            path += f"?cursor={urllib.parse.quote(cursor)}"
        payload = api_get(path, auth)
        rows = payload.get("data") or []
        collected.extend(rows)
        cursor = ((payload.get("cursor") or {}).get("next") or "")
        if not rows or not cursor:
            break
    return collected


# ---------- захват ----------

def load_state(path: Path) -> dict:
    if not path.exists():
        return {"sessions": {}}
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"sessions": {}}
    state.setdefault("sessions", {})
    return state


def known_refs(store, session_id: str) -> set[str]:
    rows = store.select(
        "SELECT source_ref FROM um_messages WHERE session_id=? AND source_ref<>''",
        (session_id,))
    return {row[0] for row in rows}


def run(dry_run: bool = False, limit_sessions: int = CAP_SESSIONS,
        since: float = 0.0, quiet: bool = False,
        state_path: Path | None = None) -> dict:
    auth = _auth()
    state_path = state_path or STATE_PATH
    state = load_state(state_path)
    per_session = state["sessions"]

    cfg = None
    store = ingest = None
    if not dry_run:
        sys.path.insert(0, os.environ["UM_SRC"])
        from unified_memory.config import Config
        from unified_memory.embeddings import make_backend
        from unified_memory.ingest import Ingest
        from unified_memory.store import Store

        cfg = Config()
        store = Store(cfg)
        ingest = Ingest(store, make_backend(cfg), cfg=cfg)

    report = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "dry_run": dry_run,
        "sessions_seen": 0,
        "sessions_with_new": 0,
        "skipped_unchanged": 0,
        "captured": 0,
        "skipped_known": 0,
        "per_session": {},
        "errors": [],
    }
    try:
        sessions = list_sessions(auth, limit_sessions)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # API недоступен (сервис лёг, пароль сменился): это деградация, а не
        # трейсбек — пишем отчёт и выходим кодом 1, чтобы было видно в systemd.
        report["errors"].append(f"/api/session: {str(exc)[:120]}")
        sessions = []
    report["sessions_seen"] = len(sessions)

    floor_ms = int(since * 1000) if since else 0
    for session in sessions:
        sid = str(session.get("id") or "")
        if not sid:
            continue
        updated = int((session.get("time") or {}).get("updated") or 0)
        mark = per_session.get(sid, {}).get("last_created_ms", 0)
        # короткое замыкание: сессия не обновлялась — сообщения не спрашиваем
        # (иначе каждый прогон дёргает API по всем сессиям ради пустого ответа)
        if updated and updated <= max(int(mark or 0), floor_ms):
            report["skipped_unchanged"] += 1
            continue
        since_ms = max(int(mark or 0), floor_ms)
        try:
            raw = session_messages(auth, sid)
            known = known_refs(store, sid) if store is not None else set()
            fresh = to_messages(raw, since_ms=since_ms, known=known)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            report["errors"].append(f"{sid}: {str(exc)[:120]}")
            continue
        report["skipped_known"] += max(0, len(known) - len(fresh))
        if not fresh:
            continue
        report["sessions_with_new"] += 1
        written = 0
        for item in fresh:
            if dry_run:
                written += 1
                continue
            try:
                ingest.remember_message(
                    sid, item["role"], item["content"], source="opencode",
                    created_at=item["created_ms"] / 1000.0,
                    source_ref=item["ref"], source_order=item["order"])
                written += 1
            except (ValueError, RuntimeError, OSError) as exc:
                report["errors"].append(f"{sid}/{item['ref']}: {str(exc)[:120]}")
        report["captured"] += written
        last_ms = max(item["created_ms"] for item in fresh)
        per_session[sid] = {
            "last_created_ms": last_ms,
            "captured_total": per_session.get(sid, {}).get("captured_total", 0) + written,
            "title": str(session.get("title") or "")[:80],
            "directory": (session.get("location") or {}).get("directory", ""),
        }
        report["per_session"][sid] = written

    report["status"] = "degraded" if report["errors"] else "ok"

    if not dry_run:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=1))
        if store is not None:
            store.close()

    if not quiet:
        print(f"сессий просмотрено {report['sessions_seen']}, "
              f"без изменений {report['skipped_unchanged']}, "
              f"с новыми сообщениями {report['sessions_with_new']}, "
              f"записано {report['captured']}, ошибок {len(report['errors'])}")
        for err in report["errors"][:5]:
            print("  ошибка:", err)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Захват сессий OpenCode в память")
    parser.add_argument("--dry-run", action="store_true",
                        help="только показать, что было бы записано")
    parser.add_argument("--limit-sessions", type=int, default=CAP_SESSIONS)
    parser.add_argument("--since", type=float, default=0.0,
                        help="unix-время: не брать сообщения старше")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--state", type=Path, default=STATE_PATH,
                        help="файл состояния (по умолчанию рядом со скриптом)")
    args = parser.parse_args(argv)
    if "UM_SRC" not in os.environ:
        os.environ["UM_SRC"] = "/home/user/.config/opencode/services/unified-memory/src"
    report = run(dry_run=args.dry_run, limit_sessions=args.limit_sessions,
                 since=args.since, quiet=args.quiet, state_path=args.state)
    return 1 if report["status"] == "degraded" else 0


if __name__ == "__main__":
    raise SystemExit(main())
