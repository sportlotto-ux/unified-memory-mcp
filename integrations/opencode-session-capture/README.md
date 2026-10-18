# Захват сессий OpenCode в unified-memory

Обвязка для OpenCode: один проход читает сессии через HTTP API (`Basic
opencode:<password>`, пароль из `~/.config/opencode/service.json`) и пишет в
память то, что имеет смысл помнить. Ставится таймером systemd (oneshot, каждые
10 минут) — тот же паттерн, что у обслуживания памяти.

Это host-специфичная обвязка, а не часть пакета: скрипт использует публичный
контракт импорта (`Ingest.remember_message` с `created_at`/`source_ref`).

## Что берём и что не берём

| источник | берём |
|---|---|
| `user` | текст запроса |
| `assistant` | только части `type=text` (reasoning и вызовы инструментов — шум) |
| `synthetic` | **никогда**: это инъекция инструкций (AGENTS.md), иначе память забьётся текстом инструкций |
| `system`, `compaction`, `idle` | никогда (служебные) |

Границы заданы числом, а не «как получится»: 4000 символов на сообщение (обрезка
с маркером), 200 сообщений на сессию, 50 сессий за прогон, 3 страницы сообщений.

## Идемпотентность

`source_ref` = id сообщения OpenCode, `created_at` = исходное время. Повторный
прогон не пишет дубликатов **даже при потере состояния** — проверено: второй
прогон с пустым файлом состояния записал 0 сообщений (сверка идёт по базе).
Состояние нужно только для короткого замыкания: сессия без изменений не
опрашивается (в простое прогон делает один запрос вместо пятидесяти).

Состояние и отчёт — runtime-артефакты, лежат вне репозитория:
`~/.config/opencode/memory/capture-{state,report}.json`
(переопределяются `UM_CAPTURE_STATE` / `UM_CAPTURE_REPORT`).

## Установка

```bash
REPO=~/.config/opencode/services/unified-memory
MEMDIR=~/.config/opencode/memory
for u in opencode-session-capture.service opencode-session-capture.timer; do
  sed -e "s|@REPO@|$REPO|g" -e "s|@MEMDIR@|$MEMDIR|g" \
      "$REPO/integrations/opencode-session-capture/$u.in" \
      > ~/.config/systemd/user/$u
done
systemctl --user daemon-reload
systemctl --user enable --now opencode-session-capture.timer
```

Юнит берёт `EnvironmentFile=$MEMDIR/memory.env` — тот же env, что у MCP и у
обслуживания (systemd не умеет читать `opencode.json`, поэтому это осознанное
дублирование в одном файле).

## Ручной запуск

```bash
python3 capture.py --dry-run                 # что было бы записано
python3 capture.py --since <unix-время>      # только сообщения новее указанного
python3 capture.py --limit-sessions 5        # ограничить обход
python3 capture.py --state /tmp/x.json       # своё состояние (для опытов)
```

Первый живой прогон стоит делать с `--since` (не тянуть всю историю разом),
дальше таймер продолжает по чекпоинту. Код выхода 1 = деградация (например, API
недоступен) — видно в `systemctl --user --failed`, отчёт при этом пишется.

## Откатить захват

```bash
sqlite3 ~/.config/opencode/memory/unified_memory.db \
  "DELETE FROM um_messages WHERE source='opencode';"
rm -f ~/.config/opencode/memory/capture-{state,report}.json
```

Захваченное узнаётся по `source='opencode'` и непустому `source_ref`.

## Замер (18.10.2026)

Захват 1253 сообщений из 49 сессий. Стенограммы больше не стоят качества поиска
по фактам: у сообщений свой FTS-индекс (`um_fts_msg`), поэтому bm25 не сводит
корпус фактов с корпусом стенограмм. Зонд из 19 реальных запросов: `scope=facts`
68% / 89% / 0.794 (как до захвата), `scope=all` 79% hit@1, MRR 0.850. До
разведения индексов `scope=facts` проседал до 63%.
