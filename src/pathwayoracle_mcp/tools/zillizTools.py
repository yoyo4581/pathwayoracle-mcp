from DeepAgent.tools.BertEmbeddings import bio_bert_embeddings
from pymilvus import AnnSearchRequest, RRFRanker
import os
from dotenv import load_dotenv
from pymilvus import (
    MilvusClient, DataType, Function, FunctionType
)
load_dotenv()


client = MilvusClient(
    uri=os.getenv("ZILLIS_URI"),
    token=os.getenv("ZILLIS_TOKEN")
)

def get_pub_linked_info(entity_phrase: str, filter_pubmed: list[int] =None, query_phrase: str=None, limit=3) -> dict[int, dict]:
    expr = None
    if filter_pubmed is not None:
        expr = f"pmid in {filter_pubmed}"

    full_text_query = {
        "data": [entity_phrase],
        "anns_field": "sparse",
        "param": {"drop_ratio_search": 0.0},
        "limit": 100,
    }
    if expr:
        full_text_query["expr"] = expr

    request_1 = AnnSearchRequest(**full_text_query)

    reqs = [request_1]

    if query_phrase:
        queryEmbedding = bio_bert_embeddings.embed_query(query_phrase).tolist()
        semantic_query = {
            "data": [queryEmbedding],
            "anns_field": "dense",
            "param": {"nprobe": 10},
            "limit": 100,
        }
        if expr:
            semantic_query["expr"] = expr
        request_2 = AnnSearchRequest(**semantic_query)
        reqs.append(request_2)

    ranker = RRFRanker(100)

    res = client.hybrid_search(
        collection_name="abstract_search",
        reqs=reqs,
        ranker=ranker,
        limit=100,
        output_fields=["pmid", "text", "dense"],
    )

    hits = res[0]
    abstract_texts = {hit["entity"]["pmid"]: {'text': hit["entity"]["text"], "score": hit["distance"], "embedding": hit["entity"]["dense"]}
                    for hit in hits[:limit]}

    return abstract_texts

