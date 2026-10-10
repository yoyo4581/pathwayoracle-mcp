"""
Exchanges -> episodes (docs/memory.md, sections 2-3).

An episode is one exchange of a chat, rewritten as triplets: every edge is
(subject, predicate, object) with {session, episode, release, cite?}. It is
built ONLY from what the ledger recorded -- consolidation reorganises facts, it
never adds one. Text bodies are left out: snippets, abstracts and reactions are
fetched again by cite id.

    build_episode   one exchange of the ledger -> its episode graph
    compact         over budget, drop the text bodies of exchanges older than
                    the last `keep`; their episodes are already saved

Episodes are saved at every call (memory.MemoryGraph.save_session), not only
at consolidation, so a killed connection loses nothing it recorded.
"""
from __future__ import annotations

import time
from collections import Counter

import networkx as nx

from .ledger import EvidenceLedger
from .view import STRUCTURAL, resolve

# Node attributes worth keeping in memory: identity and small statistics. Text
# bodies and bulky tool output (moves, controls, rows) stay out.
KEEP = ("label", "name", "etype", "atype", "source", "title", "uniprot_id", "chunk_idx", "pmid", "cite",
        "genes", "level", "information_content", "fold_enrichment", "n_genes", "hops", "aspect",
        "full_name", "read", "recalled", "origin")
DROP_WHEN_COMPACT = ("text", "expanded_text", "expanded_sentences", "moves", "controls", "rows", "coverage",
                     "breakdown")


# Memory tools load an earlier episode's triplets into the ledger: what they
# return belongs to that episode, never to the exchange that recalled it.
MEMORY_TOOLS = frozenset({"recall", "recall_episode"})
# Episode-level predicates (episode -> what it touched, read, cited, derived,
# concluded; claim -> what it cites): bookkeeping about the facts, not facts.
EPISODE_RELS = frozenset({"touched", "cited", "read", "seen", "derived", "concluded", "cites", "has_episode"})
# A participant linked to its gene (complex -> member): needed to resolve
# complexes, not a fact about what the exchange studied.
LINK_RELS = frozenset({"in_entity"})
# What names an exchange first: genes, then pathways and GO terms, then drugs;
# Reactome complexes and reactions (long names like "p-S166,S188-MDM2 dimer, ...") after.
NAMED = {"Gene": 0, "Pathway": 1, "Ontology": 1, "Drug": 2, "Compound": 2}


def episode_id(session: str, n: int) -> str:
    return f"episode:{session}:{n}"


def _node_attrs(d: dict) -> dict:
    return {k: d[k] for k in KEEP if d.get(k) not in (None, "", [])} | {"kind": d["kind"]}


def evidence_status(d: dict, cited: bool) -> str:
    """cited (a view's claim cites it) > read (its text reached the model) > seen."""
    return "cited" if cited else ("read" if d.get("read") else "seen")


def is_fact(d: dict) -> bool:
    """A triplet recorded from a tool result -- not episode bookkeeping."""
    return d.get("rel") not in EPISODE_RELS and d.get("rel") not in STRUCTURAL


# --- reading an episode's triplet graph (the summary, recall and recall_episode) -----

def facts_by_cite(eg: nx.MultiDiGraph) -> dict[str, list[tuple[str, str, dict]]]:
    """An episode graph's fact triplets, by the cite they carry."""
    out: dict[str, list] = {}
    for u, v, d in eg.edges(data=True):
        if is_fact(d) and d.get("cite"):
            out.setdefault(d["cite"], []).append((u, v, d))
    return out


def claims_of(eg: nx.MultiDiGraph, ep: str) -> list[str]:
    """The claim nodes an episode concluded, in the order they were made."""
    def order(c: str):
        view, i = c.rsplit(":", 2)[-2:]
        return int(view.lstrip("v") or 0), int(i)
    return sorted((x for _, x, d in eg.out_edges(ep, data=True) if d.get("rel") == "concluded"), key=order)


