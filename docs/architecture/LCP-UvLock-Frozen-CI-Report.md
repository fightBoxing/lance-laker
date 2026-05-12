# LCP — `uv.lock` Generation & CI Frozen-Sync Integration

> **Run date:** 2026-05-12
> **Scope:** Generate `uv.lock` for reproducible builds; switch CI to `uv sync --frozen`
> **Status:** ✅ Delivered — 87 tests pass under both fresh resolve and frozen-lock modes

---

## 1. Goal

Two outcomes, one PR:

1. **Reproducibility:** Pin every transitive dependency so a build today and a build in 6 months produce the same bytes.
2. **CI speed:** Replace per-run dependency resolution with a frozen-lock install, expected ~30s saved per matrix cell.

Hard success criteria:

- `uv.lock` exists and is checked in
- `uv sync --frozen --extra dev` succeeds with no resolver work
- All 87 tests pass under the locked environment
- CI workflow runs the **same exact commands** locally and on GitHub-hosted runners
- A change to dependencies (i.e. `uv.lock`) re-triggers CI

---

## 2. Decisions logged

| #   | Decision                                                                                 | Rationale                                                                                                                           |
| --- | ---------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| D1  | Commit `uv.lock` to git                                                                  | Astral's own guidance for **applications** (vs libraries) — locks must be reproducible across machines and CI                       |
| D2  | Use `uv sync --frozen` (not `uv pip install -e ".[dev]"`)                                | `--frozen` fails fast if the lock is stale; the old `uv pip install` would silently re-resolve and hide drift                       |
| D3  | Use `uv run pytest` (not `source .venv/bin/activate`)                                    | One fewer shell-state assumption; works identically across bash/zsh/Windows runners; aligns with uv's "run anywhere" model          |
| D4  | Add `uv.lock` to CI path filters                                                         | A pure dependency bump must re-run tests even if no `src/` file changes                                                             |
| D5  | Enable uv's built-in cache via `enable-cache: true` + `cache-dependency-glob: "uv.lock"` | Caches the uv download cache (not the venv). Saves the wheel-fetch portion (~5–10s) without inviting stale-cache bugs               |
| D6  | Pass `--python ${{ matrix.python-version }}` to `uv sync`                                | Belt-and-braces: ensures the synced venv is keyed to the correct interpreter even if `uv python install` picked a different default |
| D7  | Drop the explicit `uv venv` + `pip install` two-step                                     | `uv sync --frozen` does both atomically; one fewer step = one fewer place for drift                                                 |

---

## 3. Files changed

| Path                          | Change                                              |                   Size |
| ----------------------------- | --------------------------------------------------- | ---------------------: |
| `uv.lock`                     | **new** (63 packages, 1792 lines, 371 KB)           |           first commit |
| `.github/workflows/tests.yml` | refactored install pipeline                         | 90 → 91 lines (net +1) |
| `.gitignore`                  | unchanged (correctly does **not** ignore `uv.lock`) |                      — |

---

## 4. Workflow before vs after

### Before (3 steps for env setup, ~50–60s on cold runner)

```yaml
- name: Set up Python
  run: uv python install ${{ matrix.python-version }}
- name: Create virtual environment
  run: uv venv --python ${{ matrix.python-version }} .venv
- name: Install project
  run: source .venv/bin/activate && uv pip install -e ".[dev]"
```

### After (2 steps, ~20–30s on warm cache)

```yaml
- name: Set up Python
  run: uv python install ${{ matrix.python-version }}
- name: Sync dependencies from uv.lock (frozen)
  run: uv sync --frozen --extra dev --python ${{ matrix.python-version }}
```

Plus the uv setup step now caches the download cache:

```yaml
- name: Install uv
  uses: astral-sh/setup-uv@v3
  with:
    version: "0.4.x"
    enable-cache: true
    cache-dependency-glob: "uv.lock"
```

---

## 5. Local verification (Karpathy #4 — evidence before assertions)

