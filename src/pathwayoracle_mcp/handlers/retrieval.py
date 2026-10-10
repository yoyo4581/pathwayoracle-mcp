"""
Protein function text (Qdrant): the original five tools, now with citations.

These wrap core.retriever unchanged -- it is the code the backend's promotion
gate measures -- and only add `cite` to each piece of evidence:

    qdrant:<uniprot_id>#<chunk_idx>    a sentence window
    qdrant:<uniprot_id>                a whole function record
"""
from __future__ import annotations

from typing import Any

from ..core.config import CHUNKS, RECORDS
from ..core.retriever import MODES


def _cite_snippets(uniprot_id: str, snippets: list[dict]) -> list[dict]:
    for s in snippets:
        s["cite"] = f"qdrant:{uniprot_id}#{s['chunk_idx']}"
    return snippets


def search_proteins(tk, query: str, k: int, snippets_per_record: int, mode: str) -> dict:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    results = tk.clients.retriever.search(query, k=k, snippets_per_record=snippets_per_record,
                                          mode=mode)
    for r in results:
        _cite_snippets(r["uniprot_id"], r["snippets"])
    return {"query": query, "mode": mode, "n_results": len(results), "results": results}


def read_snippets(tk, uniprot_id: str, query: str, n: int) -> dict:
    snippets = tk.clients.retriever.snippets(uniprot_id, query, n=n)
    return {"uniprot_id": uniprot_id, "query": query, "n_results": len(snippets),
            "snippets": _cite_snippets(uniprot_id, snippets)}


def expand_snippet(tk, uniprot_id: str, chunk_idx: int, radius: int) -> dict:
    out = tk.clients.retriever.expand(uniprot_id, chunk_idx, radius=radius)
    out["cite"] = f"qdrant:{uniprot_id}#{chunk_idx}"
    return out


def read_record(tk, uniprot_id: str) -> dict:
    out = tk.clients.retriever.read_record(uniprot_id)
    out["cite"] = f"qdrant:{uniprot_id}"
    return out


def collection_info(tk) -> dict[str, Any]:
    client = tk.clients.qdrant
    out: dict[str, Any] = {"modes": list(MODES), "record_key": "uniprot_id"}
    for name in (RECORDS, CHUNKS):
        if not client.collection_exists(name):
            out[name] = {"exists": False}
            continue
        out[name] = {"exists": True, "points": client.get_collection(name).points_count}
    return out
