# PathwayOracle services layer — handoff notes

You are starting a new repo. These notes carry context from the backend repo
(`POKnowledgeBase_ETL`) that you cannot derive from the code you will be given,
plus measurements already taken so you do not spend a session re-taking them.

Provisional repo name: **`pathwayoracle-mcp`**. Lowercase/hyphen because it
becomes a Docker image and possibly a PyPI package; `mcp` in the name because
that is how people find MCP servers.

---

## 1. Where this sits

Three layers, three repos, three lifecycles:

| layer | repo | state | audience |
|---|---|---|---|
| data | `POKnowledgeBase_ETL` (public) | built, shipping | anyone who wants the databases |
| **services** | **this one** (public, free) | **to build** | Claude Desktop users, free tier |
| agents | `DeepAgents` (private, paid) | separate | paying users |

The backend builds and publishes a Neo4j graph and a Qdrant vector store. This
layer exposes MCP tools over them. The agent layer orchestrates those tools.

**The services layer is FREE and PUBLIC.** That is a business decision, not an
oversight: free users hook Claude Desktop to this stack, and the paid product is
the orchestration above it (pathway analysis, raw expression data). Do not put
proprietary logic here, and do not make this repo private — a `docker login`
between curiosity and a working stack is the main thing that would kill adoption.

## 2. What the backend already publishes

- **Snapshot releases**, tagged `serving-v<YYYYMMDDHHMMSS>`, with assets under
  *stable* names so `https://github.com/yoyo4581/POKnowledgeBase_ETL/releases/latest/download/<name>`
  resolves: `function_records.snapshot`, `function_chunks.snapshot`, `neo4j.dump`,
  `SHA256SUMS`.
- **The encoder on Hugging Face**: `yoyo458/BioBERT_GOUniProt_FineTuned`,
  Apache-2.0, a fine-tune of `dmis-lab/biobert-base-cased-v1.2`. Public, no token
  needed to read. `BIOBERT_MODEL_PATH` is passed straight to
  `SentenceTransformer(...)`, so a Hub repo id works with no code change.
- **`compose.serve.yaml`** in the backend repo: databases only, auto-fetching from
  the latest release. Verified end to end. Keep it — it serves people who want the
  data and not the toolkit. Your compose file will be a superset.

Current contents: **10,204 protein records / 42,714 chunks**, dim 768,
window 2 / stride 1. Graph: **122,347 nodes / 602,142 relationships**.
9,727 of 10,204 records carry an `entrez_id`; the rest have none at source.

## 3. What you inherit

`src/builders/Qdrant/mcp_server.py` in the backend repo — 263 lines, five tools:

```
search_proteins    shortlist, each hit carrying its best snippet as evidence
read_snippets      more windows from one protein, re-scored against a new question
expand_snippet     widen a window outward in sentences
read_record        the whole function text
collection_info    what is in the store
```

It is **Qdrant-only** (zero Neo4j references) and currently lives under
`builders/Qdrant/`, which is the wrong home the moment you add a graph tool.
That is the main reason it is moving here.

It imports `core.config` and `core.retriever` from the backend repo. Those stay
there — `store.py` builds collections (that is ETL) and `analysis/evaluate.py`
needs the retriever. Depend on them: `pip install git+https://github.com/yoyo4581/POKnowledgeBase_ETL@<tag>`
is sufficient; a published package is overhead until it isn't. Dependency
direction is services → backend, which is correct.

`Retriever.__init__` takes `encoder` and only ever calls `.encode()`. Anything
duck-typed can substitute — relevant if you later split embedding into its own
service.

## 4. Measured facts — do not re-derive these

All measured on the dev machine (WSL2, CPU, no GPU) during the backend session.

**Encoder cost**

| | |
|---|---|
| `import sentence_transformers` | **13.1 s** |
| load weights after import | 0.64 s |
| encode one short query | **50.8 ms** median (33–219) |
| encode batch of 20 | 76 ms/query — batching *hurts* for short text |
| peak RSS with model loaded | **~1.08 GB** (884 MB of it from importing torch) |

**The startup cost is torch, not the weights.** Design so the model is loaded
once per container lifetime, never per request.

**Image size** — `torch 2.14.0+cu130` in the backend venv is **1,178 MB** plus
**3,196 MB** of `nvidia/*`. A naive `pip install -r requirements.txt` ships
~4.4 GB of CUDA to a CPU-only container. Use CPU-only wheels:

```dockerfile
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu
```

That is ~3.2 GB saved for one line and zero behaviour change. **Do this.**

**ONNX** is a possible further ~1.2 GB and most of the 13 s import, but it is a
later optimisation, not a prerequisite. If you go there: fp32 export is
numerically equivalent within `rtol=1e-3, atol=1e-5` (PyTorch's own export
verification tolerance), so the existing Qdrant vectors stay valid and no
re-embedding is needed. **int8 quantization is a different matter** — reported
−1.95 nDCG@10, needs calibration data, and would require rescoring against
retained fp32 vectors to recover. Also note sentence-transformers only exports
the *Transformer* component; pooling and L2 normalisation stay outside it, so
calling ONNX Runtime directly means reproducing `1_Pooling/config.json` (mean
pooling) and `normalize_embeddings=True` exactly. That, not the graph
conversion, is where a silent mismatch comes from.

**HTTP overhead** is not worth optimising: 0.55 ms median (p95 0.84) for a
10.5 KB 768-float payload over loopback — **1.1%** of a 50 ms encode.

