"""
Views on disk, so the HTTP service can serve them as standalone pages.

A Claude Desktop session runs its own stdio server process, but the standalone
page (/evidence/<session>/<view>) is served by the long-lived HTTP service --
a different process. Both mount the same cache volume, so show_evidence writes
each view there and the HTTP route reads it back. Best effort: a view that
cannot be written still works in the panel.

    PATHWAYORACLE_EVIDENCE_DIR   default /cache/evidence (compose volume), else
                                 a temp directory
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)
# No dots: session ids (stdio-<hex>, MCP session uuids) and view ids (v<n>) never
# contain them, and allowing them let ".." through (caught by the tests).
_SAFE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def evidence_dir() -> Path:
    configured = os.environ.get("PATHWAYORACLE_EVIDENCE_DIR")
    if configured:
        return Path(configured)
    return Path("/cache/evidence") if Path("/cache").is_dir() else Path(tempfile.gettempdir()) / "pathwayoracle-evidence"


def _path(session: str, view_id: str) -> Path:
    if not (_SAFE.match(session) and _SAFE.match(view_id)):
        raise ValueError("invalid session or view id")          # no path traversal
    root = evidence_dir().resolve()
    path = (root / session / f"{view_id}.json").resolve()
    if root not in path.parents:                                 # belt and braces
        raise ValueError("view path escapes the evidence directory")
    return path


def save(session: str, view_id: str, view: dict) -> Path | None:
    try:
        path = _path(session, view_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(view, default=str), encoding="utf-8")
        tmp.replace(path)
        return path
    except Exception:                                             # noqa: BLE001
        log.warning("could not persist evidence view %s/%s", session, view_id, exc_info=True)
        return None


def load(session: str, view_id: str) -> dict | None:
    try:
        path = _path(session, view_id)
    except ValueError:
        return None
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def public_url(session: str, view_id: str) -> str:
    base = os.environ.get("PATHWAYORACLE_PUBLIC_URL", "http://localhost:8000").rstrip("/")
    return f"{base}/evidence/{session}/{view_id}"