def claim_cites(eg: nx.MultiDiGraph, claim: str) -> dict[str, set[str]]:
    """A claim's cites -> the nodes each resolved to when the claim was made."""
    out: dict[str, set] = {}
    for _, x, d in eg.out_edges(claim, data=True):
        if d.get("rel") == "cites":
            out.setdefault(d.get("cite") or x, set()).add(x)
    return out


def claim_weights(eg: nx.MultiDiGraph, claim: str, by_cite: dict) -> dict[str, float]:
    """
    Entities a claim rests on, each weighted by how specifically: 1 when cited
    directly, as an end of a cited edge or the gene of a cited passage; for a
    cited reaction, 1 / the number of participants sharing its role there -- a
    sole catalyst weighs 1, one of 32 proteasome subunits 1/32, ubiquitin one
    of several reactants.
    """
    w: dict[str, float] = {}

    def note(x: str, val: float) -> None:
        if eg.nodes[x].get("kind") == "entity":
            w[x] = max(w.get(x, 0.0), val)
    for cite, targets in claim_cites(eg, claim).items():
        facts = by_cite.get(cite, ())
        for x in targets:
            note(x, 1.0)
        if cite.startswith("reaction:"):
            rid = cite.removeprefix("reaction:")
            parts = [(v if u == rid else u, d["rel"]) for u, v, d in facts]
            share = Counter((r, eg.nodes[x].get("label") == "Gene") for x, r in parts)
            for x, r in parts:
                note(x, 1.0 / share[r, eg.nodes[x].get("label") == "Gene"])
        else:
            for u, v, _ in facts:
                note(u, 1.0)
                note(v, 1.0)
    return w


def claim_entities(eg: nx.MultiDiGraph, claim: str, by_cite: dict) -> set[str]:
    """Entities a claim rests on: cited directly, or an end of a triplet it cites
    (an edge's two genes, a cited reaction's participants, a passage's gene)."""
    return set(claim_weights(eg, claim, by_cite))


def rank_entities(eg: nx.MultiDiGraph, ep: str, by_cite: dict | None = None) -> list[str]:
    """
    An episode's entities, most telling first: genes, then pathways and GO
    terms, then the rest; within those, by how much its claims rest on them
    (claim_weights), then by how many fact triplets they take part in
    (participant -> gene links not counted).
    """
    by_cite = facts_by_cite(eg) if by_cite is None else by_cite
    weight: Counter = Counter()
    for c in claims_of(eg, ep):
        weight.update(claim_weights(eg, c, by_cite))
    degree: Counter = Counter()
    for u, v, d in eg.edges(data=True):
        if is_fact(d) and d.get("rel") not in LINK_RELS:
            degree[u] += 1
            degree[v] += 1
    ents = {x for _, x, d in eg.out_edges(ep, data=True) if d.get("rel") == "touched"} | set(weight)
    return sorted(ents, key=lambda x: (NAMED.get(eg.nodes[x].get("label"), 9), -weight[x], -degree[x], x))


def _count_list(counts: Counter, cap: int) -> str:
    top = counts.most_common()
    text = ", ".join(f"{t} x{c}" for t, c in top[:cap])
    return text + (f" (+{len(top) - cap} more)" if len(top) > cap else "")


