from typing import Annotated, List
from langchain.documents import Document

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import InjectedToolArg, InjectedToolCallId, tool
from langchain.agents.tool_node import InjectedState
from langgraph.types import Command
from typing_extensions import Annotated, Literal

from typing import Annotated, List
from DeepAgent.tools.llmTools import format_summary, process_neo4j_results
from DeepAgent.utils.state import OverallState, Neo4jState
from DeepAgent.utils.db import cQueryToServer




def greedy_set_cover(universe, subsets):
    uncovered = set(universe)
    selected = set()
    gene_assignment = {}

    while uncovered:
        # pick ontology covering most uncovered genes
        best_ontology, best_genes = max(
            subsets.items(),
            key=lambda item: len(item[1] & uncovered)
        )
        selected.add(best_ontology)
        for gene in best_genes & uncovered:
            gene_assignment[gene] = best_ontology
            uncovered.remove(gene)

    return selected, gene_assignment


def docufy(selected_ontologies, gene_assignment, itx_sub, records):
    from collections import defaultdict

    records_by_gene = {}
    for record in records:
        records_by_gene[record['gene']] = record

    # Pre-index interactions by source gene
    itx_by_source = defaultdict(list)
    for itx in itx_sub:
        itx_by_source[itx['source']].append(itx)

    remapped_dict = defaultdict(list)

    for onto in selected_ontologies:
        for gene, assigned_onto in gene_assignment.items():
            if onto != assigned_onto:
                continue

            record = records_by_gene[gene]
            content = record.get('role') or ""  # ensure string
            pathway = record.get('pathway', "")
            name = gene

            #Build reactions for this gene
            reactions = [
                f"{itx['relation']} of {itx['target']}"
                for itx in itx_by_source.get(gene, [])
            ]

            # Create document
            doc = Document(page_content="")
            doc.metadata['name'] = name
            doc.metadata['pathway'] = pathway
            doc.metadata['interactions'] = reactions
            doc.metadata['function'] = content

            remapped_dict[onto].append(doc)
    
    return remapped_dict


def ontoFind(gene_sub: List[str], hops: int):
    """ Identifies a ontology term that relates or groups the functionality of a set of genes, limiting the number of hops."""

    cypher_onto = f"""
    WITH $gList AS geneList
    UNWIND geneList AS gName

    MATCH (g:Gene {{name: gName}})-[:involved_in|n4sch__SCO|n4sch__SCO_RESTRICTION*1..{hops}]->(o:n4sch__Class)
    WITH o, collect(DISTINCT g.name) AS genesConnected
    WHERE size(genesConnected) > 2
    UNWIND genesConnected AS gene
    RETURN gene, o.name AS ontology
    ORDER BY gene, ontology
    """

    rows = cQueryToServer(query=cypher_onto, parameters={"gList": gene_sub})

    from collections import defaultdict

    # Suppose `rows` is your Neo4j query result
    ontology_to_genes = defaultdict(set)
    for row in rows:
        ontology_to_genes[row['ontology']].add(row['gene'])

    # universe = all genes
    universe = set([row['gene'] for row in rows])

    selected_ontologies, gene_assignment = greedy_set_cover(universe, ontology_to_genes)
    return selected_ontologies, gene_assignment, cypher_onto



@tool(parse_docstring=True)
def onto_relate(nodes: list,
                state: Annotated[Neo4jState, InjectedState],
                tool_call_id: Annotated[str, InjectedToolCallId],
                hops: int = 2
                ):
    """ Identifies a ontology term that relates or groups the functionality of a set of genes.

    Performs a Least Common Ancestor traversal through the ontology web from a set of genes.

    Args:
        nodes: F-Text search identified set of genes that the LLM wants to understand their common functionality.
        hops: Number of hops to traverse the ontology web, default is 2 hops. Lower this to get more specific terms, increase to get more general terms.
        state: Injected agent state for file storage
        tool_call_id: Injected tool call identifier

    Returns:
        Command that saves full results to files and provides minimal summary
    """
    selected_onto, gene_assign, cypher_onto = ontoFind(gene_sub=nodes, hops=hops)

    processed_results = process_neo4j_results([gene_assign])

    # Save each result to a file and prepare summary
    files = state.get("files", {})
    updated_files, summary_text = format_summary(processed_results=processed_results,
                                                 files=files, query=cypher_onto)


    return Command(
        update={
            "files": updated_files,
            "subagents": {
                "neo4j_explorer": {
                    "name": "neo4j_explorer",
                    "summary": summary_text,
                    "entities": processed_results['entities']
                }
            },
            "messages": [ToolMessage(summary_text, tool_call_id=tool_call_id)],
        }
    )