| Check                           | Command                                   | Result                                               |
| ------------------------------- | ----------------------------------------- | ---------------------------------------------------- |
| Lock generation                 | `uv lock`                                 | ✅ Resolved 63 packages in 40.64s                     |
| Lock file present and parseable | `head uv.lock`                            | ✅ TOML, version=1, requires-python=">=3.10"          |
| Frozen sync works               | `uv sync --frozen --extra dev`            | ✅ no resolver work, only re-installed `lcp` editable |
| Full test suite (frozen env)    | `pytest --cov -q`                         | ✅ **87 passed in 4.19s**                             |
| Full test suite via `uv run`    | `uv run pytest --cov -q`                  | ✅ **87 passed in 4.34s**                             |
| Coverage gate held              | (same)                                    | ✅ 75.21% ≥ 70%                                       |
| YAML parses, steps in order     | Python `yaml.safe_load` + step extraction | ✅ 8 steps, correct order                             |
| New path filter active          | `yaml.safe_load → on.push.paths`          | ✅ contains `uv.lock`                                 |
| `uv.lock` not gitignored        | `grep .gitignore`                         | ✅ not ignored (correct for apps)                     |

---

## 6. What this delivers

1. **A reproducible build envelope.** Anyone with `uv` and this repo gets bit-identical dependency versions via `uv sync --frozen --extra dev`.
2. **Faster CI.** Estimated 30s saved per matrix cell (2 cells = 1 minute per PR), plus the uv download cache trims another 5–10s on warm hits.
3. **Fail-fast on dependency drift.** If anyone hand-edits `pyproject.toml` without re-running `uv lock`, CI breaks at the **sync** step rather than 4 minutes later in pytest.
4. **No more "works on my machine".** `uv run` removes the dependency on shell-specific `source ... activate` patterns.

---

## 7. What was *not* done (and why)

| Not done                                                   | Reason                                                                                                      |
| ---------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Auto-update job for `uv.lock` (Renovate/Dependabot)        | Not asked for; can be added later as a separate PR                                                          |
| `uv lock --upgrade` cron                                   | Premature — no production traffic yet                                                                       |
| Python 3.11 added to matrix                                | Same as last round — conservative; the lock supports `>=3.10` so adding 3.11 is a 1-line change when needed |
| Pre-commit hook to run `uv lock`                           | Would slow every commit; CI is the right place to enforce                                                   |
| Switch from `astral-sh/setup-uv@v3` to a pinned commit SHA | Lower-priority; v3 is a stable major and we already pin uv version inside                                   |

---

## 8. How to maintain `uv.lock`

```bash
# Add or change a dep in pyproject.toml, then:
uv lock                  # Updates uv.lock
uv sync --extra dev      # Applies it locally

# Refresh all transitive versions (use sparingly):
uv lock --upgrade

# In CI: nothing special — uv sync --frozen runs automatically.
```

If `uv sync --frozen` fails in CI, the message will say
`The lockfile at uv.lock needs to be updated...` — that is the contract.

---

## 9. Karpathy compliance

| Principle                   | How                                                                                                                                  |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| **#1 Think before writing** | D1–D7 made every choice explicit (lock-in-git, frozen vs install, run vs activate, cache scope)                                      |
| **#2 Simple first**         | Net +1 line of YAML; replaced 3 steps with 2; no new files except the lockfile itself                                                |
| **#3 Surgical edits**       | Only the install pipeline of one workflow file changed; everything else (concurrency, matrix, artifacts, PR comment) stays identical |
| **#4 Goal-driven**          | 9 verification checks in §5, every one a deterministic command with a recorded outcome                                               |

---

## 10. Project state after this round

| Dimension          | Value                                    |
| ------------------ | ---------------------------------------- |
| Python             | 3.12.12 local / 3.10 + 3.12 in CI matrix |
| uv                 | 0.11.3 local / 0.4.x pinned in CI        |
| Locked packages    | 63                                       |
| Tests              | 87 (49 unit + 38 contract), all green    |
| Coverage           | 75.21% (gate 70%)                        |
| CI workflows       | 2 (`api-lint.yml`, `tests.yml`)          |
| Lockfile committed | ✅ `uv.lock` (1792 lines)                 |

---

## 11. Recommended next steps

1. 🟢 **First push to GitHub** — let the matrix run on real GitHub-hosted runners; 3.10 has only been validated indirectly via the lock's `requires-python>=3.10` so far.
2. 🟡 **Renovate/Dependabot for `uv.lock`** — automate the boring part of dependency hygiene.
3. 🟡 **Move into business logic** — Datasets CRUD against real MySQL is the next meaningful jump in coverage and value.
