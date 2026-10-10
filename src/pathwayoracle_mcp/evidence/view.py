"""
Build an evidence view from the ledger, from a small spec the model sends.

The model sends ids; this module decides nothing about WHAT matters -- it
resolves the ids, checks them against what was actually retrieved, and makes
the selection fit (grouping and a node cap). The full view is stored on the
ledger and fetched by the panel; the model only ever sees the receipt.

Cite statuses
  fetched     the ledger holds the evidence itself (a node with its content, or
              the edge that carries the cite)
  referenced  the id is known but its content was never read (e.g. a PMID seen
              inside a snippet whose abstract nobody fetched)
  unknown     never retrieved in this session -- not shown, flagged in the receipt

Everything in a view is panel-only data, so it can be generous: full text (up
to TEXT_LIMIT), the tool calls that returned each element, and the members of
every folded group so the panel can expand them without another round trip.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .ledger import EvidenceLedger

HIDDEN = {"call", "question"}
STRUCTURAL = {"returned", "asked"}
TEXT_LIMIT = 1500
ROLES = {"REACTANT", "PRODUCT", "CATALYST", "INHIBITOR", "STIMULATOR"}

# Selection priorities: lower survives the node cap first.
CITED, ANCHOR, PARTICIPANT, SCOPE, NEIGHBOR = 0, 1, 2, 3, 4
MACHINERY = 10_000           # breadth of a participant gene whose entity is unknown


def _breadth(g, gene: str, rxn: str) -> int:
    """
    How many genes share the entity through which `gene` takes part in `rxn`:
    1 for a protein acting alone, 40+ for a proteasome subunit. Small means a
    specific actor, large means machinery -- what the node cap should keep
    first among a cited reaction's participants.
    """
    sizes = []
    for e in g.successors(gene):
        if not any(d.get("rel") == "in_entity" for d in g.get_edge_data(gene, e).values()):
            continue
        linked = list((g.get_edge_data(e, rxn) or {}).values()) + list((g.get_edge_data(rxn, e) or {}).values())
        if any(d.get("rel") in ROLES for d in linked):
            node = g.nodes[e]
            sizes.append(node.get("n_genes") or len(node.get("genes") or []) or MACHINERY)
    return min(sizes, default=MACHINERY)


# --- cite -> ledger elements ------------------------------------------------------

def resolve(lg: EvidenceLedger, cite: str) -> tuple[str, list[str], list[tuple[str, str, str]]]:
    """(status, node ids, edge keys) for one cite id or bare id."""
    c = cite.strip()
    if c.startswith(("edge:", "go:", "member:")):
        edges = lg.find_edges(c)
        if edges:
            return "fetched", sorted({x for u, v, _, _ in edges for x in (u, v)}), [(u, v, k) for u, v, k, _ in edges]
        return "unknown", [], []
    if c.startswith("node:"):
        c = c.split(":")[-1]
    elif c.startswith("reaction:"):
        c = c.removeprefix("reaction:")
    elif c.startswith("GO:"):
        c = c.replace(":", "_")
    node = lg.node(c)
    if node is None or node["kind"] in HIDDEN:
        return "unknown", [], []
    if recalled(lg, c):
        return "recalled", [c], []
    if node["kind"] == "evidence" and not (node.get("read") or node.get("text") or node.get("expanded_text")):
        return "referenced", [c], []
    return "fetched", [c], []


def recalled(lg: EvidenceLedger, node_id: str) -> bool:
    """Only recall_episode calls returned it: it comes from an earlier chat's memory."""
    calls = lg.returned_by(node_id)
    return bool(calls) and all((lg.node(x) or {}).get("tool") == "recall_episode" for x in calls)


# --- node / edge payloads for the panel ------------------------------------------

def _clip(s: Any, n: int = TEXT_LIMIT) -> str | None:
    if s is None:
        return None
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _tools(lg: EvidenceLedger, calls: list[str]) -> list[str]:
    """Tool names of calls, in call order, deduplicated -- the provenance trail."""
    out = []
    for c in sorted(calls, key=lambda x: int(x.split(":")[1])):
        node = lg.node(c)
        if node and node["tool"] not in out:
            out.append(node["tool"])
    return out


