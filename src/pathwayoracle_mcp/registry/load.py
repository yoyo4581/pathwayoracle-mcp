"""
Load toolkit.yaml into Neo4j as (:Toolkit) nodes, without touching the KG.

    python -m pathwayoracle_mcp.registry.load --dry-run   # validate + test only
    python -m pathwayoracle_mcp.registry.load             # replace the registry

Order of operations, each a gate for the next:

  1. validate   the YAML is internally consistent: every category, id type and
                recipe step refers to something defined; every $parameter a
                stored query uses is declared in its params.
  2. test       every cypher tool's `example` runs in a READ transaction and
                returns rows. A stored query that cannot run never reaches the
                registry.
  3. write      ONE write transaction: delete the old registry (:Toolkit nodes
                only), create the new one, then fingerprint the KG inside the
                same transaction and roll back unless it is identical to the
                fingerprint taken before. See guard.py.

Neo4j properties cannot hold maps, so params and examples are stored as JSON
strings (params_json, example_json).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from neo4j import READ_ACCESS, WRITE_ACCESS, GraphDatabase

from .guard import (REGISTRY_REL_TYPES, KGGuardError, assert_unchanged,
                    check_label_collisions, fingerprint)

SEED = Path(__file__).with_name("toolkit.yaml")
DEFAULT_RELEASE = "serving-v20261006193533"
TEST_TIMEOUT_S = 30


def load_seed(path: Path = SEED) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def validate(seed: dict) -> list[str]:
    errors: list[str] = []
    cats = {c["name"] for c in seed["categories"]}
    ids = {i["name"] for i in seed["id_types"]}
    names = [t["name"] for t in seed["tools"]]
    tools = set(names)
    if len(names) != len(tools):
        errors.append(f"duplicate tool names: {sorted({n for n in names if names.count(n) > 1})}")

    for t in seed["tools"]:
        where = f"tool {t['name']}"
        for key in ("title", "description", "category", "accepts", "returns", "kind", "params"):
            if key not in t:
                errors.append(f"{where}: missing {key}")
        errors += [f"{where}: unknown category {c}" for c in t.get("category", []) if c not in cats]
        errors += [f"{where}: unknown id type {i}"
                   for i in t.get("accepts", []) + t.get("returns", []) if i not in ids]
        if t.get("kind") == "cypher":
            if "cypher" not in t or "example" not in t:
                errors.append(f"{where}: cypher tools need cypher and example")
                continue
            used = set(re.findall(r"\$(\w+)", t["cypher"]))
            declared = set(t["params"])
            if used - declared:
                errors.append(f"{where}: cypher uses undeclared params {sorted(used - declared)}")
            if set(t["example"]) != declared:
                errors.append(f"{where}: example keys {sorted(t['example'])} != params {sorted(declared)}")
        elif t.get("kind") == "python":
            if "handler" not in t:
                errors.append(f"{where}: python tools need a handler")
        else:
            errors.append(f"{where}: kind must be cypher or python")

    for r in seed["recipes"]:
        errors += [f"recipe {r['name']}: unknown tool {s['tool']}"
                   for s in r["steps"] if s["tool"] not in tools]
    for r in seed["recipes"]:
        if r.get("stage", "gather") not in ("gather", "synthesis"):
            errors.append(f"recipe {r['name']}: stage must be gather or synthesis")
    for i in seed["id_types"]:
        if i.get("is_a") and i["is_a"] not in ids:
            errors.append(f"id type {i['name']}: is_a unknown id type {i['is_a']}")
    return errors


def rel_entry(seed: dict, rel_type: str) -> dict | None:
    """The relationships entry describing a KG type: exact name, else prefix pattern."""
    for r in seed["relationships"]:
        if r["name"] == rel_type:
            return r
    for r in seed["relationships"]:
        if r.get("pattern") and rel_type.startswith(r["pattern"]):
            return r
    return None


def measure_rel_types(session, seed: dict) -> tuple[list[dict], list[str]]:
    """
    Source->target label patterns and counts for every KG relationship type,
    measured read-only, attached to its relationships entry. Returns the
    RelType rows plus the KG types that no entry describes.
    """
    kg_types = [t for t in session.run(
        "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType").value()
        if t not in REGISTRY_REL_TYPES]
    by_entry: dict[str, dict] = {}
    undescribed = []
    for t in sorted(kg_types):
        entry = rel_entry(seed, t)
        if entry is None:
            undescribed.append(t)
            continue
        patterns = session.run(
            f"MATCH (a)-[r:`{t}`]->(b) WHERE NOT a:Toolkit AND NOT b:Toolkit "
            "WITH head([l IN labels(a) WHERE l <> 'Entity']) AS source, "
            "     head([l IN labels(b) WHERE l <> 'Entity']) AS target, count(*) AS count "
            "RETURN source, target, count ORDER BY count DESC").data()
        if not patterns:
            continue
        row = by_entry.setdefault(entry["name"], {
            "name": entry["name"], "meaning": entry["meaning"],
            "pattern": entry.get("pattern"), "kg_types": [], "patterns": [], "count": 0})
        row["kg_types"].append(t)
        row["patterns"] += [{"rel": t, **p} for p in patterns]
        row["count"] += sum(p["count"] for p in patterns)
    rows = [{**r, "patterns_json": json.dumps(r.pop("patterns"))} for r in by_entry.values()]
    return rows, undescribed


def test_cypher_tools(session, seed: dict) -> list[str]:
    """Run each stored query's example read-only. Returns failures."""
    failures = []
    for t in seed["tools"]:
        if t["kind"] != "cypher":
            continue
        start = time.perf_counter()
        try:
            rows = session.execute_read(
                lambda tx: tx.run(t["cypher"], t["example"], timeout=TEST_TIMEOUT_S).data())
        except Exception as exc:                            # noqa: BLE001
            failures.append(f"{t['name']}: {type(exc).__name__}: {exc}")
            print(f"  FAIL  {t['name']}: {exc}")
            continue
        ms = (time.perf_counter() - start) * 1000
        status = "ok  " if rows else "EMPTY"
        print(f"  {status}  {t['name']:<22} {len(rows):>3} rows  {ms:7.0f} ms")
        if not rows:
            failures.append(f"{t['name']}: example returned no rows")
    return failures


