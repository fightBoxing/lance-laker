"""Data-plane integration layer.

This package is the **only** place in the codebase that imports the
``lance`` python wheel.  Control-plane modules (services, workers,
schedulers) MUST NOT ``import lance`` directly; instead, they call into
:mod:`lcp.data_plane.lance_io`.

Why the indirection
-------------------
1. ``pylance`` is an *optional* extra (see ``pyproject.toml``): unit
   tests and lightweight environments can run without it.  Funnelling
   every call through one module lets us monkey-patch ``lance_io`` in
   tests without installing the native wheel.
2. The lance python API is still pre-1.0.  Pinning a single import site
   means future API renames cost one diff, not N.
3. Future swap to ``lance-rs`` over gRPC, or to a connection pool,
   touches only this package.
"""
