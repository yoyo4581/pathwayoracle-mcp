# The toolkit registry

The MCP server's tools are described by a small graph that lives in the same
Neo4j database as the knowledge graph (KG). The server reads it to decide what
to list at startup, to answer "what can I do next?", and to run stored queries.
The model never reads the registry directly; it calls ordinary MCP tools.

## Why a graph

A question is answered with three kinds of step, in different orders:

| step | does | examples |
|---|---|---|
| **anchor** | text → identifiers | `find_entities`, `search_proteins`, `search_abstracts` |
| **traverse** | identifiers → more identifiers, along typed relationships | `pathway_genes`, `gene_interactions`, `gene_pathways`, `expand` |
| **enrich** | identifiers → evidence | `read_snippets`, `gene_go_annotations`, `read_abstracts` |

| exploration | order |
|---|---|
| fine-grained | anchor → enrich |
| top-down | anchor a system → traverse down → enrich the parts |
| bottom-up | enrich (search evidence) → anchor the hits → traverse up to systems |

Tools hand identifiers to each other, so recording what each tool **accepts**
and **returns** makes chaining a query rather than a convention:

```cypher
// I hold UniProt ids: what can I do next, and what does each give me?
MATCH (:IdType {name:'uniprot'})<-[:ACCEPTS]-(t:Tool)-[:RETURNS]->(o:IdType)
RETURN t.name, collect(o.name)
```

## Schema

```
(:Toolkit:Registry {version, kg_release, loaded_at, source})
(:Toolkit:Tool {name, title, description, kind, core, handler, cypher, params_json, example_json})
(:Toolkit:ToolCategory {name, description})          anchor | traverse | enrich | meta
(:Toolkit:IdType {name, description, kg_label, kg_property, store, format, example})
(:Toolkit:Recipe {name, description, when})

(:Tool)-[:IN_CATEGORY]->(:ToolCategory)
(:Tool)-[:ACCEPTS]->(:IdType)
(:Tool)-[:RETURNS]->(:IdType)
(:Recipe)-[:STEP {order, note}]->(:Tool)
```

- `kind: cypher`: `cypher` holds a parameterised query that the server runs
  in a READ transaction. New graph tools can ship in a snapshot, with no server
  release.
- `kind: python`: `handler` names server code (Qdrant, PubMed, set cover,
  discovery). The registry describes it; the code implements it.
- `core: true`: listed at MCP startup with a full definition. Everything else
  is found with `discover_tools` and run with `execute_tool`.

## Isolation from the KG

The KG is curated; the registry must never alter it.

1. Every registry node carries `:Toolkit`; every registry write is scoped to it.
2. No relationship joins a registry node to a KG node. Tools name KG labels
   as strings (`IdType.kg_label`); they never point at KG nodes.
3. `registry/load.py` writes in one transaction. It fingerprints the KG (node
   count per label, relationship count per type, crossing edges) before the
   write and again inside the transaction, and rolls back on any difference.

The source of truth is `src/pathwayoracle_mcp/registry/toolkit.yaml`, not the
database. Edit the YAML, then:

```sh
python -m pathwayoracle_mcp.registry.load --dry-run   # validate + run every stored query's example
python -m pathwayoracle_mcp.registry.load             # replace the registry
```

## Provenance

Every result item will carry a citation id built from domain identifiers, never
Neo4j element ids, which change between releases:

| evidence | citation |
|---|---|
| function-text snippet | `qdrant:P04637#3` |
| interaction | `edge:P04637-ACTS_ON->O43819@R-HSA-5632766` |
| GO annotation | `go:P04637-involved_in->GO_0006915[IDA]` |
| study | `pubmed:10801849` |

Results also carry the KG release (`Registry.kg_release`), so a citation can be
resolved later against the exact data it came from (`resolve_citations`).

## KG facts the stored queries rely on

- Composition edges point **part → whole**, despite their names:
  `(:Protein)-[:HAS_COMPONENT|HAS_MEMBER|HAS_CANDIDATE]->(:Complex|DefinedSet|CandidateSet)`.
  Pathway membership unfolds as
  `(Pathway)<-[:IN_PATHWAY]-(e)<-[…*0..6]-(:Protein)-[:IS_FORM_OF]->(:Gene)`.
  Without unfolding, 602 of 2,035 pathways reach no gene; with it, 17.
- GO ids are stored as `GO_0006915`; queries accept `GO:0006915`.
- Relationship types prefixed `NOT|` are negated annotations and are excluded.
- Pathways have no direct GO link and no hierarchy edges; `pathway_go_profile`
  goes through member genes.
- Drug ids (PubChem) and Compound ids (ChEBI) are bare numbers; they are only
  unambiguous together with their label.

## Runtime

```
server.py        registers the registry's core tools with MCP (signature = registry params)
dispatch.py      Toolkit.call: validate -> run -> cite -> trace, for EVERY call
registry/        toolkit.yaml (source), load.py (guarded writer), runtime.py (reader)
handlers/        Python implementations, keyed by the registry's `handler`
clients.py       lazy Qdrant+encoder / Neo4j (READ transactions only) / Zilliz handles
tracing.py       one record per call; JSONL today, OpenTelemetry next
```

Result keys starting with `_` are **ledger-only**: dispatch strips them from
what the model receives and from the trace, and the evidence ledger ingests
them. Use them for evidence too large for the context that the view still needs
(e.g. `reaction_details` participants' full `_members`).

The server reads the registry from Neo4j at startup and falls back, with a
logged warning, to the bundled YAML if no registry is loaded. Handlers are plain
functions of `(toolkit, **args)`, importable without MCP, so DeepAgents can
call the same implementations.

## Tracing

Every call produces one `ToolCall` record: tool, validated arguments, `via`
(listed or execute_tool), citation ids returned, KG release, latency, error.
Calls through `execute_tool` produce a parent and a child record. Set
`PATHWAYORACLE_TRACE_FILE` to write them as JSON lines; compose puts them in
the `model_cache` volume at `/cache/traces/calls.jsonl`.

## Known gaps

- The PubMed abstracts were embedded with a different encoder than the
  fine-tuned BioBERT used for queries. The semantic branch of
  `search_abstracts` is degraded until they are re-embedded; the keyword branch
  is unaffected.
- `search_abstracts` / `read_abstracts` are implemented but untested: the
  Zilliz cluster in `ZILLIS_URI` does not resolve in public DNS.
  `cited_pmids` reads Qdrant and works.
- Background counts for GO specificity are computed live (~0.5–1 s). Precomputed
  per-term counts in the snapshot would remove that cost.
- `resolve_citations` re-derives `member:`, `profile:` and `cypher:` citations
  only by re-running the producing tool.
