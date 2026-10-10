"""
The toolkit describing and running itself.

    discover_tools     search the registry by what you hold, what you want,
                       category or text; returns full definitions + recipes
    execute_tool       run any registry tool through the same dispatch path
    resolve_citations  turn citation ids from earlier results back into evidence
"""
from __future__ import annotations

import re

from ..dispatch import ToolkitError

_WORD = re.compile(r"[a-z0-9]+")


def discover_tools(tk, have: str | None, want: str | None, category: str | None,
                   text: str | None) -> dict:
    reg = tk.registry
    for label, value in (("have", have), ("want", want)):
        if value and value not in reg.id_types:
            raise ToolkitError(f"Unknown id type {value!r} for `{label}`. "
                               f"Known: {', '.join(sorted(reg.id_types))}.")
    if category and category not in {c for t in reg.tools.values() for c in t.category}:
        raise ToolkitError("category must be one of anchor, traverse, enrich, meta.")

    have_types = reg.id_type_closure(have) if have else None
    words = set(_WORD.findall(text.lower())) if text else set()

    scored = []
    for spec in reg.tools.values():
        if spec.name in ("discover_tools", "execute_tool"):
            continue
        if have_types is not None and not have_types & set(spec.accepts):
            continue
        if want and want not in spec.returns:
            continue
        if category and category not in spec.category:
            continue
        score = 0
        if words:
            hay = set(_WORD.findall(f"{spec.name} {spec.title} {spec.description}".lower()))
            score = len(words & hay)
            if score == 0:
                continue
        scored.append((score, spec))
    scored.sort(key=lambda x: (-x[0], not x[1].core, x[1].name))

    recipes = reg.recipes
    if words:
        recipes = [r for r in recipes
                   if words & set(_WORD.findall(f"{r['name']} {r['description']} {r['when']}".lower()))]
    return {
        "n_tools": len(scored),
        "tools": [s.definition() for _, s in scored],
        "recipes": [{"name": r["name"], "description": r["description"], "when": r["when"],
                     "steps": [s["tool"] + (f" ({s['note']})" if s.get("note") else "")
                               for s in r["steps"]]} for r in recipes],
        "how_to_call": "Tools with listed=false are not in your tool list; call them with "
                       "execute_tool(name=..., arguments={...}).",
    }


# --- plan ----------------------------------------------------------------
#
# The model classifies the question as a Recipe; the server walks the toolkit
# graph from there. One traversal returns the steps in order AND how each step
# is fed: a handoff exists where an earlier step's tool RETURNS an id type this
# step's tool ACCEPTS -- directly, or through SPECIALIZES (a uniprot id is also
# a `node`). That is the registry's structure doing the work, not a text search.
_PLAN = """
MATCH (r:Toolkit:Recipe {name: $recipe})-[s:STEP]->(t:Toolkit:Tool)
WITH r, s, t ORDER BY s.order
WITH r, collect({order: s.order, note: s.note, tool: t}) AS steps
UNWIND steps AS step
WITH r, steps, step, step.tool AS t
OPTIONAL MATCH (prev:Toolkit:Tool)-[:RETURNS]->(x:Toolkit:IdType)
               -[:SPECIALIZES*0..1]->(:Toolkit:IdType)<-[:ACCEPTS]-(t)
WHERE any(e IN steps WHERE e.tool = prev AND e.order < step.order)
WITH r, step, t, x, [e IN steps WHERE e.tool = prev AND e.order < step.order | e.order] AS froms
WITH r, step, t,
     collect(CASE WHEN x IS NULL THEN null
                  ELSE {from_step: froms[size(froms) - 1], id_type: x.name} END) AS feeds
RETURN r {.name, .description, .when, .scope, .stage} AS recipe,
       collect({order: step.order, tool: t.name, note: step.note, feeds: feeds}) AS steps
"""


def _plan_from_yaml(tk, recipe: dict) -> list[dict]:
    """Same handoff rule as _PLAN, for a registry not loaded into Neo4j."""
    reg, steps = tk.registry, []
    for i, s in enumerate(recipe["steps"], start=1):
        accepts = set(reg.tools[s["tool"]].accepts)
        feeds = []
        for j, prev in enumerate(recipe["steps"][: i - 1], start=1):
            for x in reg.tools[prev["tool"]].returns:
                if reg.id_type_closure(x) & accepts:
                    feeds.append({"from_step": j, "id_type": x})
        steps.append({"order": i, "tool": s["tool"], "note": s.get("note"), "feeds": feeds})
    return steps


