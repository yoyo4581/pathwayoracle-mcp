"""
Rewrite saved chat memory files to the current episode format (docs/memory.md, "As built"):

  - drop episodes that only used memory tools (recall / recall_episode) and
    concluded nothing: they were copies of the episode they recalled
  - recompute every episode's summary, n_facts and n_claims from its own triplets

Facts are never added or changed. Each rewritten file is backed up beside it as
<session>.json.bak (memory loads only *.json, so backups are not read).

    python scripts/migrate_memory.py [memory_dir] [--dry-run]

memory_dir defaults to PATHWAYORACLE_MEMORY_DIR, else /cache/memory.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import networkx as nx

from pathwayoracle_mcp.evidence.consolidate import MEMORY_TOOLS, claims_of, is_fact, summary
from pathwayoracle_mcp.evidence.memory import memory_dir


def episode_graph(g: nx.MultiDiGraph, ep: str) -> nx.MultiDiGraph:
    eg = nx.MultiDiGraph()
    for u, v, k, d in g.edges(keys=True, data=True):
        if d.get("episode") == ep:
            for x in (u, v):
                if not eg.has_node(x):
                    eg.add_node(x, **g.nodes[x])
            eg.add_edge(u, v, key=k, **d)
    if not eg.has_node(ep):
        eg.add_node(ep, **g.nodes[ep])
    return eg


def migrate(path: Path, dry_run: bool) -> str:
    g = nx.node_link_graph(json.loads(path.read_text(encoding="utf-8")), directed=True, multigraph=True,
                           edges="edges")
    episodes = [n for n, d in g.nodes(data=True) if d.get("kind") == "episode"]
    dropped = []
    for ep in episodes:
        concluded = any(e.get("rel") == "concluded" for _, _, e in g.out_edges(ep, data=True))
        if set(g.nodes[ep].get("tools") or {}) <= MEMORY_TOOLS and not concluded:
            g.remove_edges_from([(u, v, k) for u, v, k, e in g.edges(keys=True, data=True) if e.get("episode") == ep])
            g.remove_node(ep)
            dropped.append(ep)
    g.remove_nodes_from([n for n in list(g.nodes) if g.degree(n) == 0])
    kept = [ep for ep in episodes if ep not in dropped]
    for ep in kept:
        eg = episode_graph(g, ep)
        d = g.nodes[ep]
        d["n_facts"] = sum(1 for *_, e in eg.edges(data=True) if is_fact(e))
        d["n_claims"] = len(claims_of(eg, ep))
        eg.nodes[ep].update(n_facts=d["n_facts"], n_claims=d["n_claims"])
        d["summary"] = summary(eg, ep)
    if not dry_run:
        shutil.copy2(path, path.with_suffix(".json.bak"))
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(nx.node_link_data(g, edges="edges"), default=str), encoding="utf-8")
        tmp.replace(path)
    return f"{path.name}: kept {len(kept)} episodes, dropped {len(dropped)} {dropped or ''}".rstrip()


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    args = [a for a in argv if a != "--dry-run"]
    root = Path(args[0]) if args else memory_dir()
    files = sorted(root.glob("*.json"))
    if not files:
        print(f"no memory files in {root}")
    for path in files:
        print(("[dry run] " if dry_run else "") + migrate(path, dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
