"""Active-window engine: порог, fresh tail, многоуровневый DAG, сборка контекста.

Упрощённый наследник идей hermes-lcm (там 6.6k строк engine.py):
- давление считается в токенах (tiktoken если стоит, иначе chars//4);
- хвост `fresh_tail` сообщений не жмётся никогда;
- накрыло порог → старые в summary depth 0; накопилось `fanin` нод
  одного уровня → конденсируем в уровень выше (рекурсивно, с капом);
- `assemble` собирает bounded активный контекст: summaries + свежий хвост.
- Lossless: сырые сообщения не удаляются никогда.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .store import Store
from .summarize import Summarizer


def estimate_tokens(text: str) -> int:
    try:
        import tiktoken

        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:
        return max(1, len(text) // 4)


@dataclass
class Pressure:
    tokens_total: int
    threshold_tokens: int
    over: bool
    messages: int
    summaries: int


class ActiveWindow:
    def __init__(self, store: Store, summarizer: Summarizer | None,
                 cfg: Config) -> None:
        self.store = store
        self.summarizer = summarizer
        self.cfg = cfg

    def threshold_tokens(self) -> int:
        return int(self.cfg.context_tokens * self.cfg.compact_threshold)

    def pressure(self, session_id: str) -> Pressure:
        msgs = self.store.session_messages(session_id, limit=1000000)
        sums = self.store.conn.execute(
            "SELECT body FROM um_summaries WHERE session_id=?", (session_id,)).fetchall()
        total = sum(estimate_tokens(m["content"]) for m in msgs)
        total += sum(estimate_tokens(r[0]) for r in sums)
        th = self.threshold_tokens()
        return Pressure(total, th, total >= th, len(msgs), len(sums))

    def maybe_compact(self, session_id: str) -> dict:
        """Авто-компакшн при превышении порога. Bounded: проход + конденсация.

        Frontier (`um_meta.frontier:<session>`) гарантирует: каждое сообщение
        сжимается один раз, повторные вызовы поверх того же покрытия — noop.
        """
        p = self.pressure(session_id)
        base = {"pressure": p.tokens_total, "threshold": p.threshold_tokens}
        if not p.over or self.summarizer is None:
            return {"status": "ok", **base}
        frontier = int(self.store.meta_get(f"frontier:{session_id}") or 0)
        msgs = self.store.session_messages(session_id, after_id=frontier,
                                           limit=1000000)
        report: dict = {"status": "compacted", **base}
        fresh = msgs[-self.cfg.fresh_tail:] if self.cfg.fresh_tail else []
        fresh_ids = {m["id"] for m in fresh}
        head = [m for m in msgs if m["id"] not in fresh_ids]
        if head:
            body = self.summarizer.summarize(
                [m["content"] for m in head], max_sentences=8)
            sid = self.store.add_summary(session_id, body, depth=0,
                                         covers_from=head[0]["id"],
                                         covers_to=head[-1]["id"])
            self.store.meta_set(f"frontier:{session_id}", str(head[-1]["id"]))
            report["leaf_summary"] = sid
            report["covered"] = len(head)
        else:
            report["status"] = "noop"
        report["condensed"] = self.condense(session_id)
        return report

    def condense(self, session_id: str, max_passes: int = 10) -> list[dict]:
        """Схлопнуть каждые `fanin` нод уровня d в одну ноду d+1."""
        if self.summarizer is None:
            return []
        out: list[dict] = []
        for _ in range(max_passes):
            rows = self.store.conn.execute(
                "SELECT depth, count(*) FROM um_summaries WHERE session_id=?"
                " GROUP BY depth HAVING count(*) >= ? ORDER BY depth LIMIT 1",
                (session_id, self.cfg.dag_fanin)).fetchone()
            if not rows:
                break
            depth = rows[0]
            kids = self.store.conn.execute(
                "SELECT id, body, covers_from, covers_to FROM um_summaries"
                " WHERE session_id=? AND depth=? ORDER BY id LIMIT ?",
                (session_id, depth, self.cfg.dag_fanin)).fetchall()
            body = self.summarizer.summarize([k[1] for k in kids], max_sentences=8)
            covers = [c for k in kids for c in (k[2], k[3]) if c is not None]
            sid = self.store.add_summary(
                session_id, body, depth=depth + 1,
                covers_from=min(covers) if covers else None,
                covers_to=max(covers) if covers else None)
            out.append({"from_depth": depth, "to_depth": depth + 1,
                        "summary_id": sid, "children": len(kids)})
        return out

    def assemble(self, session_id: str, budget: int = 0) -> dict:
        """Bounded активный контекст: свежие summaries + свежий хвост, по старшинству."""
        budget = budget or self.cfg.assembly_budget
        half = budget // 2
        sums = self.store.conn.execute(
            "SELECT id, depth, body FROM um_summaries WHERE session_id=?"
            " ORDER BY depth DESC, id DESC", (session_id,)).fetchall()
        picked_sums, used = [], 0
        for sid, depth, body in sums:
            t = estimate_tokens(body)
            if used + t > half and picked_sums:
                break
            if used == 0 and t > half:
                # старшее саммари больше полбюджета — режем по предложениям,
                # а если пунктуации нет — жёстко по символам
                from .summarize import split_sentences
                acc, cut = [], 0
                for s in split_sentences(body):
                    st = estimate_tokens(s)
                    if cut + st > half and acc:
                        break
                    acc.append(s)
                    cut += st
                if acc:
                    body = " ".join(acc) + " …[truncated]"
                    t = cut
                else:
                    body = body[: half * 4] + " …[truncated]"
                    t = estimate_tokens(body)
            picked_sums.append({"id": sid, "depth": depth, "body": body})
            used += t
        picked_sums.reverse()
        tail, tused = [], 0
        msgs = self.store.session_messages(session_id, limit=1000000)
        for m in reversed(msgs):
            t = estimate_tokens(m["content"])
            if used + tused + t > budget and tail:
                break
            tail.append(m)
            tused += t
        tail.reverse()
        return {"summaries": picked_sums, "tail": tail,
                "tokens": used + tused, "budget": budget,
                "truncated_tail": len(tail) < len(msgs)}
