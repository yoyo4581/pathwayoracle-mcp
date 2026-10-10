"""
One span per tool call -- the hook observability attaches to.

Every call, listed or run through execute_tool, passes dispatch.Toolkit.call,
which wraps it in Tracer.span(). What a span records is what provenance needs:
the tool, its validated arguments, the citation ids it returned, the KG
release, timing, and the error if any -- so an answer can be traced back to
the calls whose citations it used.

Backends are opt-in, never inferred from credentials merely being present:

    PATHWAYORACLE_TRACING=langfuse   OpenTelemetry -> Langfuse's OTLP endpoint,
                                     using LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY /
                                     LANGFUSE_SECRET_KEY
    PATHWAYORACLE_TRACING=otlp       OpenTelemetry -> any OTLP collector, from the
                                     standard OTEL_EXPORTER_OTLP_* variables
                                     (LangSmith accepts OTLP too)
    PATHWAYORACLE_TRACE_FILE=path    also append one JSON line per call
    (neither)                        no tracing

Spans nest through OpenTelemetry's context, so a tool run via execute_tool is a
child of the execute_tool span. Over HTTP, a W3C `traceparent` header from the
caller (e.g. DeepAgents) makes the server's spans children of the caller's
trace -- one trace across both layers. Spans are grouped into Langfuse
sessions by MCP session: the transport's session id over HTTP, one id per
process over stdio (where a process serves exactly one client session).

Never writes to stdout: under the stdio transport stdout IS the protocol.
"""
from __future__ import annotations

import atexit
import base64
import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator, Mapping, Protocol

log = logging.getLogger(__name__)

# Over stdio a process is one client session; give it an id spans can share.
# PATHWAYORACLE_SESSION overrides it, so a caller that launches the process --
# the eval harness, one process per question -- can name the session and find
# its spans again.
PROCESS_SESSION = os.environ.get("PATHWAYORACLE_SESSION") or f"stdio-{uuid.uuid4().hex[:12]}"
OUTPUT_LIMIT = 16_000           # characters of result JSON attached to a span
ATTACHMENT_LIMIT = 200_000      # characters per metadata attachment (an evidence view)


@dataclass
class ToolCall:
    tool: str
    arguments: dict[str, Any]
    via: str                                   # "listed" | "execute_tool"
    kg_release: str | None
    session: str | None = None
    started: float = field(default_factory=time.time)
    ms: float | None = None
    ok: bool | None = None
    error: str | None = None
    citations: list[str] = field(default_factory=list)
    n_items: int | None = None
    result: dict | None = field(default=None, repr=False)
    # Extra data a handler attaches for observability only (e.g. the full
    # evidence view): goes to span METADATA, never into the result the model sees.
    attachments: dict = field(default_factory=dict, repr=False)


class Tracer(Protocol):
    def span(self, call: ToolCall, carrier: Mapping[str, str] | None = None
             ) -> "contextmanager[ToolCall]": ...


class NullTracer:
    @contextmanager
    def span(self, call, carrier=None) -> Iterator[ToolCall]:
        yield call


