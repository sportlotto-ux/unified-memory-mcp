"""Summarizers: чем сжимать. Протокол + две реализации.

Сжатие без LLM не бывает честным, поэтому:
- ``ExtractiveSummarizer`` (default): frequency-based экстракция предложений.
  Детерминирована, офлайн, без галлюцинаций — но это конденсат, не пересказ.
- ``EndpointSummarizer``: OpenAI-совместимый HTTP endpoint (переменная
  ``UM_SUMMARIZER_URL``, модель ``UM_SUMMARIZER_MODEL``). Настоящий пересказ,
  когда endpoint доступен; иначе — явная ошибка, не молчаливый fallback.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Protocol

from .store import tokenize


class Summarizer(Protocol):
    def summarize(self, texts: list[str], max_sentences: int = 8) -> str: ...


_SENT_SPLIT = ".!?…"


def split_sentences(text: str) -> list[str]:
    out, buf = [], ""
    for ch in text:
        buf += ch
        if ch in _SENT_SPLIT and len(buf.strip()) > 1:
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return [s for s in out if len(s) > 3]


class ExtractiveSummarizer:
    """Частотная экстракция: top-N предложений по сумме частот термов."""

    def summarize(self, texts: list[str], max_sentences: int = 8) -> str:
        sents: list[str] = []
        for t in texts:
            sents.extend(split_sentences(t))
        if not sents:
            return ""
        freq: dict[str, int] = {}
        for s in sents:
            for tok in tokenize(s):
                freq[tok] = freq.get(tok, 0) + 1
        scored = sorted(
            enumerate(sents),
            key=lambda p: (-sum(freq.get(t, 0) for t in tokenize(p[1])), p[0]),
        )
        top = sorted(i for i, _ in scored[:max_sentences])
        return " ".join(sents[i] for i in top)


class EndpointSummarizer:
    """Пересказ через OpenAI-совместимый /chat/completions endpoint."""

    def __init__(self, url: str = "", model: str = "", timeout: int = 60) -> None:
        self.url = url or os.environ.get("UM_SUMMARIZER_URL", "")
        self.model = model or os.environ.get("UM_SUMMARIZER_MODEL", "")
        self.timeout = timeout
        if not self.url or not self.model:
            raise ValueError("UM_SUMMARIZER_URL and UM_SUMMARIZER_MODEL are required")

    def summarize(self, texts: list[str], max_sentences: int = 8) -> str:
        joined = "\n\n".join(texts)
        if len(joined) > 12000:
            joined = joined[:12000] + "\n…[truncated for endpoint]"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system",
                 "content": f"Condense the conversation into at most {max_sentences} "
                            "sentences. Keep facts, names, numbers, decisions. No preamble."},
                {"role": "user", "content": joined},
            ],
            "temperature": 0.1,
        }
        req = urllib.request.Request(
            self.url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **_auth_header()},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode())
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ValueError(f"summarizer endpoint returned no content: {data!r:.200}") from e
        if not content or not content.strip():
            raise ValueError("summarizer endpoint returned empty content")
        return content.strip()


def _auth_header() -> dict:
    key = os.environ.get("UM_SUMMARIZER_API_KEY", "")
    return {"Authorization": f"Bearer {key}"} if key else {}


def default_summarizer() -> Summarizer:
    """Endpoint если сконфигурирован, иначе extractive. Выбор виден в mem_status."""
    if os.environ.get("UM_SUMMARIZER_URL") and os.environ.get("UM_SUMMARIZER_MODEL"):
        return EndpointSummarizer()
    return ExtractiveSummarizer()
