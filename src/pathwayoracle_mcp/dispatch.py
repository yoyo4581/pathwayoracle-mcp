"""
The single path every tool call takes: validate -> run -> cite -> trace.

Listed tools and tools run through execute_tool both land in Toolkit.call, so
validation, citation ids and tracing are uniform no matter how the model
reached a tool. Implementations never see unvalidated arguments and never have
to remember to cite or trace.

Citations: every evidence item in a result carries `cite`, a stable id built
from domain identifiers (never Neo4j element ids, which change between
releases). Stored queries get them from the `cite` template in the registry;
Python handlers set them themselves. Every result also carries `release`, the
KG snapshot the evidence came from, so a citation stays resolvable.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Mapping

from .clients import Clients
from .registry.runtime import Registry, ToolSpec
from .evidence import Ledgers, ingest
from .evidence.consolidate import build_episode, compact
from .evidence.memory import MemoryGraph
from .tracing import PROCESS_SESSION, Tracer, ToolCall, tracer_from_env

log = logging.getLogger(__name__)

# Read-only Cypher results are capped so one call cannot flood the context.
MAX_ROWS = 200

# Memory (docs/memory.md). An idle gap this long between calls opens a new
# exchange (measured: within a response <= 36 s, between exchanges >= 208 s).
EXCHANGE_GAP = float(os.environ.get("PATHWAYORACLE_EXCHANGE_GAP", 90))
# Result tokens the model saw, across live exchanges, before older ones are compacted.
LEDGER_BUDGET = int(os.environ.get("PATHWAYORACLE_LEDGER_BUDGET", 60_000))
KEEP_EXCHANGES = 3
# Tools whose results get no memory pointers: the meta and memory tools themselves.
NO_HINTS = {"plan", "discover_tools", "execute_tool", "show_evidence", "evidence_index",
            "recall", "recall_episode", "resolve_citations", "describe_schema", "collection_info"}


class ToolkitError(Exception):
    """A problem the model can act on: unknown tool, bad arguments, empty store."""


def _fmt(template: str, ctx: dict) -> str:
    class Ctx(dict):
        def __missing__(self, key):
            return "?"
    flat = {k: ",".join(map(str, v)) if isinstance(v, list) else v for k, v in ctx.items()}
    return template.format_map(Ctx(flat))


def strip_private(result: Any) -> Any:
    """
    The result without its ledger-only fields: keys starting with `_`, at any
    depth. A tool can return evidence too large for the model's context (every
    member of a 400-gene set) under such a key; the ledger records it, the
    model never sees it.
    """
    if isinstance(result, dict):
        return {k: strip_private(v) for k, v in result.items() if not str(k).startswith("_")}
    if isinstance(result, list):
        return [strip_private(v) for v in result]
    return result


def collect_citations(result: Any) -> list[str]:
    """Every `cite` value anywhere in a result, in order."""
    out: list[str] = []
    if isinstance(result, dict):
        if isinstance(result.get("cite"), str):
            out.append(result["cite"])
        for v in result.values():
            out += collect_citations(v)
    elif isinstance(result, list):
        for v in result:
            out += collect_citations(v)
    return out


class Toolkit:
    def __init__(self, clients: Clients, registry: Registry, tracer: Tracer | None = None,
                 memory: MemoryGraph | None = None):
        self.clients = clients
        self.registry = registry
        self.tracer = tracer or tracer_from_env()
        # Long-term memory: every chat's episodes, saved at every call.
        self.memory = memory or MemoryGraph()
        # Evidence ledgers, one per MCP session: every result is recorded here
        # so the evidence view can later show it by id (docs/ROADMAP.md).
        self.ledgers = Ledgers(release=registry.kg_release)
        self._active = threading.local()         # stack of in-flight ToolCall records

    def attach(self, key: str, value) -> None:
        """Attach observability data to the current call's span (not its result)."""
        stack = getattr(self._active, "stack", None)
        if stack:
            stack[-1].attachments[key] = value

    def spec(self, name: str) -> ToolSpec:
        try:
            return self.registry.tools[name]
        except KeyError:
            close = [t for t in self.registry.tools if name.split("_")[0] in t][:5]
            raise ToolkitError(f"No tool named {name!r}. "
                               f"{'Similar: ' + ', '.join(close) + '. ' if close else ''}"
                               f"Use discover_tools to search.") from None

    def call(self, name: str, arguments: dict | None = None, via: str = "listed",
             session: str | None = None, carrier: Mapping[str, str] | None = None,
             private: bool = False) -> dict:
        """
        session  groups calls in the trace backend (MCP session; see tracing.py)
        carrier  request headers; a W3C traceparent in them parents this call's
                 span under the caller's trace
        private  also return the ledger-only fields (see strip_private) -- for
                 in-process callers and fixtures, never for the model
        """
        spec = self.spec(name)
        if session:
            self._session = session                 # nested calls inherit it
        record = ToolCall(tool=name, arguments=dict(arguments or {}), via=via,
                          kg_release=self.registry.kg_release,
                          session=session or getattr(self, "_session", None) or PROCESS_SESSION)
        full = None                                 # the result with ledger-only fields
        with self.tracer.span(record, carrier):
            start = time.perf_counter()
            stack = self._active.__dict__.setdefault("stack", [])
            stack.append(record)
            try:
                args = spec.validate(arguments)
                record.arguments = args
                full = self._run(spec, args)
                if spec.cite and spec.kind == "cypher":
                    self._cite(spec, full)
                if spec.coverage:
                    # Stated completeness, so the model can judge the result
                    # from the result -- not by auditing it with more queries.
                    full["coverage"] = _fmt(spec.coverage, full)
                full["release"] = self.registry.kg_release
                result = strip_private(full)
                record.ok, record.result = True, result   # traced as the model sees it
                record.citations = collect_citations(result)
                record.n_items = len(record.citations)
                record.ms = round((time.perf_counter() - start) * 1000, 1)
                call = self._record_evidence(spec.name, record, full)
                # Server-driven recall: entities earlier chats worked on.
                hints = self._memory_hints(spec.name, record, call)
                if hints:
                    result["memory"] = full["memory"] = hints
                return full if private else result
            except Exception as exc:
                record.ok, record.error = False, f"{type(exc).__name__}: {exc}"
                record.ms = round((time.perf_counter() - start) * 1000, 1)
                self._record_evidence(spec.name, record)
                raise
            finally:
                stack.pop()

    def ledger(self, session: str | None = None):
        """The evidence ledger of a session (default: the current one)."""
        return self.ledgers.get(session or getattr(self, "_session", None) or PROCESS_SESSION)

    def _record_evidence(self, tool: str, record: ToolCall, full: dict | None = None) -> str | None:
        """
        Every call becomes a call node (failures too -- they are part of the
        exploration trail); successful results are ingested, ledger-only
        fields included (`full`). execute_tool is skipped: the tool it runs is
        recorded as its own call. Like tracing, recording must never break the
        call it observes.

        Then memory (docs/memory.md): a plan() call or an idle gap opens a new
        exchange; the result's size is counted as tokens the model saw; the
        chat's episodes are saved (so a killed connection loses nothing); and
        over budget, exchanges older than the last 3 are compacted.
        """
        if tool == "execute_tool":
            return None
        try:
            lg = self.ledger(record.session)
            if tool == "plan" and record.ok:
                recipe = record.arguments.get("recipe")
                # Several plans before any other call (often sent in parallel)
                # are one response classifying one question: one exchange.
                if lg.idle_for(record.started) <= EXCHANGE_GAP and lg.only_planned():
                    lg.add_recipe(recipe)
                else:
                    lg.begin_question(recipe, reason="plan")
            elif lg.last_end is not None and lg.idle_for(record.started) > EXCHANGE_GAP:
                lg.begin_question(None, reason="idle gap")
            call = lg.record_call(tool, record.arguments, bool(record.ok), record.ms, record.error)
            if record.ok and record.result is not None:
                ingest(lg, tool, record.arguments, full if full is not None else record.result, call)
                lg.tokens[lg.question] += len(json.dumps(record.result, default=str)) // 4
        except Exception:                                   # noqa: BLE001
            log.warning("evidence ledger: could not record %s", tool, exc_info=True)
            return None
        try:
            self._save_memory(lg)
            compact(lg, LEDGER_BUDGET, KEEP_EXCHANGES)
        except Exception:                                   # noqa: BLE001
            log.warning("memory: could not consolidate %s", lg.session, exc_info=True)
        return call

    def _save_memory(self, lg) -> None:
        """Rebuild the open exchange's episode (and any not built yet); rewrite the chat's file.
        An exchange that only used memory tools has no episode (None): nothing of its own to save."""
        for n in range(0, lg.question + 1):
            if n in lg.compacted or (n in lg.episodes and n != lg.question):
                continue                                    # frozen: closed or compacted
            if lg.calls_of(n):
                lg.episodes[n] = build_episode(lg, n)
        self.memory.save_session(lg.session, [e for _, e in sorted(lg.episodes.items()) if e is not None])

    def _memory_hints(self, tool: str, record: ToolCall, call: str | None) -> list[str] | None:
        if tool in NO_HINTS or not call:
            return None
        try:
            lg = self.ledger(record.session)
            ns = ("q", lg.question, "call", int(call.split(":")[1]))
            ids = sorted(n for n in lg.in_namespace(ns) if (lg.node(n) or {}).get("kind") == "entity")
            lines = self.memory.hints(ids, lg.session, lg.hinted)
            if lines:
                return ["Earlier chats worked on these (recall_episode(id) loads one):", *lines]
        except Exception:                                   # noqa: BLE001
            log.warning("memory: hints failed for %s", tool, exc_info=True)
        return None

    def _run(self, spec: ToolSpec, args: dict) -> dict:
        if spec.kind == "cypher":
            rows, truncated = self.clients.read(spec.cypher, args, max_rows=MAX_ROWS)
            if spec.result == "one":
                if not rows:
                    raise ToolkitError(f"{spec.name}: nothing found for {args}.")
                return rows[0]
            return {"n_results": len(rows), "truncated": truncated, "results": rows}
        from .handlers import HANDLERS
        try:
            fn = HANDLERS[spec.handler]
        except KeyError:
            raise ToolkitError(f"{spec.name} is described in the registry but not implemented "
                               f"in this server version (handler {spec.handler!r}).") from None
        return fn(self, **args)

    @staticmethod
    def _cite(spec: ToolSpec, result: dict) -> None:
        template, key = spec.cite["template"], spec.cite.get("items")
        if key:                                    # nested items inside one object
            for item in result.get(key) or []:
                item["cite"] = _fmt(template, {**result, **item})
        else:                                      # one citation per row
            for row in result.get("results", [result]):
                row["cite"] = _fmt(template, row)
