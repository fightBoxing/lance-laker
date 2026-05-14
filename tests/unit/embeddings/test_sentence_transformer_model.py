"""Unit tests for ``lcp.embeddings.sentence_transformer_model``.

These tests do NOT require the ``sentence-transformers`` wheel.  Every
call into the package is intercepted by monkey-patching ``_load_model``
to return a hand-rolled fake encoder, so the tests run with the slim
base extras only (mirrors how :mod:`tests.unit.data_plane.test_lance_io`
fakes pylance).
"""

from __future__ import annotations

from typing import Any

import pytest

from lcp.embeddings import sentence_transformer_model as st_mod

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fake SentenceTransformer
# ---------------------------------------------------------------------------


class _FakeRow:
    """Stand-in for the numpy.ndarray rows ``encode`` returns."""

    def __init__(self, values: list[float]) -> None:
        self._values = values

    def tolist(self) -> list[float]:
        return list(self._values)


class _FakeModel:
    """Minimal fake honouring the ``encode`` contract st_embedding uses."""

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim
        self.encode_calls: list[list[str]] = []

    def encode(self, texts: list[str]) -> list[_FakeRow]:
        self.encode_calls.append(list(texts))
        # Deterministic per-text vector so tests can assert order +
        # different inputs differ; first slot is the text length, the
        # rest are zeros.
        return [
            _FakeRow([float(len(t))] + [0.0] * (self.dim - 1)) for t in texts
        ]


@pytest.fixture
def fake_model(monkeypatch: pytest.MonkeyPatch) -> _FakeModel:
    """Patch ``_load_model`` so every call returns the same fake."""

    fake = _FakeModel()
    monkeypatch.setattr(st_mod, "_load_model", lambda _name: fake)
    # Also clear the real cache so the previous test's leftovers do
    # not leak across cases.
    st_mod._MODEL_CACHE.clear()
    return fake


# ---------------------------------------------------------------------------
# st_embedding -- happy paths
# ---------------------------------------------------------------------------


class TestStEmbedding:

    def test_returns_list_of_lists_with_model_dim(
        self, fake_model: _FakeModel,
    ) -> None:
        out = st_mod.st_embedding(
            ["hello", "world"],
            model_name="all-MiniLM-L6-v2",
        )
        assert len(out) == 2
        # Outer is plain Python list (no numpy leakage).
        assert isinstance(out, list)
        assert all(isinstance(row, list) for row in out)
        assert all(isinstance(v, float) for row in out for v in row)
        # Each row matches the fake's dim contract.
        assert all(len(row) == 384 for row in out)
        # First slot encodes len(text) so we can assert order preserved.
        assert out[0][0] == 5.0
        assert out[1][0] == 5.0

    def test_empty_iterable_returns_empty_list_without_call(
        self, fake_model: _FakeModel,
    ) -> None:
        # Mirror hash_embedding: zero inputs -> zero outputs, no model
        # invocation (no point loading a model for nothing).
        out = st_mod.st_embedding([], model_name="any-model")
        assert out == []
        assert fake_model.encode_calls == []

    def test_forwards_texts_in_order(self, fake_model: _FakeModel) -> None:
        st_mod.st_embedding(
            ["a", "bb", "ccc"], model_name="any-model",
        )
        assert fake_model.encode_calls == [["a", "bb", "ccc"]]


# ---------------------------------------------------------------------------
# st_embedding -- validation
# ---------------------------------------------------------------------------


class TestValidation:

    def test_non_string_input_raises(self, fake_model: _FakeModel) -> None:
        # Loud TypeError mirrors hash_embedding so callers cannot
        # silently encode stringified ints.
        with pytest.raises(TypeError, match="must be str"):
            st_mod.st_embedding([123], model_name="any")  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# Lazy import error
# ---------------------------------------------------------------------------


class TestSentenceTransformersNotInstalledError:

    def test_load_model_raises_friendly_error_when_missing(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When sentence-transformers is missing, ``_load_model`` must
        raise :class:`SentenceTransformersNotInstalledError`, not bare
        :class:`ImportError`."""

        # Clear cache so the lazy import is actually attempted.
        st_mod._MODEL_CACHE.clear()

        import builtins

        real_import = builtins.__import__

        def _no_st(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.startswith("sentence_transformers"):
                raise ImportError("no module named 'sentence_transformers'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_st)
        with pytest.raises(
            st_mod.SentenceTransformersNotInstalledError,
            match="sentence-transformers",
        ):
            st_mod._load_model("any-model")


# ---------------------------------------------------------------------------
# Module cache
# ---------------------------------------------------------------------------


class TestModelCache:

    def test_second_call_reuses_loaded_model(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Use a counter monkeypatch so we can verify the lazy import
        # path runs exactly once for the same model_name.
        st_mod._MODEL_CACHE.clear()
        load_count = {"n": 0}

        class _OnceLoader:
            def __init__(self, name: str) -> None:
                load_count["n"] += 1

            def encode(self, texts: list[str]) -> list[_FakeRow]:
                return [_FakeRow([1.0]) for _ in texts]

        # Stub the lazy import sentinel by monkey-patching the imported
        # SentenceTransformer symbol used inside _load_model.  We bypass
        # the import-time check by inserting straight into the cache on
        # the second call's behalf via the real _load_model's first run.
        import sys
        import types

        fake_pkg = types.ModuleType("sentence_transformers")
        fake_pkg.SentenceTransformer = _OnceLoader  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "sentence_transformers", fake_pkg)

        st_mod.st_embedding(["x"], model_name="model-a")
        st_mod.st_embedding(["y"], model_name="model-a")
        assert load_count["n"] == 1, "second call must reuse cached model"

        # A different model_name must trigger a fresh load.
        st_mod.st_embedding(["z"], model_name="model-b")
        assert load_count["n"] == 2