WRITE_STATEMENTS = [
    # Only ever matches :Toolkit nodes, and those have no edges into the KG,
    # so DETACH removes registry edges and nothing else.
    ("MATCH (n:Toolkit) DETACH DELETE n", lambda s, m: [{}]),
    ("""UNWIND $rows AS row
        CREATE (:Toolkit:Registry {version: row.version, kg_release: row.kg_release,
                                   loaded_at: row.loaded_at, source: row.source})""",
     lambda s, m: [{"version": s["version"], "kg_release": m["kg_release"],
                    "loaded_at": m["loaded_at"], "source": "registry/toolkit.yaml"}]),
    ("UNWIND $rows AS row CREATE (:Toolkit:ToolCategory {name: row.name, description: row.description})",
     lambda s, m: s["categories"]),
    ("""UNWIND $rows AS row
        CREATE (i:Toolkit:IdType {name: row.name, description: row.description})
        SET i += row.extra""",
     lambda s, m: [{"name": i["name"], "description": i["description"],
                    "extra": {k: v for k, v in i.items() if k not in ("name", "description", "is_a")}}
                   for i in s["id_types"]]),
    ("""UNWIND $rows AS row
        MATCH (a:Toolkit:IdType {name: row.name}), (b:Toolkit:IdType {name: row.is_a})
        CREATE (a)-[:SPECIALIZES]->(b)""",
     lambda s, m: [{"name": i["name"], "is_a": i["is_a"]} for i in s["id_types"] if i.get("is_a")]),
    ("""UNWIND $rows AS row
        CREATE (:Toolkit:RelType {name: row.name, meaning: row.meaning, pattern: row.pattern,
                                  kg_types: row.kg_types, count: row.count,
                                  patterns_json: row.patterns_json})""",
     lambda s, m: m["rel_types"]),
    ("""UNWIND $rows AS row
        CREATE (t:Toolkit:Tool {name: row.name, title: row.title, description: row.description,
                                kind: row.kind, core: row.core, handler: row.handler,
                                cypher: row.cypher, params_json: row.params_json,
                                example_json: row.example_json, cite_json: row.cite_json,
                                result: row.result, coverage: row.coverage})
        WITH t, row
        UNWIND row.category AS c
        MATCH (cat:Toolkit:ToolCategory {name: c})
        CREATE (t)-[:IN_CATEGORY]->(cat)""",
     lambda s, m: [{"name": t["name"], "title": t["title"], "description": t["description"].strip(),
                    "kind": t["kind"], "core": bool(t.get("core")), "handler": t.get("handler"),
                    "cypher": t.get("cypher"), "params_json": json.dumps(t["params"]),
                    "example_json": json.dumps(t["example"]) if "example" in t else None,
                    "cite_json": json.dumps(t["cite"]) if "cite" in t else None,
                    "result": t.get("result", "many"),
                    "coverage": " ".join((t.get("coverage") or "").split()) or None,
                    "category": t["category"]} for t in s["tools"]]),
    ("""UNWIND $rows AS row
        MATCH (t:Toolkit:Tool {name: row.tool}), (i:Toolkit:IdType {name: row.id_type})
        CALL apoc.create.relationship(t, row.rel, {}, i) YIELD rel
        RETURN count(rel)""",
     lambda s, m: [{"tool": t["name"], "id_type": i, "rel": rel}
                   for t in s["tools"]
                   for rel, key in (("ACCEPTS", "accepts"), ("RETURNS", "returns"))
                   for i in t[key]]),
    ("""UNWIND $rows AS row
        CREATE (r:Toolkit:Recipe {name: row.name, description: row.description, when: row.when,
                                scope: row.scope, stage: row.stage})
        WITH r, row
        UNWIND range(0, size(row.steps) - 1) AS i
        MATCH (t:Toolkit:Tool {name: row.steps[i].tool})
        CREATE (r)-[:STEP {order: i + 1, note: row.steps[i].note}]->(t)""",
     lambda s, m: [{"name": r["name"], "description": r["description"], "when": r["when"],
                    "stage": r.get("stage", "gather"),
                    "scope": " ".join((r.get("scope") or "").split()) or None,
                    "steps": [{"tool": st["tool"], "note": st.get("note")} for st in r["steps"]]}
                   for r in s["recipes"]]),
]


