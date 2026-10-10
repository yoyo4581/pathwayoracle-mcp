from fastmcp import FastMCP
from langgraph.store.base import  NamespacePath
from .graph_memory_store import GraphMemoryStore
from fastmcp import FastMCP
from typing import List, Tuple
from .mem_extract import KnowledgeTriplet
import uuid
from .logger import logger


# Initialize FastMCP server
mcp = FastMCP(
    name="graph-memory",
    instructions="""
    Memory management tools using graph-based storage with semantic search.
    Uses Qdrant for similarity search and NetworkX for graph relationships.
    """
)

store = GraphMemoryStore()  # singleton in server process


@mcp.tool()
def explore_namespaces(prefix: NamespacePath | None = None,
                       suffix: NamespacePath | None = None,
                       max_depth: int | None = None,
                       limit: int = 20,
                       offset: int = 0) -> List:
    """Explore the memory namespace hierarchy to locate relevant partitions.

    Match conditions are combined with AND logic, allowing you to filter
    namespaces by both prefix and suffix simultaneously.

    Args:
        prefix: The prefix path to match namespaces against.
        suffix: The suffix path to match namespaces against.
        max_depth: The maximum depth of namespaces to return.
        limit: The maximum number of namespaces to return.
        offset: The offset into the list of matching namespaces.

    NamespacePath matching:
    # Example if you have the following namespaces:
            # ("a", "b", "c")
            # ("a", "b", "d", "e")
            # ("a", "b", "d", "i")
            # ("a", "b", "f")
            # ("a", "c", "f")
    - A prefix of ("a", "b") would match: ("a", "b", "c"), ("a", "b", "d", "e"), ("a", "b", "d", "i"), ("a", "b", "f")
    - A suffix of ("d", "e") would match: ("a", "b", "d", "e")
    - A prefix of ("a",) and a suffix of ("f",) would match: ("a", "b", "f"), ("a", "c", "f")
    - A prefix of ("a", "b", "*") and a suffix of ("e",) would match: ("a", "b", "d", "e)
    - A prefix of ("a", "b") and a max_depth of 3 would match: ("a", "b", "c"), ("a", "b", "d"), ("a", "b", "f")

    Returns:
        List of namespace tuples matching all provided conditions.
    """

    return store.list_namespaces(prefix=prefix, suffix=suffix, max_depth=max_depth, limit=limit, offset=offset)


@mcp.tool()
def memorize(knowledge: List[KnowledgeTriplet], namespace: Tuple[str, ...], content: str) -> None:
    """Always use this to save important information for long-term recall.

    Args:
        knowledge: A list of knowledge triplets to store.
        namespace: The namespace under which to store the knowledge.
        content: A concise summary of takeaways derived from the interaction, used for recall.
    """
    store.put(
                namespace=namespace or tuple(),
                key=str(uuid.uuid4()),
                value={"summary": content, "knowledge": knowledge}
            )
    


@mcp.tool()
def search_memory(namespace_prefix: NamespacePath, query: str, limit: int = 20) -> str:
    """Search long-term memory within a specific namespace partition.

    Always call explore_namespaces first to identify the correct namespace.

    Args:
        namespace: The resolved namespace path to search within. Could be empty tuple conducting a global search across all namespaces, but it's recommended to use a specific namespace for better relevance.
        query: Natural language query for semantic search.
        limit: Maximum number of results to return. Defaults to 20.

    Returns:
        A string repesentation of knowledge graph items matching the query within the namespace.
    """
    result = store.search(namespace_prefix, query=query, limit=limit)
    return result


if __name__ == "__main__":
    # For stdio (Claude Desktop, Docker)
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--http":
        # Run HTTP server
        mcp.run(transport="http", port=8000)
    else:
        # Run stdio server (default)
        mcp.run()