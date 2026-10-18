"""Тесты выделения текста для памяти: что берём и что не берём никогда.

Скрипт захвата живёт в integrations/ (это host-специфичная обвязка, не часть
пакета), поэтому импортируем его по пути.
"""

import sys
from pathlib import Path

INTEGRATION = (Path(__file__).resolve().parents[1]
               / "integrations" / "opencode-session-capture")
sys.path.insert(0, str(INTEGRATION))

from capture import CAP_CHARS, TRUNC_MARK, clip, extract_text, to_messages  # noqa: E402


def msg(mtype, **kw):
    return {"id": kw.pop("id", "msg_1"), "type": mtype,
            "time": {"created": kw.pop("created", 1000)}, **kw}


def test_user_text_is_taken():
    assert extract_text(msg("user", text="что дальше")) == "что дальше"


def test_instruction_injection_is_never_taken():
    # synthetic = AGENTS.md и прочие инъекции: память ими забивать нельзя
    assert extract_text(msg("synthetic", text="Instructions from: AGENTS.md")) == ""
    for service in ("system", "compaction", "idle"):
        assert extract_text(msg(service, text="служебное")) == ""


def test_assistant_keeps_only_text_parts():
    message = msg("assistant", content=[
        {"type": "reasoning", "text": "думаю про память"},
        {"type": "text", "text": "готово: порт 8127"},
        {"type": "executed", "name": "bash", "state": {"output": "шум"}},
        {"type": "text", "text": "и ещё строчка"},
    ])
    assert extract_text(message) == "готово: порт 8127\n\nи ещё строчка"


def test_assistant_with_only_tool_calls_is_dropped():
    assert extract_text(msg("assistant", content=[
        {"type": "executed", "name": "read", "state": {}}])) == ""


def test_clip_marks_truncation():
    long_text = "я" * (CAP_CHARS + 500)
    clipped = clip(long_text)
    assert len(clipped) < len(long_text)
    assert clipped.endswith(TRUNC_MARK)
    assert clip("коротко") == "коротко"


def test_to_messages_filters_sorts_and_numbers():
    raw = [
        msg("assistant", id="m3", created=3000, content=[{"type": "text", "text": "третий"}]),
        msg("user", id="m2", created=2000, text="второй"),
        msg("synthetic", id="m1", created=1000, text="инъекция"),
        msg("user", id="m0", created=500, text="старый"),
    ]
    got = to_messages(raw, since_ms=1000, known={"m2"})
    assert [m["ref"] for m in got] == ["m3"]
    assert got[0]["order"] == 0 and got[0]["role"] == "assistant"

    all_new = to_messages(raw, since_ms=0)
    assert [m["ref"] for m in all_new] == ["m0", "m2", "m3"]
    assert [m["order"] for m in all_new] == [0, 1, 2]
    # инъекция инструкций не попадает ни при каких настройках
    assert "m1" not in [m["ref"] for m in all_new]


def test_to_messages_caps_tail_but_keeps_order():
    raw = [msg("user", id=f"m{i}", created=1000 + i, text=f"сообщение {i}")
           for i in range(10)]
    got = to_messages(raw, cap=3)
    assert [m["ref"] for m in got] == ["m7", "m8", "m9"]
    assert [m["order"] for m in got] == [0, 1, 2]
