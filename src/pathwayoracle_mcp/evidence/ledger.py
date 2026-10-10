"""
The evidence ledger: a per-session graph of everything the tools returned.

Built on KG_Memory's GraphMemoryBackend (src/memory_final/app/graph_backend.py)
and revised for provenance:

  KG_Memory                               here
  ---------------------------------       -----------------------------------------
  facts written by the model              facts only from tool results, ingested by
  (memorize -> free-text triplets)        the server; the model can reference them,
                                          never write them
  entities keyed by name strings          entities keyed by domain ids (P04637,
                                          R-HSA-..., GO_..., pubmed:...)
  DiGraph: one edge per pair, predicates  MultiDiGraph: one edge per (relation,
  merged into a list                      reaction/qualifier), each with its cite
  entity / memory nodes                   entity / evidence / aggregate / call /
                                          question nodes
  `extracted_from` back-links             `returned` edges call -> node: which tool
                                          call produced each piece of evidence
  hierarchical namespaces                 ("q", n) and ("q", n, "call", k): what a
                                          question or a single call touched
  one global store, pickled per write     one ledger per MCP session, in memory;
                                          JSON (node-link) snapshot on demand

Kept from KG_Memory: upserts that count mentions, the namespace index with
prefix matching, and stats() -> the top entities.

Thread-safe: an HTTP server can run several calls of one session concurrently.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any, Iterable

import networkx as nx

KINDS = ("entity", "evidence", "aggregate", "call", "question")


class EvidenceLedger:
    def __init__(self, session: str, release: str | None = None):
        self.session = session
        self.release = release
        self.graph = nx.MultiDiGraph()
        self._lock = threading.RLock()
        self._question = 0                       # 0 = before any plan() call
        self._call = 0
        self._ns_index: dict[tuple, set[str]] = defaultdict(set)
        self._edges_by_cite: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
        self.views: dict[str, dict] = {}         # show_evidence views, by view id
        self.created = time.time()
        # Exchanges (docs/memory.md). The question counter IS the exchange
        # counter: question:<n> nodes and ("q", n) namespaces mark exchanges,
        # opened by plan() or by an idle gap between calls.
        self.last_end: float | None = None       # when the last call was recorded
        self.tokens: dict[int, int] = defaultdict(int)   # result tokens the model saw, per exchange
        self.compacted: set[int] = set()         # exchanges whose text bodies were dropped
        self.episodes: dict[int, Any] = {}       # exchange -> episode triplet graph (memory)
        self.hinted: set[str] = set()            # entity ids already pointed to earlier chats

    @property
    def question(self) -> int:
        return self._question

    # --- namespaces (KG_Memory's index, keyed by question and call) -------------
    def _ns(self, call: str | None) -> tuple:
        if call is None:
            return ("q", self._question)
        return ("q", self._question, "call", int(call.split(":")[1]))

    def _index(self, node_id: str, ns: tuple) -> None:
        for i in range(1, len(ns) + 1):
            self._ns_index[ns[:i]].add(node_id)

    def in_namespace(self, prefix: tuple) -> set[str]:
        """Node ids touched under a namespace prefix, e.g. ("q", 2) for question 2."""
        with self._lock:
            return set(self._ns_index.get(tuple(prefix), set()))

    # --- questions and calls -----------------------------------------------------
    def begin_question(self, recipe: str | None = None, reason: str = "plan") -> str:
        """A new exchange (plan(), or an idle gap): later calls are scoped under it."""
        with self._lock:
            self._question += 1
            qid = f"question:{self._question}"
            self.graph.add_node(qid, kind="question", recipe=recipe, reason=reason, started=time.time())
            self._index(qid, ("q", self._question))
            return qid

    def only_planned(self) -> bool:
        """The open exchange has calls, and all of them are plan()."""
        calls = self.calls_of(self._question) if self._question else []
        return bool(calls) and all(self.graph.nodes[c]["tool"] == "plan" for c in calls)

    def add_recipe(self, recipe: str | None) -> None:
        """Another recipe for the open exchange (recipes joined in call order)."""
        with self._lock:
            q = self.graph.nodes[f"question:{self._question}"]
            have = [r for r in (q.get("recipe") or "").split(", ") if r]
            if recipe and recipe not in have:
                q["recipe"] = ", ".join(have + [recipe])

    def record_call(self, tool: str, arguments: dict, ok: bool, ms: float | None,
                    error: str | None = None) -> str:
        with self._lock:
            self._call += 1
            cid = f"call:{self._call}"
            self.graph.add_node(cid, kind="call", tool=tool, arguments=arguments, ok=ok,
                                ms=ms, error=error, question=self._question, at=time.time())
            self._index(cid, self._ns(cid))
            if self._question:
                self.graph.add_edge(f"question:{self._question}", cid, key="asked", rel="asked")
            self.last_end = time.time()
            return cid

    def idle_for(self, at: float) -> float:
        """Seconds between the last recorded call and `at` (0 before any call)."""
        return 0.0 if self.last_end is None else at - self.last_end

    def calls_of(self, n: int) -> list[str]:
        """Call node ids of exchange n, in order."""
        with self._lock:
            return sorted((c for c, d in self.graph.nodes(data=True) if d["kind"] == "call" and d.get("question") == n),
                          key=lambda c: int(c.split(":")[1]))

    # --- upserts -----------------------------------------------------------------
    def upsert_node(self, node_id: str, kind: str, call: str | None = None, **attrs: Any) -> str:
        """Add or update a node. None-valued attrs never overwrite known values."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        with self._lock:
            if not self.graph.has_node(node_id):
                self.graph.add_node(node_id, kind=kind, mention_count=0, first_call=call)
            data = self.graph.nodes[node_id]
            for k, v in attrs.items():
                if v is not None and v != "" and v != []:
                    data[k] = v
            data["mention_count"] = data.get("mention_count", 0) + 1
            data["last_call"] = call
            if call:
                self._index(node_id, self._ns(call))
                # Provenance back-link: which call returned this node.
                if not self.graph.has_edge(call, node_id, key="returned"):
                    self.graph.add_edge(call, node_id, key="returned", rel="returned")
            return node_id

    def upsert_edge(self, src: str, dst: str, rel: str, qualifier: str | None = None,
                    call: str | None = None, **attrs: Any) -> tuple[str, str, str]:
        """
        One edge per (src, dst, rel, qualifier) -- never merged across reactions,
        unlike KG_Memory's single edge per pair. `qualifier` is typically a
        reaction id or an evidence code set.
        """
        key = f"{rel}|{qualifier}" if qualifier else rel
        with self._lock:
            # networkx would silently create missing endpoints as untyped nodes --
            # a mapper bug that should fail loudly instead.
            for end in (src, dst):
                if not self.graph.has_node(end):
                    raise KeyError(f"edge {rel}: endpoint {end!r} is not in the ledger")
            if self.graph.has_edge(src, dst, key=key):
                data = self.graph.edges[src, dst, key]
                data["weight"] = data.get("weight", 1) + 1
            else:
                self.graph.add_edge(src, dst, key=key, rel=rel, qualifier=qualifier, weight=1,
                                    calls=[])
                data = self.graph.edges[src, dst, key]
            for k, v in attrs.items():
                if v is not None and v != "" and v != []:
                    data[k] = v
            if call and call not in data["calls"]:
                data["calls"].append(call)
            if data.get("cite"):
                self._edges_by_cite[data["cite"]].add((src, dst, key))
            return src, dst, key

    # --- reading -----------------------------------------------------------------
    def has(self, node_id: str) -> bool:
        with self._lock:
            return self.graph.has_node(node_id)

    def node(self, node_id: str) -> dict | None:
        with self._lock:
            return dict(self.graph.nodes[node_id]) if self.graph.has_node(node_id) else None

    def find_by_name(self, name: str, label: str = "Gene") -> list[str]:
        """Entity ids with this exact name and label (names as the tools returned them)."""
        with self._lock:
            return [n for n, d in self.graph.nodes(data=True)
                    if d.get("kind") == "entity" and d.get("label") == label and d.get("name") == name]

    def find_edges(self, cite: str) -> list[tuple[str, str, str, dict]]:
        """Edges carrying a given cite id (e.g. edge:...@R-HSA-...), via the cite index."""
        with self._lock:
            return [(u, v, k, dict(self.graph.edges[u, v, k]))
                    for u, v, k in self._edges_by_cite.get(cite, ()) if self.graph.has_edge(u, v, k)]

    def save_view(self, view: dict) -> str:
        with self._lock:
            vid = f"v{len(self.views) + 1}"
            self.views[vid] = {**view, "view_id": vid, "question": self._question}
            return vid

    def returned_by(self, node_id: str) -> list[str]:
        """The calls that returned a node -- its provenance."""
        with self._lock:
            return [u for u, _, k in self.graph.in_edges(node_id, keys=True) if k == "returned"]

    def subgraph(self, ids: Iterable[str], neighbors: int = 0) -> nx.MultiDiGraph:
        """Known ids plus `neighbors` hops, excluding the call/question layer."""
        with self._lock:
            content = [n for n, d in self.graph.nodes(data=True) if d["kind"] not in ("call", "question")]
            g = self.graph.subgraph(content)
            keep = {i for i in ids if g.has_node(i)}
            frontier = set(keep)
            for _ in range(neighbors):
                nxt = set()
                for n in frontier:
                    nxt.update(g.successors(n)); nxt.update(g.predecessors(n))
                frontier = nxt - keep
                keep |= nxt
            return g.subgraph(keep).copy()

    def stats(self, top: int = 10) -> dict:
        """KG_Memory's stats(), by kind and label; top entities by mentions and degree."""
        with self._lock:
            by_kind: dict[str, int] = defaultdict(int)
            by_label: dict[str, int] = defaultdict(int)
            for _, d in self.graph.nodes(data=True):
                by_kind[d["kind"]] += 1
                if d["kind"] == "entity":
                    by_label[d.get("label", "?")] += 1
            content = self.graph.subgraph(
                [n for n, d in self.graph.nodes(data=True) if d["kind"] in ("entity", "evidence", "aggregate")])
            entities = [n for n, d in content.nodes(data=True) if d["kind"] == "entity"]
            ranked = sorted(entities, key=lambda n: (content.degree(n), self.graph.nodes[n].get("mention_count", 0)),
                            reverse=True)[:top]
            return {
                "session": self.session, "release": self.release,
                "questions": self._question, "calls": self._call,
                "nodes": dict(by_kind), "entities_by_label": dict(by_label),
                "edges": content.number_of_edges(),
                "top_entities": [{"id": n, "label": self.graph.nodes[n].get("label"),
                                  "name": self.graph.nodes[n].get("name"), "degree": content.degree(n),
                                  "mentions": self.graph.nodes[n].get("mention_count", 0)} for n in ranked],
            }

    def to_json(self) -> dict:
        """Node-link snapshot (JSON-safe; unlike KG_Memory's pickle, safe to load)."""
        with self._lock:
            return nx.node_link_data(self.graph, edges="edges")