def _node_payload(lg: EvidenceLedger, node_id: str, d: dict) -> dict:
    kind = d["kind"]
    out = {"id": node_id, "kind": kind, "label": d.get("label") or d.get("etype") or d.get("atype"),
           "name": d.get("name") or d.get("title") or node_id, "cite": d.get("cite") or node_id,
           "returned_by": _tools(lg, lg.returned_by(node_id)), "mentions": d.get("mention_count", 0)}
    if recalled(lg, node_id):
        out["recalled"] = True
        out["origin"] = d.get("origin")
    if kind == "evidence":
        out["source"] = d.get("source")
        out["text"] = _clip(d.get("expanded_text") or d.get("text"))
        out["name"] = d.get("title") or node_id
        out["status"] = ("recalled" if out.get("recalled") else
                         "fetched" if (out["text"] or d.get("read")) else "referenced")
        if not out["text"] and d.get("read"):
            out["text"] = "(text not kept in memory -- re-read it with resolve_citations)"
    elif kind == "aggregate":
        out["source"] = "derived"
        a = d.get("atype")
        if a in ("go_group", "go_profile"):
            out["text"] = _clip(f"Genes: {', '.join(map(str, d.get('genes') or []))}"
                                + (f" | fold enrichment {d['fold_enrichment']}" if d.get("fold_enrichment") else "")
                                + (f" | IC {d['information_content']}" if d.get("information_content") else ""))
        elif a == "cypher":
            out["text"] = _clip(f"{d.get('query')}\n{d.get('n_rows')} rows")
        elif a == "path":
            out["text"] = " → ".join(d.get("genes") or [])
        else:
            out["text"] = _clip(d.get("coverage") or d.get("breakdown"))
    else:
        out["source"] = "neo4j"
        bits = [d.get(k) for k in ("full_name", "gene_synonym", "definition") if d.get(k)]
        if d.get("controls"):
            bits.append("controls: " + "; ".join(f"{c['role']} {c['entity']}" for c in d["controls"][:8]))
        if d.get("genes"):
            bits.append("genes: " + ", ".join(d["genes"][:15]))
        out["text"] = _clip(" | ".join(map(str, bits))) if bits else None
    return out


def _edge_payload(lg: EvidenceLedger, u: str, v: str, k: str, d: dict) -> dict:
    keep = ("rel", "sign", "mechanism", "reaction", "reaction_id", "via", "evidence", "sentence", "cite")
    return {"id": f"{u}|{v}|{k}", "source": u, "target": v,
            "returned_by": _tools(lg, d.get("calls") or []),
            **{x: (_clip(d[x], 400) if x == "sentence" else d[x]) for x in keep if d.get(x)}}


# --- theme provenance ----------------------------------------------------------------

# What a theme can rest on to count as a grouping FROM THE DATABASE: groups the
# synthesis tools computed, and the KG's own groupings of genes.
GROUPING_AGGREGATES = {"go_group", "go_profile", "shared_machinery"}
GROUPING_LABELS = {"Pathway", "Ontology", "Complex", "DefinedSet", "CandidateSet"}


def _grouping_name(lg: EvidenceLedger, node_id: str, d: dict) -> str:
    if d.get("name"):
        return str(d["name"])
    for m in lg.graph.successors(node_id):              # a GO group / profile names its term
        if any(e.get("rel") == "term" for e in lg.graph.get_edge_data(node_id, m).values()):
            return str(lg.graph.nodes[m].get("name") or m)
    genes = d.get("genes") or []
    return "shared by " + ", ".join(map(str, genes[:6])) + ("…" if len(genes) > 6 else "") if genes else node_id


def theme_provenance(lg: EvidenceLedger, cites: list[str]) -> dict:
    """
    Where a theme comes from, decided by the ledger, not the caller: `database`
    when every cite resolves to a grouping retrieved this session, else `model`
    (the model's own grouping -- uncited, or citing something that is not a
    retrieved grouping, which `basis` then says).
    """
    basis = []
    for c in cites:
        status, nodes, _ = resolve(lg, c)
        d = lg.node(nodes[0]) if nodes else None
        grouping = bool(status == "fetched" and d and (
            (d["kind"] == "aggregate" and d.get("atype") in GROUPING_AGGREGATES)
            or (d["kind"] == "entity" and d.get("label") in GROUPING_LABELS)))
        basis.append({"cite": c, "status": "grouping" if grouping else ("not a grouping" if d else status),
                      **({"name": _grouping_name(lg, nodes[0], d),
                          "returned_by": _tools(lg, lg.returned_by(nodes[0]))} if d else {})})
    source = "database" if basis and all(b["status"] == "grouping" for b in basis) else "model"
    return {"source": source, "basis": basis}


