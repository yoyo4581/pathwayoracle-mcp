"""
The MCP server: transport, plus registration of the toolkit's listed tools.

What the tools are is not written here. It is read from the toolkit registry
-- (:Toolkit) nodes in Neo4j, loaded from registry/toolkit.yaml -- at startup:

  * tools marked `core` are registered with MCP, so they appear in the tool
    list with full definitions;
  * every other tool is reachable through discover_tools + execute_tool;
  * every call, either way, goes through dispatch.Toolkit.call (validate ->
    run -> cite -> trace).

So adding a stored-Cypher tool is a registry change, not a server change.

Identity: tools speak domain ids -- UniProt accessions, Reactome R-HSA ids,
GO ids, PMIDs -- and every result can be fed straight into the next tool.
Evidence items carry `cite` ids and results carry `release`; see dispatch.py.

Run it
------
    QDRANT_URL=http://localhost:6333 NEO4J_URI=bolt://localhost:7687 \
    BIOBERT_MODEL_PATH=yoyo458/BioBERT_GOUniProt_FineTuned \
        pathwayoracle-mcp

MCP_TRANSPORT picks the transport: `stdio` (default, what Claude Desktop
spawns) or `streamable-http` (one long-lived process shared by every client,
which is what the agent layer wants -- the encoder costs ~10 s and ~1 GB to
load, and stdio pays that per client session). See main().

Originally ported from POKnowledgeBase_ETL src/builders/Qdrant/mcp_server.py at
a1d45dd; the five retrieval tools keep their names, arguments and behaviour
(handlers/retrieval.py), so the backend's evaluation still measures them
through this server.
"""
from __future__ import annotations

import inspect
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from .clients import Clients, make_retriever                    # noqa: F401  (re-exported)
from .core.config import Settings
from .dispatch import Toolkit
from .registry.runtime import Registry, ToolSpec

if TYPE_CHECKING:
    from .core.retriever import Retriever

# The SDK renamed FastMCP to MCPServer in 2.0. The decorator API, the
# `instructions` argument and run(transport=...) are the same in both, so one
# import shim covers the whole file.
try:
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver import MCPServer as _Server   # mcp >= 2
    from mcp.server.mcpserver.exceptions import ToolError
    MCP_MAJOR = 2
except ImportError:                                         # pragma: no cover
    from mcp.server.fastmcp import Context
    from mcp.server.fastmcp import FastMCP as _Server       # mcp 1.x
    from mcp.server.fastmcp.exceptions import ToolError
    MCP_MAJOR = 1

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Biomedical knowledge toolkit over three linked sources, all keyed by shared ids:
  - a Reactome + Gene Ontology knowledge graph (genes, pathways, reactions,
    complexes, GO terms, drugs, compounds),
  - UniProt protein function text, searchable semantically,
  - PubMed abstracts.
Gene ids are UniProt accessions (P04637) everywhere: the graph's Gene.id is the
key into the function text.

START EVERY QUESTION WITH plan: classify the question as a recipe
(fine_grained, top_down, bottom_up, gene_set, mechanistic) and call
plan(recipe). It returns the steps such questions usually need, what each step
takes from the previous one, and what is out of scope -- take the steps your
answer needs. Tools it names that are not in your list are called through
execute_tool.

Steps are of four kinds:
  ANCHOR      text -> ids         find_entities (names), search_proteins (functions)
  TRAVERSE    ids -> more ids     pathway, interaction, reaction and ontology tools;
                                  describe_node and expand for ad-hoc moves
  ENRICH      ids -> evidence     read_snippets, snippet_sources, read_abstracts, GO annotations
  SYNTHESIZE  findings -> shape   ways the graph can group what you found (plan lists them)

Results state their `coverage`: what they settle and what they do not. Trust a
result for what its coverage claims; follow its pointer for what it does not.
Free-form Cypher (read_cypher) is a last resort, for a part of the question no
tool can answer.

Answer the question, not the evidence: lead with what the findings add up to,
then the evidence that carries it. Detail that does not change the answer can
stay out unless the user asks for it. When you group findings, say which
groupings come from a tool (and cite them) and which are your own.

