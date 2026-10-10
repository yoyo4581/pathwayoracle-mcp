"""
Turn one answered question from a live session into a README example.

    python scripts/export_example.py --session stdio-21a29ece8411 --question 1 --view v1 \
        --title "Which proteins inhibit TP53's stability or activity?" \
        --answer docs/examples/tp53-inhibitors/answer.md --out docs/examples/tp53-inhibitors

Reads what the server already wrote to the shared cache volume (through
`docker exec` into the running mcp container):

    /cache/traces/calls.jsonl            every tool call: tool, arguments, via, ms, cites
    /cache/evidence/<session>/<view>.json the evidence view show_evidence built

and writes to --out:

    trace.jsonl   the question's calls, as recorded
    panel.html    the evidence panel as a standalone page (same render as in
                  Claude Desktop; Cytoscape loads from a CDN)
    README.md     question, answer (from --answer, the text the model gave),
                  the tool calls as a table, and the panel (panel.png if present)

A question is the span from its plan() call to the next one. The answer text
is not in the trace -- copy it from the chat into --answer.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pathwayoracle_mcp.apps import standalone_page   # noqa: E402

CONTAINER = "pathwayoracle-mcp-1"


def container_read(path: str, container: str) -> str:
    return subprocess.run(["docker", "exec", container, "cat", path], check=True,
                          capture_output=True, text=True, encoding="utf-8").stdout


def question_calls(calls: list[dict], session: str, question: int) -> list[dict]:
    """The calls of the session's n-th question (1-based), plan() included."""
    mine = [c for c in calls if c.get("session") == session]
    starts = [i for i, c in enumerate(mine) if c["tool"] == "plan"]
    if not 1 <= question <= len(starts):
        raise SystemExit(f"session {session} has {len(starts)} questions; asked for {question}.")
    end = starts[question] if question < len(starts) else len(mine)
    return mine[starts[question - 1]:end]


def _args(a: dict, limit: int = 110) -> str:
    """Compact arguments for a table cell: defaults left out, long values cut."""
    if "claims" in a:                                   # show_evidence: the claims are the answer
        return f"{len(a['claims'])} claims" + (f", themes: {', '.join(sorted({c['theme'] for c in a['claims'] if c.get('theme')}))}"
                                               if any(c.get("theme") for c in a["claims"]) else "")
    s = json.dumps({k: v for k, v in a.items() if v is not None and v is not False and v != [] and v != {}},
                   ensure_ascii=False)
    s = s if len(s) <= limit else s[: limit - 1] + "…"
    return s.replace("|", "\\|")


def trace_table(calls: list[dict]) -> str:
    rows = ["| # | tool | via | arguments | ms | cites returned |", "|---|---|---|---|---|---|"]
    n = 0
    for c in calls:
        if c["tool"] == "execute_tool":                 # the wrapper; its inner call is its own row
            continue
        n += 1
        rows.append(f"| {n} | `{c['tool']}` | {c.get('via', '')} | `{_args(c.get('arguments') or {})}` "
                    f"| {c.get('ms', 0):,.0f} | {c.get('n_items', 0)} |")
    return "\n".join(rows)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--session", required=True)
    p.add_argument("--question", type=int, required=True, help="1-based, counted by plan() calls")
    p.add_argument("--view", required=True, help="view id from the show_evidence receipt, e.g. v1")
    p.add_argument("--title", required=True, help="the question as the user asked it")
    p.add_argument("--answer", required=True, help="markdown file with the model's answer")
    p.add_argument("--out", required=True)
    p.add_argument("--container", default=CONTAINER)
    a = p.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    calls = [json.loads(line) for line in container_read("/cache/traces/calls.jsonl", a.container).splitlines()
             if line.strip()]
    mine = question_calls(calls, a.session, a.question)
    view = json.loads(container_read(f"/cache/evidence/{a.session}/{a.view}.json", a.container))

    (out / "trace.jsonl").write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in mine),
                                     encoding="utf-8")
    (out / "panel.html").write_text(standalone_page(view, a.title), encoding="utf-8")

    answer = Path(a.answer).read_text(encoding="utf-8").strip()
    tools = [c for c in mine if c["tool"] != "execute_tool"]
    recipe = (mine[0].get("arguments") or {}).get("recipe")
    supported = sum(c["status"] == "supported" for c in view["claims"])
    picture = ("[![Evidence panel](panel.png)](panel.html)" if (out / "panel.png").exists()
               else "[Open the evidence panel](panel.html)")
    md = f"""# {a.title}

Recipe `{recipe}` · {len(tools)} tool calls · {sum(c.get('ms', 0) for c in tools) / 1000:.1f} s in tools ·
evidence view: {len(view['nodes'])} nodes, {len(view['edges'])} edges, {supported}/{len(view['claims'])} claims supported ·
release `{view.get('release')}`

## Answer

{answer}

## Evidence panel

{picture}

`panel.html` is the panel Claude Desktop renders inline, as a standalone page: open it
in a browser to hover claims, drag nodes and click through to the evidence text.

## Tool calls

{trace_table(mine)}

Raw records: [`trace.jsonl`](trace.jsonl).
"""
    (out / "README.md").write_text(md, encoding="utf-8")
    print(f"{len(tools)} calls, view {a.view} ({len(view['nodes'])} nodes, {len(view['edges'])} edges) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
