"""Phase 3/4 server side: provenance trails, group expansion data, Mermaid
fallback, view persistence, and the span attachment."""
from __future__ import annotations

from test_ledger import replay

from pathwayoracle_mcp.evidence import store
from pathwayoracle_mcp.evidence.view import build_view, to_mermaid

CLAIMS = [{"label": "PHF20 protects TP53", "cites": ["edge:Q9BVI0-ACTS_ON->P04637@R-HSA-5633460",
                                                       "reaction:R-HSA-5633460"]},
          {"label": "POLE3 in Pol ε", "cites": ["qdrant:Q9NRF9#0", "pubmed:14759371"]}]


def test_provenance_trail_on_every_element():
    view = build_view(replay(), claims=CLAIMS, neighbors=1)
    for n in view["nodes"]:
        if n["kind"] != "group":
            assert n["returned_by"], n["id"]                    # which tool calls produced it
    edge = [e for e in view["edges"] if e.get("cite", "").startswith("edge:Q9BVI0")][0]
    assert edge["returned_by"] == ["gene_interactions"]
    passage = [n for n in view["nodes"] if n["id"] == "qdrant:Q9NRF9#0"][0]
    assert passage["status"] == "fetched"
    unread = [n for n in view["nodes"] if n["id"] == "pubmed:14759371"][0]
    assert unread["status"] == "referenced"                     # drawn dashed: seen, never read


def test_groups_carry_their_members_and_edges_for_expansion():
    view = build_view(replay(), scope="session", max_nodes=8)
    groups = [n for n in view["nodes"] if n["kind"] == "group"]
    assert groups
    for g in groups:
        members = view["group_members"][g["id"]]
        assert {m["id"] for m in members} == set(g["members"])
        assert all(m["group"] == g["id"] for m in members)
    member_ids = {m for g in groups for m in g["members"]}
    assert any(e["source"] in member_ids or e["target"] in member_ids for e in view["group_edges"])


def test_mermaid_fallback_is_compact_and_valid_looking():
    view = build_view(replay(), claims=CLAIMS, scope="session", max_nodes=150)
    text = to_mermaid(view, max_nodes=25)
    lines = text.splitlines()
    assert lines[0] == "graph LR"
    node_lines = [ln for ln in lines if ln.strip().startswith("n") and ("[" in ln or "(" in ln or "{" in ln)
                  and "-->" not in ln and "==>" not in ln]
    assert len(node_lines) <= 25
    assert "+" in text and "more in the full view" in text      # says what was left out
    assert "==>" in text                                        # cited edges are emphasised
    for ln in node_lines:                                       # labels never break the syntax
        label = ln.split('"')[1]
        assert not set(label) & set('"[]{}()<>|#;`')
    assert len(text) < 4000                                     # bounded cost for non-Apps clients


def test_view_persistence_round_trip_and_path_safety(tmp_path, monkeypatch):
    monkeypatch.setenv("PATHWAYORACLE_EVIDENCE_DIR", str(tmp_path))
    view = {"view_id": "v1", "nodes": [], "edges": []}
    assert store.save("stdio-abc123", "v1", view)
    assert store.load("stdio-abc123", "v1") == view
    assert store.load("stdio-abc123", "v2") is None
    assert store.load("../etc", "passwd") is None               # traversal rejected
    assert store.save("..", "v1", view) is None
    monkeypatch.setenv("PATHWAYORACLE_PUBLIC_URL", "http://localhost:8010/")
    assert store.public_url("s1", "v1") == "http://localhost:8010/evidence/s1/v1"


def test_show_evidence_attaches_the_view_to_its_span(tmp_path, monkeypatch):
    """The full view rides on the trace span (metadata), not in the model's result."""
    monkeypatch.setenv("PATHWAYORACLE_EVIDENCE_DIR", str(tmp_path))
    from contextlib import contextmanager

    from pathwayoracle_mcp.dispatch import Toolkit
    from pathwayoracle_mcp.registry.runtime import Registry

    seen = []

    class Capture:
        @contextmanager
        def span(self, call, carrier=None):
            yield call
            seen.append(call)

    tk = Toolkit(clients=None, registry=Registry.from_yaml(), tracer=Capture())
    lg = tk.ledger()
    for k, v in replay().graph.nodes(data=True):                # reuse the replayed evidence
        lg.graph.add_node(k, **v)
    for u, v, k, d in replay().graph.edges(keys=True, data=True):
        lg.graph.add_edge(u, v, key=k, **d)
        if d.get("cite"):
            lg._edges_by_cite[d["cite"]].add((u, v, k))
    result = tk.call("show_evidence", {"claims": CLAIMS})
    assert set(result) == {"view_id", "receipt", "release"}     # small: what the model sees
    call = [c for c in seen if c.tool == "show_evidence"][0]
    assert call.attachments["evidence_view"]["nodes"]           # the full view, for Langfuse
    assert store.load(lg.session, result["view_id"])            # and on disk, for the page


def test_reaction_participants_link_to_session_genes():
    """PHF20 the Reactome protein entity and PHF20 the gene are one thing at two levels."""
    lg = replay()
    links = {(u, v) for u, v, k, d in lg.graph.edges(keys=True, data=True) if d.get("rel") == "in_entity"}
    phf20_entities = [v for u, v in links if u == "Q9BVI0"]
    assert phf20_entities and lg.node(phf20_entities[0])["name"] == "PHF20"
    assert all(lg.node(u)["label"] == "Gene" for u, _ in links)
