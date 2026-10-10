"""
The registry as the server sees it: read once at startup, held in memory.

Source of truth at runtime is the (:Toolkit) graph in Neo4j -- that is what
lets a new snapshot add or change graph tools without a server release. If the
registry has not been loaded into this database (a fresh stack before the
registry-load step, or a test with no Neo4j), the bundled toolkit.yaml is used
instead and the fallback is logged, never silent.

Each tool's `params` (a small JSON-Schema dialect) becomes one pydantic model.
That single model drives both the MCP-listed signature of core tools and the
argument validation execute_tool applies to everything else, so a discovered
tool is checked exactly as strictly as a listed one.
"""
from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, create_model

from .load import load_seed

log = logging.getLogger(__name__)

_SCALARS = {"string": str, "integer": int, "number": float, "boolean": bool}


def _annotation(spec: dict) -> Any:
    kind = spec.get("type", "string")
    if "enum" in spec:
        base = Literal[tuple(spec["enum"])]
    elif kind == "array":
        base = list[_annotation(spec.get("items", {"type": "string"}))]
    elif kind == "object":
        base = dict[str, Any]
    else:
        base = _SCALARS[kind]
    return Optional[base] if spec.get("default", ...) is None else base


@dataclass
class ToolSpec:
    name: str
    title: str
    description: str
    kind: str                      # cypher | python
    core: bool
    params: dict[str, dict]
    category: list[str] = field(default_factory=list)
    accepts: list[str] = field(default_factory=list)
    returns: list[str] = field(default_factory=list)
    handler: str | None = None
    cypher: str | None = None
    cite: dict | None = None
    result: str = "many"           # cypher tools: one row (paged object) or many rows
    coverage: str | None = None    # what a result settles and what it does not (template)

    def __post_init__(self):
        fields = {}
        for name, spec in self.params.items():
            desc = spec.get("description")
            default = spec["default"] if "default" in spec else ...
            fields[name] = (_annotation(spec), Field(default, description=desc))
        self.model: type[BaseModel] = create_model(
            f"{self.name}_args", __config__=ConfigDict(extra="forbid"), **fields)

    def validate(self, arguments: dict | None) -> dict:
        """Coerce and default the arguments; raises pydantic.ValidationError."""
        return self.model.model_validate(arguments or {}).model_dump()

    def signature(self) -> inspect.Signature:
        """A keyword-only Python signature equivalent to `params`, for MCP registration."""
        parameters = []
        for name, f in self.model.model_fields.items():
            ann = Annotated[f.annotation, Field(description=f.description)] if f.description else f.annotation
            parameters.append(inspect.Parameter(
                name, inspect.Parameter.KEYWORD_ONLY, annotation=ann,
                default=inspect.Parameter.empty if f.is_required() else f.default))
        return inspect.Signature(parameters, return_annotation=dict[str, Any])

    def definition(self) -> dict:
        """What discover_tools hands the model: enough to call it via execute_tool."""
        schema = self.model.model_json_schema()
        schema.pop("title", None)
        return {"name": self.name, "title": self.title, "description": self.description,
                "category": self.category, "accepts": self.accepts, "returns": self.returns,
                "listed": self.core, "input_schema": schema}


