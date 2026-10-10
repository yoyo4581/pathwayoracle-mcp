"""
Knowledge-graph tools that need more than one stored query.

    read_cypher       the escape hatch: model-written Cypher, read-only
    describe_schema   the map Claude reads before writing Cypher
    common_ontology   GO terms that explain a gene set (set cover)
"""
from __future__ import annotations

import hashlib
import math

from ..dispatch import MAX_ROWS, ToolkitError
from ..registry.guard import REGISTRY_LABELS, TOOLKIT_LABEL

_REGISTRY = {TOOLKIT_LABEL, *REGISTRY_LABELS}


def read_cypher(tk, query: str, params: dict) -> dict:
    """
    Safety is the transaction, not the text: the query runs in a READ
    transaction, which Neo4j refuses to let write (APOC write procedures
    included), under a server-side timeout and a row cap. No parsing of the
    query is attempted -- a filter on Cypher text is easy to get around and
    gives false confidence; the access mode is enforced by the database.
    """
    try:
        rows, truncated = tk.clients.read(query, params, max_rows=MAX_ROWS)
    except Exception as exc:                                # noqa: BLE001
        code = getattr(exc, "code", "") or ""
        if "AccessMode" in code:
            raise ToolkitError("read_cypher is read-only; the query tried to write.") from None
        if "Timeout" in code or "TransactionTimedOut" in code:
            raise ToolkitError("Query timed out. Anchor it on specific ids, add LIMIT, or "
                               "aggregate instead of returning paths.") from None
        raise ToolkitError(f"Cypher error: {getattr(exc, 'message', exc)}") from None
    out = {"n_rows": len(rows), "truncated": truncated, "rows": rows,
           "cite": f"cypher:{hashlib.sha1(query.encode()).hexdigest()[:12]}"}
    if truncated:
        out["note"] = f"Stopped at {MAX_ROWS} rows; add LIMIT or aggregate."
    return out


def describe_schema(tk) -> dict:
    cache = getattr(tk, "_schema_cache", None)
    if cache is not None:
        return cache
    labels, _ = tk.clients.read(
        "CALL db.labels() YIELD label WHERE NOT label IN $skip "
        "CALL { WITH label MATCH (n) WHERE label IN labels(n) RETURN count(n) AS count } "
        "RETURN label, count ORDER BY count DESC", {"skip": sorted(_REGISTRY)})
    # Every Reactome node carries Entity as a SECOND label, and nodeLabels comes
    # back sorted -- so ['Entity', 'Gene'] puts Entity first. Credit each
    # property to every specific label in the combination, not to nodeLabels[0]
    # (which left Gene, Protein, Reaction, Pathway, ... with no properties).
    props, _ = tk.clients.read(
        "CALL db.schema.nodeTypeProperties() YIELD nodeLabels, propertyName "
        "WHERE propertyName IS NOT NULL "
        "UNWIND [l IN nodeLabels WHERE l <> 'Entity' AND NOT l IN $skip] AS label "
        "RETURN label, collect(DISTINCT propertyName) AS properties",
        {"skip": sorted(_REGISTRY)})
    by_label = {p["label"]: p["properties"] for p in props}
    out = {
        # Labels with no nodes are omitted: the KG keeps uniqueness constraints
        # for labels it no longer populates (Function, Glycan, Ortholog -- from
        # the KEGG model), and a constraint keeps its label registered.
        "nodes": [{"label": l["label"], "count": l["count"], "properties": by_label.get(l["label"], [])}
                  for l in labels if l["label"] != "Entity" and l["count"] > 0],
        "relationships": [
            {"name": r["name"], "meaning": r["meaning"], "types": r.get("kg_types"),
             "connects": [f"({p['source']})-[:{p['rel']}]->({p['target']}) x{p['count']}"
                          for p in r.get("patterns", [])]}
            for r in tk.registry.rel_types],
        "id_conventions": {
            name: {"label": i.get("kg_label"), "format": i.get("format"), "example": i.get("example")}
            for name, i in tk.registry.id_types.items() if i.get("kg_label")},
        "notes": [
            "Every node has an `id`; most have `name`. Gene.id is the UniProt accession, "
            "which is also the key into the protein-function text (read_snippets).",
            "Composition edges point PART -> WHOLE: (:Protein)-[:HAS_COMPONENT]->(:Complex). "
            "Pathway genes: (pw:Pathway)<-[:IN_PATHWAY]-(e)<-[:HAS_COMPONENT|HAS_MEMBER|"
            "HAS_CANDIDATE*0..6]-(:Protein)-[:IS_FORM_OF]->(g:Gene).",
            "GO ids are stored GO_0006915. Types starting with 'NOT|' are negated annotations.",
            "Entity is an extra label on every Reactome node (Gene, Protein, Reaction, Pathway, "
            "...); prefer the specific label.",
            "Not every entity reaches a gene: ~600 Proteins have no IS_FORM_OF->Gene edge -- "
            "mostly pathogen entities from infection pathways (e.g. viral 'Genomic RNA Segment').",
        ],
    }
    tk._schema_cache = out
    return out


