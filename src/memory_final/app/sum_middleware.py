from typing import Any
from typing_extensions import override
from langchain.agents.middleware import SummarizationMiddleware
from langchain.agents.middleware.types import AgentState, ContextT

from langgraph.runtime import Runtime
from langgraph.graph.message import RemoveMessage, REMOVE_ALL_MESSAGES


class AgentLoadSummarizationMiddleware(SummarizationMiddleware):
    """
    Overrides SummarizationMiddleware to run summarization once before
    agent execution rather than before every model call.
    
    Inherits all private methods from SummarizationMiddleware:
    _ensure_message_ids, _should_summarize, _determine_cutoff_index,
    _partition_messages, _create_summary, _acreate_summary, _build_new_messages
    """

    @override
    def before_model(
        self, state: AgentState[Any], runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        # Disabled — summarization moved to before_agent
        return None

    @override
    async def abefore_model(
        self, state: AgentState[Any], runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        # Disabled — summarization moved to abefore_agent
        return None

    @override
    def before_agent(
        self, state: AgentState[Any], runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        """
        Runs summarization once on agent load instead of before every model call.
        Uses identical logic to original before_model.
        """
        messages = state["messages"]
        self._ensure_message_ids(messages)

        total_tokens = self.token_counter(messages)
        if not self._should_summarize(messages, total_tokens):
            return None

        cutoff_index = self._determine_cutoff_index(messages)

        if cutoff_index <= 0:
            return None

        messages_to_summarize, preserved_messages = self._partition_messages(
            messages, cutoff_index
        )

        summary = self._create_summary(messages_to_summarize)
        new_messages = self._build_new_messages(summary)

        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *new_messages,
                *preserved_messages,
            ]
        }

    @override
    async def abefore_agent(
        self, state: AgentState[Any], runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        """
        Async version — runs summarization once on agent load.
        Uses identical logic to original abefore_model.
        """
        messages = state["messages"]
        self._ensure_message_ids(messages)

        total_tokens = self.token_counter(messages)
        if not self._should_summarize(messages, total_tokens):
            return None

        cutoff_index = self._determine_cutoff_index(messages)

        if cutoff_index <= 0:
            return None

        messages_to_summarize, preserved_messages = self._partition_messages(
            messages, cutoff_index
        )

        summary = await self._acreate_summary(messages_to_summarize)
        new_messages = self._build_new_messages(summary)

        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *new_messages,
                *preserved_messages,
            ]
        }