def build_episode(lg: EvidenceLedger, n: int) -> nx.MultiDiGraph | None:
    """One exchange -> its episode graph; None when it only used memory tools and concluded nothing."""
    g = lg.graph
    ep = episode_id(lg.session, n)
    calls = lg.calls_of(n)
    # The exchange's own work. What a memory call loaded is an earlier
    # episode's, already saved there with its provenance: re-recording it here
    # would copy that episode into this one.
    work = {c for c in calls if g.nodes[c]["tool"] not in MEMORY_TOOLS}
    views = [v for v in lg.views.values() if v.get("question") == n]
    if not work and not views:
        return None
    ns = set().union(*(lg.in_namespace(("q", n, "call", int(c.split(":")[1]))) for c in work))

    out = nx.MultiDiGraph()
    meta = {"session": lg.session, "episode": ep, "release": lg.release}

    def node(x: str) -> str:
        if not out.has_node(x):
            out.add_node(x, **_node_attrs(g.nodes[x]))
        return x

    # Claims: the only model-written text, marked as such.
    cited: set[str] = set()
    claims = []
    for v in views:
        for c in v.get("claims", []):
            cid = f"claim:{lg.session}:{v['view_id']}:{c['i']}"
            claims.append((cid, c))
            for cite in c.get("cites", {}):
                cited.update(resolve(lg, cite)[1])
    # What it cited counts as touched even when it came from memory.
    ns = {x for x in ns | cited if g.has_node(x) and g.nodes[x]["kind"] not in ("call", "question")}

    # Facts: ledger edges produced by this exchange's own calls, keyed per episode.
    n_facts = 0
    for u, v, k, d in g.edges(keys=True, data=True):
        if d.get("rel") in STRUCTURAL or not (set(d.get("calls") or []) & work):
            continue
        if g.nodes[u]["kind"] in ("call", "question") or g.nodes[v]["kind"] in ("call", "question"):
            continue
        attrs = {x: d[x] for x in ("rel", "qualifier", "cite", "sign", "mechanism", "reaction_id", "via",
                                   "evidence", "sentence") if d.get(x) not in (None, "", [])}
        out.add_edge(node(u), node(v), key=f"{k}@{ep}", **attrs, **meta)
        n_facts += 1

    # The episode itself, and what it touched, read, cited and derived.
    tools = Counter(g.nodes[c]["tool"] for c in calls)
    q = g.nodes.get(f"question:{n}", {}) if g.has_node(f"question:{n}") else {}
    for x in ns:
        node(x)
        kind = g.nodes[x]["kind"]
        if kind == "entity":
            out.add_edge(ep, x, key=f"touched@{ep}", rel="touched", mentions=g.nodes[x].get("mention_count", 1), **meta)
        elif kind == "evidence":
            st = evidence_status(g.nodes[x], x in cited)
            out.add_edge(ep, x, key=f"{st}@{ep}", rel=st, **meta)
        elif kind == "aggregate":
            out.add_edge(ep, x, key=f"derived@{ep}", rel="derived", **meta)
    for cid, c in claims:
        out.add_node(cid, kind="claim", label=c["label"], authored="model", theme=c.get("theme"))
        out.add_edge(ep, cid, key=f"concluded@{ep}", rel="concluded", **meta)
        for cite in c.get("cites", {}):
            for x in resolve(lg, cite)[1]:
                out.add_edge(cid, node(x), key=f"cites:{cite}@{ep}", rel="cites", cite=cite, **meta)

    out.add_node(ep, kind="episode", session=lg.session, n=n, release=lg.release,
                 recipe=q.get("recipe"), reason=q.get("reason"),
                 started=g.nodes[calls[0]].get("at") if calls else q.get("started"),
                 ended=g.nodes[calls[-1]].get("at") if calls else None,
                 tools=dict(tools), failures=sum(1 for c in calls if not g.nodes[c].get("ok")),
                 ms=round(sum(g.nodes[c].get("ms") or 0 for c in calls)), tokens=lg.tokens.get(n, 0),
                 n_facts=n_facts, n_claims=len(claims))
    out.nodes[ep]["summary"] = summary(out, ep)
    out.add_node(f"session:{lg.session}", kind="session", release=lg.release, started=lg.created)
    out.add_edge(f"session:{lg.session}", ep, key=f"has_episode@{ep}", rel="has_episode", **meta)
    return out


