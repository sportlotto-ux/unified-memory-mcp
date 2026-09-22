"""Fake embedding backend: deterministic, no model download."""

import hashlib
import math


class FakeBackend:
    dim = 16
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
