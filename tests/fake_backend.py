"""Fake embedding backend: deterministic, no model download."""

import hashlib
import math
import re


class FakeBackend:
    dim = 16
    model_name = "fake/test-16"
    spec_name = "fake/test-16"

    class _Spec:
        name = "fake/test-16"

    spec = _Spec()

    def _vec(self, text):
        h = hashlib.sha256(text.encode()).digest()
        v = [((b / 255.0) - 0.5) * 2 for b in h[: self.dim]]
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_docs(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


class LexicalBackend:
    """Детерминированные лексические эмбеддинги для golden-eval (v0.8 E20).

    Не модель: разреженный bag-of-tokens по словарю, который строится по мере
    встраивания корпуса (без хеш-коллизий). Токены проходят мини-стемминг
    (5 символов) и крошечный RU-словарь синонимов, поэтому «парафразы»
    (тариф/подписка, платить/цена, бэкап/копия) сближаются косинусом.
    Детерминированно (порядок корпуса фиксирован), без сети и модели.
    Инструмент для проверки ПАЙПЛАЙНА ранжирования, не качества эмбеддингов.
    """

    dim = 512
    model_name = "fake/lexical-512"
    spec_name = "fake/lexical-512"

    class _Spec:
        name = "fake/lexical-512"

    spec = _Spec()

    _SYN = {
        "платить": "цена", "плачу": "цена", "стоит": "цена", "стоимость": "цена",
        "тариф": "подписка", "подписку": "подписка", "подписки": "подписка",
        "бэкап": "копия", "компенсацию": "возврат", "компенсация": "возврат",
        "производство": "завод", "производит": "завод", "техподдержка": "поддержка",
        "география": "продажи", "данные": "хранилище",
    }

    def __init__(self):
        self._vocab: dict[str, int] = {}

    def _stem(self, tok):
        return self._SYN.get(tok, tok)[:5]  # мини-стемминг: 5 символов

    def _vec(self, text, learn=False):
        v = [0.0] * self.dim
        for tok in re.findall(r"[a-zа-я0-9]+", (text or "").lower()):
            st = self._stem(tok)
            i = self._vocab.get(st)
            if i is None and learn and len(self._vocab) < self.dim:
                i = self._vocab[st] = len(self._vocab)
            if i is not None:
                v[i] = 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_docs(self, texts):
        return [self._vec(t, learn=True) for t in texts]

    def embed_query(self, text):
        return self._vec(text)

