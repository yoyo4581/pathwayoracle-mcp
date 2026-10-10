"""
Long-term memory (docs/memory.md): exchanges, episodes, saving, compaction,
server-driven recall, and the recall tools -- through the real dispatch path,
with content tools answered from the captured fixtures.
"""
from __future__ import annotations

import copy
import json

import pytest
from test_ledger import FIXTURES

from pathwayoracle_mcp import dispatch
from pathwayoracle_mcp.dispatch import Toolkit
from pathwayoracle_mcp.evidence.consolidate import build_episode, compact, episode_id
from pathwayoracle_mcp.evidence.memory import MemoryGraph
from pathwayoracle_mcp.evidence.view import build_view, resolve
from pathwayoracle_mcp.registry.runtime import Registry
from pathwayoracle_mcp.tracing import NullTracer

BY_TOOL = {}
for f in FIXTURES:
    BY_TOOL.setdefault(f["tool"], f)


class FixtureToolkit(Toolkit):
    """Content tools answer with their captured result; meta, evidence and memory tools run for real."""

    def _run(self, spec, args):
        if spec.name in BY_TOOL and spec.name not in ("plan",):
            return copy.deepcopy(BY_TOOL[spec.name]["result"])
        return super()._run(spec, args)


def toolkit(session: str, memory: MemoryGraph | None = None) -> Toolkit:
    tk = FixtureToolkit(None, Registry.from_yaml(), tracer=NullTracer(), memory=memory or MemoryGraph())
    tk._session = session
    return tk


def run(tk, tool, args=None):
    return tk.call(tool, args if args is not None else BY_TOOL[tool]["arguments"], session=tk._session)


def mechanistic_exchange(tk):
    run(tk, "plan", {"recipe": "mechanistic"})
    run(tk, "find_entities")
    run(tk, "gene_interactions")
    run(tk, "reaction_details")
    run(tk, "read_snippets")
    return tk.call("show_evidence", {"claims": [
        {"label": "PHF20 blocks MDM2 binding TP53", "cites": ["reaction:R-HSA-5633460",
                                                             "edge:Q9BVI0-ACTS_ON->P04637@R-HSA-5633460"]},
        {"label": "POLE3 is a Pol epsilon subunit", "cites": ["qdrant:Q9NRF9#0"]}]}, session=tk._session)


# --- exchanges -----------------------------------------------------------------------------

def test_plan_and_idle_gap_open_exchanges():
    tk = toolkit("chat-a")
    run(tk, "plan", {"recipe": "mechanistic"})
    run(tk, "find_entities")
    lg = tk.ledger()
    assert lg.question == 1 and lg.node("question:1")["reason"] == "plan"
    run(tk, "find_entities")                                       # within the response: same exchange
    assert lg.question == 1
    lg.last_end -= dispatch.EXCHANGE_GAP + 1                       # the user read, then asked again
    run(tk, "read_snippets")
    assert lg.question == 2 and lg.node("question:2")["reason"] == "idle gap"
    assert lg.tokens[1] > 0 and lg.tokens[2] > 0


def test_plans_before_any_other_call_are_one_exchange():
    """Plans sent together (often in parallel) classify one question: one exchange, both recipes."""
    tk = toolkit("chat-a")
    run(tk, "plan", {"recipe": "fine_grained"})
    run(tk, "plan", {"recipe": "mechanistic"})
    lg = tk.ledger()
    assert lg.question == 1 and lg.node("question:1")["recipe"] == "fine_grained, mechanistic"
    run(tk, "find_entities")
    run(tk, "plan", {"recipe": "top_down"})                        # after real work: a new question
    assert lg.question == 2 and lg.node("question:2")["recipe"] == "top_down"
    saved = MemoryGraph()
    saved.refresh()
    assert saved.graph.nodes[episode_id("chat-a", 1)]["recipe"] == "fine_grained, mechanistic"


# --- episodes ------------------------------------------------------------------------------

