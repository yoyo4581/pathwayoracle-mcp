"""show_evidence view building: provenance statuses, selection, cap, and the size
of what the model gets back."""
from __future__ import annotations

from test_ledger import replay

from pathwayoracle_mcp.evidence.view import build_view, receipt, resolve


def lg():
    return replay()


def test_cite_statuses():
    g = lg()
    assert resolve(g, "qdrant:Q9NRF9#0")[0] == "fetched"                       # passage text held
    assert resolve(g, "pubmed:10801849")[0] == "fetched"                       # abstract was read
    assert resolve(g, "pubmed:14759371")[0] == "referenced"                    # seen cited, never read
    assert resolve(g, "pubmed:99999999")[0] == "unknown"                       # never retrieved
    status, nodes, edges = resolve(g, "edge:Q9BVI0-ACTS_ON->P04637@R-HSA-5633460")
    assert status == "fetched" and set(nodes) == {"Q9BVI0", "P04637"} and len(edges) == 1
    assert resolve(g, "reaction:R-HSA-5633460")[1] == ["R-HSA-5633460"]
    assert resolve(g, "GO:0018344")[1] == ["GO_0018344"]
    assert resolve(g, "call:1")[0] == "unknown"                                # the call layer is not evidence


def test_claim_status_and_receipt():
    g = lg()
    view = build_view(g, claims=[
        {"label": "PHF20 protects TP53", "cites": ["reaction:R-HSA-5633460", "edge:Q9BVI0-ACTS_ON->P04637@R-HSA-5633460"]},
        {"label": "POLE3 is a Pol ε subunit", "cites": ["qdrant:Q9NRF9#0", "pubmed:14759371"]},
        {"label": "made up", "cites": ["pubmed:99999999"]},
    ])
    assert [c["status"] for c in view["claims"]] == ["supported", "partial", "unsupported"]
    assert view["unknown"] == ["pubmed:99999999"]
    assert "pubmed:99999999" not in {n["id"] for n in view["nodes"]}           # never drawn
    view["view_id"] = "v1"
    line = receipt(view)
    assert "1 supported" in line and "1 partial" in line and "1 unsupported" in line
    assert "pubmed:14759371 referenced" in line
    assert len(line.split()) < 60                                               # one short line


def test_cited_elements_are_marked_and_linked_to_claims():
    g = lg()
    view = build_view(g, claims=[{"label": "c", "cites": ["edge:Q9BVI0-ACTS_ON->P04637@R-HSA-5633460"]}])
    edge = [e for e in view["edges"] if e.get("cite") == "edge:Q9BVI0-ACTS_ON->P04637@R-HSA-5633460"][0]
    assert edge["cited"] and edge["claims"] == [0] and edge["sign"] == "negative"
    assert {n["id"] for n in view["nodes"] if n["cited"]} == {"Q9BVI0", "P04637"}


def test_no_call_or_question_nodes_and_no_structural_edges():
    view = build_view(lg(), scope="session", max_nodes=500)
    assert not any(n["kind"] in ("call", "question") for n in view["nodes"])
    assert not any(e.get("rel") in ("returned", "asked") for e in view["edges"])


def test_cap_folds_overflow_into_groups_keeping_cited_nodes():
    g = lg()
    view = build_view(g, claims=[{"label": "c", "cites": ["qdrant:Q9NRF9#0"]}], scope="session", max_nodes=10)
    ids = {n["id"] for n in view["nodes"]}
    assert "qdrant:Q9NRF9#0" in ids                                             # cited survives the cap
    plain = [n for n in view["nodes"] if n["kind"] != "group"]
    assert len(plain) <= 10 and view["overflow"] > 0
    groups = [n for n in view["nodes"] if n["kind"] == "group"]
    assert groups and all(n["members"] for n in groups)


def test_explicit_collapse():
    g = lg()
    caspases = ["P42574", "P55210"]                                              # CASP3, CASP7
    view = build_view(g, include=caspases + ["P55211"], collapse=[{"label": "executioner caspases", "ids": caspases}])
    grp = [n for n in view["nodes"] if n["kind"] == "group"]
    assert grp and set(grp[0]["members"]) == set(caspases)
    assert not ({"P42574", "P55210"} & {n["id"] for n in view["nodes"]})


def test_question_scope_needs_no_ids():
    g = lg()
    view = build_view(g, scope="question", max_nodes=500)
    assert len(view["nodes"]) > 20


def test_cited_reactions_and_passages_are_drawn_connected():
    """Passages and reactions only meet through genes: citing them brings the genes,
    so the view has edges (it had none: 2026-10-09, TP53 inhibitors, view v1)."""
    view = build_view(lg(), claims=[
        {"label": "PHF20 blocks MDM2 binding TP53", "cites": ["reaction:R-HSA-5633460"], "theme": "protection"},
        {"label": "POLE3 is a Pol ε subunit", "cites": ["qdrant:Q9NRF9#0"], "theme": "replication"},
    ])
    by_id = {n["id"]: n for n in view["nodes"]}
    assert {"Q9BVI0", "Q00987", "O15151", "P04637", "Q9NRF9"} <= set(by_id)
    assert not any(by_id[n]["cited"] for n in ("Q9BVI0", "Q9NRF9"))   # brought, not cited
    pairs = {(e["source"], e["target"], e["rel"]) for e in view["edges"]}
    assert ("Q9BVI0", "R-HSA-5633460", "INHIBITOR") in pairs
    assert ("qdrant:Q9NRF9#0", "Q9NRF9", "describes") in pairs
    assert by_id["O15151"]["theme"] == "protection" and by_id["Q9NRF9"]["theme"] == "replication"


def test_brought_genes_rank_below_cited_nodes_under_the_cap():
    view = build_view(lg(), claims=[{"label": "c", "cites": ["reaction:R-HSA-5633460", "qdrant:Q9NRF9#0"]}],
                      max_nodes=5)
    shown = {n["id"] for n in view["nodes"] if n["kind"] != "group"}
    assert {"R-HSA-5633460", "qdrant:Q9NRF9#0", "Q9NRF9"} <= shown       # cited, then the passage's gene
    assert view["overflow"] > 0                                         # participants fold into a group


def test_specific_actors_outrank_complex_members_under_the_cap():
    """PHF20 and TP53 take part on their own; MDM2/MDM4 through a shared complex."""
    view = build_view(lg(), claims=[{"label": "c", "cites": ["reaction:R-HSA-5633460"]}], max_nodes=3)
    shown = {n["id"] for n in view["nodes"] if n["kind"] != "group"}
    assert shown == {"R-HSA-5633460", "Q9BVI0", "P04637"}
    folded = {m for n in view["nodes"] if n["kind"] == "group" for m in n["members"]}
    assert {"Q00987", "O15151"} <= folded
