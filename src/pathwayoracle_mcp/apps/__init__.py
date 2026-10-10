"""
MCP Apps: interactive panels the client renders inline (`io.modelcontextprotocol/ui`).

  show_evidence        model-visible. Takes ids (claims with cite ids), builds a
                       view from the session's evidence ledger, returns ONE line
                       plus a view id. Clients that render MCP Apps get only that;
                       clients that don't also get a compact Mermaid diagram and a
                       link to the standalone page (Phase 4 fallback).
  evidence_view_data   app-only (visibility ["app"]): the panel fetches the full
                       view by id through the host. Hosts do not show app-only
                       tools to the model, so the graph never costs the model
                       tokens.

The panel loads Cytoscape.js (cdnjs) and WebCola + cytoscape-cola (jsDelivr)
for live physics; both origins are declared in the resource CSP. The same HTML
also works as a standalone page (served at /evidence/<session>/<view>), with
the view embedded instead of fetched from the host.
"""
from __future__ import annotations

import logging
from importlib.resources import files
from typing import TYPE_CHECKING, Any, Literal

from mcp.server.apps import Apps, ResourceCsp, client_supports_apps
from mcp.server.mcpserver import Context

from ..evidence import store
from ..evidence.view import to_mermaid

if TYPE_CHECKING:
    from ..dispatch import Toolkit

log = logging.getLogger(__name__)

EVIDENCE_URI = "ui://pathwayoracle/evidence.html"
CSP = ResourceCsp(resource_domains=["https://cdnjs.cloudflare.com", "https://cdn.jsdelivr.net"])


def panel_html() -> str:
    return files(__package__).joinpath("evidence.html").read_text(encoding="utf-8")


def standalone_page(view: dict, title: str) -> str:
    """
    The panel as a self-contained page: the view is embedded, so it draws with
    no MCP host (the HTTP route, and exported examples). `</` is escaped so
    evidence text can never close the script element.
    """
    import html
    import json

    data = json.dumps(view, default=str).replace("</", r"<\/")
    page = panel_html().replace("<script>", f"<script>window.__EVIDENCE__ = {data};</script>\n<script>", 1)
    return page.replace("<title>Evidence</title>", f"<title>{html.escape(title)}</title>", 1)


def _session(ctx: Context) -> str | None:
    try:
        return ctx.request_context.session_id     # HTTP; None on stdio (process = session)
    except Exception:                              # noqa: BLE001
        return None


def _supports_apps(ctx: Context) -> bool:
    try:
        return client_supports_apps(ctx)
    except Exception:                              # noqa: BLE001
        return False


def build_apps(toolkit: "Toolkit") -> Apps:
    apps = Apps()
    spec = toolkit.registry.tools.get("show_evidence")

    @apps.tool(resource_uri=EVIDENCE_URI, name="show_evidence", title="Show the evidence",
               description=spec.description if spec else "Show the evidence behind your answer.")
    def show_evidence(ctx: Context,
                      # Plain list defaults, not `| None`: each Optional adds an
                      # anyOf to the schema the model pays for at every startup.
                      claims: list[dict] = [],              # noqa: B006
                      include: list[str] = [],              # noqa: B006
                      scope: Literal["claims", "question", "session"] = "claims",
                      neighbors: int = 0,
                      collapse: list[dict] = [],            # noqa: B006
                      max_nodes: int = 40,
                      themes: list[dict] = []) -> dict[str, Any]:        # noqa: B006
        session = _session(ctx)
        # The normal dispatch path: validated, traced, and recorded like any call.
        result = toolkit.call("show_evidence",
                              {"claims": list(claims), "include": list(include) or None, "scope": scope,
                               "neighbors": neighbors, "collapse": list(collapse) or None,
                               "max_nodes": max_nodes, "themes": list(themes)},
                              via="listed", session=session)
        if not _supports_apps(ctx):
            # No panel in this client: hand the model something it can show the
            # user. Costs tokens, so only clients without Apps pay it.
            lg = toolkit.ledger(session)
            view = lg.views.get(result["view_id"])
            if view is not None:
                result["mermaid"] = to_mermaid(view)
                result["url"] = store.public_url(lg.session, result["view_id"])
        return result

    @apps.tool(resource_uri=EVIDENCE_URI, name="evidence_view_data", visibility=["app"],
               title="Evidence view data (panel only)",
               description="Full data of an evidence view, for the panel. Not for the model.")
    def evidence_view_data(ctx: Context, view_id: str) -> dict[str, Any]:
        lg = toolkit.ledger(_session(ctx))
        view = lg.views.get(view_id)
        if view is None:
            return {"error": f"no view {view_id} in this session"}
        return {**view, "standalone_url": store.public_url(lg.session, view_id)}

    apps.add_html_resource(EVIDENCE_URI, panel_html(), name="evidence", title="Evidence",
                           csp=CSP, prefers_border=True)
    return apps
