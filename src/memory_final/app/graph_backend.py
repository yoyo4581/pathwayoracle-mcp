import pickle
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import networkx as nx
from .mem_extract import KnowledgeTriplet
from qdrant_client import QdrantClient
from langchain_qdrant import QdrantVectorStore
from .embeddings import MemEmbeddings
from collections import defaultdict
from langchain_core.documents import Document

from qdrant_client.models import Filter, HasIdCondition, Distance, VectorParams
from .logger import logger
import os

# ══════════════════════════════════════════════════════════════════════════════
# _GraphMemoryBackend — NetworkX graph, persistence, traversal (private)
# ══════════════════════════════════════════════════════════════════════════════

class QdrantMemoryBackend:
    def __init__(self):
        self.client = QdrantClient(url="http://localhost:6333")
        self._ensure_collection()
        self.vector_store = QdrantVectorStore(
            client = self.client,
            embedding = MemEmbeddings(),
            collection_name = "memories"
        )
        
    def _ensure_collection(self):
        collections = self.client.get_collections().collections
        existing = {c.name for c in collections}
        if "memories" not in existing:
            self.client.create_collection(
                collection_name="memories",
                vectors_config=VectorParams(
                    size=768,
                    distance=Distance.COSINE
                )
            )
     #-------------Qdrant vector store add operations-----------------
    def add_document(self, documents: List[Document], ids: List[str]):
        self.vector_store.add_documents(documents=documents, ids=ids)

    # ── Search / traversal ─────────────────────────────────────────────────────
    def search(self, query: str, mem_node_ids: List) -> List[str]:
        """
        Find memory nodes whose stored value contains the query string,
        then walk the graph neighbourhood to surface related
        entities and past interactions.
        """

        if not mem_node_ids:
            docs = self.vector_store.similarity_search(query=query, k=10)
        else:
            docs = self.vector_store.similarity_search(
                query=query, 
                filter=Filter(
                    must=[
                        HasIdCondition(has_id=mem_node_ids)
                    ]
                ),
                k=10
            )

        memory_ids = [doc.metadata["memory_id"] for doc in docs]
        return memory_ids

