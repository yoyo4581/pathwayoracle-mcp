from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from pydantic import BaseModel, Field

# ══════════════════════════════════════════════════════════════════════════════
# Pydantic schema — the structured output contract
# ══════════════════════════════════════════════════════════════════════════════

class KnowledgeTriplet(BaseModel):
    """A single (subject, predicate, object) fact extracted from text."""

    subject: str = Field(
        description=(
            "The entity or concept that the relationship originates from. "
            "Use consistent title-case, e.g. 'OpenAI', 'Alice', 'Vector Search'."
        )
    )
    predicate: str = Field(
        description=(
            "The relationship type in snake_case, "
            "e.g. works_at, cites, relates_to, uses, authored_by, part_of."
        )
    )
    object: str = Field(
        description=(
            "The entity or concept the relationship points to. "
            "Use consistent title-case."
        )
    )


class KnowledgeTripletList(BaseModel):
    """All knowledge triplets extracted from a piece of text."""

    triplets: List[KnowledgeTriplet] = Field(
        description=(
            "Every entity-relationship-entity fact found in the text. "
            "Include both explicit and strongly implied relationships."
        )
    )



# class TripletExtractor:
#     def __init__(self):
#         # Example: Use a relation extraction model
#         self.triplet_extractor = pipeline(
#             "text2text-generation",
#             model=AutoModelForSeq2SeqLM.from_pretrained("Babelscape/rebel-large"),
#             tokenizer= AutoTokenizer.from_pretrained("Babelscape/rebel-large")
#         )
    
#     def extract_triplets(self, text: str) -> List[Dict]:
#         """Extract raw triplets from text using NLP."""
#         raw_text = self.triplet_extractor.tokenizer.batch_decode(
#             [self.triplet_extractor(text, return_tensors=True, return_text=False)[0]["generated_token_ids"]]
#         )
#         extracted_triplets = self.parse_triplets(raw_text[0])
#         return extracted_triplets
        
#     def parse_triplets(self, text: str):
#         triplets = []
#         relation, subject, relation, object_ = '', '', '', ''
#         text = text.strip()
#         current = 'x'
#         for token in text.replace("<s>", "").replace("<pad>", "").replace("</s>", "").split():
#             if token == "<triplet>":
#                 current = 't'
#                 if relation != '':
#                     triplets.append({'head': subject.strip(), 'type': relation.strip(),'tail': object_.strip()})
#                     relation = ''
#                 subject = ''
#             elif token == "<subj>":
#                 current = 's'
#                 if relation != '':
#                     triplets.append({'head': subject.strip(), 'type': relation.strip(),'tail': object_.strip()})
#                 object_ = ''
#             elif token == "<obj>":
#                 current = 'o'
#                 relation = ''
#             else:
#                 if current == 't':
#                     subject += ' ' + token
#                 elif current == 's':
#                     object_ += ' ' + token
#                 elif current == 'o':
#                     relation += ' ' + token
#         if subject != '' and relation != '' and object_ != '':
#             triplets.append({'head': subject.strip(), 'type': relation.strip(),'tail': object_.strip()})
#         return triplets
    


# # ══════════════════════════════════════════════════════════════════════════════
# # LLMEntityExtractor — with_structured_output, no manual JSON parsing
# # ══════════════════════════════════════════════════════════════════════════════

# class LLMEntityExtractor:
#     """
#     Extracts KnowledgeTriplets from text using a LangChain chat model and
#     with_structured_output. Accepts any LangChain-compatible chat model
#     (ChatAnthropic, ChatOpenAI, etc.). Falls back to simple co-occurrence
#     extraction when no model is provided.

#     Usage:
#         from langchain_anthropic import ChatAnthropic
#         extractor = LLMEntityExtractor(ChatAnthropic(model="claude-sonnet-4-5-20250929"))
#         triplets = extractor.extract("Alice works at OpenAI and co-authored the GPT-4 paper.")
#         # -> [KnowledgeTriplet(subject='Alice', predicate='works_at', object='OpenAI'),
#         #     KnowledgeTriplet(subject='Alice', predicate='co_authored', object='GPT-4 Paper')]
#     """

#     _SYSTEM = (
#         "You are a knowledge-graph extraction engine. "
#         "Extract every entity and relationship from the user's text as structured triplets. "
#         "Entities include: people, organisations, locations, concepts, technologies, papers, projects. "
#         "Use snake_case predicates (works_at, cites, relates_to, authored_by, uses, part_of, etc.). "
#         "Normalise entity names to consistent title-case. "
#         "Be comprehensive — include both explicit and strongly implied relationships."
#     )

#     def __init__(self, llm: Optional[BaseChatModel] = None):
#         """
#         llm: Any LangChain chat model that supports with_structured_output.
#              Pass None to use the rule-based fallback only.
#         """
#         self._chain = (
#             llm.with_structured_output(KnowledgeTripletList) if llm else None
#         )
#         self._default_extractor = TripletExtractor()

#     # ── Public ─────────────────────────────────────────────────────────────────

#     def extract(self, text: str) -> List[KnowledgeTriplet]:
#         """Synchronous extraction. Returns a list of KnowledgeTriplet objects."""
#         if not text or not text.strip():
#             return []
#         if self._chain is None:
#             return self._fallback(text)
#         try:
#             result: KnowledgeTripletList = self._chain.invoke([
#                 SystemMessage(content=self._SYSTEM),
#                 HumanMessage(content=text),
#             ])
#             print(f"[GraphMemoryStore] Extracted {len(result.triplets)} triplets")
#             return result.triplets
#         except Exception as e:
#             print(f"[GraphMemoryStore] Structured extraction failed: {e} — using fallback")
#             return self._fallback(text)

#     async def aextract(self, text: str) -> List[KnowledgeTriplet]:
#         """Async extraction — uses ainvoke directly, no thread pool needed."""
#         if not text or not text.strip():
#             return []
#         if self._chain is None:
#             return self._fallback(text)
#         try:
#             result: KnowledgeTripletList = await self._chain.ainvoke([
#                 SystemMessage(content=self._SYSTEM),
#                 HumanMessage(content=text),
#             ])
#             print(f"[GraphMemoryStore] Extracted {len(result.triplets)} triplets")
#             return result.triplets
#         except Exception as e:
#             print(f"[GraphMemoryStore] Async extraction failed: {e} — using fallback")
#             return self._fallback(text)

#     # ── Private ────────────────────────────────────────────────────────────────

#     def _fallback(self, text: str) -> List[KnowledgeTriplet]:
#         """Uses NLP for knowledge triplet identification, without an LLM. Very basic co-occurrence extraction."""
#         return [
#             KnowledgeTriplet(subject=triplet['head'], predicate=triplet["type"], object=triplet["tail"])
#             for triplet in self._default_extractor.extract_triplets(text)
#         ]
    
