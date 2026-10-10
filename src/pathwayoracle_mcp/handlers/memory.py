"""
Memory of earlier chats (docs/memory.md, section 6).

Recall is server-driven: a digest of recent episodes is in the server
instructions at connection start, and tool results point to earlier episodes
about the entities they contain. These two tools are for drilling in:

    recall          episodes ranked by ids (cited > read > seen) and text: the
                    summaries, for navigating; with ids, also the claims that
                    rest on them
    recall_episode  one episode's triplets, claims first: each claim with the
                    triplets it rests on, then the rest grouped by hub. Loaded
                    into this chat's ledger as `recalled` evidence -- citable,
                    and drawn as from an earlier chat

Neither is a recipe step: memory is not part of answering a question.

Token budget: summaries navigate, triplets ground. recall_episode renders every
claim's grounding (an episode has a handful of claims) and compresses the
remaining triplets into one line per hub (`TP53 -ACTS_ON(+)-> [60] BAX, ...`);
`ids` narrows it to the triplets around given entities.
"""
from __future__ import annotations

from collections import Counter

import networkx as nx

from ..dispatch import ToolkitError
from ..evidence.consolidate import (LINK_RELS, NAMED, _short, claim_cites, claim_weights, claims_of,
                                    facts_by_cite, is_fact, rank_entities)

MAX_GROUNDS = 8          # cites rendered per claim
MAX_NAMES = 10           # members named on one grouped line
MAX_GROUPS = 12          # grouped lines for the triplets no claim rests on
MAX_GROUPS_IDS = 30      # ... when narrowed to ids
MAX_ENTITIES = 20
MAX_EVIDENCE = 25
ROLE_ORDER = ("CATALYST", "STIMULATOR", "INHIBITOR", "REACTANT", "PRODUCT")


def recall(tk, query: str | None, ids: list[str] | None, limit: int) -> dict:
    if not (query or ids):
        raise ToolkitError("Give a query, ids, or both.")
    rows = tk.memory.search(query, ids, max(1, min(limit, 20)), current_session=tk.ledger().session)
    return {"n_results": len(rows), "results": rows}


# --- rendering triplets ------------------------------------------------------------------

def _name(g: nx.MultiDiGraph, x: str, n: int = 40) -> str:
    d = g.nodes[x]
    return _short(str(d.get("name") or d.get("title") or x), n)


def _label(g: nx.MultiDiGraph, x: str) -> str:
    d = g.nodes[x]
    name = d.get("name") or d.get("title") or d.get("label")
    return f"{x} {name}" if name and name != x else x


def _rel(d: dict) -> str:
    """The predicate, with the sign when the tool gave one: ACTS_ON(+), ACTS_ON(-)."""
    sign = {"positive": "+", "negative": "-"}.get(d.get("sign"))
    return f"{d['rel']}({sign})" if sign else d["rel"]


def _fact(g: nx.MultiDiGraph, u: str, v: str, d: dict) -> str:
    """One triplet: subject -predicate-> object (mechanism; reaction it was recorded in)."""
    bits = [d["mechanism"]] if d.get("mechanism") else []
    rid = d.get("reaction_id")
    if rid:
        bits.append(f"{rid} {_name(g, rid, 60)}" if g.has_node(rid) and g.nodes[rid].get("name") else rid)
    return f"{_name(g, u)} -{_rel(d)}-> {_name(g, v)}" + (f" ({'; '.join(bits)})" if bits else "")


def _members(g: nx.MultiDiGraph, x: str) -> list[str]:
    """The genes of a participant (complex, set, protein form): its gene -in_entity-> links, else its own list."""
    genes = sorted(_name(g, u) for u, _, d in g.in_edges(x, data=True) if d.get("rel") == "in_entity")
    return genes or [str(m) for m in g.nodes[x].get("genes") or []]


def _reaction(g: nx.MultiDiGraph, cite: str, facts: list) -> str:
    """
    A cited reaction: each role with the entity that plays it and that entity's
    genes -- `CATALYST p-S1981-ATM [ATM]` -- so a complex's role is not read as
    each member's. Gene-level role triplets only when the reaction was never
    resolved to its participating entities.
    """
    rid = cite.removeprefix("reaction:")
    parts = [(v if u == rid else u, d["rel"], d.get("via")) for u, v, d in facts]
    entities = [p for p in parts if g.nodes[p[0]].get("label") != "Gene"]
    roles: dict[str, list] = {}
    for x, r, via in entities or parts:
        if entities:
            genes = _members(g, x)
            shown = ", ".join(genes[:6]) + (f" +{len(genes) - 6}" if len(genes) > 6 else "")
            text = _name(g, x) + (f" [{shown}]" if genes and genes != [_name(g, x)] else "")
        else:
            text = _name(g, x) + (f" (via {_short(str(via), 40)})" if via and via != _name(g, x) else "")
        if text not in roles.setdefault(r, []):
            roles[r].append(text)
    order = sorted(roles, key=lambda r: ROLE_ORDER.index(r) if r in ROLE_ORDER else 9)
    body = "; ".join(f"{r} " + ", ".join(roles[r][:4]) + (f" +{len(roles[r]) - 4}" if len(roles[r]) > 4 else "")
                     for r in order)
    name = _name(g, rid, 70) if g.has_node(rid) else ""
    return f"{cite} {name}".strip() + (f": {body}" if body else "")


