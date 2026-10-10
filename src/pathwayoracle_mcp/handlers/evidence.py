"""
The evidence tools the model calls. Both are built to cost the model as few
tokens as possible: the model sends ids and short labels, and gets back one
line (show_evidence) or a short index (evidence_index). The full view goes to
the panel through the app-only evidence_view_data tool, never into context.
"""
from __future__ import annotations

from ..dispatch import ToolkitError
from ..evidence import store
from ..evidence.view import build_view, receipt


def show_evidence(tk, claims: list[dict], include: list[str] | None, scope: str,
                  neighbors: int, collapse: list[dict] | None, max_nodes: int,
                  themes: list[dict] | None = None) -> dict:
    if scope not in ("claims", "question", "session"):
        raise ToolkitError("scope must be claims, question or session.")
    if not claims and not include and scope == "claims":
        raise ToolkitError("Nothing to show: give claims with cites, include ids, or scope='question'.")
    lg = tk.ledger()
    view = build_view(lg, claims, include, scope, neighbors, collapse, max(5, min(max_nodes, 150)),
                      themes=themes)
    vid = lg.save_view(view)
    view = lg.views[vid]
    # Persist for the standalone page (served by the HTTP service, another
    # process) and attach to the trace span as metadata -- both off-context.
    store.save(lg.session, vid, view)
    tk.attach("evidence_view", view)
    # Small on purpose: this result is what lands in the model's context.
    return {"view_id": vid, "receipt": receipt(view)}


def evidence_index(tk, label: str | None, question_only: bool, skip: int, limit: int) -> dict:
    lg = tk.ledger()
    s = lg.stats(top=10)
    out = {
        "session": s["session"], "question": s["questions"], "calls": s["calls"],
        "episodes_saved": sum(e is not None for e in lg.episodes.values()), "exchanges_compacted": sorted(lg.compacted),
        "counts": {**s["nodes"], **{f"entity:{k}": v for k, v in s["entities_by_label"].items()}},
        # "id name (label, degree)" strings: a fraction of the tokens of nested objects
        "most_connected": [f"{e['id']} {e['name'] or ''} ({e['label']}, {e['degree']})".replace("  ", " ")
                           for e in s["top_entities"]],
    }
    if label:
        ids = lg.in_namespace(("q", lg.question)) if question_only and lg.question else set(lg.graph.nodes)
        rows = sorted(n for n in ids if (lg.node(n) or {}).get("label") == label
                      or (lg.node(n) or {}).get("etype") == label or (lg.node(n) or {}).get("atype") == label)
        out["ids"] = [f"{n} {lg.node(n).get('name') or ''}".strip() for n in rows[skip: skip + limit]]
        out["total"] = len(rows)
    return out
