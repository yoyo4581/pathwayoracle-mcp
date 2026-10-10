"""
Capture real tool results as test fixtures for the evidence ledger.

    python scripts/capture_fixtures.py        # needs the stack up (Qdrant, Neo4j; Zilliz optional)

Runs a fixed set of calls through the dispatch path -- the same path the
server uses -- and writes {tool, arguments, result} records to
tests/fixtures/tool_results.json. Uses the registry from toolkit.yaml, so the
fixtures match the result shapes in the working tree.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines() if (ROOT / ".env").exists() else []:
    k, sep, v = line.strip().partition("=")
    if sep and k.strip() in ("ZILLIS_URI", "ZILLIS_TOKEN") and not os.environ.get(k.strip()):
        os.environ[k.strip()] = v.strip().strip('"').strip("'")
os.environ.setdefault("QDRANT_URL", "http://127.0.0.1:6333")
os.environ.setdefault("NEO4J_URI", "bolt://127.0.0.1:7687")
os.environ.setdefault("BIOBERT_MODEL_PATH", "yoyo458/BioBERT_GOUniProt_FineTuned")
os.environ.setdefault("BIOBERT_REVISION", "dc05085095f1ae926aa014bb0a88d087bc0dce8f")

from pathwayoracle_mcp.clients import Clients            # noqa: E402
from pathwayoracle_mcp.dispatch import Toolkit           # noqa: E402
from pathwayoracle_mcp.registry.runtime import Registry  # noqa: E402
from pathwayoracle_mcp.tracing import NullTracer         # noqa: E402

# The 14 TP53 inhibitors from the 2026-10-09 Claude Desktop answer.
INHIBITORS = ["Q00987", "O15151", "Q969K3", "Q8WZ73", "O14965", "P21675", "Q96GD4", "Q9Y3T9",
              "Q9NRG4", "Q9NQR1", "Q9Y468", "Q9H9B1", "Q96KQ7", "Q8WUF5"]

CALLS = [
    ("plan", {"recipe": "mechanistic"}),
    ("find_entities", {"text": "TP53", "labels": ["Gene"], "limit": 3}),
    ("describe_node", {"id": "P04637"}),
    ("expand", {"id": "P04637", "rel": "ACTS_ON", "direction": "in", "limit": 5}),
    ("search_proteins", {"query": "DNA polymerase epsilon accessory subunit", "k": 3}),
    ("read_snippets", {"uniprot_id": "Q9NRF9", "query": "role in DNA polymerase epsilon", "n": 2}),
    ("expand_snippet", {"uniprot_id": "Q9NRF9", "chunk_idx": 0, "radius": 1}),
    ("read_record", {"uniprot_id": "Q9NRF9"}),
    ("snippet_sources", {"uniprot_id": "Q9NRF9", "chunk_idx": 0}),
    ("cited_pmids", {"uniprot_ids": ["Q9NRF9"]}),
    ("read_abstracts", {"pmids": ["10801849"]}),
    ("search_abstracts", {"entity_phrase": "POLE3 polymerase epsilon", "limit": 2}),
    ("pathway_genes", {"pathway_id": "R-HSA-111459"}),
    ("gene_pathways", {"uniprot_ids": ["P04637"], "limit": 3}),
    ("gene_interactions", {"uniprot_ids": ["P04637"], "direction": "in", "sign": "negative"}),
    ("pathway_interactions", {"pathway_id": "R-HSA-111459", "limit": 10}),
    ("interaction_paths", {"source": "Q13315", "target": "P24941"}),
    ("gene_reactions", {"uniprot_ids": ["P04637"], "role": "REACTANT", "regulated_only": True, "limit": 5}),
    ("reaction_details", {"reaction_ids": ["R-HSA-5633460"]}),
    ("gene_go_annotations", {"uniprot_ids": ["Q9NRF9"], "experimental_only": True, "limit": 5}),
    ("go_term_genes", {"go_id": "GO:0018344", "limit": 5}),
    ("pathway_go_profile", {"pathway_id": "R-HSA-6803205", "limit": 3}),
    ("common_ontology", {"uniprot_ids": INHIBITORS, "aspect": "molecular_function", "levels": 2,
                         "exclude_terms": ["p53"]}),
    ("shared_machinery", {"uniprot_ids": INHIBITORS, "limit": 6}),
    ("read_cypher", {"query": "MATCH (g:Gene {id:'P04637'})-[r:ACTS_ON]->(t:Gene) "
                              "RETURN t.id AS id, t.name AS name, r.reaction_id AS reaction LIMIT 3"}),
    ("resolve_citations", {"citations": ["qdrant:Q9NRF9#0", "edge:Q9BVI0-ACTS_ON->P04637@R-HSA-5633460"]}),
]


def main() -> int:
    tk = Toolkit(Clients(), Registry.from_yaml(), tracer=NullTracer())
    out = []
    for name, args in CALLS:
        try:
            # private: the ledger-side result, as ingest receives it
            result = tk.call(name, args, via="listed", private=True)
        except Exception as exc:                            # noqa: BLE001
            print(f"  skip {name}: {type(exc).__name__}: {str(exc)[:100]}")
            continue
        out.append({"tool": name, "arguments": args, "result": result})
        print(f"  ok   {name}")
    path = ROOT / "tests" / "fixtures" / "tool_results.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print(f"{len(out)} fixtures -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
