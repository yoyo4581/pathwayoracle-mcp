"""
Controlled test of the toolkit: Claude answers the questions in eval/questions.yaml
through the real MCP server, and each run is scored on what it did, not only on
what it said.

    python eval/run.py --label baseline                 # every question, 3 runs each
    python eval/run.py --label baseline --only q2_tp53_negative --runs 1

The server is launched exactly as Claude Desktop launches it -- `docker exec`
into the running `mcp` container, stdio transport -- one process per run, with
PATHWAYORACLE_SESSION set so its tool spans land in a named Langfuse session.
The server's `instructions` become the system prompt, as a desktop client
passes them to the model. Differences from Claude Desktop that remain: its own
system prompt, and whatever model/effort the user picked there.

Scores per run
  facts           fraction of expected fact groups present in the answer
  cites_valid     fraction of cite ids in the answer that some tool in THIS run
                  returned -- the provenance check: every cited piece of
                  evidence must trace to a call
  plan_first      the first tool call was plan; recipe_match: it chose the
                  expected recipe
  tool_calls      effective calls (execute_tool counts as the tool it ran)
  read_cypher     how often the model fell back to free-form Cypher
  cost_usd        from token usage at the model's list price

Outputs: eval/results/<label>.jsonl (one line per run, transcript included) and
a summary table. With LANGFUSE_* set, one scored trace per run is added to the
run's session, next to the server's own tool spans.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import statistics
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcp import StdioServerParameters          # noqa: E402
from mcp.client import Client                  # noqa: E402

from pathwayoracle_mcp.dispatch import collect_citations   # noqa: E402
from pathwayoracle_mcp.server import unwrap                # noqa: E402

MODEL = "claude-opus-5-5"
# List price per million tokens (claude-opus-5-5): input, output, cache read, cache write.
PRICE = {"in": 4.00, "out": 20.00, "cache_read": 0.20, "cache_write": 5.00}
CONTAINER = "pathwayoracle-mcp-1"
MAX_TURNS = 40
CITE = re.compile(r"\b(?:qdrant|pubmed|edge|go|node|reaction|member|profile|path|cypher):[^\s,;\]\)\[\(`*]+")


def load_env(keys=("ANTHROPIC_API_KEY", "LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")):
    """Selected keys from ./.env into os.environ (values are never printed)."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        k, sep, v = line.strip().partition("=")
        if sep and k.strip() in keys and not os.environ.get(k.strip()):
            os.environ[k.strip()] = v.strip().strip('"').strip("'")


# --- one run ---------------------------------------------------------------

async def run_question(q: dict, label: str, run_no: int, effort: str) -> dict:
    import anthropic

    session = f"eval-{label}-{q['id']}-r{run_no}-{uuid.uuid4().hex[:6]}"
    params = StdioServerParameters(command="docker", args=[
        "exec", "-i", "-e", "MCP_TRANSPORT=stdio", "-e", f"PATHWAYORACLE_SESSION={session}",
        CONTAINER, "pathwayoracle-mcp"])
    api = anthropic.AsyncAnthropic()
    calls, returned_cites, seen_text = [], set(), []
    usage = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
    started = time.time()
    stop, answer = None, ""

    async with Client(params, read_timeout_seconds=300) as mcp:
        tools = [{"name": t.name, "description": t.description or "",
                  "input_schema": getattr(t, "inputSchema", None) or getattr(t, "input_schema")}
                 for t in (await mcp.list_tools()).tools]
        system = mcp.instructions or ""
        messages = [{"role": "user", "content": q["text"]}]

        for _turn in range(MAX_TURNS):
            response = await api.beta.messages.create(
                model=MODEL, max_tokens=16000, system=system, tools=tools, messages=messages,
                output_config={"effort": effort},
                cache_control={"type": "ephemeral"},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            )
            u = response.usage
            usage["in"] += u.input_tokens or 0
            usage["out"] += u.output_tokens or 0
            usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
            usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
            # Append the full content (thinking included), unmodified: the
            # harness stays append-only.
            messages.append({"role": "assistant", "content": response.content})
            stop = response.stop_reason
            if stop != "tool_use":
                answer = "\n".join(b.text for b in response.content if b.type == "text")
                break

            results = []
            for block in (b for b in response.content if b.type == "tool_use"):
                t0 = time.perf_counter()
                res = await mcp.call_tool(block.name, block.input)
                text = "\n".join(c.text for c in (res.content or []) if getattr(c, "text", None))
                is_error = bool(getattr(res, "is_error", False) or getattr(res, "isError", False))
                cites = []
                if not is_error:
                    try:
                        cites = collect_citations(unwrap(res))
                    except Exception:                       # noqa: BLE001
                        cites = []
                returned_cites.update(cites)
                seen_text.append(text)
                effective = (block.input.get("name") if block.name == "execute_tool" else block.name)
                calls.append({"tool": effective, "via": block.name, "input": block.input,
                              "is_error": is_error, "ms": round((time.perf_counter() - t0) * 1000),
                              "result_chars": len(text), "n_cites": len(cites)})
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": text or "(empty)", "is_error": is_error})
            messages.append({"role": "user", "content": results})
        else:
            stop = "max_turns"

    return {"session": session, "question": q["id"], "run": run_no, "label": label,
            "effort": effort, "model": MODEL, "stop_reason": stop, "answer": answer,
            "calls": calls, "returned_cites": sorted(returned_cites), "usage": usage,
            "_seen_text": chr(10).join(seen_text),
            "seconds": round(time.time() - started, 1)}


