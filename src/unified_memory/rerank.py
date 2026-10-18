"""Реранкер recall: Jev (TypeSafe System One) как судья релевантности.

Зачем: RRF складывает голоса плеч и не сравнивает пару (вопрос, кандидат)
целиком. На замере 18.10.2026 (19 реальных запросов, пул 25) он уверенно ставил
правила Skillshop первыми в ответ на вопрос про Saylor — Jev развёл их в 15 раз
(0.92 против 0.06) и поднял hit@1 с 79% до 89%. Jev не генератор: он возвращает
числа, а не пересказ, поэтому не может «отмыть» неверный ответ в уверенный текст.

Границы — осознанные, из того же замера:

* **fail-open и это видно.** Любая ошибка транспорта/ключа → recall идёт в
  порядке RRF, а причина попадает в diagnostics. Молчаливое выключение
  недопустимо: именно так инструмент Jev в системе оказался рабочим «на вид»,
  но выключенным (нет `OPENCODE_API_KEY` — и он молча не фильтровал).
* **Приватность.** Наружу уходят только отредактированные тела (`redact_text`).
  Проверка 18.10.2026 по всей базе (86 фактов + 1308 сообщений, независимые
  шаблоны) реальных секретов не нашла; редакция остаётся обязательной, потому
  что захват сессий пишет непрерывно.
* **Порог и «не нашёл».** Кандидаты ниже `rerank_threshold` отсекаются. Когда не
  проходит никто, возвращается пустая выдача — это ответ «памяти не хватает», а
  не повод подставить третьестепенное. Тот же сигнал даёт `need_wider_search`
  (на вопросах без ответа в памяти он был 0.65–0.88, а топ-оценка — 0.03–0.33
  против 0.84–0.97 там, где ответ есть).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .redact import redact_text

DEFAULT_URL = "https://opencode.ai/zen/v1/systemone"
# Zen-канал Jev ключа не требует (cost 0). Второй канал — ключ TypeSafe
# (UM_RERANK_KEY + UM_RERANK_URL + модель jev-latest), он платный.
DEFAULT_KEY = "public"
DEFAULT_MODEL = "jev-1.13-free"
_UA = "Mozilla/5.0 (X11; Linux x86_64) unified-memory-rerank/1.0"
_TRUNC_MARK = "…"


def zen_transport(url: str, key: str, model: str, state: str,
                  questions: dict, timeout: float) -> dict:
    """Транспорт по умолчанию: POST {model, state, questions} → JSON."""
    body = json.dumps({"model": model, "state": state,
                       "questions": questions}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def prepare(document: str, cap: int) -> str:
    """Текст кандидата, пригодный к отправке: редакция, затем обрезка."""
    text = redact_text(document or "")
    if cap > 0 and len(text) > cap:
        text = text[:cap].rstrip() + _TRUNC_MARK
    return text


def rerank(query: str, documents: list[dict], cfg: dict,
           transport=None) -> dict:
    """Переупорядочить кандидатов по релевантности к запросу.

    `documents` — [{"key": str, "text": str}]. Возвращается словарь:
    `{order, scores, widen, kept, dropped, ms, error, provider}` — при любой
    ошибке заполнен `error`, и вызывающий обязан продолжить в порядке RRF.
    """
    transport = transport or zen_transport
    doc_cap = int(cfg.get("doc_chars", 900))
    payload = [{"id": d["key"], "snippet": prepare(d["text"], doc_cap)}
               for d in documents]
    state = ["TASK:", query or "", "", "CANDIDATES:"]
    for i, cand in enumerate(payload):
        state.append(f"--- [{i}] {cand['id']} ---\n{cand['snippet']}")
    state.append("")
    state.append("If uncertain about a candidate, score it HIGH "
                 "(prefer recall over precision).")
    questions = {
        f"rel_{i}": {
            "type": "noul",
            "instructions": (
                f"How relevant is candidate [{i}] ({cand['id']}) for completing "
                f"the task? Score high if it likely contains the implementation, "
                f"definition, tests, or configuration the task needs."),
        }
        for i, cand in enumerate(payload)
    }
    questions["need_wider_search"] = {
        "type": "noul",
        "instructions": "Is it likely that NONE of the candidates is sufficient "
                        "and the agent should run a wider search before reading?",
    }

    started = time.perf_counter()
    try:
        data = transport(cfg["url"], cfg["key"], cfg["model"],
                         "\n".join(state), questions,
                         float(cfg.get("timeout", 20.0)))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:160]}",
                "ms": round((time.perf_counter() - started) * 1000)}
    except Exception as exc:                                    # noqa: BLE001
        # Транспорт внешний: любую неожиданную поломку тоже превращаем в
        # деградацию, а не в падение recall.
        return {"error": f"{type(exc).__name__}: {str(exc)[:160]}",
                "ms": round((time.perf_counter() - started) * 1000)}

    answers = (data or {}).get("answers") or {}
    scored = []
    for i, cand in enumerate(payload):
        try:
            value = float(answers.get(f"rel_{i}", {}).get("noul", 0.0))
        except (TypeError, ValueError):
            value = 0.0
        scored.append((value, cand["id"]))
    scored.sort(key=lambda pair: -pair[0])
    threshold = float(cfg.get("threshold", 0.5))
    kept = [cid for value, cid in scored if value >= threshold]
    try:
        widen = float(answers.get("need_wider_search", {}).get("noul", 0.0))
    except (TypeError, ValueError):
        widen = 0.0
    usage = (data or {}).get("usage") or {}
    return {
        "order": kept,
        "scores": {cid: value for value, cid in scored},
        "widen": widen,
        "kept": len(kept),
        "dropped": len(scored) - len(kept),
        "tokens": usage.get("input_tokens"),
        "ms": round((time.perf_counter() - started) * 1000),
    }
