"""Backwards-compat shim for ``client.web``.

Historically every route, template string, and helper lived in this
module. The real code now lives in ``client.app`` and the
``client.routes`` / ``client.services`` / ``client.templates`` /
``client.static`` trees — see ``client/app.py`` for the entry point.

This shim exists so ``uvicorn client.web:app`` (the Dockerfile CMD) and
the smoke-test imports of ``client.web.app`` / ``SESSION_COOKIE`` keep
working without anyone having to update them.
"""
from client.app import app
from client.services.coordinator_client import _client, _public_client
from client.services.session import SESSION_COOKIE

__all__ = ["app", "_client", "_public_client", "SESSION_COOKIE"]