# --- the view ------------------------------------------------------------------------

def build_view(lg: EvidenceLedger, claims: list[dict] | None = None, include: list[str] | None = None,
               scope: str = "claims", neighbors: int = 0, collapse: list[dict] | None = None,
               max_nodes: int = 40, themes: list[dict] | None = None) -> dict:
    """
    themes  [{name, cites}]: what a claim's `theme` rests on. A theme citing a
            retrieved grouping (common_ontology group, shared_machinery row,
            pathway, GO term, complex) is marked `database`; any other theme is
            the model's own grouping, and marked so.
    """
    claims = claims or []
    declared = {str(t["name"]): list(t.get("cites") or []) for t in themes or [] if t.get("name")}
    g = lg.graph
    content = {n for n, d in g.nodes(data=True) if d["kind"] not in HIDDEN}

    # 1. claims: resolve every cite
    claim_out, cited_nodes, cited_edges = [], [], set()
    unknown = []
    for i, cl in enumerate(claims):
        statuses = {}
        for c in cl.get("cites", []):
            status, nodes, edges = resolve(lg, c)
            statuses[c] = status
            if status == "unknown":
                unknown.append(c)
            cited_nodes += [(n, i) for n in nodes]
            cited_edges |= {(e, i) for e in edges}
        vals = set(statuses.values())
        overall = ("unsupported" if not vals or vals == {"unknown"} else
                   "supported" if vals <= {"fetched", "recalled"} else "partial")
        claim_out.append({"i": i, "label": cl.get("label") or f"claim {i + 1}",
                          "status": overall, "cites": statuses,
                          **({"theme": str(cl["theme"])} if cl.get("theme") else {})})

    # 2. selection, in priority order: cited, included, scope, neighbours
    priority: dict[str, int] = {}

    def add(n: str, p: int) -> None:
        if n in content and (n not in priority or p < priority[n]):
            priority[n] = p

    for n, _ in cited_nodes:
        add(n, CITED)
    # Cited nodes bring what they are about, and those inherit the claims citing
    # them so a theme box holds the genes, not just the label:
    #   aggregate (GO group, shared machinery, path) -> its members; drawn alone
    #             it is a category with nothing in it
    #   passage / record -> the gene it describes
    #   reaction  -> the genes that take part in it (role edges, `via` kept);
    #             passages and reactions only meet through genes, so without
    #             this a view of them has no edges at all. Specific actors
    #             outrank machinery under the cap (_breadth).
    breadth: dict[str, int] = {}
    for n, i in list(cited_nodes):
        d = g.nodes[n]
        if d["kind"] == "aggregate":
            for m in g.successors(n):
                if any(e.get("rel") in ("groups", "term", "shares", "traverses")
                       for e in g.get_edge_data(n, m).values()):
                    add(m, ANCHOR)
                    cited_nodes.append((m, i))
        elif d["kind"] == "evidence":
            for m in g.successors(n):
                if any(e.get("rel") == "describes" for e in g.get_edge_data(n, m).values()):
                    add(m, ANCHOR)
                    cited_nodes.append((m, i))
        elif d.get("label") == "Reaction":
            for m in set(g.successors(n)) | set(g.predecessors(n)):
                edges = list((g.get_edge_data(n, m) or {}).values()) + list((g.get_edge_data(m, n) or {}).values())
                if g.nodes[m].get("label") == "Gene" and any(e.get("rel") in ROLES for e in edges):
                    add(m, PARTICIPANT)
                    cited_nodes.append((m, i))
                    breadth[m] = min(breadth.get(m, MACHINERY), _breadth(g, m, n))
    for inc in include or []:
        _, nodes, _ = resolve(lg, inc)
        for n in nodes:
            add(n, ANCHOR)
    if scope == "question" and lg.question:
        for n in lg.in_namespace(("q", lg.question)):
            add(n, SCOPE)
    elif scope == "session":
        for n in content:
            add(n, SCOPE)
    sub = g.subgraph(content)
    for _ in range(max(0, min(neighbors, 2))):
        for n in list(priority):
            for m in list(sub.successors(n)) + list(sub.predecessors(n)):
                add(m, NEIGHBOR)

    # 3. cap: keep by priority, then (participants) breadth, then degree; fold
    #    the overflow into per-label groups
    ranked = sorted(priority, key=lambda n: (priority[n], breadth.get(n, 0) if priority[n] == PARTICIPANT else 0,
                                             -sub.degree(n), n))
    keep, overflow = ranked[:max_nodes], ranked[max_nodes:]
    groups: dict[str, dict] = {}
    member_of: dict[str, str] = {}
    for grp in collapse or []:
        gid = f"group:{grp.get('label', 'group')}"
        members = [m for m in (resolve(lg, x)[1] for x in grp.get("ids", [])) for m in m]
        groups[gid] = {"label": grp.get("label", "group"), "members": members, "auto": False}
        for m in members:
            member_of[m] = gid
    by_label = defaultdict(list)
    for n in overflow:
        if n not in member_of:
            by_label[g.nodes[n].get("label") or g.nodes[n]["kind"]].append(n)
    for label, members in by_label.items():
        gid = f"group:+{len(members)} {label}"
        groups[gid] = {"label": f"+{len(members)} more {label}", "members": members, "auto": True}
        for m in members:
            member_of[m] = gid

    shown = [n for n in keep if n not in member_of]
    node_claims = defaultdict(set)
    for n, i in cited_nodes:
        node_claims[n].add(i)

    # A node belongs to a theme when every claim citing it has that one theme;
    # evidence shared across themes stays outside, between them.
    claim_theme = {c["i"]: c.get("theme") for c in claim_out}

    def theme_of(n: str) -> str | None:
        themes = {claim_theme.get(i) for i in node_claims.get(n, ())}
        return themes.pop() if len(themes) == 1 and None not in themes else None

    def payload(n: str) -> dict:
        p = _node_payload(lg, n, g.nodes[n])
        p["cited"] = priority.get(n) == 0
        p["claims"] = sorted(node_claims.get(n, ()))
        if theme_of(n):
            p["theme"] = theme_of(n)
        return p

    nodes = [payload(n) for n in shown]
    group_members: dict[str, list[dict]] = {}
    for gid, grp in groups.items():
        names = [g.nodes[m].get("name") or m for m in grp["members"]]
        nodes.append({"id": gid, "kind": "group", "label": "group", "name": grp["label"],
                      "text": _clip(", ".join(map(str, names))), "members": grp["members"],
                      "cited": False, "claims": sorted({i for m in grp["members"] for i in node_claims.get(m, ())})})
        # Panel-only: the members themselves, so a click can expand the group.
        group_members[gid] = [{**payload(m), "group": gid} for m in grp["members"]]

    # 4. edges among what is shown; edges into a group are redirected to it, and
    #    kept with their real endpoints in group_edges for when it is expanded
    visible = set(shown) | set(groups)
    edge_claims = defaultdict(set)
    for e, i in cited_edges:
        edge_claims[e].add(i)
    edges, group_edges, seen = [], [], set()
    for u, v, k, d in sub.edges(keys=True, data=True):
        if d.get("rel") in STRUCTURAL:
            continue
        reachable = lambda x: x in visible or x in member_of  # noqa: E731
        if (u in member_of or v in member_of) and reachable(u) and reachable(v):
            ge = _edge_payload(lg, u, v, k, d)
            ge["claims"] = sorted(edge_claims.get((u, v, k), ()))
            ge["cited"] = bool(ge["claims"])
            group_edges.append(ge)
        a, b = member_of.get(u, u), member_of.get(v, v)
        if a not in visible or b not in visible or a == b:
            continue
        sig = (a, b, d.get("rel"), d.get("qualifier")) if (a != u or b != v) else (u, v, k)
        if sig in seen:
            continue
        seen.add(sig)
        e = _edge_payload(lg, a, b, k, d)
        e["claims"] = sorted(edge_claims.get((u, v, k), ()))
        e["cited"] = bool(e["claims"])
        edges.append(e)

    used: dict[str, list[int]] = {}
    for c in claim_out:
        if c.get("theme"):
            used.setdefault(c["theme"], []).append(c["i"])
    return {"release": lg.release, "session": lg.session, "scope": scope, "claims": claim_out,
            "themes": [{"name": k, "claims": v, **theme_provenance(lg, declared.get(k, []))}
                       for k, v in used.items()],
            "nodes": nodes, "edges": edges, "group_members": group_members, "group_edges": group_edges,
            "unknown": sorted(set(unknown)),
            "overflow": sum(len(gp["members"]) for gp in groups.values() if gp["auto"])}


