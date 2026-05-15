#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 embedding 功能验证脚本。

无需 k8s / MySQL / MinIO，直接在本地 /tmp 目录验证 embedding 全链路：

  Step 1: hash_embedding (mock 模型)
          - 确定性：同输入 → 同输出
          - 维度正确 (384-d)
          - 不同输入 → 不同向量

  Step 2: st_embedding (真实 sentence-transformers 模型)
          - 如果 sentence-transformers 已安装，验证真实模型输出
          - 未安装则跳过（不影响整体结果）

  Step 3: lance 写入 (add_columns_from_func)
          - 创建临时 lance 数据集 (id, title)
          - 用 mock 模型通过 _build_batch_transform 写入 embedding 列
          - 验证 embedding 列存在、维度 384、行数正确

  Step 4: vector search (lance_io.vector_search)
          - 用第一行的 embedding 做 self-search
          - 验证返回结果非空、有 _distance 字段
          - 第一条结果 distance ≈ 0

  Step 5: 多列 concat embedding (beta.2b)
          - 创建 (id, title, body) 数据集
          - 用 mock 模型对 [title, body] 做 concat embedding
          - 验证 concat 后的 embedding 与单列不同

用法:
    python scripts/test_embedding_local.py

退出码:
    0 - 全部通过
    1 - 有失败
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_pass_count = 0
_fail_count = 0
_skip_count = 0