# --- scoring ---------------------------------------------------------------

def score(q: dict, r: dict) -> dict:
    a = r["answer"].lower()
    groups = q.get("facts", [])
    facts = sum(any(alt.lower() in a for alt in g) for g in groups) / len(groups) if groups else None
    cited = {c.rstrip(".:") for c in CITE.findall(r["answer"])}
    valid = cited & set(r["returned_cites"])
    # Not returned as evidence, but its id occurs in some tool's output (e.g. a
    # PMID inside a snippet): cited without fetching what it points to.
    seen = r.get("_seen_text", "").lower()
    mentioned = {c for c in cited - valid if c.split(":", 1)[1].lower() in seen}
    tools = [c["tool"] for c in r["calls"]]
    plan_calls = [c for c in r["calls"] if c["tool"] == "plan"]
    u = r["usage"]
    cost = (u["in"] * PRICE["in"] + u["out"] * PRICE["out"]
            + u["cache_read"] * PRICE["cache_read"] + u["cache_write"] * PRICE["cache_write"]) / 1e6
    return {
        "facts": round(facts, 2) if facts is not None else None,
        "n_cited": len(cited),
        "cites_valid": round(len(valid) / len(cited), 2) if cited else 0.0,
        "cites_mentioned_only": round(len(mentioned) / len(cited), 2) if cited else 0.0,
        "cites_unsupported": round(len(cited - valid - mentioned) / len(cited), 2) if cited else 0.0,
        "invalid_cites": sorted(cited - valid)[:10],
        "plan_first": bool(tools) and tools[0] == "plan",
        "recipe_match": bool(plan_calls) and plan_calls[0]["input"].get("recipe") == q.get("recipe"),
        "tool_calls": len(tools),
        "read_cypher": tools.count("read_cypher"),
        "describe_schema": tools.count("describe_schema"),
        "tool_errors": sum(c["is_error"] for c in r["calls"]),
        "tool_output_chars": sum(c["result_chars"] for c in r["calls"]),
        "answer_words": len(r["answer"].split()),
        "cost_usd": round(cost, 3),
    }


# --- Langfuse: one scored trace per run, in the run's session --------------