class JsonlTracer:
    """
    Appends one line per call. Creates its directory, and a failed write is a
    logged warning, never a failed tool call: observability must not break the
    thing it observes. (Measured the hard way: a named volume created before
    the image had /cache/traces lacked the directory, and every tool call
    failed with FileNotFoundError.)
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._warned = False

    @contextmanager
    def span(self, call, carrier=None) -> Iterator[ToolCall]:
        try:
            yield call
        finally:
            record = {k: v for k, v in asdict(call).items() if k not in ("result", "attachments")}
            try:
                with self._lock:
                    os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                    with open(self.path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(record, default=str) + "\n")
            except OSError as exc:
                if not self._warned:
                    log.warning("trace file %s unwritable (%s); continuing without it",
                                self.path, exc)
                    self._warned = True


class OtelTracer:
    """
    Tool calls as OpenTelemetry spans, with Langfuse's attribute conventions
    (langfuse.observation.*, langfuse.session.id). Other OTLP backends see
    ordinary span attributes; nothing here is Langfuse-only in transport.
    """

    def __init__(self, exporter, service: str = "pathwayoracle-mcp"):
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        self.provider = TracerProvider(resource=Resource.create({"service.name": service}))
        self.provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=1000))
        self.tracer = self.provider.get_tracer("pathwayoracle_mcp")
        atexit.register(self.provider.shutdown)            # flush on exit (stdio sessions end this way)

    @contextmanager
    def span(self, call, carrier=None) -> Iterator[ToolCall]:
        from opentelemetry import context as otel_context
        from opentelemetry.propagate import extract
        from opentelemetry.trace import Status, StatusCode

        # A caller's traceparent only matters for the outermost call; nested
        # calls (execute_tool -> tool) inherit the current span instead.
        parent = extract(carrier) if carrier else otel_context.get_current()
        with self.tracer.start_as_current_span(f"tool:{call.tool}", context=parent) as span:
            span.set_attribute("langfuse.observation.type", "tool")
            span.set_attribute("langfuse.observation.input", json.dumps(call.arguments, default=str))
            if call.session:
                span.set_attribute("langfuse.session.id", call.session)
                span.set_attribute("session.id", call.session)
            if call.kg_release:
                span.set_attribute("langfuse.release", call.kg_release)
            span.set_attribute("langfuse.observation.metadata.via", call.via)
            if call.via == "listed":
                span.set_attribute("langfuse.trace.name", call.tool)
            try:
                yield call
            finally:
                span.set_attribute("langfuse.observation.metadata.ms", call.ms or 0)
                span.set_attribute("langfuse.observation.metadata.citations", call.citations[:200])
                span.set_attribute("pathwayoracle.n_citations", len(call.citations))
                for key, value in call.attachments.items():
                    blob = json.dumps(value, default=str)
                    span.set_attribute(f"langfuse.observation.metadata.{key}",
                                       blob if len(blob) <= ATTACHMENT_LIMIT
                                       else blob[:ATTACHMENT_LIMIT] + '..." [truncated]')
                if call.ok:
                    out = json.dumps(call.result, default=str)
                    if len(out) > OUTPUT_LIMIT:
                        out = out[:OUTPUT_LIMIT] + f'..." [truncated, {len(out)} chars]'
                    span.set_attribute("langfuse.observation.output", out)
                    span.set_status(Status(StatusCode.OK))
                else:
                    span.set_attribute("langfuse.observation.level", "ERROR")
                    span.set_attribute("langfuse.observation.status_message", call.error or "")
                    span.set_status(Status(StatusCode.ERROR, call.error))


class MultiTracer:
    def __init__(self, tracers: list):
        self.tracers = tracers

    @contextmanager
    def span(self, call, carrier=None) -> Iterator[ToolCall]:
        from contextlib import ExitStack
        with ExitStack() as stack:
            for t in self.tracers:
                stack.enter_context(t.span(call, carrier))
            yield call


def _langfuse_exporter():
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    host = os.environ.get("LANGFUSE_HOST", "").rstrip("/")
    pk, sk = os.environ.get("LANGFUSE_PUBLIC_KEY"), os.environ.get("LANGFUSE_SECRET_KEY")
    if not (host and pk and sk):
        raise RuntimeError("PATHWAYORACLE_TRACING=langfuse needs LANGFUSE_HOST, "
                           "LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY.")
    auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    return OTLPSpanExporter(endpoint=f"{host}/api/public/otel/v1/traces",
                            headers={"Authorization": f"Basic {auth}"})


def tracer_from_env() -> Tracer:
    tracers: list = []
    mode = os.environ.get("PATHWAYORACLE_TRACING", "off").lower()
    if mode == "langfuse":
        tracers.append(OtelTracer(_langfuse_exporter()))
    elif mode == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        tracers.append(OtelTracer(OTLPSpanExporter()))
    elif mode not in ("off", "", "none"):
        raise RuntimeError(f"PATHWAYORACLE_TRACING={mode!r}; expected langfuse, otlp or off.")
    if path := os.environ.get("PATHWAYORACLE_TRACE_FILE"):
        tracers.append(JsonlTracer(path))
    if not tracers:
        return NullTracer()
    return tracers[0] if len(tracers) == 1 else MultiTracer(tracers)
