# LCP control-plane image -- minimal, fast-build.
#
# Single image used by three workloads (api / planner / worker); the entry
# command is selected via the k8s manifest, not at build time.
#
# Why no build-essential / apt-get install:
# - python:3.12-slim already has the C libs aiomysql + cryptography need
#   at runtime;
# - cryptography ships arm64 manylinux2014 wheels for Py3.12, no compile;
# - skipping apt-get drops ~3 minutes from the build on slow networks.

FROM python:3.12-slim AS runtime

# Pip / uv index mirrors -- the default PyPI is unreliable from Colima
# in mainland China.  Tsinghua's TUNA is a complete pypi mirror; uv
# respects UV_INDEX_URL.  These are dev-only conveniences; remove for
# production builds where you want fully reproducible upstream pulls.
ENV UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

# uv -- chosen consistently with the project's local toolchain.# pip here (not uv pip install --no-binary) is intentional: pip's wheel
# resolver gracefully falls back to compiled wheels if a sdist-only dep
# appears later.
RUN pip install --no-cache-dir uv==0.9.6

WORKDIR /app

# Copy first the dependency manifests so a pure source-change rebuild
# skips the heavy install layer (Docker layer caching).
COPY pyproject.toml uv.lock README-skeleton.md ./

# Slice 4 / B.5: opt-in real-model embedding extras (sentence-transformers
# + transformers + torch CPU).  Adds ~500 MB to the image, so default off:
# build with `docker build --build-arg ENABLE_EMBEDDING=true -t lcp:dev .`
# when the worker pod must run a real model.  The base image (default
# build) still works -- VECTORIZE tasks with model_name='mock' do not
# need the extras, and tasks with a real model_name fail loudly with
# SentenceTransformersNotInstalledError so the operator sees exactly
# what is missing.
ARG ENABLE_EMBEDDING=false

# Install runtime deps only (NO dev extras: pytest etc. don't need to
# ship to k8s).  Two stages so a transitive dep change cannot invalidate
# the source layer.  When ENABLE_EMBEDDING=true, fold the [embedding]
# extras into the same export so torch / transformers land in the same
# layer (kept as one RUN to avoid a wasted layer when the flag is off).
RUN if [ "$ENABLE_EMBEDDING" = "true" ]; then \
    EXTRA_FLAG="--extra embedding"; \
    else \
    EXTRA_FLAG=""; \
    fi \
    && uv export --no-dev --frozen --no-emit-project $EXTRA_FLAG --format requirements-txt --output-file /tmp/requirements.txt \
    && uv pip install --system --no-cache --requirement /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Now copy source and install the project itself.
COPY src ./src
RUN uv pip install --system --no-cache --no-deps .

# In-cluster probe script.  Only this single file from scripts/ -- the
# rest of scripts/ is dev-only (local smoke tests, MinIO seeders) and
# must NOT ship to k8s, otherwise an operator might `kubectl exec` and
# accidentally run a script that targets localhost.  Whitelist, don't
# blanket-copy.  See deploy/k8s/99-probe-gravitino-job.yaml for usage.
COPY scripts/probe_gravitino.py ./scripts/probe_gravitino.py

# Defense-in-depth: run as non-root.
RUN useradd --create-home --uid 10001 lcp
USER lcp

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 8080
# Default to the REST API; planner / worker manifests override this.
# Using the console script `lcp-rest` so the container picks up
# LCP_REST_HOST / LCP_REST_PORT from env -- the manifest controls binding,
# the image stays generic.
CMD ["lcp-rest"]
