"""
Handles to the three stores, each opened lazily on first use.

Nothing here runs at import or at server construction: the encoder costs ~10 s
and ~1 GB, and a client that only lists tools should not pay for it. Every
handle is created once and shared by all tool calls for the life of the
process.

    Qdrant + BioBERT   protein function text        QDRANT_URL / QDRANT_PATH,
                                                     BIOBERT_MODEL_PATH, BIOBERT_REVISION
    Neo4j              knowledge graph + registry   NEO4J_URI (bolt://localhost:7687),
                                                     NEO4J_USER / NEO4J_PASSWORD (optional)
    Zilliz (Milvus)    PubMed abstracts             ZILLIS_URI, ZILLIS_TOKEN (read-only)
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from .core.config import Settings, load_encoder, load_sparse_encoder, open_client
if TYPE_CHECKING:
    # Importing qdrant_client costs ~3 s -- most of the server's import time, and
    # Claude Desktop gives a server 10 s to start. Defer it to the first call.
    from .core.retriever import Retriever

# Free-form Cypher can ask for anything; these keep one call bounded.
CYPHER_TIMEOUT_S = float(os.environ.get("NEO4J_QUERY_TIMEOUT", 20))


def make_retriever(settings: Settings | None = None) -> "Retriever":
    from .core.retriever import Retriever

    settings = settings or Settings.from_env()
    return Retriever(
        client=open_client(storage=settings.storage, url=settings.url, api_key=settings.api_key),
        encoder=load_pinned_encoder(settings.model_path, os.environ.get("BIOBERT_REVISION")),
        sparse_encoder=load_sparse_encoder(),
        prefetch_limit=settings.prefetch_limit,
        shortlist=settings.shortlist,
        chunk_prefetch_limit=settings.chunk_prefetch_limit,
    )


def load_pinned_encoder(model_path: str, revision: str | None):
    """
    load_encoder, plus a Hub revision pin. The collections were embedded by one
    specific set of weights; a query encoded by any other set lands in a
    different vector space and retrieval degrades without erroring. Pinning the
    commit means a later push to the Hub repo cannot change what this serves.
    """
    if not revision:
        return load_encoder(model_path)
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(model_path, revision=revision)


def jsonable(value: Any) -> Any:
    """Neo4j values -> JSON. Nodes/relationships come back from .data() as dicts already."""
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)                                   # neo4j temporal/spatial types


class Clients:
    def __init__(self, settings: Settings | None = None, retriever: "Retriever | None" = None):
        self.settings = settings
        self._retriever = retriever
        self._driver = None
        self._milvus = None

    # --- Qdrant + encoder ------------------------------------------------
    @property
    def retriever(self) -> "Retriever":
        if self._retriever is None:
            self._retriever = make_retriever(self.settings)
        return self._retriever

    @property
    def qdrant(self):
        """
        The Qdrant client alone, for tools that read payloads but never embed a
        query (cited_pmids, collection_info). Building the retriever would load
        the encoder -- measured at 22 s on a cold process -- for nothing.
        """
        if self._retriever is not None:
            return self._retriever.client
        if getattr(self, "_qdrant", None) is None:
            s = self.settings or Settings.from_env()
            self._qdrant = open_client(storage=s.storage, url=s.url, api_key=s.api_key)
        return self._qdrant

    @property
    def encoder(self):
        """The fine-tuned BioBERT, shared with the retriever -- loaded once."""
        return self.retriever.encoder

    # --- Neo4j -----------------------------------------------------------
    @property
    def neo4j(self):
        if self._driver is None:
            from neo4j import GraphDatabase
            user, password = os.environ.get("NEO4J_USER"), os.environ.get("NEO4J_PASSWORD")
            self._driver = GraphDatabase.driver(
                os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
                auth=(user, password) if user else None,
                notifications_min_severity="OFF",
                connection_timeout=5)
        return self._driver

    def read(self, cypher: str, params: dict | None = None,
             timeout: float = CYPHER_TIMEOUT_S, max_rows: int | None = None) -> tuple[list[dict], bool]:
        """
        Run Cypher in a READ transaction: the server rejects any write
        (Neo.ClientError.Statement.AccessMode), APOC write procedures included
        -- verified against this Community instance. Returns (rows, truncated).
        """
        from neo4j import READ_ACCESS, unit_of_work

        @unit_of_work(timeout=timeout)
        def work(tx):
            result = tx.run(cypher, params or {})
            rows, truncated = [], False
            for record in result:
                if max_rows is not None and len(rows) >= max_rows:
                    truncated = True
                    break
                rows.append(jsonable(record.data()))
            return rows, truncated

        with self.neo4j.session(default_access_mode=READ_ACCESS) as session:
            return session.execute_read(work)

    # --- Zilliz ----------------------------------------------------------
    @property
    def milvus(self):
        if self._milvus is None:
            uri, token = os.environ.get("ZILLIS_URI"), os.environ.get("ZILLIS_TOKEN")
            if not uri:
                raise RuntimeError("The literature store is not configured: set ZILLIS_URI "
                                   "and ZILLIS_TOKEN to enable the PubMed tools.")
            from pymilvus import MilvusClient
            self._milvus = MilvusClient(uri=uri, token=token, timeout=15)
        return self._milvus