@dataclass
class Registry:
    tools: dict[str, ToolSpec]
    recipes: list[dict]
    id_types: dict[str, dict]
    rel_types: list[dict]
    kg_release: str | None
    source: str                    # "neo4j" | "yaml"

    @property
    def core(self) -> list[ToolSpec]:
        return [t for t in self.tools.values() if t.core]

    def id_type_closure(self, name: str) -> set[str]:
        """An id type plus what it specializes: a uniprot id is also a `node`."""
        out, cur = {name}, name
        while (parent := self.id_types.get(cur, {}).get("is_a")) and parent not in out:
            out.add(parent)
            cur = parent
        return out

    def with_code_tools(self) -> "Registry":
        """
        Python tools ship with the code; the registry in Neo4j can lag it. Add
        any python-kind tool from the bundled toolkit.yaml that the loaded
        registry lacks, and say so. Stored-query (cypher) tools are never
        added this way: for those the loaded registry is the source of truth.
        """
        bundled = Registry.from_yaml()
        added = [n for n, s in bundled.tools.items() if s.kind == "python" and n not in self.tools]
        for n in added:
            self.tools[n] = bundled.tools[n]
        if added:
            log.warning("registry predates these code tools; using bundled definitions: %s", added)
        return self

    # --- loading ---------------------------------------------------------
    @classmethod
    def load(cls, clients=None) -> "Registry":
        if clients is not None:
            try:
                reg = cls.from_neo4j(clients)
                if reg is not None:
                    return reg.with_code_tools()
                log.warning("No (:Toolkit:Registry) in Neo4j -- using bundled toolkit.yaml. "
                            "Load it with: python -m pathwayoracle_mcp.registry.load")
            except Exception as exc:                        # noqa: BLE001
                log.warning("Toolkit registry unreadable from Neo4j (%s) -- using bundled "
                            "toolkit.yaml.", exc)
        return cls.from_yaml()

    @classmethod
    def from_yaml(cls) -> "Registry":
        seed = load_seed()
        tools = {t["name"]: ToolSpec(
            name=t["name"], title=t["title"], description=" ".join(t["description"].split()),
            kind=t["kind"], core=bool(t.get("core")), params=t["params"] or {},
            category=t["category"], accepts=t["accepts"], returns=t["returns"],
            handler=t.get("handler"), cypher=t.get("cypher"), cite=t.get("cite"),
            result=t.get("result", "many"),
            coverage=" ".join((t.get("coverage") or "").split()) or None) for t in seed["tools"]}
        rels = [{"name": r["name"], "meaning": r["meaning"], "kg_types": [r["name"]],
                 "patterns": []} for r in seed["relationships"]]
        return cls(tools=tools, recipes=seed["recipes"],
                   id_types={i["name"]: i for i in seed["id_types"]}, rel_types=rels,
                   kg_release=None, source="yaml")

    @classmethod
    def from_neo4j(cls, clients) -> "Registry | None":
        reg, _ = clients.read("MATCH (r:Toolkit:Registry) RETURN r.kg_release AS kg_release")
        if not reg:
            return None
        rows, _ = clients.read("""
            MATCH (t:Toolkit:Tool)
            RETURN t {.*,
                      category: [(t)-[:IN_CATEGORY]->(c:Toolkit:ToolCategory) | c.name],
                      accepts:  [(t)-[:ACCEPTS]->(i:Toolkit:IdType) | i.name],
                      returns:  [(t)-[:RETURNS]->(i:Toolkit:IdType) | i.name]} AS t""")
        tools = {}
        for r in (row["t"] for row in rows):
            tools[r["name"]] = ToolSpec(
                name=r["name"], title=r["title"], description=" ".join(r["description"].split()),
                kind=r["kind"], core=bool(r["core"]), params=json.loads(r["params_json"] or "{}"),
                category=sorted(r["category"]), accepts=sorted(r["accepts"]),
                returns=sorted(r["returns"]), handler=r.get("handler"), cypher=r.get("cypher"),
                cite=json.loads(r["cite_json"]) if r.get("cite_json") else None,
                result=r.get("result") or "many", coverage=r.get("coverage"))
        ids, _ = clients.read("""
            MATCH (i:Toolkit:IdType)
            OPTIONAL MATCH (i)-[:SPECIALIZES]->(p:Toolkit:IdType)
            RETURN i {.*, is_a: p.name} AS i""")
        recipes, _ = clients.read("""
            MATCH (r:Toolkit:Recipe)-[s:STEP]->(t:Toolkit:Tool)
            WITH r, s, t ORDER BY s.order
            RETURN r.name AS name, r.description AS description, r.when AS when, r.scope AS scope,
                   coalesce(r.stage, 'gather') AS stage,
                   collect({tool: t.name, note: s.note}) AS steps ORDER BY name""")
        rels, _ = clients.read("""
            MATCH (r:Toolkit:RelType)
            RETURN r.name AS name, r.meaning AS meaning, r.kg_types AS kg_types,
                   r.count AS count, r.patterns_json AS patterns_json ORDER BY r.count DESC""")
        for r in rels:
            r["patterns"] = json.loads(r.pop("patterns_json") or "[]")
        return cls(tools=tools, recipes=recipes, id_types={i["i"]["name"]: i["i"] for i in ids},
                   rel_types=rels, kg_release=reg[0]["kg_release"], source="neo4j")