# --- common_ontology -----------------------------------------------------

# How far up the GO hierarchy candidates are gathered. Merging (levels >= 2)
# needs ancestors well above the genes' own annotations.
MAX_CLIMB = 6
# Terms carried by more than this share of annotated genes say nothing.
MAX_TERM_SHARE = 0.5


def weighted_cover(terms: dict, genes: set, min_genes: int) -> tuple[list, set]:
    """
    Greedy weighted set cover: gain = (newly covered genes) x information content.

    Unweighted cover favours the vaguest term ("protein binding" covers
    everything and says nothing), so specificity is part of the gain. A term
    must cover at least `min_genes` new genes; leftovers are reported, never
    forced in.
    """
    uncovered, pool, chosen = set(genes), dict(terms), []
    while uncovered and pool:
        best = max(pool.values(), key=lambda t: (len(t["genes"] & uncovered) * t["ic"], t["ic"]))
        gained = best["genes"] & uncovered
        if len(gained) < min_genes:
            break
        chosen.append({**best, "covered": gained})
        uncovered -= gained
        del pool[best["go_id"]]
    return chosen, uncovered


def build_hierarchy(terms: dict, parents: dict, genes: set, levels: int,
                    min_genes: int = 2) -> tuple:
    """
    Group genes by GO terms at several levels of generality.

    Level 1 is the weighted cover: the most specific informative terms that
    each group >= min_genes genes. Each further level JOINS groups from the
    level below under the most informative term that is an ancestor of at
    least two of them in the ontology (`parents`: term -> its ancestor terms
    among the candidates). A joining term also absorbs any still-ungrouped
    genes it covers -- that is how climbing higher groups MORE genes, instead
    of the cover simply picking a vaguer term up front.

    Pure function over pre-fetched data, so it is unit-testable without the KG.
    Returns (groups, ungrouped); each group has level, parent and children.
    """
    level1, ungrouped = weighted_cover(terms, genes, min_genes)
    groups = [{**g, "level": 1, "parent": None, "children": []} for g in level1]
    by_id = {g["go_id"]: g for g in groups}
    current = [g["go_id"] for g in groups]
    used = set(current)
    for level in range(2, levels + 1):
        candidates = sorted((t for k, t in terms.items() if k not in used), key=lambda t: -t["ic"])
        nxt, joined = [], set()
        for t in candidates:
            kids = [c for c in current if c not in joined and t["go_id"] in parents.get(c, set())]
            if len(kids) < 2:
                continue
            absorbed = t["genes"] & ungrouped
            covered = set().union(*(by_id[c]["covered"] for c in kids)) | absorbed
            g = {**t, "covered": covered, "absorbed": absorbed, "level": level,
                 "parent": None, "children": kids}
            for c in kids:
                by_id[c]["parent"] = t["go_id"]
            by_id[t["go_id"]] = g
            groups.append(g)
            ungrouped = ungrouped - absorbed
            joined.update(kids)
            used.add(t["go_id"])
            nxt.append(t["go_id"])
        if not nxt:
            break
        current = nxt + [c for c in current if c not in joined]
    return groups, ungrouped


