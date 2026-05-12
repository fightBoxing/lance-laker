# LCP Skeleton — pytest CI Integration Report

> **Run date:** 2026-05-12
> **Scope:** Wire the existing 87-test suite into GitHub Actions
> **Status:** ✅ Delivered — new `tests.yml` workflow added; existing `api-lint.yml` untouched

---

## 1. Goal

Add a CI job that automatically runs the full pytest suite (with coverage)
on every `push` to `main` and every `pull_request` that touches Python
source, tests, or `pyproject.toml`. The bar:

- Must run on at least **two Python versions** (lower/upper edge of `requires-python`)
- Must enforce the **70% coverage gate** that already lives in `pyproject.toml`
- Must publish **artifacts** (HTML coverage, `coverage.xml`, JUnit XML) for downstream tools
- Must **not** disturb the existing `api-lint.yml` workflow

---

## 2. Decisions logged

| #   | Decision                                                     | Rationale                                                                                                                                                                                                                                      |
| --- | ------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| D1  | New file `tests.yml` instead of extending `api-lint.yml`     | The lint workflow's name and path filters are scoped to API artefacts; mixing pytest in there would dilute its job and force every PR to re-run all four jobs. Single-responsibility per workflow follows GitHub Actions community convention. |
| D2  | Matrix on `["3.10", "3.12"]` only                            | 3.10 is the floor declared in `requires-python`; 3.12 is the local dev version. 3.11 is omitted to keep CI fast — added later only if a specific 3.11-only regression appears.                                                                 |
| D3  | Use `astral-sh/setup-uv@v3` instead of pip + cache           | Local dev already uses `uv`; CI matches local exactly to avoid "works on my machine" delta. uv also resolves+installs in seconds, no manual cache wiring required.                                                                             |
| D4  | `concurrency` group cancels in-progress runs on the same ref | A force-push or rapid PR updates should not stack CI minutes.                                                                                                                                                                                  |
| D5  | Coverage comment on PR is `continue-on-error`                | A third-party action failure (rate limit, etc.) should not red-flag the PR. The hard gate is `fail_under=70` already inside pytest.                                                                                                            |
| D6  | Artifacts retained 14 days                                   | Long enough to triage flakes, short enough to stay under storage quotas.                                                                                                                                                                       |

---

## 3. Files changed

| Path                             | Change                    | Lines |
| -------------------------------- | ------------------------- | ----: |
| `.github/workflows/tests.yml`    | **new**                   |    86 |
| `.gitignore`                     | added `pytest-report.xml` |    +1 |
| `.github/workflows/api-lint.yml` | unchanged                 |     0 |

That is it. No source or test code touched (Karpathy #3 — surgical edits).

---

## 4. Workflow shape

```yaml
on:
  push: [main, paths: src/** tests/** pyproject.toml workflow file]
  pull_request: [same paths]

concurrency: tests-<ref>, cancel in-progress

jobs:
  pytest:
    matrix: python = [3.10, 3.12]
    steps:
      - checkout
      - install uv (pinned 0.4.x)
      - install Python <matrix>
      - create .venv
      - uv pip install -e ".[dev]"
      - pytest --cov --cov-report=term-missing --cov-report=xml --cov-report=html --junitxml=...
      - upload artifacts (always, even on failure)
      - comment coverage on PR (3.12 only, soft-fail)
```

---

## 5. Local verification (Karpathy #4 — evidence before assertions)

| Check                            | Command                                         | Result                                                                                       |
| -------------------------------- | ----------------------------------------------- | -------------------------------------------------------------------------------------------- |
| YAML parses                      | `yaml.safe_load('.github/workflows/tests.yml')` | ✅ jobs=[`pytest`], triggers=[`push`,`pull_request`]                                          |
| 87 tests pass                    | `pytest --cov --cov-report=xml --junitxml=…`    | ✅ 87 passed in 4.35s                                                                         |
| Coverage ≥ 70%                   | same                                            | ✅ 75.21% (gate is 70%)                                                                       |
| Coverage XML produced            | `coverage.xml`                                  | ✅ generated                                                                                  |
| JUnit XML produced               | `pytest-report.xml`                             | ✅ generated                                                                                  |
| `.gitignore` covers CI artefacts | grep                                            | ✅ all 4 patterns present (`coverage.xml`, `htmlcov/`, `pytest-report.xml`, `.pytest_cache/`) |

---

## 6. What this delivers

1. **Every PR that touches Python now blocks merge if any test fails or
   coverage drops below 70%.**
2. **Two-version matrix** catches `requires-python>=3.10` regressions
   that would only surface in production-like environments.
3. **Coverage HTML and JUnit XML** are downloadable from the run page
   for any reviewer needing to drill into a flake or a missing line.
4. **uv-based install** mirrors the local workflow exactly — anyone can
   reproduce a CI failure with one `uv pip install -e ".[dev]"`
   followed by `pytest`.

---

## 7. What was *not* done (and why)

| Not done                             | Reason                                                                                                   |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------- |
| Codecov integration                  | Adds an external service dependency; the in-PR comment via `orgoro/coverage` covers the most common need |
| Slack/email notification on failure  | GitHub already notifies the PR author by default                                                         |
| Spectral/buf merged into `tests.yml` | Existing `api-lint.yml` already covers them; merging would re-run lint on every Python change            |
| Mutation testing job                 | Way too slow for every PR; better as a nightly cron later                                                |
| Real MySQL container                 | No MySQL-specific code paths exercised yet; revisit when business logic lands                            |
| 3.11 in matrix                       | Conservative bet; can add in 30s if a 3.11 regression appears                                            |

---

## 8. How to verify after first push

```bash
# Open in browser after the next PR or push:
gh run list --workflow tests.yml
gh run view --log
```

A green check named `pytest (Python 3.10)` and `pytest (Python 3.12)`
appears next to each PR. Coverage drops below 70% will fail the run
and block the merge.

---

## 9. Karpathy compliance

| Principle                   | How                                                                               |
| --------------------------- | --------------------------------------------------------------------------------- |
| **#1 Think before writing** | D1 (separate workflow vs extend) was made explicit, not silent                    |
| **#2 Simple first**         | One file, one job, one matrix — no Codecov, no Slack, no mutation tests           |
| **#3 Surgical edits**       | Existing `api-lint.yml` untouched; only `.gitignore` got a one-line addition      |
| **#4 Goal-driven**          | Each verification step (§5) had a deterministic check; no "let's see if it works" |

---

## 10. Recommended next steps (none of which are required to land this)

1. Wait for the first real push to confirm the matrix runs cleanly on
   GitHub-hosted runners — local Python 3.10 was not exercised this round.
2. After 2–3 weeks of green runs, raise the coverage gate from 70% to
   75% (current actual is 75.2%).
3. When `buf generate` produces `*_pb2` modules, the gRPC server's
   coverage will jump from 42% to ~80%, at which point the gate can
   move to 80%.
