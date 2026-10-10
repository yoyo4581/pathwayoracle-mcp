"""
Evidence ledger tests, against real tool results captured from the stack
(tests/fixtures/tool_results.json; regenerate with scripts/capture_fixtures.py).
No services needed to run them.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pathwayoracle_mcp.evidence import EvidenceLedger, Ledgers, MAPPERS, ingest

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "tool_results.json").read_text(encoding="utf-8"))


def replay(fixtures=FIXTURES) -> EvidenceLedger:
    """Feed fixtures through the ledger the way dispatch does: call node, then ingest."""
    lg = EvidenceLedger("test-session", "serving-v20261006193533")
    for f in fixtures:
        if f["tool"] == "plan":
            lg.begin_question(f["arguments"].get("recipe"))
        call = lg.record_call(f["tool"], f["arguments"], ok=True, ms=1.0)
        ingest(lg, f["tool"], f["arguments"], f["result"], call)
    return lg


@pytest.fixture(scope="module")
def lg():
    return replay()


def edges(lg, src, dst):
    return [(k, d) for k, d in lg.graph.get_edge_data(src, dst, default={}).items()]


def test_every_content_tool_has_a_mapper():
    meta = {"plan", "describe_schema", "discover_tools", "execute_tool", "collection_info"}
    captured = {f["tool"] for f in FIXTURES} - meta
    assert captured <= set(MAPPERS), captured - set(MAPPERS)


def test_entities_are_keyed_by_domain_ids(lg):
    assert lg.node("P04637")["label"] == "Gene" and lg.node("P04637")["name"] == "TP53"
    assert lg.node("R-HSA-111459")["label"] == "Pathway"
    assert lg.node("GO_0018344")["label"] == "Ontology"          # GO:... normalised to GO_
    assert not any(n.startswith("GO:") for n in lg.graph.nodes)


def test_passage_text_and_its_study(lg):
    p = lg.node("qdrant:Q9NRF9#0")
    assert p["kind"] == "evidence" and "polymerase epsilon" in p["text"]
    assert edges(lg, "qdrant:Q9NRF9#0", "Q9NRF9")                  # passage describes POLE3
    cites = edges(lg, "qdrant:Q9NRF9#0", "pubmed:10801849")         # ...and cites the study
    assert cites and cites[0][1]["cite"] == "pubmed:10801849"
    assert "histone fold" in lg.node("pubmed:10801849")["text"]    # whose abstract was read


def test_one_edge_per_reaction_not_per_pair(lg):
    """The KG_Memory/ACTS_ON failure mode: never merge two reactions into one edge."""
    e = edges(lg, "Q9BVI0", "P04637")                               # PHF20 -> TP53
    assert [d["reaction_id"] for _, d in e] == ["R-HSA-5633460"]
    assert e[0][1]["sign"] == "negative" and e[0][1]["reaction"] == "MDM2 binds TP53"
    # MDM2 plays two roles on one reaction: two edges, each with its own `via`.
    mdm2 = edges(lg, "Q00987", "R-HSA-6804879")
    roles = {d["rel"]: d.get("via") for _, d in mdm2}
    assert set(roles) == {"CATALYST", "INHIBITOR"}
    assert "p14ARF" in roles["INHIBITOR"]                           # inhibition is the complex's


def test_regulators_missing_from_the_edge_layer_are_recorded(lg):
    stat3 = [n for n, d in lg.graph.nodes(data=True) if d.get("name") == "STAT3"]
    assert stat3
    assert edges(lg, stat3[0], "R-HSA-6789960")[0][1]["rel"] == "INHIBITOR"


def test_reaction_participants_by_role(lg):
    rels = {d["rel"] for u, v, k, d in lg.graph.edges(keys=True, data=True)
            if "R-HSA-5633460" in (u, v) and d["rel"] != "returned"}
    assert {"INHIBITOR", "REACTANT", "PRODUCT"} <= rels


def test_go_annotation_keeps_evidence_codes(lg):
    anns = [d for u, v, k, d in lg.graph.edges(keys=True, data=True) if u == "Q9NRF9" and v.startswith("GO_")]
    assert anns and all(d.get("evidence") for d in anns)


def test_aggregates(lg):
    kinds = {d.get("atype") for _, d in lg.graph.nodes(data=True) if d["kind"] == "aggregate"}
    assert {"go_profile", "go_group", "path", "cypher", "edge_breakdown", "reaction_layer"} <= kinds
    cy = [n for n, d in lg.graph.nodes(data=True) if d.get("atype") == "cypher"][0]
    assert lg.node(cy)["query"].startswith("MATCH")


def test_every_content_node_has_provenance(lg):
    """No node exists without the call that returned it."""
    for n, d in lg.graph.nodes(data=True):
        if d["kind"] in ("entity", "evidence", "aggregate"):
            calls = lg.returned_by(n)
            assert calls, n
            assert all(lg.node(c)["kind"] == "call" for c in calls)


def test_question_namespace_scopes_calls(lg):
    q1 = lg.in_namespace(("q", 1))
    assert "question:1" in q1 and "P04637" in q1
    assert lg.node("question:1")["recipe"] == "mechanistic"


def test_subgraph_excludes_the_call_layer(lg):
    g = lg.subgraph(["P04637", "Q9BVI0"], neighbors=1)
    assert "R-HSA-5633460" in g or "Q00987" in g
    assert not any(d["kind"] in ("call", "question") for _, d in g.nodes(data=True))


def test_stats_and_json_snapshot(lg):
    s = lg.stats()
    assert s["calls"] == len(FIXTURES) and s["questions"] == 1
    assert s["top_entities"][0]["degree"] >= s["top_entities"][-1]["degree"]
    json.dumps(lg.to_json(), default=str)                            # JSON-safe, no pickle


def test_older_result_shapes_are_tolerated():
    """Registry may lag the code: v1 gene_reactions had no by_regulator/coverage."""
    old = copy.deepcopy([f for f in FIXTURES if f["tool"] in ("gene_reactions", "gene_interactions")])
    for f in old:
        for k in ("by_regulator", "n_regulators", "coverage", "breakdown", "in_scope"):
            f["result"].pop(k, None)
    lg = replay(old)
    assert lg.has("P04637")


def test_ledgers_are_per_session():
    ls = Ledgers(max_sessions=2)
    a, b = ls.get("a"), ls.get("b")
    assert a is not b and ls.get("a") is a
    ls.get("c")                                                      # evicts the oldest (b)
    assert ls.get("b") is not b


def test_reaction_details_records_every_constituent_gene(lg):
    """All participant genes enter the ledger by UniProt id -- MDM4 was never anchored
    by another tool -- linked to their entity and straight to the reaction, with `via`."""
    rd = [f for f in FIXTURES if f["tool"] == "reaction_details"][0]
    assert lg.node("O15151")["name"] == "MDM4"
    assert any(lg.node(c)["tool"] == "reaction_details" for c in lg.returned_by("O15151"))
    assert any(d["rel"] == "in_entity" for _, d in edges(lg, "O15151", "R-HSA-6804745"))
    role = [d for _, d in edges(lg, "O15151", "R-HSA-5633460") if d["rel"] == "REACTANT"]
    assert role and role[0]["via"] == "p-S166,S188-MDM2 dimer, p-S166,S188-MDM2:MDM4"
    assert any(d["rel"] == "PRODUCT" for _, d in edges(lg, "R-HSA-5633460", "P04637"))
    members = {m["uniprot_id"] for p in rd["result"]["results"][0]["participants"] for m in p["_members"]}
    assert all(lg.has(u) for u in members)


def test_reaction_details_without_members_links_only_known_genes():
    """Older result shape (symbols only): no new genes, links to the ones already held."""
    old = copy.deepcopy([f for f in FIXTURES if f["tool"] in ("find_entities", "reaction_details")])
    for p in old[-1]["result"]["results"][0]["participants"]:
        p.pop("_members"), p.pop("n_genes")
    lg = replay(old)
    assert not lg.has("O15151")                                     # MDM4 never anchored
    assert any(d["rel"] == "in_entity" for _, d in edges(lg, "P04637", "R-HSA-3209194"))


def test_ledger_only_fields_reach_the_ledger_not_the_model():
    from contextlib import contextmanager

    from pathwayoracle_mcp.dispatch import Toolkit, strip_private
    from pathwayoracle_mcp.registry.runtime import Registry

    rd = [f for f in FIXTURES if f["tool"] == "reaction_details"][0]

    class Clients:                                                  # the stored query's rows
        def read(self, cypher, params, max_rows=None):
            return copy.deepcopy(rd["result"]["results"]), False

    traced = []

    class Capture:
        @contextmanager
        def span(self, call, carrier=None):
            yield call
            traced.append(call)

    tk = Toolkit(Clients(), Registry.from_yaml(), tracer=Capture())
    out = tk.call("reaction_details", rd["arguments"])
    assert "_members" not in json.dumps(out)                        # the model's result
    assert "_members" not in json.dumps(traced[0].result)           # and the trace span
    assert out == strip_private(tk.call("reaction_details", rd["arguments"], private=True))
    assert tk.ledger().has("O15151")                                # the ledger got the members
