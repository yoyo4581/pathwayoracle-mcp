adj_fxn = """
WITH $edgeList AS edges
UNWIND edges AS edge

MATCH (g1:Gene {name: edge.src})-[r]->(g2:Gene {name: edge.dst})
WHERE type(r) = edge.relation AND ANY(p IN edge.pathway WHERE p IN r.Pathway)

MATCH (g1)-[c1:CONTAINS]->(t:TextNode)
CALL db.index.vector.queryNodes("textnode_embedding_index", 10, t.embedding) 
YIELD node AS candidate, score

MATCH (g2)-[c2:CONTAINS]->(candidate)
RETURN g1.name AS source,
       t.Text AS sourceText,
       t.chunk_id AS s_chunk,
       type(r) AS relation,
       g2.name AS target,
       candidate.Text AS targetText,
       candidate.chunk_id AS t_chunk,
       score
ORDER BY score DESC
"""

context_fxn = """
WITH $gList as geneList
MATCH (g:Gene)-[:CONTAINS]->(t:TextNode)
WHERE g.name IN geneList
WITH t, gds.similarity.cosine(t.embedding, $qEmbed) AS score, g.name as geneName
RETURN geneName, t.Text as text, t.chunk_id as chunk_id, score
ORDER BY score DESC
LIMIT 50
"""

from itertools import chain
from pprint import pprint
from langchain.agents.tool_node import InjectedState
from langchain_core.tools import InjectedToolArg, InjectedToolCallId, tool
from DeepAgent.utils.state import OverallState, Neo4jState
from typing_extensions import Annotated
from DeepAgent.tools.BertEmbeddings import bio_bert_embeddings
from DeepAgent.utils.db import cQueryToServer
from langgraph.types import Command, Send
from langchain_core.messages import ToolMessage

def stitch_function(function_dict: dict) -> dict:
    """
    Stitch overlapping chunks into sentences, removing duplicates while preserving order.
    """
    stitch_dict = {}
    for gene, content in function_dict.items():
        chunk_ids = sorted(content.keys())

        # generate sentence indices (3 per chunk)
        sentence_ids = list(dict.fromkeys(
            i for chunk_id in chunk_ids for i in range(chunk_id, chunk_id+3)
        ))

        # flatten chunks in order and split into sentences
        all_sentences = list(chain.from_iterable(
            [s.strip() for s in content[cid].split('.') if s.strip()]
            for cid in chunk_ids
        ))

        # select sentences by index and remove duplicates using dict.fromkeys
        selected_sentences = [all_sentences[i] for i in sentence_ids if i < len(all_sentences)]
        sentences = list(dict.fromkeys(selected_sentences))

        stitch_dict[gene] = '. '.join(sentences)

    return stitch_dict


def calculate_role_genes(entry_data: dict):
    filled = 0
    total = 0
    for gene, content in entry_data.items():
        total += 1
        if content['role']:
            filled += 1
    
    return filled, total

@tool(parse_docstring=True)
def function_enrich(
    query:str,
    state:Annotated[Neo4jState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId]):
    """ Use to enrich genes in the graph structure with relevant functional information. The query phrase acts as a semantic search string improving relevance, it should consolidate the overall research progress.

    Conducts functional enrichment on the current graph structure state using a query phrase to direct relevant extraction of functional annotations from each gene using semantic similarity search.

    Args:
        query: Consolidate research progress into a string query that will direct relevant functional annotations from genes using semantic similarity search.
        state: Injected agent state for file storage
        tool_call_id: Injected tool call identifier
    
    Returns:
        Command that saves full results to files and provides minimal summary
    """
    
    from collections import defaultdict

    graph = state.get('_structure_map')
    geneList = list(state.get('entities'))
    qEmbed = bio_bert_embeddings.embed_query(query).tolist()

    edgeList = [
        {"src": e.src, "dst": e.dst, "relation": e.relation, "pathway": list(e.pathway)}
        for e in graph.edges.values()
    ]

    fxn_adj = cQueryToServer(query=adj_fxn, parameters={"edgeList": edgeList})
    fxn_context = cQueryToServer(query=context_fxn, parameters={"gList": geneList, "qEmbed":qEmbed})

    db_records = state.get("entity_data")
    if not db_records:
        db_records = defaultdict(dict, {gene: {} for gene in geneList})


    fxn_dict = {gene: {} for gene in geneList}
    for record in fxn_adj:
        source_gene = record['source']
        target_gene = record['target']
        fxn_dict[source_gene][record['s_chunk']] = record['sourceText']
        fxn_dict[target_gene][record['t_chunk']] = record['targetText']
    

    for record in fxn_context:
        geneName = record['geneName']
        fxn_dict[geneName][record['chunk_id']] = record['text']
    
    stitch_dict = stitch_function(fxn_dict)
    for gene, content in db_records.items():
        if gene in stitch_dict:
            db_records[gene]['role'] = stitch_dict[gene]

    filled, total = calculate_role_genes(db_records)

    return Command(
        goto= Send(
            "summarization_flow",
            {
                **state,
                "entity_data": db_records
            }
        ),
        update={
            "entity_data": db_records,
            "messages": [ToolMessage(f"Function enrichment completed: Out of {total} genes, {filled} had relevant data", tool_call_id=tool_call_id)],
        }
    )