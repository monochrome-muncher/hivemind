"""Hivemind REST surface: the FastAPI app, schemas, and entry point.

Modules:

* ``schemas`` — pydantic request/response models (SPEC.md §5.1)
* ``deps`` — the ``HivemindApp`` bundle, the FastAPI factory, and the
  auth dependency (SPEC.md §8.1)
* ``routes`` — the /v1 endpoints (thin: validation + service calls)
* ``main`` — the production entry point (uvicorn)
"""