def receipt(view: dict) -> str:
    """The one line the model sees."""
    cl = view["claims"]
    counts = {s: sum(c["status"] == s for c in cl) for s in ("supported", "partial", "unsupported")}
    parts = [f"view {view['view_id']}: {len(view['nodes'])} nodes, {len(view['edges'])} edges"]
    if view.get("themes"):
        n_db = sum(t.get("source") == "database" for t in view["themes"])
        parts.append(f"{len(view['themes'])} themes ({n_db} from database)")
    if view["overflow"]:
        parts.append(f"{view['overflow']} more folded into groups")
    if cl:
        parts.append("claims " + ", ".join(f"{v} {k}" for k, v in counts.items() if v))
    weak = [f"{c['i'] + 1}: " + ", ".join(f"{x} {s}" for x, s in c["cites"].items() if s not in ("fetched", "recalled"))
            for c in cl if c["status"] != "supported"]
    n_recalled = sum(s == "recalled" for c in cl for s in c["cites"].values())
    if n_recalled:
        parts.append(f"{n_recalled} cites from earlier chats")
    if weak:
        parts.append("check claim " + "; ".join(weak))
    shaky = [f"{t['name']}: " + ", ".join(f"{b['cite']} {b['status']}" for b in t["basis"] if b["status"] != "grouping")
             for t in view.get("themes", []) if t.get("basis") and t.get("source") != "database"]
    if shaky:
        parts.append("theme shown as model grouping -- " + "; ".join(shaky))
    return " | ".join(parts) + "."