def test_episode_facts_all_come_from_the_ledger():
    tk = toolkit("chat-a")
    mechanistic_exchange(tk)
    lg = tk.ledger()
    ep = build_episode(lg, 1)
    eid = episode_id("chat-a", 1)
    ledger_edges = {(u, v, d.get("rel"), d.get("cite")) for u, v, d in lg.graph.edges(data=True)}
    episode_meta = {"touched", "cited", "read", "seen", "derived", "concluded", "cites", "has_episode"}
    for u, v, k, d in ep.edges(keys=True, data=True):
        assert d["session"] == "chat-a" and d["episode"] == eid and d["release"] == lg.release
        assert k.endswith("@" + eid)                               # same fact, two episodes -> two edges
        if d["rel"] not in episode_meta:
            assert (u, v, d["rel"], d.get("cite")) in ledger_edges, (u, v, d["rel"])
    assert not any("text" in d for _, d in ep.nodes(data=True))    # no text bodies
    claims = [d for _, d in ep.nodes(data=True) if d["kind"] == "claim"]
    assert claims and all(c["authored"] == "model" for c in claims)
    status = {v: d["rel"] for u, v, d in ep.out_edges(eid, data=True) if d["rel"] in ("cited", "read", "seen")}
    assert status["qdrant:Q9NRF9#0"] == "cited"
    assert "PHF20" in ep.nodes[eid]["summary"] or "TP53" in ep.nodes[eid]["summary"]


def test_every_call_saves_the_chat():
    """A killed connection loses nothing it recorded: the file is rewritten at every call."""
    mem = MemoryGraph()
    tk = toolkit("chat-a", mem)
    run(tk, "plan", {"recipe": "mechanistic"})
    run(tk, "find_entities")
    fresh = MemoryGraph()                                          # another process, after a crash
    fresh.refresh()
    assert fresh.graph.has_node(episode_id("chat-a", 1)) and fresh.graph.has_node("P04637")


# --- compaction ----------------------------------------------------------------------------

def test_compaction_keeps_the_last_three_exchanges(monkeypatch):
    tk = toolkit("chat-a")
    lg = tk.ledger()
    monkeypatch.setattr(dispatch, "LEDGER_BUDGET", 10 ** 9)
    for _ in range(5):
        run(tk, "plan", {"recipe": "fine_grained"})
        run(tk, "read_snippets")
    assert not lg.compacted                                        # under budget: nothing
    assert compact(lg, budget=1, keep=3) == [1, 2]                 # over: all but the last 3
    assert lg.compacted == {1, 2}
    assert compact(lg, budget=1, keep=3) == []                     # the last 3 stay, even over budget
    passage = lg.node("qdrant:Q9NRF9#0")                           # read again in a kept exchange
    assert passage.get("text")


def test_views_resolve_the_same_after_compaction():
    tk = toolkit("chat-a")
    mechanistic_exchange(tk)
    lg = tk.ledger()
    cites = ["reaction:R-HSA-5633460", "qdrant:Q9NRF9#0", "edge:Q9BVI0-ACTS_ON->P04637@R-HSA-5633460"]
    before = {c: resolve(lg, c)[0] for c in cites}
    for _ in range(3):
        run(tk, "plan", {"recipe": "top_down"})
        run(tk, "pathway_genes")
    assert compact(lg, budget=0, keep=3) == [1]
    assert lg.node("qdrant:Q9NRF9#0").get("text") is None         # text dropped...
    assert {c: resolve(lg, c)[0] for c in cites} == before         # ...status unchanged
    view = build_view(lg, claims=[{"label": "x", "cites": cites}])
    assert view["claims"][0]["status"] == "supported"


# --- long-term memory and recall -------------------------------------------------------------

def test_two_chats_share_entities_and_keep_their_own_edges():
    mem = MemoryGraph()
    mechanistic_exchange(toolkit("chat-a", mem))
    mechanistic_exchange(toolkit("chat-b", mem))
    g = mem.graph
    assert g.has_node("P04637")                                    # one node, shared
    eps = {d["episode"] for _, _, d in g.in_edges("P04637", data=True) if d.get("rel") == "touched"}
    assert eps == {episode_id("chat-a", 1), episode_id("chat-b", 1)}


def test_server_driven_recall_digest_and_pointers():
    mem = MemoryGraph()
    mechanistic_exchange(toolkit("chat-a", mem))
    digest = mem.digest(exclude_session="chat-b")
    assert episode_id("chat-a", 1) in digest and "recall_episode" in digest
    assert mem.digest(exclude_session="chat-a") == ""              # a chat is not reminded of itself

    tk = toolkit("chat-b", MemoryGraph())
    run(tk, "plan", {"recipe": "mechanistic"})
    first = run(tk, "find_entities")
    assert any(episode_id("chat-a", 1) in line for line in first["memory"])
    again = run(tk, "find_entities")
    assert "memory" not in again                                   # once per entity per chat


