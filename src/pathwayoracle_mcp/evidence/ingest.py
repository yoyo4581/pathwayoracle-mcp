"""
Tool result -> ledger: one small mapper per tool.

Each mapper reads only what the tool returned -- no extra queries -- and keys
everything by the ids the result already carries, so a node or edge in the
ledger is exactly as trustworthy as the call that produced it. A tool without
a mapper still gets its call recorded; its result just adds no nodes.

Mappers tolerate older result shapes (fields missing in the registry version
the server is running), because the registry can lag the code.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from .ledger import EvidenceLedger

Mapper = Callable[[EvidenceLedger, dict, dict, str], None]
MAPPERS: dict[str, Mapper] = {}


def mapper(*names: str):
    def wrap(fn: Mapper) -> Mapper:
        for n in names:
            MAPPERS[n] = fn
        return fn
    return wrap


def ingest(ledger: EvidenceLedger, tool: str, arguments: dict, result: dict, call: str) -> None:
    fn = MAPPERS.get(tool)
    if fn is not None and isinstance(result, dict):
        fn(ledger, arguments, result, call)


# --- helpers -------------------------------------------------------------------

def go_id(x: str) -> str:
    return x.replace(":", "_") if x else x


def entity(lg: EvidenceLedger, eid: str | None, label: str | None, call: str, name: str | None = None,
           **attrs: Any) -> str | None:
    if not eid:
        return None
    return lg.upsert_node(eid, "entity", call, label=label, name=name, **attrs)


def gene(lg, uid, call, symbol=None, **attrs):
    return entity(lg, uid, "Gene", call, symbol, **attrs)


def evidence(lg: EvidenceLedger, eid: str, etype: str, call: str, source: str, text: str | None = None,
             **attrs: Any) -> str:
    # `read`: its text reached the model. Kept when consolidation drops the text,
    # so a view still knows it was read (docs/memory.md, evidence status).
    if text or attrs.get("expanded_text"):
        attrs["read"] = True
    return lg.upsert_node(eid, "evidence", call, etype=etype, source=source, text=text, **attrs)


def aggregate(lg: EvidenceLedger, aid: str, atype: str, call: str, **attrs: Any) -> str:
    return lg.upsert_node(aid, "aggregate", call, atype=atype, **attrs)


# --- anchor --------------------------------------------------------------------

@mapper("find_entities")
def _find_entities(lg, args, r, call):
    for row in r.get("results", []):
        entity(lg, go_id(row["id"]) if row.get("label") == "Ontology" else row["id"],
               row.get("label"), call, row.get("name"), cite=row.get("cite"))


@mapper("describe_node")
def _describe_node(lg, args, r, call):
    labels = r.get("labels") or []
    props = r.get("properties") or {}
    keep = {k: props[k] for k in ("full_name", "gene_synonym", "entrez_ids", "definition",
                                  "hasOBONamespace", "compartment") if k in props}
    entity(lg, r.get("id"), labels[0] if labels else None, call, r.get("name"),
           moves=r.get("moves"), cite=r.get("cite"), **keep)


@mapper("expand")
def _expand(lg, args, r, call):
    src = r.get("from")
    label = args.get("label")
    if not lg.has(src):
        entity(lg, src, label, call)
    else:
        lg.upsert_node(src, "entity", call)
    rel = r.get("rel")
    for nb in r.get("neighbors", []):
        nid = go_id(nb["id"]) if (nb.get("labels") or [""])[0] == "Ontology" else nb["id"]
        entity(lg, nid, (nb.get("labels") or [None])[0], call, nb.get("name"))
        props = nb.get("rel_props") or {}
        qual = props.get("reaction_id") or (",".join(props["evidence"]) if props.get("evidence") else None)
        a, b = (src, nid) if nb.get("direction") == "out" else (nid, src)
        lg.upsert_edge(a, b, rel, qualifier=qual, call=call, cite=nb.get("cite"),
                       **{k: v for k, v in props.items() if k in ("mechanism", "sign", "reaction_id",
                                                                   "pathway_ids", "evidence", "stoichiometry")})


@mapper("search_proteins")
def _search_proteins(lg, args, r, call):
    for hit in r.get("results", []):
        uid = hit["uniprot_id"]
        gene(lg, uid, call, entrez_id=hit.get("entrez_id"), n_chunks=hit.get("n_chunks"))
        for s in hit.get("snippets", []):
            pid = s.get("cite") or f"qdrant:{uid}#{s['chunk_idx']}"
            evidence(lg, pid, "passage", call, "qdrant", s.get("text"), chunk_idx=s.get("chunk_idx"),
                     uniprot_id=uid, score=s.get("score"), query=r.get("query"))
            lg.upsert_edge(pid, uid, "describes", call=call, cite=pid)


@mapper("read_snippets")
def _read_snippets(lg, args, r, call):
    uid = r.get("uniprot_id") or args.get("uniprot_id")
    gene(lg, uid, call)
    for s in r.get("snippets", []):
        pid = s.get("cite") or f"qdrant:{uid}#{s['chunk_idx']}"
        evidence(lg, pid, "passage", call, "qdrant", s.get("text"), chunk_idx=s.get("chunk_idx"),
                 uniprot_id=uid, score=s.get("score"), query=r.get("query"))
        lg.upsert_edge(pid, uid, "describes", call=call, cite=pid)


@mapper("expand_snippet")
def _expand_snippet(lg, args, r, call):
    uid = r.get("uniprot_id")
    pid = r.get("cite") or f"qdrant:{uid}#{r.get('chunk_idx')}"
    gene(lg, uid, call)
    # The widened text is the same passage seen with more context.
    evidence(lg, pid, "passage", call, "qdrant", chunk_idx=r.get("chunk_idx"), uniprot_id=uid,
             expanded_text=r.get("text"), expanded_sentences=r.get("sentences"))
    lg.upsert_edge(pid, uid, "describes", call=call, cite=pid)


@mapper("read_record")
def _read_record(lg, args, r, call):
    uid = r.get("uniprot_id")
    rid = r.get("cite") or f"qdrant:{uid}"
    gene(lg, uid, call, entrez_id=r.get("entrez_id"), n_chunks=r.get("n_chunks"))
    evidence(lg, rid, "record", call, "qdrant", r.get("text"), uniprot_id=uid)
    lg.upsert_edge(rid, uid, "describes", call=call, cite=rid)


# --- literature ----------------------------------------------------------------

def _pubmed(lg, pmid, call, text=None, **attrs):
    return evidence(lg, f"pubmed:{pmid}", "abstract", call, "pubmed", text, pmid=str(pmid), **attrs)


@mapper("snippet_sources")
def _snippet_sources(lg, args, r, call):
    uid = r.get("uniprot_id")
    pid = r.get("passage_cite") or f"qdrant:{uid}#{r.get('chunk_idx')}"
    gene(lg, uid, call)
    evidence(lg, pid, "passage", call, "qdrant", chunk_idx=r.get("chunk_idx"), uniprot_id=uid)
    lg.upsert_edge(pid, uid, "describes", call=call, cite=pid)
    for s in r.get("sources", []):
        pm = _pubmed(lg, s["pmid"], call)
        # The passage cites the study, for this sentence.
        lg.upsert_edge(pid, pm, "cites", qualifier=str(s.get("sentence_idx")), call=call,
                       sentence=s.get("sentence"), cite=s.get("cite"))


@mapper("cited_pmids")
def _cited_pmids(lg, args, r, call):
    for rec in r.get("results", []):
        uid = rec.get("uniprot_id")
        if not rec.get("found"):
            continue
        gene(lg, uid, call)
        rid = f"qdrant:{uid}"
        evidence(lg, rid, "record", call, "qdrant", uniprot_id=uid)
        lg.upsert_edge(rid, uid, "describes", call=call, cite=rid)
        for c in rec.get("citations", []):
            pm = _pubmed(lg, c["pmid"], call)
            lg.upsert_edge(rid, pm, "cites", qualifier=str(c.get("sentence_idx")), call=call,
                           sentence=c.get("sentence"), cite=c.get("cite"))


@mapper("read_abstracts", "search_abstracts")
def _abstracts(lg, args, r, call):
    for a in r.get("results", []):
        if a.get("found") is False:
            continue
        text = a.get("text") or ""
        title = text.split("\n", 1)[0].removeprefix("Title:").strip() if text.startswith("Title:") else None
        _pubmed(lg, a["pmid"], call, text, title=title, score=a.get("score"))


# --- graph: membership, interactions, reactions ------------------------------

@mapper("pathway_genes")
def _pathway_genes(lg, args, r, call):
    pw = entity(lg, r.get("pathway_id"), "Pathway", call, r.get("pathway"), n_genes=r.get("n_genes"))
    for g in r.get("genes", []):
        gene(lg, g["uniprot_id"], call, g.get("symbol"))
        lg.upsert_edge(g["uniprot_id"], pw, "member_of", call=call, cite=g.get("cite"))


@mapper("gene_pathways")
def _gene_pathways(lg, args, r, call):
    for row in r.get("results", []):
        pw = entity(lg, row["pathway_id"], "Pathway", call, row.get("pathway"), cite=row.get("cite"))
        for uid in row.get("genes", []):
            gene(lg, uid, call)
            lg.upsert_edge(uid, pw, "member_of", call=call, cite=f"member:{pw}>{uid}")


def _acts_on(lg, i: dict, call: str) -> None:
    src = gene(lg, i["source"], call, i.get("source_symbol"))
    dst = gene(lg, i["target"], call, i.get("target_symbol"))
    if i.get("reaction_id"):
        entity(lg, i["reaction_id"], "Reaction", call, i.get("reaction"))
    lg.upsert_edge(src, dst, "ACTS_ON", qualifier=i.get("reaction_id"), call=call,
                   mechanism=i.get("mechanism"), sign=i.get("sign"), reaction_id=i.get("reaction_id"),
                   reaction=i.get("reaction"), pathway_ids=i.get("pathway_ids"), cite=i.get("cite"))


@mapper("gene_interactions", "pathway_interactions")
def _interactions(lg, args, r, call):
    for i in r.get("interactions", []):
        _acts_on(lg, i, call)
    if "breakdown" in r:                     # v2: the whole scope, counted
        scope = ",".join(args.get("uniprot_ids", [])) or args.get("pathway_id", "")
        aid = f"breakdown:{scope}:{args.get('direction', 'both')}"
        aggregate(lg, aid, "edge_breakdown", call, breakdown=r["breakdown"], in_scope=r.get("in_scope"),
                  coverage=r.get("coverage"))
        for uid in args.get("uniprot_ids", []):
            if lg.has(uid):
                lg.upsert_edge(aid, uid, "summarises", call=call)


@mapper("interaction_paths")
def _paths(lg, args, r, call):
    for row in r.get("results", []):
        ids, steps = row.get("ids", []), row.get("steps", [])
        pid = row.get("cite") or "path:" + ",".join(ids)
        aggregate(lg, pid, "path", call, genes=row.get("genes"), hops=row.get("hops"))
        for k, st in enumerate(steps):
            if k + 1 < len(ids):
                # Steps carry gene NAMES in source/target; the ids come from `ids`.
                _acts_on(lg, {**st, "source": ids[k], "target": ids[k + 1],
                              "source_symbol": st.get("source"), "target_symbol": st.get("target")}, call)
        for uid in ids:
            lg.upsert_edge(pid, uid, "traverses", call=call)


ROLE_IN = ("REACTANT", "CATALYST", "INHIBITOR", "STIMULATOR")   # entity -> reaction


def _role_edge(lg, who: str, rxn: str, role: str, call: str, **attrs) -> None:
    if role == "PRODUCT":
        lg.upsert_edge(rxn, who, "PRODUCT", call=call, cite=f"reaction:{rxn}", **attrs)
    else:
        lg.upsert_edge(who, rxn, role, call=call, cite=f"reaction:{rxn}", **attrs)


@mapper("gene_reactions")
def _gene_reactions(lg, args, r, call):
    for row in r.get("reactions", []):
        rxn = entity(lg, row["reaction_id"], "Reaction", call, row.get("reaction"),
                     controls=row.get("controls"), cite=row.get("cite"))
        g = gene(lg, row["uniprot_id"], call, row.get("symbol"))
        for role in row.get("gene_roles", []):
            _role_edge(lg, g, rxn, role, call, via=row.get("via"))
    for reg in r.get("by_regulator", []):     # v2
        uid = reg.get("uniprot_id")
        who = gene(lg, uid, call, reg.get("regulator")) if uid else entity(
            lg, f"entity:{reg.get('regulator')}", "Entity", call, reg.get("regulator"))
        for rx in reg.get("reactions", []):
            if isinstance(rx, dict) and rx.get("reaction_id"):
                rxn = entity(lg, rx["reaction_id"], "Reaction", call, rx.get("reaction"))
                # `via`: the complex or set that actually plays the role.
                _role_edge(lg, who, rxn, reg["role"], call, via=rx.get("via"), via_type=rx.get("via_type"))
    if "coverage" in r:
        aid = f"reactions:{','.join(args.get('uniprot_ids', []))}:{args.get('role') or 'any'}"
        aggregate(lg, aid, "reaction_layer", call, total=r.get("total"),
                  n_regulators=r.get("n_regulators"), coverage=r.get("coverage"))


@mapper("reaction_details")
def _reaction_details(lg, args, r, call):
    """
    A reaction with ALL its constituents: each participant entity, and every
    gene inside it (`_members`, ledger-only) as a gene node linked two ways --
    `in_entity` to the complex or set it sits in, and straight to the reaction
    with the participant's role and `via` (as gene_reactions records it). So
    the view can draw who takes part in a cited reaction at the gene level.
    """
    for row in r.get("results", []):
        rxn = entity(lg, row["reaction_id"], "Reaction", call, row.get("reaction"), cite=row.get("cite"))
        for p in row.get("participants", []):
            pid = p.get("entity_id") or f"entity:{p.get('entity')}"
            entity(lg, pid, p.get("type") or "Entity", call, p.get("entity"), genes=p.get("genes"),
                   n_genes=p.get("n_genes"))
            _role_edge(lg, pid, rxn, p["role"], call)
            if p.get("type") == "Gene":
                continue
            if "_members" in p:
                gids = [gene(lg, m["uniprot_id"], call, m.get("symbol")) for m in p["_members"]]
            else:
                # Older result shape: symbols only, so link the genes this
                # session already holds under those names.
                gids = [gid for name in p.get("genes") or [] for gid in lg.find_by_name(name)]
            for gid in gids:
                # "PHF20" the protein entity and PHF20 the gene: one thing at
                # two levels, not a duplicate.
                lg.upsert_edge(gid, pid, "in_entity", call=call)
                _role_edge(lg, gid, rxn, p["role"], call, via=p.get("entity"), via_type=p.get("type"))


# --- ontology --------------------------------------------------------------------

@mapper("gene_go_annotations")
def _go_annotations(lg, args, r, call):
    for a in r.get("results", []):
        g = gene(lg, a["uniprot_id"], call, a.get("symbol"))
        t = entity(lg, go_id(a["go_id"]), "Ontology", call, a.get("term"), aspect=a.get("aspect"))
        ev = a.get("evidence") or []
        lg.upsert_edge(g, t, a["relation"], qualifier=",".join(ev), call=call, evidence=ev, cite=a.get("cite"))


@mapper("go_term_genes")
def _go_term_genes(lg, args, r, call):
    t = entity(lg, go_id(r.get("go_id")), "Ontology", call, r.get("term"))
    for g in r.get("genes", []):
        gene(lg, g["uniprot_id"], call, g.get("symbol"))
        lg.upsert_edge(g["uniprot_id"], t, "annotated_to", call=call, via_terms=g.get("via_terms"),
                       cite=g.get("cite"))


@mapper("pathway_go_profile")
def _pathway_go_profile(lg, args, r, call):
    for row in r.get("results", []):
        pw = entity(lg, row["pathway_id"], "Pathway", call, row.get("pathway"))
        t = entity(lg, go_id(row["go_id"]), "Ontology", call, row.get("term"))
        aid = row.get("cite") or f"profile:{pw}~{t}"
        aggregate(lg, aid, "go_profile", call, genes=row.get("genes"),
                  genes_with_term=row.get("genes_with_term"), pathway_genes=row.get("pathway_genes"),
                  genes_with_term_overall=row.get("genes_with_term_overall"),
                  fold_enrichment=row.get("fold_enrichment"))
        lg.upsert_edge(aid, pw, "profiles", call=call)
        lg.upsert_edge(aid, t, "term", call=call)


@mapper("common_ontology")
def _common_ontology(lg, args, r, call):
    ids = {}
    for grp in r.get("groups", []):
        t = entity(lg, go_id(grp["go_id"]), "Ontology", call, grp.get("term"))
        aid = grp.get("cite") or f"profile:set~{t}"
        ids[grp["go_id"]] = aid
        aggregate(lg, aid, "go_group", call, genes=grp.get("genes"), level=grp.get("level"),
                  also_covers=grp.get("also_covers"), absorbed=grp.get("absorbed"),
                  information_content=grp.get("information_content"),
                  genes_with_term_overall=grp.get("genes_with_term_overall"))
        lg.upsert_edge(aid, t, "term", call=call)
        # Symbols are listed sorted, uniprot ids sorted separately: pair by gene node name.
        for uid in grp.get("uniprot_ids", []):
            gene(lg, uid, call)
            lg.upsert_edge(aid, uid, "groups", call=call)
    for grp in r.get("groups", []):          # hierarchy: a group sits within its parent group
        if grp.get("parent") in ids:
            lg.upsert_edge(ids[grp["go_id"]], ids[grp["parent"]], "within", call=call)


@mapper("shared_machinery")
def _shared_machinery(lg, args, r, call):
    for row in r.get("results", []):
        aid = row.get("cite") or "profile:machinery~" + ",".join(row.get("uniprot_ids", []))
        aggregate(lg, aid, "shared_machinery", call, genes=row.get("genes"),
                  n_complexes=row.get("n_complexes"), n_reactions=row.get("n_reactions"))
        for uid, sym in zip(row.get("uniprot_ids", []), row.get("genes", [])):
            gene(lg, uid, call, sym)
            lg.upsert_edge(aid, uid, "groups", call=call)
        for ex in row.get("complex_examples") or []:
            entity(lg, ex["id"], "Complex", call, ex.get("name"))
            lg.upsert_edge(aid, ex["id"], "shares", call=call)
        for ex in row.get("reaction_examples") or []:
            entity(lg, ex["id"], "Reaction", call, ex.get("name"))
            lg.upsert_edge(aid, ex["id"], "shares", call=call)


# --- free-form and meta ----------------------------------------------------------

_IDS = re.compile(r"\b(R-HSA-\d+|GO_\d{7}|[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})\b")


@mapper("read_cypher")
def _read_cypher(lg, args, r, call):
    aid = r.get("cite")
    if not aid:
        return
    rows = r.get("rows", [])
    aggregate(lg, aid, "cypher", call, query=args.get("query"), n_rows=r.get("n_rows"),
              truncated=r.get("truncated"), rows=rows[:50])
    # Link the result to entities it mentions that the ledger already knows:
    # free-form rows have no schema, so never create entities from them.
    seen = set(_IDS.findall(str(rows)))
    for eid in seen:
        if lg.has(eid):
            lg.upsert_edge(aid, eid, "mentions", call=call)


@mapper("recall_episode")
def _recall_episode(lg, args, r, call):
    """
    An earlier chat's episode, loaded as `recalled` evidence. Nodes this chat
    already holds keep their own data (only a mention is added); new ones come
    with their origin. view.resolve marks a node recalled when only
    recall_episode calls returned it.
    """
    import networkx as nx

    data = r.get("_triplets")
    if not data:
        return
    g = nx.node_link_graph(data, directed=True, multigraph=True, edges="edges")
    origin = args.get("episode_id")
    loaded = set()
    for n, d in g.nodes(data=True):
        kind = d.get("kind")
        if kind not in ("entity", "evidence", "aggregate"):
            continue
        if lg.has(n):
            lg.upsert_node(n, lg.node(n)["kind"], call)
        else:
            attrs = {k: v for k, v in d.items() if k not in ("kind", "read", "mention_count")}
            lg.upsert_node(n, kind, call, **attrs, origin=origin, origin_release=d.get("release"))
        loaded.add(n)
    for u, v, k, d in g.edges(keys=True, data=True):
        if u in loaded and v in loaded:
            lg.upsert_edge(u, v, d.get("rel"), qualifier=d.get("qualifier"), call=call, cite=d.get("cite"),
                           sign=d.get("sign"), mechanism=d.get("mechanism"), reaction_id=d.get("reaction_id"),
                           via=d.get("via"), origin=origin)


@mapper("resolve_citations")
def _resolve(lg, args, r, call):
    for x in r.get("results", []):
        c = x.get("cite", "")
        if c.startswith("qdrant:") and x.get("text"):
            uid = c.removeprefix("qdrant:").split("#")[0]
            gene(lg, uid, call)
            evidence(lg, c, "passage" if "#" in c else "record", call, "qdrant", x.get("text"), uniprot_id=uid)
            lg.upsert_edge(c, uid, "describes", call=call, cite=c)
        elif c.startswith("pubmed:") and x.get("text"):
            _pubmed(lg, c.removeprefix("pubmed:"), call, x.get("text"))
