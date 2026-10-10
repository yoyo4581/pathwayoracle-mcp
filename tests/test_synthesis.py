"""Synthesis: hierarchical GO grouping (pure algorithm), synthesis tools in the
ledger, and themes in evidence views."""
from __future__ import annotations

import json
import math
from pathlib import Path

from test_ledger import FIXTURES, replay

from pathwayoracle_mcp.evidence.view import build_view, receipt
from pathwayoracle_mcp.handlers.graph import build_hierarchy

N = 10_000


def term(go_id, genes, background):
    return {"go_id": go_id, "term": go_id, "genes": set(genes), "background": background,
            "ic": -math.log(background / N)}


# A small ontology:            root-ish (broad)
#                           /                \
#              ligases {A,B,C}          methylases {D,E}
# Gene F is annotated only to "broad". Gene G shares nothing.
TERMS = {
    "ligase": term("ligase", "ABC", 50),
    "methylase": term("methylase", "DE", 40),
    "broad": term("broad", "ABCDEF", 2000),
    "vague": term("vague", "ABCDEFG", 4900),        # covers everything, says little
}
PARENTS = {"ligase": {"broad", "vague"}, "methylase": {"broad", "vague"}, "broad": {"vague"}}
GENES = set("ABCDEFG")


def test_level_one_prefers_specific_terms_over_vague_ones():
    groups, ungrouped = build_hierarchy(TERMS, PARENTS, GENES, levels=1)
    assert {g["go_id"] for g in groups} == {"ligase", "methylase"}
    assert ungrouped == {"F", "G"}


def test_higher_level_joins_groups_and_absorbs_ungrouped_genes():
    groups, ungrouped = build_hierarchy(TERMS, PARENTS, GENES, levels=2)
    top = [g for g in groups if g["level"] == 2]
    assert len(top) == 1 and top[0]["go_id"] == "broad"         # most informative shared ancestor
    assert set(top[0]["children"]) == {"ligase", "methylase"}
    assert top[0]["absorbed"] == {"F"}                          # climbing grouped one more gene
    assert ungrouped == {"G"}
    assert all(g["parent"] == "broad" for g in groups if g["level"] == 1)


def test_levels_stop_when_nothing_joins():
    groups, _ = build_hierarchy(TERMS, PARENTS, GENES, levels=3)
    assert max(g["level"] for g in groups) == 2                 # only one group left at level 2


def test_min_genes_keeps_singletons_out():
    lone = {"solo": term("solo", "A", 10)}
    groups, ungrouped = build_hierarchy(lone, {}, {"A", "B"}, levels=2)
    assert groups == [] and ungrouped == {"A", "B"}


def test_live_capture_of_the_14_inhibitors():
    """The real KG output for the 2026-10-09 TP53 answer (tests/fixtures)."""
    co = [f for f in FIXTURES if f["tool"] == "common_ontology"][0]["result"]
    assert co["excluded"] == ["p53"]
    assert not any("p53" in g["term"].lower() for g in co["groups"])
    level2 = [g for g in co["groups"] if g["level"] == 2]
    assert level2 and level2[0].get("absorbed")                 # higher level grouped more genes
    assert {g["go_id"] for g in co["groups"] if g.get("parent")} <= {g["go_id"] for g in co["groups"]}


def test_synthesis_results_land_in_the_ledger():
    lg = replay()
    groups = [n for n, d in lg.graph.nodes(data=True) if d.get("atype") == "go_group"]
    within = [(u, v) for u, v, k, d in lg.graph.edges(keys=True, data=True) if d.get("rel") == "within"]
    assert groups and within                                    # the hierarchy is recorded
    mach = [n for n, d in lg.graph.nodes(data=True) if d.get("atype") == "shared_machinery"]
    assert mach
    assert any(d.get("rel") == "shares" for _, _, _, d in lg.graph.edges(keys=True, data=True))


def test_themes_cluster_claims_and_their_evidence():
    lg = replay()
    view = build_view(lg, claims=[
        {"label": "PHF20 protects TP53", "cites": ["reaction:R-HSA-5633460"], "theme": "protection"},
        {"label": "STAT3 represses TP53", "cites": ["reaction:R-HSA-6789960"], "theme": "repression"},
        {"label": "shared", "cites": ["P04637"]},
    ])
    assert [t["name"] for t in view["themes"]] == ["protection", "repression"]
    by_id = {n["id"]: n for n in view["nodes"]}
    assert by_id["R-HSA-5633460"].get("theme") == "protection"
    assert by_id["R-HSA-6789960"].get("theme") == "repression"
    assert "theme" not in by_id["P04637"]                       # evidence without one theme stays outside
    view["view_id"] = "v1"
    assert "2 themes" in receipt(view)
    json.dumps(view)


# --- theme provenance: database grouping vs the model's own ------------------------

CLAIMS = [
    {"label": "MDM2, RNF34, RFFL ubiquitinate TP53", "cites": ["reaction:R-HSA-5633460"], "theme": "Ubiquitin ligases"},
    {"label": "STAT3 represses TP53", "cites": ["reaction:R-HSA-6789960"], "theme": "Repression"},
]


def test_a_theme_citing_a_retrieved_grouping_is_from_the_database():
    view = build_view(replay(), claims=CLAIMS,
                      themes=[{"name": "Ubiquitin ligases", "cites": ["profile:set~GO_0061630"]}])
    t = {x["name"]: x for x in view["themes"]}
    assert t["Ubiquitin ligases"]["source"] == "database"
    b = t["Ubiquitin ligases"]["basis"][0]
    assert b["name"] == "ubiquitin protein ligase activity" and b["returned_by"] == ["common_ontology"]
    assert t["Repression"]["source"] == "model" and t["Repression"]["basis"] == []   # never declared
    view["view_id"] = "v1"
    assert "2 themes (1 from database)" in receipt(view)


def test_pathways_and_machinery_rows_count_as_database_groupings():
    themes = [{"name": "Ubiquitin ligases", "cites": ["node:Pathway:R-HSA-111459"]},
              {"name": "Repression", "cites": ["profile:machinery~Q00987,O15151"]}]
    view = build_view(replay(), claims=CLAIMS, themes=themes)
    assert {t["source"] for t in view["themes"]} == {"database"}


def test_the_model_cannot_vouch_for_its_own_theme():
    """Cites that are not retrieved groupings leave the theme marked as the model's, and say why."""
    themes = [{"name": "Ubiquitin ligases", "cites": ["qdrant:Q9NRF9#0"]},           # evidence, not a grouping
              {"name": "Repression", "cites": ["profile:set~GO_9999999"]}]          # never retrieved
    view = build_view(replay(), claims=CLAIMS, themes=themes)
    t = {x["name"]: x for x in view["themes"]}
    assert t["Ubiquitin ligases"]["source"] == "model" and t["Ubiquitin ligases"]["basis"][0]["status"] == "not a grouping"
    assert t["Repression"]["source"] == "model" and t["Repression"]["basis"][0]["status"] == "unknown"
    view["view_id"] = "v1"
    line = receipt(view)
    assert "0 from database" in line and "qdrant:Q9NRF9#0 not a grouping" in line