def common_ontology(tk, uniprot_ids: list[str], aspect: str, levels: int,
                    exclude_terms: list[str] | None, min_genes: int = 2) -> dict:
    """
    Group a gene set by the GO terms it shares, as a small hierarchy.

    Everything comes from the KG: the genes' annotations, their ancestors up to
    MAX_CLIMB levels, and the ontology's own is_a / part_of links between
    candidate terms. Specificity is information content over PROPAGATED
    background (genes annotated to the term or any descendant) -- direct counts
    make the vaguest terms look the most specific ("biological_process": 1
    direct gene, 9,972 propagated).

    exclude_terms sets aside terms that would only restate the question's
    premise (e.g. "p53" when asking about TP53's regulators: "p53 binding"
    groups everything and explains nothing). Matched as a case-insensitive
    substring of the term name, or as an exact GO id.
    """
    if not 1 <= levels <= 3:
        raise ToolkitError("levels must be between 1 and 3.")
    ids = list(dict.fromkeys(uniprot_ids))
    rows, _ = tk.clients.read(f"""
        UNWIND $ids AS uid
        MATCH (g:Gene {{id: uid}})-[r]->(t:Ontology {{hasOBONamespace: $aspect}})
        WHERE NOT type(r) STARTS WITH 'NOT|'
        MATCH (t)-[:is_a|part_of*0..{MAX_CLIMB}]->(o:Ontology {{hasOBONamespace: $aspect}})
        WITH o, collect(DISTINCT g.id) AS genes
        WHERE size(genes) >= $min_genes
        CALL {{
          WITH o
          MATCH (d:Ontology)-[:is_a|part_of*0..20]->(o)
          WITH DISTINCT d
          MATCH (g2:Gene)-[r2]->(d) WHERE NOT type(r2) STARTS WITH 'NOT|'
          RETURN count(DISTINCT g2) AS background
        }}
        RETURN o.id AS go_id, o.name AS term, genes, background
        """, {"ids": ids, "aspect": aspect, "min_genes": min_genes}, max_rows=5000)
    universe, _ = tk.clients.read(
        "MATCH (g:Gene) WHERE EXISTS { (g)-[]->(:Ontology {hasOBONamespace: $aspect}) } "
        "RETURN count(g) AS n", {"aspect": aspect})
    n_universe = max(universe[0]["n"], 1)
    known, _ = tk.clients.read("UNWIND $ids AS uid MATCH (g:Gene {id: uid}) RETURN g.id AS id, g.name AS symbol",
                               {"ids": ids})
    symbol = {k["id"]: k["symbol"] for k in known}

    excl = [x.strip() for x in (exclude_terms or []) if x and x.strip()]
    excl_ids = {x.replace(":", "_").upper() for x in excl if x.upper().startswith("GO")}
    excl_words = [x.lower() for x in excl if not x.upper().startswith("GO")]

    def excluded(r):
        return (r["go_id"].upper() in excl_ids or any(w in r["term"].lower() for w in excl_words)
                or r["background"] > n_universe * MAX_TERM_SHARE)

    terms = {r["go_id"]: {"go_id": r["go_id"], "term": r["term"], "genes": set(r["genes"]),
                          "background": r["background"],
                          "ic": -math.log(max(r["background"], 1) / n_universe)}
             for r in rows if not excluded(r)}
    parents: dict = {}
    if levels > 1 and terms:
        anc, _ = tk.clients.read(
            "UNWIND $ids AS a MATCH (x:Ontology {id: a})-[:is_a|part_of*1..8]->(y:Ontology) "
            "WHERE y.id IN $ids RETURN a AS child, collect(DISTINCT y.id) AS parents",
            {"ids": list(terms)}, max_rows=5000)
        parents = {r["child"]: set(r["parents"]) for r in anc}

    groups, ungrouped = build_hierarchy(terms, parents, {i for i in ids if i in symbol}, levels, min_genes)
    out_groups = []
    for g in groups:
        out_groups.append({
            "go_id": g["go_id"], "term": g["term"], "level": g["level"],
            "genes": sorted(symbol[x] for x in g["covered"]), "uniprot_ids": sorted(g["covered"]),
            **({"parent": g["parent"]} if g["parent"] else {}),
            **({"children": g["children"]} if g["children"] else {}),
            **({"absorbed": sorted(symbol[x] for x in g["absorbed"])} if g.get("absorbed") else {}),
            "genes_with_term_overall": g["background"],
            "information_content": round(g["ic"], 2),
            "cite": f"profile:set~{g['go_id']}",
        })
    return {"aspect": aspect, "levels": levels, "excluded": excl, "n_genes": len(ids),
            "unknown_ids": sorted(set(ids) - set(symbol)),
            "groups": out_groups,
            "ungrouped": sorted(symbol[g] for g in ungrouped)}