def plan(tk, recipe: str) -> dict:
    reg = tk.registry
    by_name = {r["name"]: r for r in reg.recipes}
    if recipe not in by_name:
        raise ToolkitError(f"Unknown recipe {recipe!r}. Recipes: "
                           + "; ".join(f"{r['name']} -- {r['description']}"
                                       for r in sorted(reg.recipes, key=lambda r: r.get("stage") == "synthesis")))
    if reg.source == "neo4j":
        rows, _ = tk.clients.read(_PLAN, {"recipe": recipe})
        info, steps = rows[0]["recipe"], rows[0]["steps"]
    else:
        info, steps = by_name[recipe], _plan_from_yaml(tk, by_name[recipe])

    out = []
    for s in sorted(steps, key=lambda s: s["order"]):
        spec = reg.tools[s["tool"]]
        # The latest earlier step that can feed this one is the natural source.
        feeds = sorted({(f["from_step"], f["id_type"]) for f in s["feeds"] if f})
        latest = max((f for f, _ in feeds), default=None)
        out.append({
            "step": s["order"], "tool": spec.name, "purpose": s.get("note") or spec.title,
            "takes": ([f"{t} from step {f}" for f, t in feeds if f == latest] if latest
                      else ["the question / names in it"]),
            "call": (f"{spec.name}(...)" if spec.core
                     else f'execute_tool(name="{spec.name}", arguments={{...}})'),
            # Listed tools already carry their schema in the tool list.
            **({} if spec.core else {"input_schema": spec.definition()["input_schema"]}),
        })
    plan_out = {
        "recipe": info["name"], "description": info["description"],
        "scope": " ".join((info.get("scope") or "").split()) or None,
        # Not a checklist: a step is worth taking only while the answer still
        # needs it. No stopping rule either -- some questions need a long read.
        "how_to_use": "These are the steps this kind of question usually needs, in the usual "
                      "order. Take a step only if the answer still needs it; stop gathering "
                      "once you can answer.",
        "steps": out,
    }
    if (info.get("stage") or "gather") == "gather":
        # Synthesis is a separate stage, offered after gathering and never
        # required: ways the graph can group what was found.
        plan_out["to_structure_the_answer"] = [
            {"recipe": r["name"], "purpose": r["description"],
             "tools": [s["tool"] for s in r["steps"]]}
            for r in reg.recipes if (r.get("stage") or "gather") == "synthesis"
        ] + [{"recipe": None, "purpose": "draw the evidence for the answer", "tools": ["show_evidence"]}]
    plan_out.update({
        "last_resort": "If no tool here can answer a specific part of the question, read_cypher "
                       "(via execute_tool) can -- say which part, and why the tools could not.",
        "provenance": "Each result item carries a `cite` id; cite the evidence each claim rests on.",
        "next_question": "For a new question, classify it again and call plan again.",
    })
    return plan_out


def execute_tool(tk, name: str, arguments: dict) -> dict:
    if name in ("execute_tool", "discover_tools"):
        raise ToolkitError(f"Call {name} directly.")
    return tk.call(name, arguments, via="execute_tool")


# --- resolve_citations ---------------------------------------------------

_EDGE_TYPED = re.compile(r"^edge:(?P<a>[^-]+(?:-[^-]+)*?)-(?P<rel>[A-Z_]+)->(?P<b>[^@]+)@(?P<rxn>.+)$")
_EDGE_ANY = re.compile(r"^edge:(?P<a>.+?)-(?P<rel>[A-Za-z_|]+)-(?P<b>.+)$")
_GO = re.compile(r"^go:(?P<g>[^-]+)-(?P<rel>.+?)->(?P<t>GO_\d+)")


def _resolve_one(tk, c: str) -> dict:
    r = tk.clients.retriever if c.startswith("qdrant:") else None
    if c.startswith("qdrant:"):
        body = c.removeprefix("qdrant:")
        if "#" in body:
            uid, idx = body.split("#", 1)
            return r.expand(uid, int(idx), radius=0)
        return r.read_record(body)
    if c.startswith("pubmed:"):
        from .literature import read_abstracts
        return read_abstracts(tk, [c])["results"][0]
    if m := _EDGE_TYPED.match(c):
        rows, _ = tk.clients.read(
            "MATCH (a {id: $a})-[r]->(b {id: $b}) WHERE type(r) = $rel AND r.reaction_id = $rxn "
            "RETURN a.name AS source, type(r) AS rel, b.name AS target, properties(r) AS properties",
            m.groupdict())
        return rows[0] if rows else {"found": False}
    if m := _EDGE_ANY.match(c):
        rows, _ = tk.clients.read(
            "MATCH (a {id: $a})-[r]-(b {id: $b}) WHERE type(r) = $rel "
            "RETURN a.name AS a, type(r) AS rel, b.name AS b, properties(r) AS properties LIMIT 5",
            m.groupdict())
        return {"edges": rows} if rows else {"found": False}
    if m := _GO.match(c):
        rows, _ = tk.clients.read(
            "MATCH (g:Gene {id: $g})-[r]->(t:Ontology {id: $t}) WHERE type(r) = $rel "
            "RETURN g.name AS gene, type(r) AS relation, t.name AS term, r.evidence AS evidence",
            m.groupdict())
        return rows[0] if rows else {"found": False}
    if c.startswith("node:"):
        node_id = c.split(":")[-1]
        rows, _ = tk.clients.read(
            "MATCH (n {id: $id}) RETURN [l IN labels(n) WHERE l <> 'Entity'] AS labels, "
            "n.id AS id, n.name AS name LIMIT 3", {"id": node_id})
        return {"nodes": rows} if rows else {"found": False}
    if c.startswith(("member:", "profile:", "cypher:")):
        return {"derived": True,
                "note": "Computed by a tool rather than stored; re-run the tool that produced it "
                        "(pathway_genes / go_term_genes / pathway_go_profile / common_ontology / "
                        "read_cypher) to reproduce it."}
    raise ToolkitError(f"Unrecognised citation {c!r}.")


def resolve_citations(tk, citations: list[str]) -> dict:
    out = []
    for c in citations[:25]:
        try:
            out.append({"cite": c, **_resolve_one(tk, c)})
        except ToolkitError:
            raise
        except Exception as exc:                            # noqa: BLE001
            out.append({"cite": c, "error": f"{type(exc).__name__}: {exc}"})
    return {"n_results": len(out), "results": out}