**MCP transport** — `mcp 2.2.0` is installed, where `FastMCP` was renamed
`MCPServer` (`from mcp.server.mcpserver import MCPServer`).

```python
MCPServer.run(transport: Literal['stdio', 'sse', 'streamable-http'] = 'stdio')
```

Both transports you need already exist; this is an env var, not a rewrite.
`stdio` spawns the server **once per client session**, not per tool call — the
subprocess persists. So stdio costs 14 s per session and ~1.08 GB *per client*;
a long-lived HTTP service loads once and shares. Free tier (Claude Desktop) is
naturally stdio; the paid agent layer wants `streamable-http`. Support both.

`build_server(retriever=None)` **lazy-loads** the retriever on first tool call,
so startup does not block on the model. Preserve that.

## 5. Container traps already paid for

These cost four separate debugging rounds in the backend session. The rule:
**a container writing to a mounted path needs its uid to match that path's owner,
and the owner depends on how the path came to exist.**

| mount | owner | why |
|---|---|---|
| bind mount, host dir already exists | the host user | pre-existing |
| bind mount, host dir **missing** | **root** | Docker creates a missing bind source as root |
| named volume, image has content at that path | **the image's uid** | Docker seeds content *and* ownership |
| named volume, image has nothing there | **root** | nothing to seed from |

Consequences that bit us: `neo4j` drops to uid **7474** via its entrypoint and
cannot write a host dir owned by someone else (`--user $(id -u):$(id -g)` fixes
it); `curlimages/curl` runs as uid **100** and cannot write a fresh named volume
(run it as root); a fresh named volume at `/data` *does* inherit 7474 from the
neo4j image, so a loader running as 7474 works on first boot. Any bind-mounted
directory must exist in a fresh clone — track a `.gitkeep`.

Other things verified: `docker compose include:` does **not** accept remote URLs
(it treats them as local paths), so ship **one self-contained compose file** and
accept duplicating the two database service definitions. Bind every published
port to `127.0.0.1` explicitly — a bare `"6333:6333"` publishes on `0.0.0.0`,
which with no API key is an unauthenticated store on the LAN. That loopback
binding is the only reason the Neo4j container ships `NEO4J_AUTH: none`.

`qdrant-client` defaults to a **5 s** HTTP timeout. Fine for queries, not for
administrative calls — pass an explicit timeout for anything that is not a query.

## 6. The cross-repo hazard

Collections were embedded by one specific encoder. Mixing a snapshot release
built by encoder A with Hub revision B puts queries and documents in different
vector spaces: **retrieval degrades and nothing errors.** The backend's
promotion gate catches this before publishing and cannot protect a consumer who
assembles a mismatched pair afterwards.

The pairing is recorded on both sides and you should verify it:

- model → `provenance.json` on the Hub carries `weights_sha256` and
  `built_collections.release_tag`
- release → the GitHub release body carries `encoder sha256`, matching that hash

**Pin both in your compose** — a release tag and an HF revision — rather than
`latest`. Bump them as a pair. They are paired at origin (model publishing is
downstream of the gate on the same DAG run) but can drift if one publish step
skips.

## 7. Preliminary instruction

Set up the repo. Do not write tool logic yet.

1. **Structure.** Decide and create the layout. Suggested shape, adapt with
   reason: `src/pathwayoracle_mcp/` with `server.py` (transport + tool
   registration), `tools/` split by capability (`retrieval.py`, `graph.py`), and
   `clients.py` for the Qdrant/Neo4j/encoder handles. Keep tool *definitions*
   separate from tool *implementations* — the agent layer will want to import the
   implementations without the MCP wrapper.
2. **Port `mcp_server.py`** from the backend repo unchanged first, as a working
   baseline, before refactoring. Its five tools already pass an end-to-end smoke
   test and its evaluation numbers were measured through this exact tool surface.
3. **`pyproject.toml`** depending on the backend repo by git tag, with CPU-only
   torch pinned.
4. **One compose file** bringing up qdrant + neo4j + loaders + this service.
   Start from `compose.serve.yaml` in the backend repo — it is verified working,
   including idempotency — and add your service to it.
5. **Dockerfile** with CPU-only torch, `HF_HOME` on a named volume, and
   `BIOBERT_MODEL_PATH` pointing at the Hub repo. Do not bake the weights in.
6. **Make the transport an env var** with `stdio` as the default, so Claude
   Desktop works out of the box and the agent layer can ask for HTTP.

Verify as you go by running things, not by reasoning about them. Every item in
§4 and §5 above was found by running something that then failed.

## 8. Open decisions, not yet made

- Whether embedding becomes a separate service. Measured cost is 1.1% per query,
  so it is a licensing/reuse decision rather than a performance one. In-process
  is fine given a long-lived HTTP service.
- Whether the backend's promotion gate keeps measuring through the MCP layer. It
  currently does (`"via": "mcp/in-memory"` in the eval summary), which is a good
  property — the thing benchmarked is the thing served. Moving the server here
  breaks it unless something replaces it. Do **not** solve this by keeping a copy
  of the server in both repos; they will drift silently.
- Which Neo4j tools to expose, and whether graph and vector results get joined
  server-side or left for the agent to join. The stores share the UniProt
  accession as their join key (`Gene.id` in Neo4j == `uniprot_id` in Qdrant,
  verified: all 10,204 Qdrant records map to a Gene node).