def _ground(g: nx.MultiDiGraph, cite: str, targets: set[str], facts: list) -> str:
    """What one cite of a claim resolves to in the episode's triplets."""
    if cite.startswith("reaction:"):
        return _reaction(g, cite, facts)
    evidence = [x for x in targets if g.nodes[x].get("kind") == "evidence"]
    if evidence:
        d = g.nodes[evidence[0]]
        about = sorted({_name(g, v) for u, v, _ in facts if u == evidence[0]})
        what = d.get("title") or d.get("etype") or d.get("source") or ""
        return f"{cite} {what}".strip() + (f" about {', '.join(about)}" if about else "")
    if facts:
        return "; ".join(dict.fromkeys(_fact(g, u, v, d) for u, v, d in facts)) + f" [{cite}]"
    return f"{cite} " + ", ".join(_label(g, x) for x in sorted(targets))


def _grouped(g: nx.MultiDiGraph, facts: list, weight: Counter, cap: int) -> list[str]:
    """
    Triplets compressed to one line per hub: each fact joins the larger of its
    subject's or its object's (predicate, sign) group. A group of one is the
    triplet itself; members are named claim-relevant first.
    """
    out_n = Counter((u, _rel(d)) for u, v, d in facts)
    in_n = Counter((v, _rel(d)) for u, v, d in facts)
    groups: dict[tuple, dict] = {}
    for u, v, d in facts:
        r = _rel(d)
        key, member = (("out", u, r), v) if out_n[u, r] >= in_n[v, r] else (("in", v, r), u)
        groups.setdefault(key, {}).setdefault(member, (u, v, d))
    ranked = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    lines = []
    for (side, hub, r), members in ranked[:cap]:
        if len(members) == 1:
            lines.append(_fact(g, *next(iter(members.values()))))
            continue
        ms = sorted(members, key=lambda m: (-weight[m], NAMED.get(g.nodes[m].get("label"), 9), _name(g, m)))
        # Gene symbols are short; reaction and pathway names are long: fewer of them, clipped.
        genes = sum(g.nodes[m].get("label") == "Gene" for m in ms) * 2 >= len(ms)
        cap, width = (MAX_NAMES, 40) if genes else (MAX_NAMES // 2, 32)
        names = ", ".join(_name(g, m, width) for m in ms[:cap]) + (f" +{len(ms) - cap}" if len(ms) > cap else "")
        lines.append(f"{_name(g, hub)} -{r}-> [{len(ms)}] {names}" if side == "out"
                     else f"[{len(ms)}] {names} -{r}-> {_name(g, hub)}")
    if len(ranked) > cap:
        lines.append(f"(+{len(ranked) - cap} more groups; pass ids to narrow)")
    return lines


# --- recall_episode -----------------------------------------------------------------------

def recall_episode(tk, episode_id: str, ids: list[str] | None) -> dict:
    eg = tk.memory.episode_subgraph(episode_id)
    d = eg.nodes[episode_id]
    idset = set(ids or [])
    by_cite = facts_by_cite(eg)
    weight: Counter = Counter()

    # Claims first, each with the triplets it rests on: the grounding.
    claims, grounding = [], set()
    for c in claims_of(eg, episode_id):
        w = claim_weights(eg, c, by_cite)
        weight.update(w)
        ents = set(w)
        cites = claim_cites(eg, c)
        for cite in cites:
            grounding.update(id(f) for *_, f in by_cite.get(cite, ()))
        row = {"claim": f"{eg.nodes[c].get('label')} (model)"}
        if not idset or ents & idset:
            lines = [_ground(eg, cite, targets, by_cite.get(cite, [])) for cite, targets in cites.items()]
            row["rests_on"] = lines[:MAX_GROUNDS] + ([f"(+{len(lines) - MAX_GROUNDS} more cites)"]
                                                     if len(lines) > MAX_GROUNDS else [])
        claims.append(row)

    # The other triplets, grouped: what the exchange found but no claim used.
    facts = [(u, v, f) for u, v, f in eg.edges(data=True) if is_fact(f)]
    rest = [(u, v, f) for u, v, f in facts if f.get("rel") not in LINK_RELS and id(f) not in grounding
            and (not idset or u in idset or v in idset)]
    evidence = [f"{v} {f['rel']}" for u, v, f in eg.out_edges(episode_id, data=True)
                if f.get("rel") in ("cited", "read", "seen")]
    ranked = rank_entities(eg, episode_id, by_cite)
    return {
        "episode": tk.memory.row(episode_id, d, current_session=tk.ledger().session),
        "claims": claims,
        "facts": {"n": len(facts), "by_predicate": dict(Counter(f["rel"] for *_, f in facts).most_common()),
                  "not_in_claims" if not idset else "around_ids":
                      _grouped(eg, rest, weight, MAX_GROUPS_IDS if idset else MAX_GROUPS)},
        "entities": [_label(eg, x) + f" ({eg.nodes[x].get('label')})" for x in ranked[:MAX_ENTITIES]],
        "n_entities": len(ranked),
        "evidence": sorted(evidence, key=lambda s: s.split()[-1] != "cited")[:MAX_EVIDENCE],
        "n_evidence": len(evidence),
        "how_to_use": "Each claim lists the triplets it rested on when it was made (subject -predicate-> "
                      "object; (+)/(-) sign; mechanism; reaction). All of this episode's ids are now in "
                      "this chat's evidence: cite them like any other (shown as from an earlier chat). "
                      "Pass ids to see the triplets around given entities. Text bodies were not kept: "
                      "re-read with read_snippets / resolve_citations if the answer needs the words.",
        # Ledger-only: the whole episode, so every id above can be cited.
        "_triplets": nx.node_link_data(eg, edges="edges"),
    }
