"""Active-window engine: порог, fresh tail, многоуровневый DAG, сборка контекста.

Упрощённый наследник идей hermes-lcm (там 6.6k строк engine.py):
- давление считается в токенах (tiktoken если стоит, иначе RU-aware
  эвристика ASCII/4 + не-ASCII/2; точный cl100k — extra `.[tokens]`);
- хвост `fresh_tail` сообщений не жмётся никогда;
- накрыло порог → старые в summary depth 0; накопилось `fanin` нод
  одного уровня → конденсируем в уровень выше (рекурсивно, с капом);
- `assemble` собирает bounded активный контекст: summaries + свежий хвост.
- Lossless: сырые сообщения не удаляются никогда.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .store import Store, estimate_tokens
from .summarize import Summarizer


@dataclass
class Pressure:
    tokens_total: int
    threshold_tokens: int
    over: bool
    messages: int
    summaries: int


def _mkey(base: str, session_id: str, owner: str) -> str:
    """Scoped meta-ключи: legacy '' без суффикса — старые счётчики не сиротят."""
    return f"{base}:{owner}:{session_id}" if owner else f"{base}:{session_id}"


class ActiveWindow:
    def __init__(self, store: Store, summarizer: Summarizer | None,
                 cfg: Config) -> None:
        self.store = store
        self.summarizer = summarizer
        self.cfg = cfg

    def threshold_tokens(self) -> int:
        return int(self.cfg.context_tokens * self.cfg.compact_threshold)

    def pressure(self, session_id: str, owner: str = "") -> Pressure:
        # #3: инкрементный счётчик вместо полного скана на каждое сообщение.
        tkey = _mkey("tokens", session_id, owner)
        raw = self.store.meta_get(tkey)
        if raw is None:  # старая БД — один полный скан, дальше инкремент
            msgs = self.store.session_messages(session_id, limit=1000000, owner=owner)
            sums = self.store.select(
                "SELECT body FROM um_summaries WHERE session_id=?"
                + (" AND owner=?" if owner else ""),
                (session_id, owner) if owner else (session_id,))
            total = sum(estimate_tokens(m["content"]) for m in msgs)
            total += sum(estimate_tokens(r[0]) for r in sums)
            self.store.meta_set(tkey, str(total))
        else:
            total = int(raw)
        if owner:
            n_msgs = self.store.select(
                "SELECT count(*) FROM um_messages WHERE session_id=? AND owner=?",
                (session_id, owner))[0][0]
            n_sums = self.store.select(
                "SELECT count(*) FROM um_summaries WHERE session_id=? AND owner=?",
                (session_id, owner))[0][0]
        else:
            n_msgs = self.store.select(
                "SELECT count(*) FROM um_messages WHERE session_id=?", (session_id,))[0][0]
            n_sums = self.store.select(
                "SELECT count(*) FROM um_summaries WHERE session_id=?", (session_id,))[0][0]
        th = self.threshold_tokens()
        return Pressure(total, th, total >= th, n_msgs, n_sums)

    def maybe_compact(self, session_id: str, owner: str = "") -> dict:
        """Авто-компакшн при превышении порога. Bounded: проход + конденсация.

        Frontier (`um_meta.frontier:<session>`) гарантирует: каждое сообщение
        сжимается один раз, повторные вызовы поверх того же покрытия — noop.
        """
        p = self.pressure(session_id, owner)
        base = {"pressure": p.tokens_total, "threshold": p.threshold_tokens}
        if not p.over or self.summarizer is None:
            return {"status": "ok", **base}
        fkey = _mkey("frontier", session_id, owner)
        frontier = int(self.store.meta_get(fkey) or 0)
        # D15: голова ограничена frontier'ом и капом (не 1M-скан). Если свежих
        # сообщений больше капа — сожмём первые cap, frontier сдвинется к их
        # концу; остаток досжимается следующим вызовом (покрытие не теряется).
        msgs = self.store.session_messages(session_id, after_id=frontier,
                                            limit=self.cfg.compact_max_msgs, owner=owner)
        report: dict = {"status": "compacted", **base}
        fresh = msgs[-self.cfg.fresh_tail:] if self.cfg.fresh_tail else []
        fresh_ids = {m["id"] for m in fresh}
        head = [m for m in msgs if m["id"] not in fresh_ids]
        if head:
            try:
                body = self.summarizer.summarize(
                    [m["content"] for m in head], max_sentences=8)
            except Exception as e:  # noqa: BLE001 — сжатие не роняет запись
                return {"status": "degraded", **base,
                        "error": f"{type(e).__name__}: {e}"[:200]}
        with self.store.transaction():
            if head:
                sid = self.store.add_summary(
                    session_id, body, depth=0,
                    covers_from=head[0]["id"], covers_to=head[-1]["id"],
                    owner=owner, _commit=False)
                self.store.meta_set(fkey, str(head[-1]["id"]), _commit=False)
                report["leaf_summary"] = sid
                report["covered"] = len(head)
            else:
                report["status"] = "noop"
            report["condensed"] = self.condense(
                session_id, owner=owner, _commit=False)
        return report

    def condense(self, session_id: str, max_passes: int = 10,
                 owner: str = "", _commit: bool = True) -> list[dict]:
        """Схлопнуть каждые `fanin` живых нод уровня d в одну ноду d+1.

        Дети помечаются superseded_by (давление и сборка их пропускают,
        lineage для drill-down живёт) — #2.
        """
        if self.summarizer is None:
            return []
        if _commit:
            with self.store.transaction():
                return self._condense(session_id, max_passes, owner)
        return self._condense(session_id, max_passes, owner)

    def _condense(self, session_id: str, max_passes: int,
                  owner: str) -> list[dict]:
        oc = " AND owner=?" if owner else ""
        op = (owner,) if owner else ()
        out: list[dict] = []
        for _ in range(max_passes):
            rows = self.store.select(
                "SELECT depth, count(*) FROM um_summaries WHERE session_id=?"
                f"{oc} AND superseded_by=0"
                " GROUP BY depth HAVING count(*) >= ? ORDER BY depth LIMIT 1",
                (session_id, *op, self.cfg.dag_fanin))
            if not rows:
                break
            depth = rows[0][0]
            kids = self.store.select(
                "SELECT id, body, covers_from, covers_to FROM um_summaries"
                f" WHERE session_id=?{oc} AND depth=? AND superseded_by=0"
                " ORDER BY id LIMIT ?",
                (session_id, *op, depth, self.cfg.dag_fanin))
            body = self.summarizer.summarize([k[1] for k in kids], max_sentences=8)
            covers = [c for k in kids for c in (k[2], k[3]) if c is not None]
            sid = self.store.add_summary(
                session_id, body, depth=depth + 1,
                covers_from=min(covers) if covers else None,
                covers_to=max(covers) if covers else None, owner=owner,
                _commit=False)
            self.store.execute_write(
                f"UPDATE um_summaries SET superseded_by={int(sid)}"
                f" WHERE id IN ({','.join('?' * len(kids))})",
                tuple(k[0] for k in kids), _commit=False)
            # Счётчик честный: дети больше не в активном окне — вычитаем их тела.
            self.store.bump_tokens(
                session_id, -sum(estimate_tokens(k[1]) for k in kids), owner,
                _commit=False)
            out.append({"from_depth": depth, "to_depth": depth + 1,
                        "summary_id": sid, "children": len(kids)})
        return out

    def assemble(self, session_id: str, budget: int = 0,
                 owner: str = "") -> dict:
        """Bounded активный контекст: свежие summaries + свежий хвост, по старшинству."""
        budget = budget or self.cfg.assembly_budget
        half = budget // 2
        oc = " AND owner=?" if owner else ""
        op = (owner,) if owner else ()
        sums = self.store.select(
            "SELECT id, depth, body FROM um_summaries WHERE session_id=?"
            f"{oc} AND superseded_by=0 ORDER BY depth DESC, id DESC",
            (session_id, *op))
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
                if acc and cut <= half:
                    body = " ".join(acc) + " …[truncated]"
                    t = cut
                else:
                    body = body[: half * 4] + " …[truncated]"
                    t = estimate_tokens(body)
            picked_sums.append({"id": sid, "depth": depth, "body": body})
            used += t
        picked_sums.reverse()
        tail, tused = [], 0
        # D15: тянем только возможный хвост (каждое сообщение >= 1 токена, значит
        # больше budget штук в бюджет не влезет) — DESC/LIMIT, без чтения сессии.
        msgs = self.store.session_messages_tail(session_id, limit=max(1, budget),
                                                owner=owner)
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
