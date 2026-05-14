"""Unit tests for ``lcp.embeddings.mock_model.hash_embedding``.

These tests pin the contracts every downstream caller relies on:
determinism, dimension, value range, and parameter validation.  We do
NOT pin the exact byte recipe (one canary vector aside) so the
implementation has room to evolve internally.
"""

from __future__ import annotations

import math

import pytest

from lcp.embeddings.mock_model import hash_embedding

pytestmark = pytest.mark.unit


class TestDeterminism:

    def test_same_input_same_output(self) -> None:
        a = hash_embedding(["hello world"])
        b = hash_embedding(["hello world"])
        assert a == b

    def test_different_inputs_differ(self) -> None:
        # Two unrelated texts must produce visibly different vectors.
        # Equality check is enough: any single float collision across
        # 384 dims would be effectively impossible.
        v1 = hash_embedding(["alpha"])[0]
        v2 = hash_embedding(["beta"])[0]
        assert v1 != v2

    def test_empty_string_is_stable(self) -> None:
        # The empty string is a legitimate input (e.g. NULL source
        # column rendered as ""); the function must still produce a
        # vector and the vector must be reproducible.
        a = hash_embedding([""])[0]
        b = hash_embedding([""])[0]
        assert a == b
        assert len(a) == 384


class TestShape:

    def test_default_dim_is_384(self) -> None:
        # 384 = BGE-base default; the executor's downstream lance
        # column will reserve this many slots.
        out = hash_embedding(["x"])
        assert len(out) == 1
        assert len(out[0]) == 384

    def test_explicit_dim_768(self) -> None:
        out = hash_embedding(["x"], dim=768)
        assert len(out[0]) == 768

    def test_one_vector_per_input(self) -> None:
        out = hash_embedding(["a", "b", "c"])
        assert len(out) == 3
        # Each vector independently sized.
        assert {len(v) for v in out} == {384}


class TestValueRange:

    def test_values_in_unit_interval(self) -> None:
        # The wrapper centres values around 0 by mapping uint32 ->
        # [-1, 1]; downstream lance columns assume this contract for
        # cosine-distance compatibility with real model outputs.
        out = hash_embedding(["lance laker"])[0]
        assert all(-1.0 <= v <= 1.0 for v in out), (
            "values must fall in [-1, 1]",
        )

    def test_values_are_floats(self) -> None:
        out = hash_embedding(["x"])[0]
        assert all(isinstance(v, float) for v in out)
        # Sanity: at least one value must differ from the centre, else
        # the recipe accidentally produced an all-zero vector.
        assert any(not math.isclose(v, 0.0, abs_tol=1e-9) for v in out)


class TestValidation:

    def test_dim_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            hash_embedding(["x"], dim=0)

    def test_dim_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            hash_embedding(["x"], dim=-4)

    def test_dim_not_multiple_of_4_raises(self) -> None:
        # 384 % 4 == 0 OK; 385 % 4 != 0 must raise.
        with pytest.raises(ValueError, match="multiple of 4"):
            hash_embedding(["x"], dim=385)

    def test_non_string_input_raises(self) -> None:
        # The ORM JSON column for source_columns is list[str]; if a
        # caller forgot to coerce, surface it loudly here, not silently
        # via str(123) inside the hash.
        with pytest.raises(TypeError, match="must be str"):
            hash_embedding([123])  # type: ignore[list-item]


class TestEmptyIterable:

    def test_no_inputs_returns_empty_list(self) -> None:
        # The executor uses this shape for batches of size 0 (e.g. a
        # dataset with zero rows after a planner edge case).
        assert hash_embedding([]) == []
