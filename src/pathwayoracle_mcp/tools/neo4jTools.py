from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt.tool_node import InjectedState
from langgraph.types import Command
from pydantic import BaseModel, Field
from typing_extensions import Annotated
from DeepAgent.tools.TextSearch import get_MultCandidates
from langchain.chat_models import init_chat_model
from DeepAgent.utils.prompts import ENTITY_EXTRACT
from typing import Annotated, List
from DeepAgent.tools.llmTools import format_summary, process_neo4j_results
from DeepAgent.utils.state import Graph
from DeepAgent.utils.db import cQueryToServer
from DeepAgent.graph_subagents.state import Neo4jState


#----------------Term Extraction
extraction_model = init_chat_model(model="openai:gpt-4o-mini")

class ExtractOutput(BaseModel):
    entities: List[str] = Field(
        description='Extracted genes or pathways (processes) from the question or context only. Not sourced from non-sourced knowledge.'
    )


def entity_extraction(query_phrase: str) -> list[dict]:
    """Core entity extraction logic. Works standalone.

    Conducts a full text search on the graph database identifying relevant node entites such as genes or pathways (processes or diseases).

    Args:
        query_phrase: Any name that is evaluated as referencing a biological gene, entity, or process.

    Returns:
        List of dictionaries that is finding top 3 best results for a given query and the node label (gene or a pathway).
    """
    structured_extraction = extraction_model.with_structured_output(ExtractOutput)
    extract_output = structured_extraction.invoke([
        HumanMessage(content=ENTITY_EXTRACT.format(
            query_phrase=query_phrase
        ))
    ])
    entities = extract_output.entities or []

    candidate_list = get_MultCandidates(entities, limit=3, threshold=0.7)
    return candidate_list



#---------------------------- Graph Traversal and Structure Update

def update_graph(graph:Graph, records: List[dict])-> Graph:

    graph_quads = [(r['source'], r['relation'], r['target'], r['pathway']) for r in records]
    graph.add_edges(graph_quads)

    return graph



@tool(parse_docstring=True)
def pathway_traverse(pathway_name: str,
                     state: Annotated[Neo4jState, InjectedState],
                     tool_call_id: Annotated[str, InjectedToolCallId]):
    """ If any information about a general pathway (biological process) is requested, this query will identify all genes and their interactions.

    Navigates the r.Pathway parameter in the Neo4j database to identify all genes, their interactions, in a given pathway.

    Args:
        pathway_name: entity_extraction search identified pathway candidate name that is evaluated as of interest by the LLM.
        state: Injected agent state for file storage
        tool_call_id: Injected tool call identifier
    
    Returns:
        Command that saves full results to files and provides minimal summary
    """
    PATHWAY_TRAVERSE =  """
                        MATCH (g:Gene)-[r]->(g2:Gene)
                        WHERE $pathwayName IN r.Pathway
                        RETURN g.name as source, type(r) as relation, g2.name as target, collect($pathwayName) as pathway
                        """
    records = cQueryToServer(query=PATHWAY_TRAVERSE,
                                   parameters = {"pathwayName": pathway_name})
    
    processed_results = process_neo4j_results(records)

    # Save each result to a file and prepare summary
    files = state.get("files", {})
    updated_files, summary_text = format_summary(processed_results=processed_results,
                                                 files=files, query=PATHWAY_TRAVERSE)

    # Update the current graph context with the new discovered edges.
    old_graph = state.get("_structure_map")
    print(old_graph.nodes)

    graph_quads = [(r['source'], r['relation'], r['target'], r['pathway']) for r in records]
    old_graph.add_edges(graph_quads)

    print(old_graph.nodes)

    old_entities = state.get("entities", set())
    new_entities = old_entities | set(processed_results['entities'])

    return Command(
        update={
            "files": updated_files,
            "_structure_map": old_graph,
            "entities": new_entities,
            "messages": [ToolMessage(summary_text, tool_call_id=tool_call_id)],
        }
    )



from typing import Optional
@tool(parse_docstring=True)
def graph_explore(nodes: List[str],
                  pathway: Optional[str], 
                  state: Annotated[Neo4jState, InjectedState],
                  tool_call_id: Annotated[str, InjectedToolCallId]
                  ):
    """ Explore the neighboring interactions of a specific set of genes using the structured Neo4j database.

    Performs a 1hop exploration of genes around key genes of interest, may be a filtered hop only finding interacting genes in a specific pathway.

    Args:
        nodes: A list of F-text search identified gene names to explore in the graph database.
        pathway: F-Text search identified pathway that are evaluated as of interest by the LLM. Can be optional (not provided), in that case searches all hop neighbors not just in specific pathway.
        state: Injected agent state for file storage
        tool_call_id: Injected tool call identifier

    Returns:
        Command that saves full results to files and provides minimal summary
    """

    if pathway:
        INTX_FIND = """
                    MATCH (g:Gene)-[r]->(g2:Gene)
                    WHERE g.name in $node_list and $pathwayName in r.Pathway
                    RETURN g.name as source, type(r) as relation, g2.name as target, collect($pathwayName) as pathway
                    """
        records = cQueryToServer(query=INTX_FIND, parameters={"node_list": nodes, "pathwayName": pathway})
    else:
        INTX_FIND = """
                    MATCH (g:Gene)-[r]->(g2:Gene)
                    WHERE g.name IN $node_list
                    RETURN g.name as source, type(r) as relation, g2.name as target, r.Pathway as pathway
                    """
        records = cQueryToServer(query=INTX_FIND,
                            parameters = {"node_list": nodes})
        
    
    processed_results = process_neo4j_results(records)

    # Save each result to a file and prepare summary
    files = state.get("files", {})
    updated_files, summary_text = format_summary(processed_results=processed_results,
                                                 files=files, query=INTX_FIND)

    # Update the current graph context with the new discovered edges.
    old_graph = state.get("_structure_map")
    print(old_graph.nodes)

    graph_quads = [(r['source'], r['relation'], r['target'], r['pathway']) for r in records]
    old_graph.add_edges(graph_quads)
    
    print(old_graph.nodes)
    old_entities = state.get("entities", set())
    new_entities = old_entities | set(processed_results['entities'])

    return Command(
        update={
            "files": updated_files,
            "_structure_map": old_graph,
            "entities": new_entities,
            "messages": [ToolMessage(summary_text, tool_call_id=tool_call_id)],
        }
    )