def test_recall_by_id_and_text_and_load_an_episode():
    mem = MemoryGraph()
    mechanistic_exchange(toolkit("chat-a", mem))
    tk = toolkit("chat-b", MemoryGraph())
    by_id = tk.call("recall", {"ids": ["Q9NRF9"]}, session="chat-b")
    assert by_id["results"][0]["episode"] == episode_id("chat-a", 1)
    assert "Q9NRF9 cited" in by_id["results"][0]["matched"]
    by_text = tk.call("recall", {"query": "PHF20 MDM2"}, session="chat-b")
    assert by_text["n_results"] == 1

    ep = tk.call("recall_episode", {"episode_id": episode_id("chat-a", 1)}, session="chat-b")
    assert "_triplets" not in json.dumps(ep)                       # ledger-only
    assert ep["claims"] and all(c["claim"].endswith("(model)") for c in ep["claims"])
    lg = tk.ledger()
    assert resolve(lg, "qdrant:Q9NRF9#0")[0] == "recalled"
    view = build_view(lg, claims=[{"label": "from memory", "cites": ["qdrant:Q9NRF9#0"]}])
    assert view["claims"][0]["status"] == "supported"
    assert [n for n in view["nodes"] if n["id"] == "qdrant:Q9NRF9#0"][0]["recalled"]


def test_summary_lists_every_tool_and_counts_graph_cites():
    tk = toolkit("chat-a")
    mechanistic_exchange(tk)
    s = build_episode(tk.ledger(), 1).nodes[episode_id("chat-a", 1)]["summary"]
    for tool in ("plan", "find_entities", "gene_interactions", "reaction_details", "read_snippets", "show_evidence"):
        assert f"{tool} x1" in s, tool                             # none dropped by a top-N cut
    assert "claims cite 1 edge, 1 reaction" in s                   # graph cites count as evidence too
    entities = s.split("entities: ")[1].split(" | ")[0].split(", ")
    assert entities[:2] == ["TP53", "PHF20"] or entities[:2] == ["PHF20", "TP53"]   # what the claims rest on


def test_memory_only_exchanges_are_not_saved_or_copied():
    """recall_episode loads an earlier episode; the recalling exchange must not re-record it as its own."""
    mem = MemoryGraph()
    mechanistic_exchange(toolkit("chat-a", mem))
    tk = toolkit("chat-b", MemoryGraph())
    tk.call("recall_episode", {"episode_id": episode_id("chat-a", 1)}, session="chat-b")
    lg = tk.ledger()
    assert lg.question == 0 and lg.episodes == {0: None}           # nothing of its own
    saved = MemoryGraph()
    saved.refresh()
    assert not [n for n, d in saved.graph.nodes(data=True) if d.get("kind") == "episode" and d["session"] == "chat-b"]
    assert not (saved.root / "chat-b.json").exists()               # and no empty file for it

    run(tk, "plan", {"recipe": "mechanistic"})                     # then real work in chat-b
    run(tk, "find_entities")
    ep = build_episode(lg, 1)
    recalled_cite = "edge:Q9BVI0-ACTS_ON->P04637@R-HSA-5633460"    # chat-a's triplet, loaded by recall
    assert not [1 for *_, d in ep.edges(data=True) if d.get("cite") == recalled_cite]
    assert ep.nodes[episode_id("chat-b", 1)]["tools"] == {"plan": 1, "find_entities": 1}


def test_recall_episode_grounds_every_claim_in_triplets():
    mem = MemoryGraph()
    mechanistic_exchange(toolkit("chat-a", mem))
    tk = toolkit("chat-b", MemoryGraph())
    ep = tk.call("recall_episode", {"episode_id": episode_id("chat-a", 1)}, session="chat-b")
    phf20 = next(c for c in ep["claims"] if c["claim"].startswith("PHF20"))
    lines = " | ".join(phf20["rests_on"])
    assert "PHF20 -ACTS_ON(-)-> TP53" in lines                     # the edge triplet, with its sign
    assert "reaction:R-HSA-5633460" in lines and "INHIBITOR PHF20" in lines
    pole3 = next(c for c in ep["claims"] if c["claim"].startswith("POLE3"))
    assert pole3["rests_on"][0].startswith("qdrant:Q9NRF9#0")
    assert ep["facts"]["n"] and "in_entity" not in " ".join(ep["facts"]["not_in_claims"])

    narrowed = tk.call("recall_episode", {"episode_id": episode_id("chat-a", 1), "ids": ["Q9NRF9"]},
                       session="chat-b")
    assert [c for c in narrowed["claims"] if "rests_on" in c] == [
        c for c in ep["claims"] if c["claim"].startswith("POLE3")]  # only claims on the ids are expanded
    assert "around_ids" in narrowed["facts"]