# --- Mermaid: the fallback for clients without MCP Apps --------------------------------

_MERMAID_UNSAFE = re.compile(r'["\[\]{}()<>|#;`]')


def to_mermaid(view: dict, max_nodes: int = 25) -> str:
    """
    A compact Mermaid flowchart of a view, for clients that cannot render the
    panel. Only clients WITHOUT Apps get this -- it costs the model tokens --
    so it is capped (cited nodes first) and labels are kept short.
    """
    nodes = sorted(view["nodes"], key=lambda n: (not n.get("cited"), n["kind"] == "group"))[:max_nodes]
    ids = {n["id"]: f"n{i}" for i, n in enumerate(nodes)}

    def label(n: dict) -> str:
        text = _MERMAID_UNSAFE.sub(" ", str(n.get("name") or n["id"]))
        text = text if len(text) <= 36 else text[:35] + "…"
        tag = n.get("label") if n["kind"] == "entity" else n["kind"]
        return f"{text} - {tag}" if tag and tag not in ("Gene",) else text

    lines = ["graph LR"]
    for n in nodes:
        nid, lab = ids[n["id"]], label(n)
        if n["kind"] == "evidence":
            lines.append(f'  {nid}[/"{lab}"/]')
        elif n.get("label") == "Reaction":
            lines.append(f'  {nid}(["{lab}"])')
        elif n["kind"] in ("aggregate", "group"):
            lines.append(f'  {nid}{{{{"{lab}"}}}}')
        else:
            lines.append(f'  {nid}["{lab}"]')
    for e in view["edges"]:
        if e["source"] in ids and e["target"] in ids:
            rel = _MERMAID_UNSAFE.sub(" ", e.get("rel", ""))
            if e.get("sign"):
                rel += " +" if e["sign"] == "positive" else " −"
            arrow = "==>" if e.get("cited") else "-->"
            lines.append(f'  {ids[e["source"]]} {arrow}|{rel}| {ids[e["target"]]}')
    cited = [ids[n["id"]] for n in nodes if n.get("cited")]
    if cited:
        lines.append("  classDef cited stroke-width:3px")
        lines.append(f"  class {','.join(cited)} cited")
    hidden = len(view["nodes"]) - len(nodes)
    if hidden > 0:
        lines.append(f"  more[\"+{hidden} more in the full view\"]")
    return "\n".join(lines)
