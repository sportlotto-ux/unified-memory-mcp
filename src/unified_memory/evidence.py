"""mem_evidence: детерминированная проверка опоры (cite) и агрегация (compute).

Без LLM. Тул работает ТОЛЬКО над refs, которые передал вызывающий (никакого
авто-поиска). NL-интент («посчитай сумму…») парсит хост-агент, тул — арифметика.

cite: дословное/почти-дословное вхождение claim в тела refs → verdict.
compute: count/sum/min/max/avg/median по числам из тел ТЕХ ЖЕ refs.
"""

from __future__ import annotations

import re
from typing import Callable

import regex

# kind из mem_recall (um_*) и короткие алиасы
KINDS = {
    "fact": "um_facts", "um_facts": "um_facts",
    "message": "um_messages", "um_messages": "um_messages",
    "summary": "um_summaries", "um_summaries": "um_summaries",
    "edge": "um_edges", "um_edges": "um_edges",
}

_NUM = re.compile(r"-?\d[\d\u00a0 ]*(?:[.,]\d+)?")
_WORD = re.compile(r"[0-9a-zа-яё]+")
_OPS = ("count", "sum", "min", "max", "avg", "median")
_PATTERN_MAX_CHARS = 256
_PATTERN_TIMEOUT_SECONDS = 0.05


def parse_ref(ref: str) -> tuple[str, int]:
    """'fact:3' | 'um_facts:3' → ('um_facts', 3). Кидает ValueError на мусор."""
    if not isinstance(ref, str) or ":" not in ref:
        raise ValueError(f"bad ref {ref!r}: ожидаю 'kind:id', напр. 'fact:3'")
    kind, _, raw = ref.partition(":")
    table = KINDS.get(kind.strip().lower())
    if table is None:
        raise ValueError(f"unknown ref kind {kind!r}: {sorted(set(KINDS))}")
    try:
        return table, int(raw.strip())
    except ValueError:
        raise ValueError(f"bad ref id in {ref!r}: ожидаю целое")


def _norm(s: str) -> str:
    return " ".join(s.split()).casefold()


def _tokens(s: str) -> list[str]:
    return _WORD.findall(s.casefold())


def _tok_match(a: str, b: str) -> bool:
    """Дешёвый prefix-stem для RU-морфологии: река≈реке, москва≈москве."""
    if a == b:
        return True
    n = min(len(a), len(b))
    if n >= 5 and a[:4] == b[:4]:
        return True
    if n >= 4 and a[:3] == b[:3]:
        return True
    return False


def coverage(claim: str, body: str) -> tuple[float, bool]:
    """(доля токенов claim, найденных в body; дословное вхождение).

    Дословность требует контигуального вхождения нормализованного claim и
    не срабатывает на коротком однословном claim (иначе 'да' ⊂ 'удар').
    """
    c = _norm(claim)
    b = _norm(body)
    if not c:
        return 0.0, False
    verbatim = False
    if c == b:
        verbatim = True
    elif len(_tokens(claim)) >= 2 or len(c) >= 8:
        verbatim = c in b
    if verbatim:
        return 1.0, True
    ct = _tokens(claim)
    if not ct:
        return 0.0, False
    bt = _tokens(body)
    hit = sum(1 for t in ct if any(_tok_match(t, x) for x in bt))
    return hit / len(ct), False