def test_recall_by_ids_names_the_claims_resting_on_them():
    mem = MemoryGraph()
    mechanistic_exchange(toolkit("chat-a", mem))
    tk = toolkit("chat-b", MemoryGraph())
    row = tk.call("recall", {"ids": ["Q9BVI0"]}, session="chat-b")["results"][0]
    assert row["claims_on_ids"] == ["PHF20 blocks MDM2 binding TP53 (model)"]
    assert row["size"].startswith("2 claims, ")


def test_dates_are_utc_and_say_so():
    from pathwayoracle_mcp.evidence.memory import _date
    assert _date(1_000_000_000) == "2001-09-09 01:46Z"


# --- a real episode (this chat's TP53 question, 2026-10-10) ----------------------------------

def tp53_memory(tmp_path) -> MemoryGraph:
    import gzip
    from pathlib import Path
    src = Path(__file__).parent / "fixtures" / "memory_tp53_episode.json.gz"
    root = tmp_path / "real"
    root.mkdir()
    (root / "stdio-79c22aeed7e5.json").write_text(gzip.open(src, "rt", encoding="utf-8").read(), encoding="utf-8")
    return MemoryGraph(root)


def test_real_episode_claims_rest_on_the_triplets_they_cited(tmp_path):
    from pathwayoracle_mcp.evidence.consolidate import rank_entities, summary
    mem = tp53_memory(tmp_path)
    eid = "episode:stdio-79c22aeed7e5:2"
    tk = toolkit("chat-b", mem)
    ep = tk.call("recall_episode", {"episode_id": eid}, session="chat-b")
    assert len(ep["claims"]) == 9 and all(c.get("rests_on") for c in ep["claims"])
    grounding = " | ".join(line for c in ep["claims"] for line in c["rests_on"])
    eg = mem.episode_subgraph(eid)
    cited_edges = {d["cite"] for *_, d in eg.edges(data=True) if d.get("rel") == "cites" and d["cite"].startswith("edge:")}
    assert len(cited_edges) == 11 and all(c in grounding for c in cited_edges)   # every cited edge, as a triplet
    # A complex's role is the complex's, not each member's.
    assert "INHIBITOR p14ARF:p-S166,S188-MDM2 dimer" in grounding and "[CDKN2A, MDM2, MDM4]" in grounding
    assert "CATALYST p-S1981,Ac-K3016-ATM [ATM]" in grounding
    assert len(json.dumps(ep)) // 4 < 3000                         # claims + grouped rest fit a small budget
    # Ranking: what the claims rest on, not machinery that joined the reactions.
    top = [eg.nodes[x]["name"] for x in rank_entities(eg, eid)[:8]]
    assert top[:2] == ["TP53", "MDM2"] and not {"UBB", "UBC", "RPS27A", "UBA52"} & set(top)
    # recall by [MDM2, TP53]: every claim rests on TP53; the ones on MDM2 come first.
    on = mem.claims_on(eid, {"Q00987", "P04637"})
    assert on[0].startswith("MDM2/MDM4 ubiquitinate TP53") and on[1].startswith("USP7/USP10")
    s = summary(eg, eid)
    assert "gene_pathways x1" in s and "claims cite 11 edge, 11 reaction" in s


def test_recall_episode_rejects_unknown_ids():
    tk = toolkit("chat-b")
    with pytest.raises(LookupError):
        tk.call("recall_episode", {"episode_id": "episode:nope:1"}, session="chat-b")


def test_session_files_cannot_escape_the_memory_dir():
    mem = MemoryGraph()
    with pytest.raises(ValueError):
        mem._path("../evil")
    mem.save_session("../evil", [])                                # best effort: logged, not written
    assert not (mem.root.parent / "evil.json").exists()
