"""MCP server skeleton (этап 3). Тулы объявлены, адаптеры — заглушки.

Финальный неймспейс — см. docs/TOOL_MAP.md. Здесь только каркас,
чтобы зафиксировать поверхность API до переноса логики.
"""

TOOL_DEFS = [
    ("mem_remember", "Сохранить факт/наблюдение (mnemosyne remember + canonical)"),
    ("mem_recall", "Единый recall: FTS + vectors + RRF, scope/recency priors"),
    ("mem_expand", "Bounded drill-down: сообщение / дочерние саммари / externalized payload"),
    ("mem_forget", "Удалить факт(а) с подтверждением области"),
    ("mem_status", "Здоровье стора, давление контекста, lineage"),
    ("mem_doctor", "Диагностика БД/FTS/vec-dim + backup-first repair"),
]


def main() -> None:
    # Этап 3: здесь встанет mcp.server.Server с веерным роутером.
    # Пока фиксируем поверхность, чтобы TOOL_MAP было обо что проверять.
    for name, desc in TOOL_DEFS:
        print(f"{name}: {desc}")


if __name__ == "__main__":
    main()
