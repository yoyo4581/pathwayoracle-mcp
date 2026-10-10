"""
GraphMemoryStore — LangGraph BaseStore backed by a NetworkX knowledge graph.

Integrates:
  - KnowledgeTriplet / KnowledgeTripletList  (Pydantic schema for structured extraction)
  - LLMEntityExtractor   (uses with_structured_output — no prompt-engineered JSON)
  - _GraphMemoryBackend  (NetworkX graph, persistence, traversal — internal)
  - GraphMemoryStore     (public LangGraph BaseStore implementation)

Two-layer memory model:
  - Checkpointer      → short-term, per-thread session state (managed by LangGraph)
  - GraphMemoryStore  → long-term, cross-thread knowledge graph (this file)
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import networkx as nx
from langgraph.store.base import BaseStore, Item, SearchItem, NamespacePath, MatchCondition
from langchain_core.documents import Document



# ══════════════════════════════════════════════════════════════════════════════
# GraphMemoryStore — public LangGraph BaseStore implementation
# ══════════════════════════════════════════════════════════════════════════════

from langgraph.store.base import BaseStore, GetOp, PutOp, SearchOp, ListNamespacesOp, Op, Result
from .graph_backend import GraphMemoryBackend, QdrantMemoryBackend
from .logger import logger

class GraphMemoryStore(BaseStore):
    """
    LangGraph-compatible long-term memory store backed by a NetworkX knowledge graph.

    Drop-in replacement for InMemoryStore / PostgresStore:

        from langchain_anthropic import ChatAnthropic
        from langgraph.checkpoint.sqlite import SqliteSaver

        store = GraphMemoryStore(
            storage_path="research_memory.pkl",
            llm=ChatAnthropic(model="claude-sonnet-4-5-20250929"),
        )
        app = create_react_agent(model, tools, checkpointer=checkpointer, store=store)

    Every put() call automatically:
      1. Stores the value as a memory node in the graph
      2. Runs LLM extraction via with_structured_output -> KnowledgeTripletList
      3. Adds the resulting KnowledgeTriplets as typed edges between entity nodes
      4. Persists the graph to disk

    Every search() call traverses the graph neighbourhood of matching nodes,
    surfacing related findings the agent may not have explicitly asked for.
    """

    def __init__(self):
        """
        llm:              Any LangChain chat model supporting with_structured_output,
                          e.g. ChatAnthropic(model="claude-sonnet-4-5-20250929")
                               ChatOpenAI(model="gpt-4o")
                          Pass None to use rule-based fallback extraction only.
        extract_on_write: Set False to skip entity extraction (useful for testing).
        """
        self._vector_backend = QdrantMemoryBackend()
        self._graph_backend = GraphMemoryBackend()

    # ── BaseStore — batch ────────────────────────────────────────────────────────

    def batch(self, ops):
        results = []
        for op in ops:
            if isinstance(op, GetOp):
                results.append(self._handle_get(op))
            elif isinstance(op, PutOp):
                results.append(self._handle_put(op))
            elif isinstance(op, SearchOp):
                results.append(self._handle_search(op))
            elif isinstance(op, ListNamespacesOp):
                results.append(self._handle_namespace(op))
            else:
                raise ValueError(f"Unsupported operation type: {type(op)}")
        return results

    async def abatch(self, ops):
        logger.info("ASYNC BATCH CALLED", flush=True)
        ops = list(ops)
        for op in ops:
            logger.info("ASYNC OP:", op, flush=True)
        return [None] * len(ops)


    def _handle_search(self, op: SearchOp) -> str:
    
        # Stage 1 — Regardless of the namespace prefix being empty or not, we extract all relevant namespaces and find their memory nodes.
        namespace_select = self.list_namespaces(prefix= op.namespace_prefix)


        # Stage 2 - Now that the memory node pool is identified we can run semantic search on it.
        memory_ids = list({id_ for ns in namespace_select for id_ in self._graph_backend._namespace_index.get(ns, set())})
        print(f"memory_ids set to use in search: {memory_ids}")

        ranked_mem_ids = self._vector_backend.search(query=op.query, mem_node_ids=memory_ids)
        
        # Stage 3 - Now we extract the knowledge triplets from the memory node pool.
        triplets = {
            triplet
            for mem_id in ranked_mem_ids
            for triplet in self._graph_backend.get_triplets_from_memory(mem_id)
        }
        formatted_knowledge = "Relevant knowledge:\n" + "\n -".join(f"{subj} {pred} {obj}." for subj, pred, obj in triplets)
        
        return formatted_knowledge

    def _handle_get(self, namespace: Tuple[str, ...], key: str) -> Result:
        node_id = self._node_id(namespace, key)
        value = self._graph_backend.get_node_value(node_id)
        return Result(success=value is not None, value=value)
    
    def _handle_put(self, op: PutOp ) -> str:
        node_id = op.key
        ns_str = self._ns_str(op.namespace)
        summary = op.value.get("summary", "")
        knowledge = op.value.get("knowledge", [])

        self._write_node(node_id, ns_str, {"summary": summary})
        self._graph_backend.add_triplets(memory_node_id=node_id, triplets=knowledge)
        return f"Added {len(knowledge)}"



    def _write_node(self, node_id: str, ns_str: str, value: Dict):
        """Upsert the memory node itself (separate from entity nodes)."""
        now = datetime.now().isoformat()
        existing = self._graph_backend.graph.nodes.get(node_id, {})
        self._graph_backend.graph.add_node(
            node_id,
            type="memory",
            namespace=ns_str,
            value=value,
            created_at=existing.get("created_at", now),
            updated_at=now,
            mention_count=existing.get("mention_count", 0),
        )
        self._graph_backend.save()
        self._vector_backend.add_document(
            documents=[Document(page_content=json.dumps(value), metadata={"namespace": ns_str, "memory_id": node_id})],
            ids=[node_id]
        )
        self._graph_backend._update_namespace_index(node_id, ns_str)

    # ── Extras — useful for agents / debugging ─────────────────────────────────

    def stats(self) -> Dict:
        """Return graph statistics."""
        return self._graph_backend.stats()

    def get_entity_info(self, entity: str) -> Dict:
        """Inspect a specific entity node and its relationships."""
        if entity not in self._backend.graph:
            return {"error": "Entity not found"}
        nd = self._graph_backend.graph.nodes[entity]
        relationships = []
        for nb in self._graph_backend.graph.neighbors(entity):
            if self._graph_backend.graph.nodes.get(nb, {}).get("type") == "entity":
                ed = self._graph_backend.graph[entity][nb]
                for pred in ed.get("predicates", [ed.get("relation", "related_to")]):
                    relationships.append({
                        "predicate": pred,
                        "object": nb,
                        "weight": ed.get("weight", 1),
                    })
        return {
            "entity": entity,
            "mention_count": nd.get("mention_count", 0),
            "first_seen": nd.get("created_at"),
            "relationships": relationships,
        }

    
    def list_namespaces(
        self,
        *,
        prefix: NamespacePath | None = None,
        suffix: NamespacePath | None = None,
        max_depth: int | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[tuple[str, ...]]:
        """List and filter namespaces in the store.

        Used to explore the organization of data,
        find specific collections, or navigate the namespace hierarchy.

        Args:
            prefix: Filter namespaces that start with this path.
            suffix: Filter namespaces that end with this path.
            max_depth: Return namespaces up to this depth in the hierarchy.
                Namespaces deeper than this level will be truncated.
            limit: Maximum number of namespaces to return (default 100).
            offset: Number of namespaces to skip for pagination (default 0).

        Returns:
            List[Tuple[str, ...]]: A list of namespace tuples that match the criteria.
            Each tuple represents a full namespace path up to `max_depth`.

        ???+ example "Examples":
            Setting max_depth=3. Given the namespaces:
            ```python
            # Example if you have the following namespaces:
            # ("a", "b", "c")
            # ("a", "b", "d", "e")
            # ("a", "b", "d", "i")
            # ("a", "b", "f")
            # ("a", "c", "f")
            store.list_namespaces(prefix=("a", "b"), max_depth=3)
            # [("a", "b", "c"), ("a", "b", "d"), ("a", "b", "f")]
            ```
        """
        match_conditions = []
        if prefix:
            match_conditions.append(MatchCondition(match_type="prefix", path=prefix))
        if suffix:
            match_conditions.append(MatchCondition(match_type="suffix", path=suffix))

        op = ListNamespacesOp(
            match_conditions=tuple(match_conditions),
            max_depth=max_depth,
            limit=limit,
            offset=offset,
        )
        return self.batch([op])[0]

    # ── Internal helpers ───────────────────────────────────────────────────────
    def _handle_namespace(self, op: ListNamespacesOp) -> str:
        """
        Return a List of tuple existing namespaces and their
        contents for LLM context — so the LLM can decide where to save
        new findings or whether to create a new namespace.
        
        Namespaces returned are informed by the ListNamespacesOp's filters.
        ListNamespacesOp has 1 filter field: 
            match_conditions
        Inside is a list of conditions MatchCondition with fields
            match_type: "prefix" | "suffix"
            path: "equals" | "contains" | "starts_with"

        And limiters on the schema search:
        limit: int
        max_depth: int
        """

        
        if not op.match_conditions:
            namespaces = sorted(self._graph_backend._namespace_index.keys(), key=len)[:op.limit]
        else:
            results = []

            for condition in op.match_conditions:
                if condition.match_type == "prefix":
                    results.append(
                        set(self._graph_backend._match_namespaces_prefix(condition.path, max_depth=op.max_depth))
                    )
                elif condition.match_type == "suffix":
                    results.append(
                        set(self._graph_backend._match_namespaces_suffix(condition.path, max_depth=op.max_depth))
                    )

            namespaces = set.intersection(*results) if results else set()
            offset = op.offset or 0
            namespaces = sorted(namespaces, key=len)[offset : offset + op.limit]

        print(namespaces)
        return namespaces


    @staticmethod
    def _node_id(namespace: List[str], key: str) -> str:
        return f"{':'.join(namespace)}::{key}"

    @staticmethod
    def _ns_str(namespace: List[str]) -> str:
        return ":".join(namespace)

    @staticmethod
    def _value_to_text(value: Any) -> str:
        """Flatten a value dict to plain text for entity extraction."""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return " ".join(str(v) for v in value.values() if v)
        return str(value)

    logger.info("GraphMemoryStore ready. See docstring for wiring example.")