class GraphMemoryBackend:
    """
    Internal NetworkX graph backend.
    Handles persistence, entity upsert, and neighbourhood traversal.
    Not meant to be used directly — call through GraphMemoryStore.
    """

    def __init__(self):
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.storage_dir = os.path.abspath(os.path.join(self.base_dir, "data"))
        self.storage_file = f"{self.storage_dir}/graph_memory.pkl"
        self.graph: nx.DiGraph = self._load()
        self._namespace_index : dict[tuple[str,...], set[str]] =  self._build_namespace_index()# namespace -> set(node_ids)
        
    # ── Persistence ────────────────────────────────────────────────────────────

    def _load(self) -> nx.DiGraph:
        path = Path(self.storage_file)
        if path.exists():
            try:
                with open(path, "rb") as f:
                    g = pickle.load(f)
                logger.info(
                    f"[GraphMemoryStore] Loaded graph: "
                    f"{g.number_of_nodes()} nodes, {g.number_of_edges()} edges"
                )
                return g
            except Exception as e:
                logger.warning(f"[GraphMemoryStore] Could not load graph ({e}). Starting fresh.")
        return nx.DiGraph()
    
    def _build_namespace_index(self) -> dict[tuple[str,...], set[str]]:
        index = defaultdict(set)
        for node_id, data in self.graph.nodes(data=True):
            namespace = data.get("namespace", None)
            if namespace:
                ns_builder = []
                for ns_item in namespace.split(":"):
                    ns_builder.append(ns_item)
                    index[tuple(ns_builder)].add(node_id)
        return index
    
    def _update_namespace_index(self, node_id: str, namespace: str):
        ns_builder = []
        for ns_item in namespace.split(":"):
            ns_builder.append(ns_item)
            self._namespace_index[tuple(ns_builder)].add(node_id)
    
    def _match_namespaces_prefix(self, prefix: tuple[str, ...], max_depth: int = None) -> list[tuple[str, ...]]:
        matches = []
        
        for ns_tuple in self._namespace_index.keys():
            if len(ns_tuple) < len(prefix):
                continue
            if max_depth is not None and len(ns_tuple) > max_depth:
                continue

            matched = True
            for i, token in enumerate(prefix):
                if token != "*" and ns_tuple[i] != token:
                    matched = False
                    break

            if matched:
                matches.append(ns_tuple)

        return matches

    
    def _match_namespaces_suffix(self, suffix: tuple[str, ...], max_depth: int = None) -> list[tuple[str, ...]]:
        matches = []

        for ns_tuple in self._namespace_index.keys():
            if len(ns_tuple) < len(suffix):
                continue
            if max_depth is not None and len(ns_tuple) > max_depth:
                continue

            start = len(ns_tuple) - len(suffix)

            matched = True
            for i, token in enumerate(suffix):
                if token != "*" and ns_tuple[start + i] != token:
                    matched = False
                    break

            if matched:
                matches.append(ns_tuple)

        return matches



    def save(self):
        try:
            with open(self.storage_file, "wb") as f:
                pickle.dump(self.graph, f)
        except Exception as e:
            logger.warning(f"[GraphMemoryStore] Save failed: {e}")


    # ── Node / edge operations ─────────────────────────────────────────────────

    def upsert_node(self, node_id: str, **attrs):
        if not self.graph.has_node(node_id):
            self.graph.add_node(node_id, mention_count=0, **attrs)
        else:
            self.graph.nodes[node_id].update(attrs)
        self.graph.nodes[node_id]["mention_count"] = (
            self.graph.nodes[node_id].get("mention_count", 0) + 1
        )

    def upsert_edge(self, source: str, target: str, predicate: str):
        if self.graph.has_edge(source, target):
            ed = self.graph[source][target]
            ed["weight"] = ed.get("weight", 1) + 1
            if predicate not in ed.get("predicates", []):
                ed.setdefault("predicates", []).append(predicate)
        else:
            self.graph.add_edge(
                source, target, relation=predicate, predicates=[predicate], weight=1
            )

    def add_triplets(
        self,
        triplets: List[KnowledgeTriplet],
        memory_node_id: str,
    ):
        """
        Merge KnowledgeTriplet objects into the graph and link each entity
        back to the memory node they were extracted from.
        """
        for triplet in triplets:
            self.upsert_node(triplet.subject, type="entity")
            self.upsert_node(triplet.object, type="entity")
            self.upsert_edge(triplet.subject, triplet.object, triplet.predicate)

            # Back-links: entity -> memory node for reverse lookup
            if self.graph.has_node(memory_node_id):
                self.graph.add_edge(triplet.subject, memory_node_id, relation="extracted_from")
                self.graph.add_edge(triplet.object, memory_node_id, relation="extracted_from")

        self.save()
        
    def _get_entities_from_memory(self, memory_node_id: str)-> set[str]:
        entities = set()

        for pred in self.graph.predecessors(memory_node_id):
            edge_data = self.graph.get_edge_data(pred, memory_node_id)
            if edge_data.get("relation") == "extracted_from":
                entities.add(pred)
        return entities
    
    def get_triplets_from_memory(self, memory_node_id: str):
        triplets = set()
        entities = self._get_entities_from_memory(memory_node_id)

        for source in entities:
            for target in self.graph.successors(source):

                edge_data = self.graph.get_edge_data(source, target)

                #Skip extracted_from edges
                if edge_data.get("relation") == "extracted_from":
                    continue

                if self.graph.nodes[target].get("type") == "entity":
                    predicate = edge_data.get("relation")
                    triplets.add((source, predicate, target))
        
        return triplets


    def get_node_value(self, node_id: str) -> Optional[Dict]:
        if not self.graph.has_node(node_id):
            return None
        return self.graph.nodes[node_id].get("value")

    def delete_node(self, node_id: str):
        if self.graph.has_node(node_id):
            self.graph.remove_node(node_id)


    def stats(self) -> Dict:
        entities = [n for n, d in self.graph.nodes(data=True) if d.get("type") == "entity"]
        return {
            "total_nodes": self.graph.number_of_nodes(),
            "total_edges": self.graph.number_of_edges(),
            "entity_count": len(entities),
            "top_entities": sorted(
                [(e, self.graph.nodes[e].get("mention_count", 0)) for e in entities],
                key=lambda x: x[1],
                reverse=True,
            )[:10],
        }

    def visualize(self, output_path: str = "graph_memory.png"):
        """Render the graph to a PNG (requires matplotlib)."""
        try:
            import matplotlib.pyplot as plt
            color_map = [
                "lightblue" if self.graph.nodes[n].get("type") == "entity"
                else "lightgreen"
                for n in self.graph.nodes()
            ]
            plt.figure(figsize=(20, 15))
            pos = nx.spring_layout(self.graph, k=1, iterations=50)
            nx.draw_networkx_nodes(self.graph, pos, node_color=color_map, node_size=700, alpha=0.7)
            nx.draw_networkx_labels(self.graph, pos, font_size=9, font_weight="bold")
            nx.draw_networkx_edges(self.graph, pos, edge_color="gray", arrows=True, alpha=0.5, arrowsize=20)
            edge_labels = {
                (u, v): data.get("predicates", [data.get("relation", "")])[0][:15]
                for u, v, data in self.graph.edges(data=True)
                if self.graph.nodes.get(u, {}).get("type") == "entity"
                and self.graph.nodes.get(v, {}).get("type") == "entity"
            }
            nx.draw_networkx_edge_labels(self.graph, pos, edge_labels, font_size=7)
            plt.title("GraphMemoryStore — Blue=Entities, Green=Memory nodes")
            plt.axis("off")
            plt.tight_layout()
            plt.savefig(f"{self.storage_dir}/{output_path}", dpi=150, bbox_inches="tight")
            logger.info(f"[GraphMemoryStore] Visualization saved to {self.storage_dir}/{output_path}")
        except ImportError:
            logger.error("Install matplotlib to use visualize()")