def to_langfuse(q: dict, r: dict, s: dict) -> None:
    host = os.environ.get("LANGFUSE_HOST", "").rstrip("/")
    pk, sk = os.environ.get("LANGFUSE_PUBLIC_KEY"), os.environ.get("LANGFUSE_SECRET_KEY")
    if not (host and pk and sk):
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    provider = TracerProvider(resource=Resource.create({"service.name": "pathwayoracle-eval"}))
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(
        endpoint=f"{host}/api/public/otel/v1/traces", headers={"Authorization": f"Basic {auth}"})))
    with provider.get_tracer("eval").start_as_current_span(f"eval:{q['id']}") as span:
        span.set_attribute("langfuse.trace.name", f"eval:{q['id']}")
        span.set_attribute("langfuse.session.id", r["session"])
        span.set_attribute("langfuse.trace.tags", ["eval", r["label"]])
        span.set_attribute("langfuse.trace.input", q["text"])
        span.set_attribute("langfuse.trace.output", r["answer"][:16000])
        span.set_attribute("langfuse.trace.metadata.label", r["label"])
        span.set_attribute("langfuse.trace.metadata.tools", json.dumps([c["tool"] for c in r["calls"]]))
        trace_id = format(span.get_span_context().trace_id, "032x")
    provider.shutdown()

    for name in ("facts", "cites_valid", "tool_calls", "read_cypher", "cost_usd"):
        if s.get(name) is None:
            continue
        body = json.dumps({"traceId": trace_id, "name": name, "value": float(s[name]),
                           "dataType": "NUMERIC"}).encode()
        req = urllib.request.Request(f"{host}/api/public/scores", data=body, method="POST",
                                     headers={"Authorization": f"Basic {auth}",
                                              "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=20).read()
        except Exception as exc:                            # noqa: BLE001
            print(f"  (langfuse score {name} failed: {exc})")


# --- summary ---------------------------------------------------------------

def summarize(rows: list[dict]) -> None:
    keys = ["facts", "cites_valid", "cites_unsupported", "n_cited", "tool_calls", "read_cypher", "describe_schema",
            "answer_words", "cost_usd"]
    print(f"\n{'question':22} {'n':>2} " + " ".join(f"{k[:11]:>11}" for k in keys) + "  plan_first recipe_ok")
    by_q: dict[str, list] = {}
    for row in rows:
        by_q.setdefault(row["question"], []).append(row)
    for qid, rs in by_q.items():
        cells = []
        for k in keys:
            vals = [r["score"][k] for r in rs if r["score"][k] is not None]
            m = statistics.mean(vals) if vals else 0
            spread = f"±{(max(vals) - min(vals)) / 2:.1f}" if len(vals) > 1 else ""
            cells.append(f"{m:>7.2f}{spread:>4}" if k in ("facts", "cites_valid", "cites_unsupported", "cost_usd")
                         else f"{m:>7.1f}{spread:>4}")
        pf = sum(r["score"]["plan_first"] for r in rs)
        rm = sum(r["score"]["recipe_match"] for r in rs)
        print(f"{qid:22} {len(rs):>2} " + " ".join(cells) + f"  {pf}/{len(rs)}        {rm}/{len(rs)}")
    total = sum(r["score"]["cost_usd"] for r in rows)
    print(f"\ntotal cost ${total:.2f} over {len(rows)} runs")


def describe_error(exc: BaseException) -> str:
    """The innermost causes: anyio wraps failures in ExceptionGroups, hiding them."""
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(describe_error(e) for e in exc.exceptions)
    return f"{type(exc).__name__}: {str(exc)[:300]}"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True, help="name for this toolkit version, e.g. baseline")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--only", nargs="*", help="question ids")
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    args = ap.parse_args()
    load_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set (in the environment or .env).")
        return 1

    questions = yaml.safe_load((ROOT / "eval" / "questions.yaml").read_text(encoding="utf-8"))["questions"]
    if args.only:
        questions = [q for q in questions if q["id"] in args.only]
    out = ROOT / "eval" / "results" / f"{args.label}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    for q in questions:
        for n in range(1, args.runs + 1):
            print(f"{q['id']} run {n}/{args.runs} ...", flush=True)
            try:
                r = await run_question(q, args.label, n, args.effort)
            except Exception as exc:                        # noqa: BLE001
                print(f"  run failed: {describe_error(exc)}")
                continue
            r["score"] = score(q, r)
            r.pop("_seen_text", None)              # scoring input only; keep the jsonl small
            s = r["score"]
            print(f"  {r['stop_reason']}, {s['tool_calls']} calls ({s['read_cypher']} cypher), "
                  f"facts {s['facts']}, cites {s['n_cited']} ({s['cites_valid']:.0%} fetched, "
                  f"{s['cites_mentioned_only']:.0%} mentioned-only, {s['cites_unsupported']:.0%} unsupported), "
                  f"{s['answer_words']} words, ${s['cost_usd']:.3f}, {r['seconds']}s")
            with out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(r, default=str) + "\n")
            to_langfuse(q, r, s)
            rows.append(r)
    summarize(rows)
    print(f"results: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