def _step(n: int, msg: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  Step {n}: {msg}")
    print(f"{'=' * 60}", flush=True)


def _ok(msg: str) -> None:
    global _pass_count
    _pass_count += 1
    print(f"  ✅ PASS: {msg}", flush=True)


def _fail(msg: str) -> None:
    global _fail_count
    _fail_count += 1
    print(f"  ❌ FAIL: {msg}", flush=True)


def _skip(msg: str) -> None:
    global _skip_count
    _skip_count += 1
    print(f"  ⏭️  SKIP: {msg}", flush=True)


def _check(condition: bool, pass_msg: str, fail_msg: str) -> bool:
    if condition:
        _ok(pass_msg)
        return True
    _fail(fail_msg)
    return False


# ---------------------------------------------------------------------------
# Step 1: hash_embedding (mock 模型)
# ---------------------------------------------------------------------------

def test_hash_embedding() -> None:
    _step(1, "hash_embedding (mock 模型) — 确定性 + 维度 + 区分度")

    from lcp.embeddings.mock_model import hash_embedding

    texts = ["hello world", "lance laker smoke test", ""]
    vectors = hash_embedding(texts, dim=384)

    _check(
        len(vectors) == 3,
        f"输出 {len(vectors)} 个向量 (期望 3)",
        f"输出 {len(vectors)} 个向量, 期望 3",
    )

    for i, vec in enumerate(vectors):
        _check(
            len(vec) == 384,
            f"vectors[{i}] 维度={len(vec)} (期望 384)",
            f"vectors[{i}] 维度={len(vec)}, 期望 384",
        )

    # 确定性：同输入 → 同输出
    vectors2 = hash_embedding(["hello world"], dim=384)
    _check(
        vectors[0] == vectors2[0],
        "确定性: 同输入产生同向量",
        "确定性失败: 同输入产生了不同向量",
    )

    # 区分度：不同输入 → 不同向量
    _check(
        vectors[0] != vectors[1],
        "区分度: 不同输入产生不同向量",
        "区分度失败: 不同输入产生了相同向量",
    )

    # 值域 [-1, 1]
    all_in_range = all(
        -1.0 <= v <= 1.0
        for vec in vectors
        for v in vec
    )
    _check(
        all_in_range,
        "值域: 所有分量在 [-1, 1] 范围内",
        "值域失败: 存在超出 [-1, 1] 的分量",
    )

    # 空列表 → 空输出
    empty = hash_embedding([], dim=384)
    _check(
        empty == [],
        "空输入 → 空输出",
        f"空输入返回了 {len(empty)} 个向量",
    )

    print(f"\n  向量样本 (前 5 维): {vectors[0][:5]}", flush=True)


# ---------------------------------------------------------------------------
# Step 2: st_embedding (真实模型)
# ---------------------------------------------------------------------------

def test_st_embedding() -> None:
    _step(2, "st_embedding (sentence-transformers 真实模型)")

    try:
        from lcp.embeddings.sentence_transformer_model import st_embedding
    except ImportError:
        _skip("sentence_transformer_model 模块导入失败")
        return

    model_name = "sentence-transformers/all-MiniLM-L6-v2"

    try:
        vectors = st_embedding(
            ["hello world", "lance laker embedding test"],
            model_name=model_name,
        )
    except Exception as exc:
        from lcp.embeddings.sentence_transformer_model import (
            SentenceTransformersNotInstalledError,
        )
        if isinstance(exc, SentenceTransformersNotInstalledError):
            _skip(f"sentence-transformers 未安装: {exc}")
            return
        _fail(f"st_embedding 抛出异常: {exc!r}")
        return

    _check(
        len(vectors) == 2,
        f"输出 {len(vectors)} 个向量 (期望 2)",
        f"输出 {len(vectors)} 个向量, 期望 2",
    )

    if vectors:
        dim = len(vectors[0])
        _check(
            dim == 384,
            f"MiniLM-L6-v2 维度={dim} (期望 384)",
            f"MiniLM-L6-v2 维度={dim}, 期望 384",
        )

        # 不同输入 → 不同向量
        _check(
            vectors[0] != vectors[1],
            "区分度: 不同输入产生不同向量",
            "区分度失败: 不同输入产生了相同向量",
        )

        print(f"\n  真实模型向量样本 (前 5 维): {vectors[0][:5]}", flush=True)


# ---------------------------------------------------------------------------
# Step 3: lance 写入 (add_columns_from_func)
# ---------------------------------------------------------------------------

def test_lance_write(tmp_dir: Path) -> str | None:
    _step(3, "lance 写入 — mock embedding → lance add_columns")

    try:
        import lance
        import pyarrow as pa
    except ImportError as exc:
        _skip(f"lance/pyarrow 未安装: {exc}")
        return None

    uri = str(tmp_dir / "test_embedding.lance")

    # 创建种子数据集
    table = pa.table({
        "id": pa.array(list(range(5)), type=pa.int64()),
        "title": pa.array(
            [f"row {i}: lance laker embedding test" for i in range(5)],
            type=pa.string(),
        ),
    })
    lance.write_dataset(table, uri, mode="create")
    _ok(f"种子数据集已创建: 5 行, schema={table.schema}")

    # 用 EmbeddingExecutor 的 _build_batch_transform 构建 UDF
    from lcp.workers.executors.embedding import _build_batch_transform

    transform = _build_batch_transform(
        source_columns=["title"],
        target_column="v",
        model_name="mock",
        vector_dim=384,
        separator=" ",
    )
    _ok("batch_transform UDF 已构建 (mock 模型, dim=384)")

    # 写入 embedding 列
    from lcp.data_plane.lance_io import add_columns_from_func

    rows_after = add_columns_from_func(
        uri,
        transforms=transform,
        read_columns=["title"],
    )
    _check(
        rows_after == 5,
        f"写入后行数={rows_after} (期望 5)",
        f"写入后行数={rows_after}, 期望 5",
    )

    # 验证 schema
    ds = lance.dataset(uri)
    schema = ds.schema
    _check(
        "v" in schema.names,
        "embedding 列 'v' 存在于 schema 中",
        f"embedding 列 'v' 不存在, schema={schema.names}",
    )

    if "v" in schema.names:
        v_field = schema.field("v")
        _check(
            pa.types.is_fixed_size_list(v_field.type),
            f"v 列类型={v_field.type} (fixed_size_list)",
            f"v 列类型={v_field.type}, 期望 fixed_size_list",
        )
        if pa.types.is_fixed_size_list(v_field.type):
            _check(
                v_field.type.list_size == 384,
                f"v 列维度={v_field.type.list_size} (期望 384)",
                f"v 列维度={v_field.type.list_size}, 期望 384",
            )

    # 读取一行验证向量内容
    sample = ds.to_table(columns=["v"], limit=1)
    vec = sample.column("v")[0].as_py()
    _check(
        len(vec) == 384,
        f"读回的向量维度={len(vec)} (期望 384)",
        f"读回的向量维度={len(vec)}, 期望 384",
    )
    print(f"\n  写入的向量样本 (前 5 维): {vec[:5]}", flush=True)

    return uri


# ---------------------------------------------------------------------------
# Step 4: vector search
# ---------------------------------------------------------------------------

def _lance_vector_search(
    uri: str,
    *,
    vector: list[float],
    column: str,
    k: int = 3,
) -> list[dict]:
    """用 lance 6.x scanner(nearest=...) API 做 vector search。

    lance 6.0 移除了 ds.search()，改用 ds.scanner(nearest=...) 接口。
    """

    import lance

    ds = lance.dataset(uri)
    scanner = ds.scanner(
        nearest={"column": column, "q": vector, "k": k},
    )
    tbl = scanner.to_table()
    # 转为 list[dict]
    return [
        {col: tbl.column(col)[i].as_py() for col in tbl.column_names}
        for i in range(tbl.num_rows)
    ]


def test_vector_search(uri: str | None) -> None:
    _step(4, "vector search — lance scanner(nearest=...) 检索")

    if uri is None:
        _skip("Step 3 未成功, 跳过 vector search")
        return

    try:
        import lance
    except ImportError:
        _skip("lance 未安装")
        return

    # 读取第一行的向量作为 query
    ds = lance.dataset(uri)
    sample = ds.to_table(columns=["v"], limit=1)
    query_vector = sample.column("v")[0].as_py()

    # 执行 search
    results = _lance_vector_search(uri, vector=query_vector, column="v", k=3)

    _check(
        len(results) > 0,
        f"搜索返回 {len(results)} 条结果 (期望 > 0)",
        "搜索返回 0 条结果",
    )

    _check(
        len(results) <= 3,
        f"结果数={len(results)} ≤ k=3",
        f"结果数={len(results)} > k=3",
    )

    # 每条结果都有 _distance
    all_have_distance = all("_distance" in r for r in results)
    _check(
        all_have_distance,
        "所有结果都包含 _distance 字段",
        "部分结果缺少 _distance 字段",
    )

    if results and "_distance" in results[0]:
        first_dist = results[0]["_distance"]
        _check(
            first_dist < 0.01,
            f"self-search 第一条 distance={first_dist:.6f} ≈ 0",
            f"self-search 第一条 distance={first_dist:.6f}, 期望 ≈ 0",
        )
        print(f"\n  搜索结果:", flush=True)
        for i, r in enumerate(results):
            print(f"    [{i}] _distance={r['_distance']:.6f} id={r.get('id')}", flush=True)

    # 用不同向量搜索，验证距离 > 0
    from lcp.embeddings.mock_model import hash_embedding

    diff_vec = hash_embedding(["completely different query text"])[0]
    results2 = _lance_vector_search(uri, vector=diff_vec, column="v", k=1)
    if results2 and "_distance" in results2[0]:
        dist2 = results2[0]["_distance"]
        _check(
            dist2 > 0.001,
            f"不同向量搜索 distance={dist2:.6f} > 0 (非 self-search)",
            f"不同向量搜索 distance={dist2:.6f} ≈ 0, 不符合预期",
        )


# ---------------------------------------------------------------------------
# Step 5: 多列 concat embedding (beta.2b)
# ---------------------------------------------------------------------------

def test_multi_column_concat(tmp_dir: Path) -> None:
    _step(5, "多列 concat embedding (beta.2b)")

    try:
        import lance
        import pyarrow as pa
    except ImportError:
        _skip("lance/pyarrow 未安装")
        return

    uri = str(tmp_dir / "test_concat.lance")

    # 创建含 title + body 的数据集
    table = pa.table({
        "id": pa.array([0, 1, 2], type=pa.int64()),
        "title": pa.array(["hello", "world", None], type=pa.string()),
        "body": pa.array(["body text", None, "only body"], type=pa.string()),
    })
    lance.write_dataset(table, uri, mode="create")
    _ok("多列种子数据集已创建: 3 行 (title, body)")

    from lcp.workers.executors.embedding import _build_batch_transform

    # 多列 concat
    transform = _build_batch_transform(
        source_columns=["title", "body"],
        target_column="v",
        model_name="mock",
        vector_dim=384,
        separator=" ",
    )

    from lcp.data_plane.lance_io import add_columns_from_func

    rows = add_columns_from_func(
        uri,
        transforms=transform,
        read_columns=["title", "body"],
    )
    _check(rows == 3, f"写入后行数={rows} (期望 3)", f"写入后行数={rows}, 期望 3")

    ds = lance.dataset(uri)
    tbl = ds.to_table(columns=["v"])
    vecs = [tbl.column("v")[i].as_py() for i in range(3)]

    # row 0: "hello body text" → 两列都有值
    # row 1: "world"           → body 为 None, 只用 title
    # row 2: "only body"       → title 为 None, 只用 body
    _check(
        len(vecs[0]) == 384,
        f"row 0 (两列) 维度={len(vecs[0])}",
        f"row 0 维度={len(vecs[0])}, 期望 384",
    )

    # 验证 concat 后的向量与单列不同
    # 注意: lance 存储 float32, hash_embedding 返回 float64,
    # 所以需要用近似比较 (atol=1e-6) 而非严格相等。
    from lcp.embeddings.mock_model import hash_embedding

    def _approx_eq(a: list[float], b: list[float], atol: float = 1e-6) -> bool:
        if len(a) != len(b):
            return False
        return all(abs(x - y) < atol for x, y in zip(a, b))

    single_title = hash_embedding(["hello"])[0]
    concat_both = hash_embedding(["hello body text"])[0]

    _check(
        _approx_eq(vecs[0], concat_both),
        "row 0 向量 ≈ hash('hello body text') — concat 正确",
        "row 0 向量 != hash('hello body text') — concat 逻辑异常",
    )

    _check(
        not _approx_eq(vecs[0], single_title),
        "concat('hello body text') != single('hello') — 多列 vs 单列有区分",
        "concat 和 single 产生了相同向量 — 异常",
    )

    # row 1: title="world", body=None → 应该等于 hash("world")
    world_only = hash_embedding(["world"])[0]
    _check(
        _approx_eq(vecs[1], world_only),
        "row 1 (body=None) 向量 ≈ hash('world') — NULL 跳过正确",
        "row 1 向量 != hash('world') — NULL 处理异常",
    )

    # row 2: title=None, body="only body" → 应该等于 hash("only body")
    body_only = hash_embedding(["only body"])[0]
    _check(
        _approx_eq(vecs[2], body_only),
        "row 2 (title=None) 向量 ≈ hash('only body') — NULL 跳过正确",
        "row 2 向量 != hash('only body') — NULL 处理异常",
    )

    print("\n  多列 concat 验证完成 ✓", flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 60)
    print("  Embedding 功能本地验证脚本")
    print("  无需 k8s / MySQL / MinIO")
    print("=" * 60)

    tmp_dir = Path(tempfile.mkdtemp(prefix="lcp_embedding_test_"))
    print(f"\n临时目录: {tmp_dir}\n")

    try:
        # Step 1: mock embedding
        test_hash_embedding()

        # Step 2: 真实模型 (可选)
        test_st_embedding()

        # Step 3: lance 写入
        uri = test_lance_write(tmp_dir)

        # Step 4: vector search
        test_vector_search(uri)

        # Step 5: 多列 concat
        test_multi_column_concat(tmp_dir)

    finally:
        # 清理临时目录
        shutil.rmtree(tmp_dir, ignore_errors=True)
        print(f"\n临时目录已清理: {tmp_dir}")

    # 汇总
    total = _pass_count + _fail_count + _skip_count
    print(f"\n{'=' * 60}")
    print(f"  汇总: {total} 项检查")
    print(f"    ✅ PASS: {_pass_count}")
    print(f"    ❌ FAIL: {_fail_count}")
    print(f"    ⏭️  SKIP: {_skip_count}")
    print(f"{'=' * 60}")

    if _fail_count > 0:
        print("\n❌ 存在失败项，请检查上方输出。")
        return 1
    print("\n✅ 全部通过！Embedding 功能正常。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