def write_registry(driver, seed: dict, kg_release: str, rel_types: list[dict]) -> dict:
    meta = {"kg_release": kg_release, "loaded_at": datetime.now(timezone.utc).isoformat(),
            "rel_types": rel_types}
    with driver.session(default_access_mode=READ_ACCESS) as s:
        check_label_collisions(s)
        before = fingerprint(s)

    with driver.session(default_access_mode=WRITE_ACCESS) as s:
        tx = s.begin_transaction()
        try:
            for statement, rows in WRITE_STATEMENTS:
                tx.run(statement, rows=rows(seed, meta)).consume()
            assert_unchanged(before, fingerprint(tx))        # same tx: rollback on any change
            counts = tx.run("MATCH (n:Toolkit) RETURN labels(n)[1] AS label, count(*) AS n "
                            "ORDER BY label").data()
            rels = tx.run("MATCH (:Toolkit)-[r]->(:Toolkit) RETURN type(r) AS rel, count(*) AS n "
                          "ORDER BY rel").data()
            tx.commit()
        except BaseException:
            tx.rollback()
            raise
    return {"nodes": counts, "rels": rels, "kg_unchanged": True}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--uri", default=os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687"))
    ap.add_argument("--kg-release", default=os.environ.get("POKB_RELEASE", DEFAULT_RELEASE))
    ap.add_argument("--dry-run", action="store_true", help="validate and test; write nothing")
    args = ap.parse_args()

    seed = load_seed()
    errors = validate(seed)
    print(f"validate: {len(seed['tools'])} tools, {len(seed['id_types'])} id types, "
          f"{len(seed['recipes'])} recipes -- {len(errors)} errors")
    for e in errors:
        print(f"  {e}")
    if errors:
        return 1

    driver = GraphDatabase.driver(args.uri, auth=None, notifications_min_severity="OFF")
    with driver.session(default_access_mode=READ_ACCESS) as s:
        print("test stored queries (read-only):")
        failures = test_cypher_tools(s, seed)
        rel_types, undescribed = measure_rel_types(s, seed)
    print(f"relationship meanings: {len(rel_types)} entries cover "
          f"{sum(len(r['kg_types']) for r in rel_types)} KG types")
    if undescribed:
        print(f"  KG types with no meaning in toolkit.yaml: {undescribed}")
        failures.append("undescribed relationship types")
    if failures:
        print(f"{len(failures)} problems; registry not written.")
        return 1
    if args.dry_run:
        print("dry run: nothing written.")
        return 0

    try:
        result = write_registry(driver, seed, args.kg_release, rel_types)
    except KGGuardError as exc:
        print(f"ROLLED BACK: {exc}")
        return 1
    print("written:", json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
