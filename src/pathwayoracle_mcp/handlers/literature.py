"""
PubMed: the studies behind the annotations.

    cited_pmids       PubMed ids a protein's UniProt function text cites, with
                      the sentence each supports (Qdrant -- always available)
    search_abstracts  hybrid search over abstracts (Zilliz)
    read_abstracts    abstracts by PMID (Zilliz)

Ported from DeepAgents' zillizTools.get_pub_linked_info. Changes: PMIDs are
validated as integers before they reach the filter expression; the 768-float
`dense` field is no longer returned; every abstract carries `cite`.

KNOWN MISMATCH (developer-facing only): the abstracts were embedded with a
different BioBERT than the fine-tuned one used for queries here, so the
semantic (query_phrase) branch ranks in a mismatched vector space until the
abstracts are re-embedded upstream. Deliberately NOT surfaced to the model: a
caveat in the result makes it distrust the ranking and read every hit to
re-rank by hand, spending tokens on a data problem the re-embedding fixes. The
model should trust the scores, as it will once the store is re-embedded.
"""
from __future__ import annotations

import re

from ..dispatch import ToolkitError

COLLECTION = "abstract_search"
_PMID = re.compile(r"PubMed:(\d+)")


def _pmids(values) -> list[int]:
    try:
        return [int(str(v).removeprefix("pubmed:").strip()) for v in values]
    except ValueError:
        raise ToolkitError("PMIDs must be numeric, e.g. 10801849.") from None


def cited_pmids(tk, uniprot_ids: list[str]) -> dict:
    from ..core.config import RECORDS, record_id

    out = []
    for uid in dict.fromkeys(uniprot_ids):
        recs = tk.clients.qdrant.retrieve(RECORDS, ids=[record_id(uid)], with_payload=["sentences"])
        if not recs:
            out.append({"uniprot_id": uid, "found": False, "citations": []})
            continue
        cites = []
        for i, sentence in enumerate(recs[0].payload["sentences"]):
            for pmid in dict.fromkeys(_PMID.findall(sentence)):
                cites.append({"pmid": pmid, "sentence_idx": i, "sentence": sentence,
                              "cite": f"pubmed:{pmid}"})
        out.append({"uniprot_id": uid, "found": True, "n_pmids": len({c["pmid"] for c in cites}),
                    "citations": cites})
    return {"results": out}


def snippet_sources(tk, uniprot_id: str, chunk_idx: int, radius: int) -> dict:
    """
    PMIDs cited inside one passage -- the narrow link from the sentence an
    answer uses to the study behind it. cited_pmids gives the whole record's
    studies; this gives the ones attached to these sentences only.
    """
    from ..core.config import CHUNKS, RECORDS, chunk_id, record_id

    ch = tk.clients.qdrant.retrieve(CHUNKS, ids=[chunk_id(uniprot_id, chunk_idx)],
                                    with_payload=["sent_start", "sent_end"])
    if not ch:
        raise LookupError(f"No chunk {chunk_idx} for {uniprot_id!r}.")
    rec = tk.clients.qdrant.retrieve(RECORDS, ids=[record_id(uniprot_id)], with_payload=["sentences"])
    if not rec:
        raise LookupError(f"No record for {uniprot_id!r}.")
    sents = rec[0].payload["sentences"]
    start = max(0, ch[0].payload["sent_start"] - radius)
    end = min(len(sents), ch[0].payload["sent_end"] + radius)

    sources = []
    for i in range(start, end):
        for pmid in dict.fromkeys(_PMID.findall(sents[i])):
            sources.append({"pmid": pmid, "sentence_idx": i, "sentence": sents[i],
                            "cite": f"pubmed:{pmid}"})
    out = {"uniprot_id": uniprot_id, "chunk_idx": chunk_idx, "sentences": [start, end],
           "passage_cite": f"qdrant:{uniprot_id}#{chunk_idx}",
           "n_pmids": len({s["pmid"] for s in sources}), "sources": sources}
    if not sources:
        out["note"] = ("No PubMed ids inside this passage. Retry with radius=1, or use "
                       "cited_pmids for every study the record cites.")
    return out


def search_abstracts(tk, entity_phrase: str, query_phrase: str | None,
                     pmids: list[str] | None, limit: int) -> dict:
    from pymilvus import AnnSearchRequest, RRFRanker

    expr = f"pmid in {_pmids(pmids)}" if pmids else None
    reqs = [AnnSearchRequest(data=[entity_phrase], anns_field="sparse",
                             param={"drop_ratio_search": 0.0}, limit=100, expr=expr)]
    if query_phrase:
        vec = tk.clients.encoder.encode(query_phrase, normalize_embeddings=True,
                                        show_progress_bar=False).tolist()
        reqs.append(AnnSearchRequest(data=[vec], anns_field="dense",
                                     param={"nprobe": 10}, limit=100, expr=expr))
    hits = tk.clients.milvus.hybrid_search(
        collection_name=COLLECTION, reqs=reqs, ranker=RRFRanker(100),
        limit=limit, output_fields=["pmid", "text"])[0]
    results = [{"pmid": str(h["entity"]["pmid"]), "score": h["distance"],
                "text": h["entity"]["text"], "cite": f"pubmed:{h['entity']['pmid']}"}
               for h in hits[:limit]]
    out = {"entity_phrase": entity_phrase, "query_phrase": query_phrase,
           "n_results": len(results), "results": results}
    return out


def read_abstracts(tk, pmids: list[str]) -> dict:
    ids = _pmids(pmids)
    rows = tk.clients.milvus.query(COLLECTION, filter=f"pmid in {ids}",
                                   output_fields=["pmid", "text"])
    found = {str(r["pmid"]): r["text"] for r in rows}
    return {"results": [{"pmid": str(p), "found": str(p) in found, "text": found.get(str(p)),
                         "cite": f"pubmed:{p}"} for p in ids]}