def parse_number(raw: str) -> float | None:
    s = raw.replace("\u00a0", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _numbers(text: str, rx: re.Pattern | regex.Pattern | None) -> list[float]:
    if rx is not None:
        try:
            raw = [m.group(1) if m.groups() else m.group(0)
                   for m in rx.finditer(text, timeout=_PATTERN_TIMEOUT_SECONDS)]
        except TimeoutError as e:
            raise ValueError(
                "pattern exceeded execution timeout") from e
    else:
        raw = _NUM.findall(text)
    out = []
    for r in raw:
        v = parse_number(r)
        if v is not None:
            out.append(v)
    return out


def _resolve(store, refs, owner, max_refs, max_chars, archived_fetch):
    """Разбор refs → resolvable тела + rejections. Возвращает (rows, rejections)."""
    rows: list[tuple[tuple[str, int], str, bool]] = []
    rejections: list[dict] = []
    seen: set[tuple[str, int]] = set()
    parsed: list[tuple[str, int]] = []
    for ref in refs:
        try:
            key = parse_ref(ref)
        except ValueError as e:
            rejections.append({"ref": ref, "reason_code": "bad_ref",
                               "detail": str(e)[:120]})
            continue
        if key in seen:
            continue
        seen.add(key)
        parsed.append(key)
    if len(parsed) > max_refs:
        for key in parsed[max_refs:]:
            rejections.append({"kind": key[0], "id": key[1],
                               "reason_code": "budget"})
        parsed = parsed[:max_refs]
    bodies = store.bodies_for(parsed)
    owners = store.owners_for(parsed) if owner else {}
    for key in parsed:
        if owner and owners.get(key, "") != owner:
            rejections.append({"kind": key[0], "id": key[1],
                               "reason_code": "owner_mismatch"})
            continue
        got = bodies.get(key)
        body = got[0] if got else None
        if body is None:
            rejections.append({"kind": key[0], "id": key[1],
                               "reason_code": "not_found"})
            continue
        archived = False
        if body == "[archived]":
            body = archived_fetch(key[0], key[1]) if archived_fetch else None
            archived = True
            if body is None:
                rejections.append({"kind": key[0], "id": key[1],
                                   "reason_code": "archived"})
                continue
        rows.append((key, body[:max_chars], archived))
    return rows, rejections


def run_cite(store, claim: str, refs: list[str], owner: str = "",
             max_refs: int = 50, max_chars: int = 8000, partial: float = 0.5,
             archived_fetch: Callable[[str, int], str | None] | None = None) -> dict:
    rows, rejections = _resolve(store, refs, owner, max_refs, max_chars,
                                archived_fetch)
    refs_out = []
    best = "unsupported"
    rank = {"unsupported": 0, "partial": 1, "supported": 2}
    for key, body, archived in rows:
        cov, verbatim = coverage(claim, body)
        v = ("supported" if (verbatim or cov >= 1.0)
             else "partial" if cov >= partial else "unsupported")
        if rank[v] > rank[best]:
            best = v
        refs_out.append({"kind": key[0], "id": key[1], "verdict": v,
                         "coverage": round(cov, 4), "verbatim": verbatim,
                         "archived": archived})
    return {"mode": "cite", "claim": claim, "verdict": best,
            "refs": refs_out, "rejections": rejections}


def _slot_prefix(s: str) -> str:
    """Срезает 'name: ' у факта, чтобы сравнивать значение, а не обёртку."""
    return re.sub(r"^[^:]{1,40}:\s*", "", _norm(s))


def _is_negation(a: str, b: str) -> bool:
    a, b = _slot_prefix(a), _slot_prefix(b)
    if not a or not b or a == b:
        return False
    return any(a == neg + b or b == neg + a for neg in ("не ", "not "))


def run_conflicts(store, refs: list[str], owner: str = "", max_refs: int = 50,
                  max_chars: int = 8000,
                  archived_fetch: Callable[[str, int], str | None] | None = None) -> dict:
    """Кандидаты противоречий среди refs — БЕЗ вердикта. Судья — хост-агент.

    Только высокоточные сигналы: (1) slot_versions — refs одного
    (owner,category,name) слота с разными телами (смена значения во времени);
    (2) negation — тело A дословно равно «не » + тело B. Любой кандидат несёт
    needs_judgment=true: тул не утверждает конфликт, лишь указывает пару.
    """
    rows, rejections = _resolve(store, refs, owner, max_refs, max_chars,
                                archived_fetch)
    candidates: list[dict] = []
    slots = store.fact_slots([k for k, _, _ in rows])
    by_slot: dict[tuple, list[tuple[tuple[str, int], str]]] = {}
    for key, body, _ in rows:
        meta = slots.get(key)
        if meta:
            by_slot.setdefault((meta["owner"], meta["category"],
                                meta["name"]), []).append((key, meta["body"]))
    for slot, items in by_slot.items():
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                (k1, b1), (k2, b2) = items[i], items[j]
                if _norm(b1) != _norm(b2):
                    candidates.append({
                        "reason_code": "slot_versions",
                        "slot": {"owner": slot[0], "category": slot[1],
                                 "name": slot[2]},
                        "refs": [{"kind": k1[0], "id": k1[1]},
                                 {"kind": k2[0], "id": k2[1]}],
                        "bodies": [b1[:max_chars], b2[:max_chars]]})
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            k1, b1, _ = rows[i]
            k2, b2, _ = rows[j]
            if _is_negation(b1, b2):
                candidates.append({
                    "reason_code": "negation",
                    "refs": [{"kind": k1[0], "id": k1[1]},
                             {"kind": k2[0], "id": k2[1]}],
                    "bodies": [b1[:max_chars], b2[:max_chars]]})
    return {"mode": "conflicts", "candidates": candidates,
            "count": len(candidates), "needs_judgment": True,
            "rejections": rejections}


def run_compute(store, refs: list[str], op: str = "count", pattern: str = "",
                owner: str = "", max_refs: int = 50, max_chars: int = 8000,
                archived_fetch: Callable[[str, int], str | None] | None = None) -> dict:
    op = (op or "").strip().lower()
    if op not in _OPS:
        raise ValueError(f"unknown op {op!r}: {list(_OPS)}")
    rx = None
    if pattern:
        if len(pattern) > _PATTERN_MAX_CHARS:
            raise ValueError(
                f"pattern exceeds {_PATTERN_MAX_CHARS} characters")
        try:
            rx = regex.compile(pattern)
        except regex.error as e:
            raise ValueError(f"bad pattern {pattern!r}: {e}")
    rows, rejections = _resolve(store, refs, owner, max_refs, max_chars,
                                archived_fetch)
    values: list[float] = []
    refs_out = []
    for key, body, archived in rows:
        nums = _numbers(body, rx)
        values += nums
        refs_out.append({"kind": key[0], "id": key[1],
                         "numbers": [round(v, 6) for v in nums],
                         "archived": archived})
    if op == "count":
        return {"mode": "compute", "op": op, "result": float(len(rows)),
                "n": len(values), "refs": refs_out, "rejections": rejections,
                "verdict": "supported" if rows else "unsupported"}
    if not values:
        return {"mode": "compute", "op": op, "result": None, "n": 0,
                "refs": refs_out,
                "rejections": rejections + [{"reason_code": "no_numbers"}],
                "verdict": "unsupported"}
    if op == "sum":
        result = sum(values)
    elif op == "min":
        result = min(values)
    elif op == "max":
        result = max(values)
    elif op == "avg":
        result = sum(values) / len(values)
    else:  # median
        s = sorted(values)
        mid = len(s) // 2
        result = s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2
    return {"mode": "compute", "op": op, "result": round(result, 6),
            "n": len(values), "refs": refs_out, "rejections": rejections,
            "verdict": "supported"}