Evidence items carry a `cite` id; cite what each claim rests on.
resolve_citations turns ids back into the evidence.
"""


def _model_facing_error(exc: Exception) -> Exception:
    """
    The SDK shows the model an exception's message only if it is a ToolError;
    anything else becomes "Error executing tool X" -- measured: every
    actionable message (unknown tool, read-only, bad arguments, no such record)
    was reaching the model as that one line. Known, safe error types are
    re-raised as ToolError with a clean message so the model can correct
    course. Anything unexpected stays generic -- its text may carry internals
    (hosts, paths) -- and is logged here instead.
    """
    from pydantic import ValidationError

    from .dispatch import ToolkitError

    if isinstance(exc, ValidationError):
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}"
                             for e in exc.errors(include_url=False))
        return ToolError(f"Invalid arguments -- {problems}")
    if isinstance(exc, (ToolkitError, LookupError, ValueError)):
        return ToolError(str(exc))
    if isinstance(exc, RuntimeError) and "not configured" in str(exc):
        return ToolError(str(exc))
    log.exception("unexpected tool failure")
    return ToolError(f"Internal error ({type(exc).__name__}); the failure was logged on the server.")


def _listed_function(toolkit: Toolkit, spec: ToolSpec):
    """
    A function whose signature IS the registry's parameter schema, so MCP
    derives the listed inputSchema and validates arguments exactly as the
    registry says. The body just forwards to the dispatch path.
    """
    def tool(ctx: Context, **kwargs) -> dict[str, Any]:
        session, headers = None, None
        try:
            session = ctx.request_context.session_id      # HTTP; None on stdio
            headers = ctx.headers                          # may carry a W3C traceparent
        except Exception:                                  # noqa: BLE001  (no request context)
            pass
        try:
            return toolkit.call(spec.name, kwargs, via="listed", session=session,
                                carrier=dict(headers) if headers else None)
        except ToolError:
            raise
        except Exception as exc:                            # noqa: BLE001
            raise _model_facing_error(exc) from None

    # The SDK injects the request Context into the parameter annotated with it
    # and leaves it out of the listed schema.
    sig = spec.signature()
    sig = sig.replace(parameters=[*sig.parameters.values(),
                                  inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY,
                                                    annotation=Context)])
    tool.__name__ = spec.name
    tool.__doc__ = spec.description
    tool.__signature__ = sig
    tool.__annotations__ = {p.name: p.annotation for p in sig.parameters.values()}
    tool.__annotations__["return"] = dict[str, Any]
    return tool


def _add_evidence_routes(mcp) -> None:
    """
    Standalone evidence pages, served by the HTTP transport (Phase 4 fallback):

      GET /evidence/{session}/{view_id}        the panel, with the view embedded
      GET /evidence/{session}/{view_id}.json   the view itself

    Views are written to the shared cache volume by whichever process built
    them (a Claude Desktop stdio session included), so this long-lived service
    can serve them. Only routes on the HTTP transport; bound to loopback.
    """
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, Response

    from .apps import standalone_page
    from .evidence import store

    @mcp.custom_route("/evidence/{session}/{view_id}", methods=["GET"], include_in_schema=False)
    async def evidence_page(request: Request) -> Response:
        session = request.path_params["session"]
        view_id = request.path_params["view_id"]
        as_json = view_id.endswith(".json")
        view = store.load(session, view_id.removesuffix(".json"))
        if view is None:
            return JSONResponse({"error": "no such view"}, status_code=404)
        if as_json:
            return JSONResponse(view)
        return HTMLResponse(standalone_page(view, f"Evidence {view_id} - {session}"))


def build_server(retriever: "Retriever | None" = None,
                 settings: Settings | None = None,
                 registry: Registry | None = None):
    """
    A fresh server. `retriever` lets the backend's evaluation share one loaded
    encoder across a sweep; otherwise everything is opened lazily on first use.

    The registry is read here (from Neo4j, falling back to the bundled YAML),
    because the tool list has to exist before the first call. That costs one
    small Neo4j query, never the encoder.
    """
    clients = Clients(settings=settings, retriever=retriever)
    registry = registry or Registry.load(clients)
    toolkit = Toolkit(clients, registry)
    log.info("toolkit: %d tools (%d listed) from %s, KG release %s",
             len(registry.tools), len(registry.core), registry.source, registry.kg_release)

    from .apps import build_apps

    # Server-driven recall: recent episodes of earlier chats go in the
    # instructions, which the client puts in front of the model at connection
    # start -- the model sees them without asking (docs/memory.md, section 6).
    digest = toolkit.memory.digest(exclude_session=toolkit.ledger().session)
    instructions = INSTRUCTIONS + ("\n" + digest + "\n" if digest else "")
    mcp = _Server("pathwayoracle", instructions=instructions, extensions=[build_apps(toolkit)])
    _add_evidence_routes(mcp)
    # plan's description lists the recipes, read from the registry, so the
    # model sees what it is classifying into before its first call.
    recipes = "; ".join(f"{r['name']} = {r['description']}" for r in registry.recipes
                        if (r.get("stage") or "gather") == "gather")
    for spec in registry.core:
        mcp.add_tool(_listed_function(toolkit, spec), name=spec.name, title=spec.title,
                     description=spec.description.replace("{recipes}", recipes))
    mcp.toolkit = toolkit                       # for tests and in-process callers
    return mcp


def is_error(result) -> bool:
    """1.x spells it isError, 2.x spells it is_error."""
    return bool(getattr(result, "is_error", False) or getattr(result, "isError", False))


def unwrap(result) -> dict:
    """
    An MCP tool result -> the dict the tool returned.

    Structured content when the SDK provides it (1.x camelCase, 2.x snake_case),
    otherwise the JSON text block. Every tool returns a single object precisely
    so this stays unambiguous: a list return is serialized as one content block
    per element, and a one-element list is then indistinguishable from a single
    object.
    """
    for attr in ("structured_content", "structuredContent"):
        structured = getattr(result, attr, None)
        if isinstance(structured, dict):
            return structured.get("result", structured)

    blocks = [b for b in (getattr(result, "content", None) or []) if getattr(b, "text", None)]
    if not blocks:
        return {}
    if len(blocks) > 1:
        raise ValueError(f"Expected one content block from the tool, got {len(blocks)} -- "
                         f"a tool is returning a bare list instead of an object.")
    return json.loads(blocks[0].text)


@asynccontextmanager
async def open_session(server):
    """
    A connected client for `server` over the SDK's in-memory transport: real
    tool schemas, real argument validation, real JSON round-trip -- just no
    subprocess and no pipe. Yields an object exposing list_tools() and
    call_tool(), which both SDK generations provide.
    """
    if MCP_MAJOR >= 2:
        from mcp.client import Client
        async with Client(server) as client:
            yield client
    else:                                                   # pragma: no cover
        from mcp.shared.memory import create_connected_server_and_client_session
        async with create_connected_server_and_client_session(server._mcp_server) as session:
            yield session


TRANSPORTS = ("stdio", "streamable-http")

# Host headers the HTTP transport accepts. Inside a container the server has to
# bind 0.0.0.0, and the SDK only switches on DNS-rebinding protection by itself
# when bound to loopback -- so without this list, any web page could reach the
# published port through a rebound hostname. `mcp` is the compose service name,
# for callers on the compose network.
DEFAULT_ALLOWED_HOSTS = "localhost:*,127.0.0.1:*,[::1]:*,mcp:*"


def main() -> None:
    """
    Entry point. Configured entirely from the environment, because both callers
    -- a Claude Desktop config entry and a compose file -- set env, not argv.

      MCP_TRANSPORT      stdio (default) | streamable-http
      MCP_HOST           bind address for HTTP (default 127.0.0.1)
      MCP_PORT           default 8000; the endpoint is /mcp
      MCP_ALLOWED_HOSTS  comma-separated Host header patterns for HTTP
    """
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport not in TRANSPORTS:
        raise SystemExit(f"MCP_TRANSPORT={transport!r}; expected one of {TRANSPORTS}.")

    server = build_server()
    if transport == "stdio":
        server.run(transport="stdio")
        return

    from mcp.server.transport_security import TransportSecuritySettings

    hosts = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", DEFAULT_ALLOWED_HOSTS).split(",")
             if h.strip()]
    server.run(
        transport="streamable-http",
        host=os.environ.get("MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("MCP_PORT", 8000)),
        transport_security=TransportSecuritySettings(
            allowed_hosts=hosts,
            allowed_origins=[f"http://{h}" for h in hosts],
        ),
    )


if __name__ == "__main__":
    main()
