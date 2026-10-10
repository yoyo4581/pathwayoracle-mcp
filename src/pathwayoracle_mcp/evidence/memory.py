"""
Long-term memory: every chat's episodes, as one triplet graph (docs/memory.md, sections 5-6).

    /cache/memory/<session>.json     node-link graph of one chat's episodes

A session is one chat (one MCP connection). Its file is rewritten at every call,
so a killed connection loses nothing it recorded. All files compose into one
MultiDiGraph: entity ids are shared, so "which chats touched MDM2" is a
neighbourhood lookup. Files are re-read when they change, so a chat sees
another chat that is still open in a different process.

Recall is server-driven (user, 2026-10-09):
  digest()   recent episodes, put in the server instructions at connection
             start -- the model sees them without asking
  hints()    when a tool result contains an entity earlier chats worked on,
             a short pointer is added to that result (once per entity per chat)
  search() / episode_subgraph() back the recall / recall_episode tools.

Never written: Neo4j, the release Qdrant / Zilliz collections. Only this dir.

    PATHWAYORACLE_MEMORY_DIR    default /cache/memory (compose volume), else a temp dir
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path

import networkx as nx

from .consolidate import claim_weights, claims_of, facts_by_cite
from .store import _SAFE

log = logging.getLogger(__name__)
_WORD = re.compile(r"[a-z0-9]+")
STATUS_RANK = {"cited": 3, "cites": 3, "read": 2, "seen": 1, "touched": 1}


def memory_dir() -> Path:
    configured = os.environ.get("PATHWAYORACLE_MEMORY_DIR")
    if configured:
        return Path(configured)
    return Path("/cache/memory") if Path("/cache").is_dir() else Path(tempfile.gettempdir()) / "pathwayoracle-memory"


def _date(ts) -> str:
    """UTC, marked as such: the server's clock says nothing about the user's time zone."""
    return time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(ts)) if ts else "?"


class MemoryGraph:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else memory_dir()
        self._sessions: dict[str, nx.MultiDiGraph] = {}
        self._mtimes: dict[str, float] = {}
        self._graph: nx.MultiDiGraph | None = None
        self._by_episode: dict[str, list] | None = None     # episode -> its edges, for self._graph
        self._lock = threading.RLock()

    # --- files ---------------------------------------------------------------------
    def _path(self, session: str) -> Path:
        if not _SAFE.match(session):
            raise ValueError("invalid session id")                  # no path traversal
        root = self.root.resolve()
        path = (root / f"{session}.json").resolve()
        if path.parent != root:
            raise ValueError("memory path escapes the memory directory")
        return path

    def save_session(self, session: str, episodes: list[nx.MultiDiGraph]) -> None:
        """Rewrite one chat's file from its episodes (atomic). Best effort, never raises."""
        try:
            path = self._path(session)
            if not episodes and not path.exists():
                return                                     # a chat that only recalled: no file
            g = nx.compose_all(episodes) if episodes else nx.MultiDiGraph()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(nx.node_link_data(g, edges="edges"), default=str), encoding="utf-8")
            tmp.replace(path)
            with self._lock:
                self._sessions[session] = g
                self._mtimes[session] = path.stat().st_mtime
                self._graph = None
        except Exception:                                          # noqa: BLE001
            log.warning("memory: could not save session %s", session, exc_info=True)

    def refresh(self) -> None:
        """Load new or changed chat files (other processes write them too)."""
        if not self.root.is_dir():
            return
        with self._lock:
            for path in self.root.glob("*.json"):
                session = path.stem
                if not _SAFE.match(session):
                    continue
                try:
                    mtime = path.stat().st_mtime
                    if self._mtimes.get(session) == mtime:
                        continue
                    data = json.loads(path.read_text(encoding="utf-8"))
                    self._sessions[session] = nx.node_link_graph(data, directed=True, multigraph=True, edges="edges")
                    self._mtimes[session] = mtime
                    self._graph = None
                except Exception:                                  # noqa: BLE001
                    log.warning("memory: skipping unreadable %s", path, exc_info=True)

    @property
    def graph(self) -> nx.MultiDiGraph:
        with self._lock:
            if self._graph is None:
                self._graph = nx.compose_all(list(self._sessions.values())) if self._sessions else nx.MultiDiGraph()
                self._by_episode = None
            return self._graph

    def _episode_edges(self, ep: str) -> list[tuple]:
        """An episode's edges, from an index built once per composed graph."""
        with self._lock:
            g = self.graph
            if self._by_episode is None:
                idx: dict[str, list] = {}
                for u, v, k, d in g.edges(keys=True, data=True):
                    if d.get("episode"):
                        idx.setdefault(d["episode"], []).append((u, v, k, d))
                self._by_episode = idx
            return self._by_episode.get(ep, [])

    # --- reading ---------------------------------------------------------------------
    def episodes(self, exclude_session: str | None = None) -> list[tuple[str, dict]]:
        g = self.graph
        return [(n, d) for n, d in g.nodes(data=True)
                if d.get("kind") == "episode" and d.get("session") != exclude_session]

    def _matched_ids(self, ep: str, ids: set[str]) -> dict[str, str]:
        """For each id this episode reached: its best status (cited > read > seen/touched)."""
        g, out = self.graph, {}

        def note(i, st):
            if STATUS_RANK.get(st, 0) > STATUS_RANK.get(out.get(i), 0):
                out[i] = st
        for _, x, d in g.out_edges(ep, data=True):
            rel = d.get("rel")
            if x in ids:
                note(x, rel)
            if rel in ("cited", "read", "seen"):                 # evidence about an id
                for _, y, e in g.out_edges(x, data=True):
                    if y in ids and e.get("episode") == ep:
                        note(y, rel)
            if rel == "concluded":                               # a claim citing the id
                for _, y, e in g.out_edges(x, data=True):
                    if y in ids:
                        note(y, "cited")
        return out

    def _text(self, ep: str, d: dict) -> set[str]:
        g = self.graph
        words = set(_WORD.findall(str(d.get("summary", "")).lower()))
        for _, x, e in g.out_edges(ep, data=True):
            if e.get("rel") in ("touched", "concluded"):
                nd = g.nodes[x]
                words |= set(_WORD.findall(f"{nd.get('name') or ''} {nd.get('label') or ''} {x}".lower()))
        return words

    def search(self, query: str | None = None, ids: list[str] | None = None, limit: int = 5,
               current_session: str | None = None) -> list[dict]:
        """Episodes ranked by id matches (cited > read > seen), then text overlap."""
        self.refresh()
        idset, words = set(ids or []), set(_WORD.findall((query or "").lower()))
        scored = []
        for ep, d in self.episodes():
            matched = self._matched_ids(ep, idset) if idset else {}
            id_score = sum(STATUS_RANK.get(s, 0) for s in matched.values())
            overlap = len(words & self._text(ep, d)) if words else 0
            if (idset or words) and not (id_score or overlap):
                continue
            scored.append((id_score, overlap, d.get("started") or 0, ep, d, matched))
        scored.sort(key=lambda x: (-x[0], -x[1], -x[2]))
        rows = []
        for *_, ep, d, matched in scored[:limit]:
            row = self.row(ep, d, matched, current_session)
            if idset:
                on = self.claims_on(ep, idset)
                if on:
                    row["claims_on_ids"] = on[:3] + ([f"(+{len(on) - 3} more)"] if len(on) > 3 else [])
            rows.append(row)
        return rows

    def claims_on(self, ep: str, ids: set[str]) -> list[str]:
        """
        Labels of an episode's claims that rest on any of `ids` (cited, or an
        end of a cited triplet), those resting on more of the ids -- and more
        specifically (claim_weights) -- first: with ids=[MDM2, TP53] in a TP53
        episode, every claim rests on TP53, and the MDM2 ones must lead.
        """
        eg = self.episode_subgraph(ep)
        by_cite = facts_by_cite(eg)
        scored = []
        for i, c in enumerate(claims_of(eg, ep)):
            w = claim_weights(eg, c, by_cite)
            hit = ids & set(w)
            if hit:
                scored.append((-len(hit), -sum(w[x] for x in hit), i, f"{eg.nodes[c].get('label')} (model)"))
        return [label for *_, label in sorted(scored)]

    def row(self, ep: str, d: dict, matched: dict | None = None, current_session: str | None = None) -> dict:
        out = {"episode": ep, "date": _date(d.get("started")), "release": d.get("release"),
               "recipe": d.get("recipe"), "summary": d.get("summary")}
        if d.get("n_facts") is not None:
            out["size"] = f"{d.get('n_claims', 0)} claims, {d['n_facts']} triplets"
        if d.get("session") == current_session:
            out["this_chat"] = True
        if matched:
            out["matched"] = [f"{i} {s}" for i, s in sorted(matched.items())]
        return out

    def episode_subgraph(self, ep: str, ids: list[str] | None = None) -> nx.MultiDiGraph:
        """One episode's triplets (edges keyed to it), all or one hop around `ids`."""
        self.refresh()
        g = self.graph
        if not g.has_node(ep) or g.nodes[ep].get("kind") != "episode":
            raise LookupError(f"No episode {ep!r} in memory. recall lists them.")
        edges = self._episode_edges(ep)
        if ids:
            keep = set(ids)
            edges = [e for e in edges if e[0] in keep or e[1] in keep]
        out = nx.MultiDiGraph()
        for u, v, k, d in edges:
            for x in (u, v):
                if not out.has_node(x):
                    out.add_node(x, **g.nodes[x])
            out.add_edge(u, v, key=k, **d)
        if not out.has_node(ep):
            out.add_node(ep, **g.nodes[ep])
        return out

    # --- server-driven recall ----------------------------------------------------------
    def digest(self, limit: int = 5, exclude_session: str | None = None) -> str:
        """Recent episodes from earlier chats, for the server instructions (~40 tokens each)."""
        self.refresh()
        eps = sorted(self.episodes(exclude_session), key=lambda x: -(x[1].get("started") or 0))
        if not eps:
            return ""
        lines = []
        for ep, d in eps[:limit]:
            s = str(d.get("summary") or "")
            ents = next((p.removeprefix("entities: ") for p in s.split(" | ") if p.startswith("entities: ")), "")
            claim = next((p.removeprefix("claims (model): ").split("; ")[0] for p in s.split(" | ")
                          if p.startswith("claims (model): ")), "")
            bits = [_date(d.get("started")), d.get("recipe") or "no plan", ents[:90]]
            lines.append(f"  - {ep}: " + " · ".join(b for b in bits if b) + (f" · claim: {claim[:80]}" if claim else ""))
        sessions = {d.get("session") for _, d in eps}
        return (f"MEMORY of earlier chats ({len(eps)} episodes in {len(sessions)} chats; most recent first). "
                "recall(query or ids) searches them; recall_episode(id) loads one's evidence:\n" + "\n".join(lines))

    def hints(self, entity_ids: list[str], current_session: str, skip: set[str],
              max_entities: int = 3, per_entity: int = 2) -> list[str]:
        """Pointers from entities in a fresh result to earlier chats' episodes about them."""
        self.refresh()
        g, out = self.graph, []
        ranked = []
        for i in entity_ids:
            if i in skip or not g.has_node(i):
                continue
            eps = {}
            for ep, _, d in g.in_edges(i, data=True):
                e = d.get("episode")
                if e and g.has_node(e) and g.nodes[e].get("session") != current_session:
                    eps[e] = max(eps.get(e, 0), STATUS_RANK.get(d.get("rel"), 1))
            if eps:
                ranked.append((max(eps.values()), len(eps), i, eps))
        ranked.sort(key=lambda x: (-x[0], -x[1], x[2]))
        for _, n, i, eps in ranked[:max_entities]:
            best = sorted(eps, key=lambda e: (-eps[e], -(g.nodes[e].get("started") or 0)))[:per_entity]
            name = g.nodes[i].get("name") or i
            out.append(f"{name} ({i}): " + "; ".join(
                f"{e} {_date(g.nodes[e].get('started'))} {g.nodes[e].get('recipe') or ''}".strip() for e in best)
                + (f" (+{n - len(best)} more)" if n > len(best) else ""))
            skip.add(i)
        return out
