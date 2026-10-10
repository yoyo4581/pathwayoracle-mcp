"""
Keep the knowledge graph untouched while the toolkit lives in the same database.

The toolkit registry shares a Neo4j database with the curated KG (Community
edition has one database per instance). Isolation is by construction:

  * every registry node carries TOOLKIT_LABEL plus one of REGISTRY_LABELS;
  * registry relationships only ever join two registry nodes -- a tool refers
    to KG labels by name (a string property), never by an edge, because an
    edge would show up in the KG node's own neighbourhood and degree;
  * every registry write is scoped to TOOLKIT_LABEL.

And verified by measurement: fingerprint() counts KG nodes per label and KG
relationships per type, excluding anything touching a registry node, and the
loader refuses to commit unless the fingerprint is identical before and after.
"""
from __future__ import annotations

TOOLKIT_LABEL = "Toolkit"
REGISTRY_LABELS = ("Tool", "ToolCategory", "IdType", "Recipe", "Registry", "RelType")
# Relationship types that only ever join registry nodes.
REGISTRY_REL_TYPES = ("IN_CATEGORY", "ACCEPTS", "RETURNS", "STEP", "SPECIALIZES")


class KGGuardError(RuntimeError):
    pass


def check_label_collisions(session) -> None:
    """A registry label that already exists on KG nodes would merge the two."""
    rows = session.run(
        "MATCH (n) WHERE any(l IN labels(n) WHERE l IN $names) AND NOT n:Toolkit "
        "RETURN DISTINCT [l IN labels(n) WHERE l IN $names] AS clash LIMIT 5",
        names=[TOOLKIT_LABEL, *REGISTRY_LABELS]).data()
    if rows:
        raise KGGuardError(f"KG nodes already use registry labels: {rows}")


def fingerprint(session) -> dict[str, int]:
    """KG node counts per label and relationship counts per type, registry excluded."""
    fp: dict[str, int] = {}
    for label in session.run("CALL db.labels() YIELD label RETURN label").value():
        if label in (TOOLKIT_LABEL, *REGISTRY_LABELS):
            continue
        fp[f"node:{label}"] = session.run(
            f"MATCH (n:`{label}`) WHERE NOT n:Toolkit RETURN count(n)").single()[0]
    for rtype in session.run(
            "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType").value():
        fp[f"rel:{rtype}"] = session.run(
            f"MATCH (a)-[r:`{rtype}`]->(b) WHERE NOT a:Toolkit AND NOT b:Toolkit "
            f"RETURN count(r)").single()[0]
    # Edges crossing the boundary must not exist at all.
    fp["crossing_edges"] = session.run(
        "MATCH (a)-[r]-(b) WHERE a:Toolkit AND NOT b:Toolkit RETURN count(r)").single()[0]
    return fp


def assert_unchanged(before: dict[str, int], after: dict[str, int]) -> None:
    # Missing == 0: the registry's own relationship types appear in
    # db.relationshipTypes() with zero KG edges, which is not a KG change. A KG
    # type falling to zero still differs (N vs 0).
    diff = {k: (before.get(k, 0), after.get(k, 0)) for k in before.keys() | after.keys()
            if before.get(k, 0) != after.get(k, 0)}
    if diff:
        raise KGGuardError(f"KG fingerprint changed: {diff}")
    if after.get("crossing_edges"):
        raise KGGuardError(f"{after['crossing_edges']} edges join registry and KG nodes")
