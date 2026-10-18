"""Этап 0: детерминированное обслуживание стора (systemd timer, без LLM).

Зачем: MCP обслуживает себя *лениво* — `server._maybe_maintenance` висит на
путях записи и в простое не делает ничего. Поэтому пропущенные вектора после
сбоя эмбеддера, несобранный vec0 и разросшийся WAL остаются незамеченными
(замер 2026-10-18: без индекса поиск 110-120 мс на 2k векторов против 8 мс).

Этот модуль выполняет тот же набор шагов по расписанию и с отчётом:

  1. диагностика (integrity, набор моделей, vec_index, гигиена) — read-only;
  2. вектора: доэмбеддить пропущенные и пересобрать vec0 (`Ingest.reindex`);
     пропускается, если пропусков нет и индекс уже собран под активный dim
     (`build_vec_index` — полная пересборка, гонять её вхолостую нельзя);
  3. гигиена: при сиротах/висячих ссылках — `Store.repair` (backup-first);
  4. архив: age-based (`retention_days`>0, не чаще раза в неделю) и size-based
     (>= `archive_size_mb`) — та же политика, что у ленивого пути;
  5. `PRAGMA wal_checkpoint(TRUNCATE)`.

Ничего не удаляет: `purge` остаётся ручным (`mem_doctor`). При несовпадении
модели векторов со стором модуль не трогает вектора, а громко рапортует —
лечение это `python -m unified_memory.reembed`, отдельное осознанное решение.

Запуск::

    python3 -m unified_memory.maintenance [--dry-run] [--report PATH] [--batch N]

Коды выхода: 0 — всё в порядке; 1 — есть упавший шаг или деградация.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from . import archive
from .config import Config, load
from .embeddings import EmbedServerError, make_backend
from .ingest import Ingest
from .store import Store

DEFAULT_REPORT_NAME = "maintenance-report.json"
# Политика — как у server._maybe_maintenance, чтобы оба пути не расходились.
RETENTION_PERIOD_S = 7 * 86400
ARCHIVE_MAX_BATCHES = 10
HYGIENE_KEYS = ("orphan_vectors", "orphan_fts", "orphan_entities",
                "dangling_vecidx", "dangling_links", "duplicate_facts")


def _candidates(hygiene: dict) -> dict:
    """Только непустые кандидаты: пустой список — это «чисто», не сигнал."""
    return {k: v for k, v in hygiene.items()
            if k in HYGIENE_KEYS and _not_empty(v)}


def _not_empty(value) -> bool:
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) > 0
    return bool(value)


def _fail(report: dict, step: str, exc: BaseException) -> None:
    report["errors"].append(f"{step}: {type(exc).__name__}: {exc}"[:300])


def _write_report(report: dict, path: Path) -> None:
    """Атомарно: читающий в любой момент видит либо старый, либо новый отчёт."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def run(cfg: Config | None = None, *, apply: bool = True,
        now: float | None = None, batch: int = 64,
        report_path: Path | None = None) -> dict:
    """Один проход обслуживания. `apply=False` — только диагностика и план.

    Ошибка любого шага не отменяет остальные: обслуживание должно доводить до
    конца то, что может, а сломанное — показывать в `errors`/`status`.
    """
    cfg = cfg or load()
    now = now if now is not None else time.time()
    report: dict = {
        "status": "ok",
        "apply": apply,
        "started_at": now,
        "db": str(cfg.db_path),
        "steps": {},
        "errors": [],
    }
    store = Store(cfg)
    try:
        # ── 1 диагностика ────────────────────────────────────────────────
        step: dict = {}
        t0 = time.perf_counter()
        hygiene: dict = {}
        dim = 0
        backend = None
        try:
            diag = store.diagnostics()
            hygiene = store.hygiene()
            step = {"integrity": diag["integrity"],
                    "vectors_by_model": diag["vectors_by_model"],
                    "vec_index": diag["vec_index"],
                    "candidates": _candidates(hygiene)}
            report["store"] = {**store.stats(),
                               "db_size_bytes": store.db_size_bytes()}
            if diag["integrity"] != "ok":
                report["errors"].append(f"integrity_check: {diag['integrity']}")
        except Exception as e:  # noqa: BLE001 — шаг не должен ронять сервис
            _fail(report, "diagnostics", e)
            step["error"] = str(e)[:200]
        step["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        report["steps"]["diagnostics"] = step

        # ── 2 вектора ────────────────────────────────────────────────────
        step = {}
        t0 = time.perf_counter()
        try:
            backend = make_backend(cfg)
            ing = Ingest(store, backend, cfg=cfg)
            missing = len(ing._embedding_jobs(missing_only=True))
            idx = store.vec_index_status()
            stored_models = {m for m, _ in (store.diagnostics()["vectors_by_model"])}
            mismatch = bool(stored_models) and backend.model_name not in stored_models
            step = {"missing": missing, "vec_index": idx,
                    "model": backend.model_name,
                    "model_mismatch": mismatch}
            if mismatch:
                report["errors"].append(
                    "vectors: model mismatch, store="
                    f"{sorted(stored_models)} active={backend.model_name} — "
                    "нужен python -m unified_memory.reembed")
            needs_probe = cfg.embedding_backend == "openai" or missing > 0
            if needs_probe:
                try:
                    backend.warm()
                    step["embed_backend"] = "ok"
                except EmbedServerError as e:
                    step["embed_backend"] = "unavailable"
                    _fail(report, "vectors", e)
                    raise
            else:
                step["embed_backend"] = "not_probed"  # local: модель не грузим зря
            if mismatch:
                pass  # вектора не трогаем — иначе смешаем модели в индексе
            elif not missing and idx.get("mode") == "ready" \
                    and idx.get("dim") == backend.dim:
                step["skipped"] = "no missing vectors, index ready"
            elif apply:
                step["reindex"] = ing.reindex(batch=batch)
            else:
                step["would_embed"] = missing
            dim = backend.dim if not mismatch else 0
        except EmbedServerError:
            step["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        except Exception as e:  # noqa: BLE001
            _fail(report, "vectors", e)
            step["error"] = str(e)[:200]
        step.setdefault("ms", round((time.perf_counter() - t0) * 1000, 1))
        report["steps"]["vectors"] = step

        # ── 3 гигиена (backup-first, только при кандидатах) ──────────────
        step = {}
        cand = _candidates(hygiene)
        try:
            if not cand:
                step["status"] = "clean"
            elif not apply:
                step["apply_required"] = True
                step["candidates"] = cand
            else:
                rep = store.repair(dim=dim)
                step = {"candidates": cand, "repair": rep,
                        "candidates_after": _candidates(store.hygiene())}
        except Exception as e:  # noqa: BLE001
            _fail(report, "hygiene", e)
            step["error"] = str(e)[:200]
        report["steps"]["hygiene"] = step

        # ── 4 архив ──────────────────────────────────────────────────────
        step = {}
        size_limit = cfg.archive_size_mb * 1024 * 1024
        try:
            size_now = store.db_size_bytes()
            due_age = (cfg.retention_days > 0
                       and now - float(store.meta_get("retention_last_run") or 0)
                       >= RETENTION_PERIOD_S)
            due_size = size_now >= size_limit
            step = {"db_size_bytes": size_now, "size_limit_bytes": size_limit,
                    "retention_days": cfg.retention_days,
                    "due_age": due_age, "due_size": due_size}
            if not (apply and (due_age or due_size)):
                step["skipped"] = ("dry-run" if (due_age or due_size) and not apply
                                   else "nothing due")
            else:
                moved_age = moved_size = 0
                if due_age:
                    cutoff = now - cfg.retention_days * 86400
                    conn = archive.open_archive(cfg.archive_path)
                    try:
                        moved_age = archive.move_oldest(
                            store, conn, cfg.archive_batch, before_ts=cutoff,
                            label=str(cfg.archive_path))
                    finally:
                        conn.close()
                    store.meta_set("retention_last_run", str(now))
                    store.meta_set("retention_last_moved", str(moved_age))
                if due_size:  # bounded: не более ARCHIVE_MAX_BATCHES за проход
                    conn = archive.open_archive(cfg.archive_path)
                    try:
                        for _ in range(ARCHIVE_MAX_BATCHES):
                            n = archive.move_oldest(store, conn, cfg.archive_batch,
                                                    label=str(cfg.archive_path))
                            moved_size += n
                            if n == 0 or store.db_size_bytes() < size_limit:
                                break
                    finally:
                        conn.close()
                    store.meta_set("archive_last_run", str(now))
                    store.meta_set("archive_last_moved", str(moved_size))
                step.update(moved_age=moved_age, moved_size=moved_size,
                            db_size_after=store.db_size_bytes())
        except Exception as e:  # noqa: BLE001
            _fail(report, "archive", e)
            step["error"] = str(e)[:200]
        report["steps"]["archive"] = step

        # ── 5 WAL ────────────────────────────────────────────────────────
        step = {}
        try:
            if apply:
                busy, log_pages, checkpointed = store.conn.execute(
                    "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                step = {"busy": busy, "log_pages": log_pages,
                        "checkpointed": checkpointed}
            else:
                step["skipped"] = "dry-run"
        except Exception as e:  # noqa: BLE001
            _fail(report, "wal", e)
            step["error"] = str(e)[:200]
        report["steps"]["wal"] = step
    finally:
        store.close()

    report["finished_at"] = time.time()
    report["elapsed_s"] = round(report["finished_at"] - now, 3)
    if report["errors"]:
        report["status"] = "degraded"
    path = report_path or _report_path(cfg)
    try:
        _write_report(report, path)
        report["report_path"] = str(path)
    except Exception as e:  # noqa: BLE001 — отчёт вторичен, но провал виден
        report["errors"].append(f"report: {type(e).__name__}: {e}"[:200])
        report["status"] = "degraded"
    return report


def _report_path(cfg: Config) -> Path:
    return cfg.db_path.parent / DEFAULT_REPORT_NAME


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m unified_memory.maintenance",
        description="Один проход обслуживания стора памяти (вектора, индекс, "
                    "гигиена, архив, WAL). Без LLM и без удаления данных.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="только диагностика и план, без мутаций")
    parser.add_argument("--report", type=Path, default=None,
                        help=f"куда писать JSON-отчёт (по умолчанию рядом с БД: "
                             f"{DEFAULT_REPORT_NAME})")
    parser.add_argument("--batch", type=int, default=64,
                        help="документов за раз в батче эмбеддинга (по умолчанию 64)")
    parser.add_argument("--quiet", action="store_true",
                        help="не печатать отчёт в stdout")
    args = parser.parse_args(argv)
    if args.batch <= 0:
        parser.error("--batch must be > 0")
    report = run(apply=not args.dry_run, batch=args.batch,
                 report_path=args.report)
    if not args.quiet:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
