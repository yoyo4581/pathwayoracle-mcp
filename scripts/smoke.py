"""
End-to-end check against a RUNNING stack, through a real MCP transport.

Unlike the backend's smoke_test.py (toy corpus, stand-in encoders, in-memory),
this exercises the thing users get: the published data, the real encoder, and
the wire protocol. It is slow on a cold container -- the first tool call loads
the encoder -- and that latency is reported rather than hidden.

    # HTTP, against `docker compose up`:
    python scripts/smoke.py http://localhost:8000/mcp

    # stdio, the way Claude Desktop launches it:
    python scripts/smoke.py --stdio docker compose run --rm -i -T -e MCP_TRANSPORT=stdio mcp
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

from mcp.client import Client

from pathwayoracle_mcp.server import is_error, unwrap

# What the pinned release contains. A mismatch means the stack loaded something
# other than what compose.yaml pins.
EXPECTED = {"function_records": 10204, "function_chunks": 42714}
# Listed at startup (registry `core: true`); everything else via execute_tool.
TOOLS = {"plan", "find_entities", "search_proteins", "describe_node", "expand", "read_cypher",
         "read_snippets", "describe_schema", "discover_tools", "execute_tool"}

# Phrased as a function, not an identifier -- the encoder is trained on GO term
# -> function text, and this is the query shape it is good at.
QUERY = "DNA-directed DNA polymerase activity during replication"

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILURES.append(label)


async def call(client, tool: str, args: dict, listed: bool = True):
    """Listed tools are called directly; the rest the way a model must: execute_tool."""
    t = time.perf_counter()
    if listed:
        res = await client.call_tool(tool, args)
    else:
        res = await client.call_tool("execute_tool", {"name": tool, "arguments": args})
    return res, time.perf_counter() - t


async def main(target) -> int:
    async with Client(target, read_timeout_seconds=300) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        check("the core tools are listed", TOOLS <= names,
              "missing: " + ", ".join(sorted(TOOLS - names)) if TOOLS - names
              else "extras: " + (", ".join(sorted(names - TOOLS)) or "none"))

        res, cold = await call(client, "collection_info", {}, listed=False)
        info = unwrap(res)
        print(f"  first call (no encoder needed): {cold:.1f}s")
        for name, n in EXPECTED.items():
            got = info.get(name, {}).get("points")
            check(f"{name} has {n} points", got == n, str(got))

        res, t = await call(client, "search_proteins", {"query": QUERY, "k": 5})
        out = unwrap(res)
        rows = out.get("results", [])
        print(f"  search_proteins (loads the encoder on a cold server): {t * 1000:.0f} ms")
        check("search_proteins returns 5", len(rows) == 5, str(len(rows)))
        check("every hit carries a snippet", all(r["snippets"] for r in rows))
        for r in rows:
            print(f"      {r['uniprot_id']:<12} {r['snippets'][0]['text'][:90]}")
        check("top hits are about DNA polymerases",
              sum("polymerase" in r["snippets"][0]["text"].lower() for r in rows[:3]) >= 2)

        top = rows[0]
        acc, idx = top["uniprot_id"], top["snippets"][0]["chunk_idx"]

        res, _ = await call(client, "read_snippets", {"uniprot_id": acc, "query": "proofreading", "n": 2})
        check("read_snippets returns windows", 1 <= len(unwrap(res)["snippets"]) <= 2)

        res, _ = await call(client, "expand_snippet", {"uniprot_id": acc, "chunk_idx": idx, "radius": 2},
                            listed=False)
        exp = unwrap(res)
        check("expand_snippet widens the window", len(exp["text"]) >= len(top["snippets"][0]["text"]),
              f"sentences {exp['sentences']} of {exp['of']}")

        res, _ = await call(client, "read_record", {"uniprot_id": acc}, listed=False)
        rec = unwrap(res)
        check("read_record returns the full text", exp["text"] in rec["text"] or rec["chars"] >= exp["chars"])

        res, _ = await call(client, "read_record", {"uniprot_id": "NOT_AN_ACCESSION"}, listed=False)
        check("unknown accession is an error result, not a crash", is_error(res))

        # --- the graph, chaining, provenance ------------------------------
        check("snippets carry citation ids", top["snippets"][0].get("cite", "").startswith("qdrant:"),
              top["snippets"][0].get("cite"))
        check("results carry the KG release", bool(out.get("release")), str(out.get("release")))

        res, _ = await call(client, "find_entities", {"text": "TP53", "labels": ["Gene"], "limit": 1})
        gene = unwrap(res)["results"][0]
        check("find_entities resolves a symbol", gene["id"] == "P04637", gene["id"])

        res, _ = await call(client, "discover_tools", {"have": "uniprot", "want": "reactome_pathway"})
        found = [t["name"] for t in unwrap(res)["tools"]]
        check("discover_tools chains uniprot -> pathway", "gene_pathways" in found, ", ".join(found))

        res, _ = await call(client, "gene_pathways", {"uniprot_ids": [gene["id"]], "limit": 3}, listed=False)
        pw = unwrap(res)["results"][0]
        res, _ = await call(client, "pathway_genes", {"pathway_id": pw["pathway_id"]}, listed=False)
        members = {g["uniprot_id"] for g in unwrap(res)["genes"]}
        check("pathway round trip: TP53 is in its own pathway's genes", gene["id"] in members,
              f"{pw['pathway_id']}: {len(members)} genes")

        res, _ = await call(client, "read_cypher", {"query": "CREATE (n:Probe) RETURN n"})
        check("read_cypher refuses writes", is_error(res))

        res, _ = await call(client, "execute_tool", {"name": "pathway_genes", "arguments": {"bogus": 1}})
        check("execute_tool validates arguments", is_error(res))

        res, _ = await call(client, "resolve_citations", {"citations": [top["snippets"][0]["cite"]]},
                            listed=False)
        check("a citation resolves back to its evidence",
              unwrap(res)["results"][0].get("text", "") != "")

        # --- literature: protein -> the studies behind it -------------------
        res, _ = await call(client, "cited_pmids", {"uniprot_ids": ["P56282"]}, listed=False)
        pmids = [c["pmid"] for c in unwrap(res)["results"][0]["citations"]]
        check("cited_pmids finds the studies a function text cites", bool(pmids), ", ".join(pmids))
        res, _ = await call(client, "read_abstracts", {"pmids": pmids[:2]}, listed=False)
        text = (getattr(res, "content", None) or [None])[0]
        if is_error(res) and "not configured" in str(getattr(text, "text", "")):
            print("  SKIP  PubMed abstracts: ZILLIS_URI / ZILLIS_TOKEN not set on the server")
        else:
            got = unwrap(res)["results"] if not is_error(res) else []
            check("read_abstracts returns the cited abstract", bool(got) and got[0]["found"],
                  got[0]["text"][:60] if got and got[0]["found"] else "")

    print(f"\n{'FAILED: ' + ', '.join(FAILURES) if FAILURES else 'All checks passed.'}")
    return 1 if FAILURES else 0


def parse(argv: list[str]):
    if not argv:
        return "http://localhost:8000/mcp"
    if argv[0] == "--stdio":
        import os

        from mcp import StdioServerParameters
        # The SDK hands a stdio child only a minimal safe environment by
        # default; forward ours so QDRANT_URL and friends reach the server.
        return StdioServerParameters(command=argv[1], args=argv[2:], env=dict(os.environ))
    return argv[0]


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse(sys.argv[1:]))))