def _group_name(eg: nx.MultiDiGraph, x: str) -> str:
    """A grouping's name, from the episode graph alone (as view._grouping_name does from the ledger)."""
    d = eg.nodes[x]
    if d.get("name"):
        return str(d["name"])
    for _, m, e in eg.out_edges(x, data=True):              # a GO group / profile names its term
        if e.get("rel") == "term":
            return str(eg.nodes[m].get("name") or m)
    genes = d.get("genes") or []
    return "shared by " + ", ".join(map(str, genes[:6])) + ("…" if len(genes) > 6 else "") if genes else x


def summary(eg: nx.MultiDiGraph, ep: str) -> str:
    """
    Deterministic, no LLM, from the episode graph alone (so saved episodes can
    be re-summarised): the line recall shows for navigating episodes -- recipe,
    tools, top entities, groupings, evidence counts, claims (model). The
    triplets behind it are recall_episode's.
    """
    d = eg.nodes[ep]
    names = [_short(str(eg.nodes[x].get("name") or x)) for x in rank_entities(eg, ep)[:8]]
    groups = [_group_name(eg, x) for _, x, e in eg.out_edges(ep, data=True)
              if e.get("rel") == "derived" and eg.nodes[x].get("atype") in ("go_group", "go_profile", "shared_machinery")]
    status = Counter(e["rel"] for _, _, e in eg.out_edges(ep, data=True) if e["rel"] in ("cited", "read", "seen"))
    # Claims rest on graph triplets (edges, reactions, memberships) as well as passages.
    graph_cites = Counter(c.split(":", 1)[0] for c in {e.get("cite") for _, _, e in eg.edges(data=True)
                                                        if e.get("rel") == "cites"}
                          if c and eg.nodes.get(c, {}).get("kind") != "evidence")
    evidence = f"evidence: {status.get('cited', 0)} cited, {status.get('read', 0)} read, {status.get('seen', 0)} seen"
    if graph_cites:
        evidence += "; claims cite " + ", ".join(f"{c} {k}" for k, c in sorted(graph_cites.items(), key=lambda x: (-x[1], x[0])))
    labels = [eg.nodes[c].get("label") for c in claims_of(eg, ep)]
    parts = [d.get("recipe") or "no plan", _count_list(Counter(d.get("tools") or {}), 8)]
    if names:
        parts.append("entities: " + ", ".join(names))
    if groups:
        parts.append("groupings: " + "; ".join(sorted(groups)[:3]))
    parts.append(evidence)
    if labels:
        more = f" (+{len(labels) - 4} more)" if len(labels) > 4 else ""
        parts.append("claims (model): " + "; ".join(labels[:4]) + more)
    return " | ".join(p for p in parts if p)[:900]


def _short(s: str, n: int = 40) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def live_tokens(lg: EvidenceLedger) -> int:
    return sum(t for n, t in lg.tokens.items() if n not in lg.compacted)


def compact(lg: EvidenceLedger, budget: int, keep: int = 3) -> list[int]:
    """
    Over budget, drop the text bodies of every live exchange older than the
    last `keep` (which stay intact even if they alone exceed it). A node also
    touched by a kept exchange keeps its text. Call nodes stay (they are the
    provenance trail) but lose their arguments. Returns the exchanges compacted.
    """
    if live_tokens(lg) <= budget:
        return []
    current = lg.question
    kept = set(range(max(0, current - keep + 1), current + 1))
    targets = sorted(n for n in lg.tokens if n not in lg.compacted and n not in kept)
    if not targets:
        return []
    protected = set().union(*(lg.in_namespace(("q", n)) for n in kept))
    g = lg.graph
    with lg._lock:
        for n in targets:
            if n not in lg.episodes:                     # saved before it is thinned
                lg.episodes[n] = build_episode(lg, n)
            for x in lg.in_namespace(("q", n)) - protected:
                if not g.has_node(x):
                    continue
                d = g.nodes[x]
                if d["kind"] == "call":
                    d.pop("arguments", None)
                for k in DROP_WHEN_COMPACT:
                    d.pop(k, None)
                if d["kind"] in ("entity", "evidence", "aggregate"):
                    d["consolidated"] = time.time()
            lg.compacted.add(n)
    return targets
