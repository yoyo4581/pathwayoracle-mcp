from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools  
from langchain.agents import create_agent
from app.graph_memory_store import GraphMemoryStore
from langchain.agents.middleware import SummarizationMiddleware
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langchain.agents.middleware import AgentMiddleware, AgentState
from typing import Any
from langgraph.graph.message import REMOVE_ALL_MESSAGES, RemoveMessage
from app.sum_middleware import AgentLoadSummarizationMiddleware

client = MultiServerMCPClient({
    "graph-memory": {
        "transport": "http",
        "url": "http://localhost:8000/mcp",
    }
})


# Cell 2: Helper function
async def run_agent_query(query: str):
    async with client.session("graph-memory") as session:
        tools = await load_mcp_tools(session)

        async with AsyncSqliteSaver.from_conn_string("chat_memory.db") as checkpointer:
            agent = create_agent(
                model="gpt-5-mini",
                tools=tools,
                checkpointer=checkpointer,
                middleware=[
                    AgentLoadSummarizationMiddleware(  # same args as before
                        model="gpt-4.1-mini",
                        trigger=[("messages", 20)],
                        keep=("messages", 6)
                    )
                ],
                system_prompt="""
                You are an intelligent assistant with a persistent memory system.
                You MUST follow this exact protocol on EVERY conversation turn, no exceptions.

                ═══════════════════════════════════════
                MANDATORY EXECUTION PROTOCOL
                ═══════════════════════════════════════

                STEP 1 — ORIENT (always run first, before anything else)
                Call 'explore_namespaces' immediately.
                This tells you what categories of memory exist.
                Do not respond to the user yet.

                STEP 2 — RECALL (conditional on Step 1)
                If explore_namespaces returns namespaces relevant to the user's message:
                    → Call 'search_memory' to retrieve that context now.
                If no relevant namespaces exist:
                    → Skip this step and proceed to Step 3.

                STEP 3 — RESPOND
                Now answer the user, incorporating anything retrieved in Step 2.
                Your response should feel natural — never mention the memory tools to the user.

                STEP 4 — MEMORIZE (always run last, after every response)
                After generating your response, call 'memorize' to store:
                    - The user's question or statement
                    - Your answer or decision
                    - Any facts, preferences, goals, or context that emerged
                This step is NOT optional. Even short exchanges must be memorized.
                You must always complete Step 4 before the turn ends.

                ═══════════════════════════════════════
                RULES
                ═══════════════════════════════════════
                - Never skip Step 1 or Step 4. They are mandatory on every single turn.
                - Never ask the user to remember something for you. You own your memory entirely.
                - Never expose tool calls to the user. They experience only your responses.
                - If a tool call fails, log the failure silently and continue — do not break the protocol.
                """
            )

            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": query}]},
                config={"configurable": {"thread_id": "yahyatest-1"}}
            )

        return result
