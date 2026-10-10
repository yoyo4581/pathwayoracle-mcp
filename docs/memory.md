# Memory: ledger consolidation and long-term recall

Status: **built and deployed (2026-10-10)**; see "As built" at the end for where the
build differs from the design below. Tracked in `docs/ROADMAP.md` ("KG_Memory integration").
Decisions from 2026-10-09 are folded in. The one still open is at the end.

**Session = chat** (user, 2026-10-09). A session is one chat conversation: the user's
messages and the model's responses. In normal use one chat is one MCP connection (one
stdio process), so the server's session id stands for the chat.

**Continuity comes from long-term memory, not from the model's context** (user,
2026-10-09). No chat id is handed to the model. When a connection is lost mid-chat (a
container rebuild kills the stdio process; the reconnect starts a new session), the new
session's first `plan` recalls what the old one saved: every exchange is written to disk
at its boundary (section 5), so at most the exchange in progress is lost.

**To verify:** whether Claude Desktop runs one server process per chat or one per app
(ask a question in two chats, compare session ids in `/cache/traces/calls.jsonl`). If it
is one per app, recall also fires on the first `plan` after an idle gap of
`PATHWAYORACLE_SESSION_IDLE`, which is how the server would notice a new chat.

## Tiers

| tier | holds | lifetime | written by |
|---|---|---|---|
| **Ledger** (short-term) | the 3 most recent exchanges in full, plus episode triplets for everything older | the session | `dispatch` → `ingest` (exists); consolidation (new) |
| **Long-term memory** | episode triplets, one file per session (chat), used and unused evidence | persistent | the server, at every exchange boundary (saving) and at consolidation (compaction) |
| **Views** | the subgraphs behind each answer | persistent | `show_evidence` (exists) |

Both memory tiers are NetworkX `MultiDiGraph`s and use the same triplet model. The consolidated part of the ledger is exactly what gets written to long-term memory.

Rules carried over from the ledger design:
- **Facts only come from tool results.** Consolidation reorganises what was recorded. It never adds facts. Claim labels from views are the only model-written text, and they're marked `authored: model`.
- **Keyed by domain ids.** Triplets carry ids and cites, not text bodies. Snippets, abstracts and reactions can be fetched again by cite id.
- **Not in the KG.** Files go under `/cache/memory/`. Nothing is written to Neo4j or to the release Qdrant or Zilliz collections.

## 1. Exchanges

An **exchange** is one user message plus the model's full response, including all of the response's tool calls. MCP carries no turn boundary, so the server infers one. A new exchange starts at the first call after either of these:
- a **`plan` call**, which opens a new question (already the case today); or
- an **idle gap** of `PATHWAYORACLE_EXCHANGE_GAP` seconds (default 90) since the previous call ended. Within one response, calls follow each other within seconds. Between exchanges, the user reads and types.

The ledger stores this as `exchange:<n>` nodes (replacing `question:<n>`). The `plan` recipe becomes an attribute of its exchange.

**Measured (2026-10-09, 13 connections in `/cache/traces/calls.jsonl`):** within one
response the longest gaps were 36.2, 25.1, 22.6 and 20.2 s, all just before
`show_evidence` (the model writing its claims); between exchanges, 208.6 and 652.9 s.
45 s separated them with little margin, so the default is 90 s.

## 2. When to consolidate

- `EvidenceLedger.tokens_seen`: the summed size of the results the model received (`len(json)/4` of the stripped `record.result`). It's counted in `Toolkit._record_evidence`. Ledger-only `_` fields don't count, because the model never saw them.
- **Rule:**
  - when the live (unconsolidated) part of the ledger exceeds `PATHWAYORACLE_LEDGER_BUDGET` (default 60000) tokens seen, every exchange **older than the 3 most recent** is consolidated;
  - the 3 most recent exchanges always stay intact, even if they alone exceed the budget;
  - at **session end**, every exchange is consolidated, the recent 3 included.
- Consolidation runs after ingest, inside the same `try` as recording, so it never fails a tool call.

## 3. Episodes as triplets

One episode per consolidated exchange. Every edge is a triplet `(subject, predicate, object)` with attributes `{session, episode, release, cite?, status?}`. The edge key includes the episode, so the same fact recorded in two episodes stays two edges with their own provenance. It is never merged.

Nodes are shared by id across episodes and sessions. Entities get KG_Memory-style upserts, with `mention_count` accumulating.

| node kind | id | attributes |
|---|---|---|
| `session` | `session:<id>` | started, ended, release |
| `episode` | `episode:<session>:<n>` | recipe, tool sequence with counts, failures, timings, deterministic summary (~150 tokens) |
| `entity` | domain id (`P04637`, `R-HSA-…`, `GO_…`) | label, name |
| `evidence` | cite id (`qdrant:…`, `pubmed:…`) | source, title — no text body |
| `aggregate` | its cite (`profile:set~GO_…`) | atype, statistics |
| `claim` | `claim:<view>:<i>` | label, `authored: model` |

| triplet | from |
|---|---|
| `session —has_episode→ episode` | exchange order |
| `episode —touched→ entity` {mentions, degree} | ledger namespace index |
| `episode —cited / read / seen→ evidence` | evidence status; the predicate is the status |
| `evidence —about→ entity` | ledger edges (a snippet of a gene, an abstract on a gene) |
| `entity —<relation>→ entity` {qualifier, cite, sign} | ledger edges as the tools returned them (ACTS_ON, membership, reaction roles) |
| `aggregate —groups→ entity`, `—term→ GO`, `—within→ aggregate` | synthesis results |
| `episode —derived→ aggregate` | the call that produced it |
| `episode —concluded→ claim`, `claim —cites→ evidence / entity / aggregate` | views shown in that exchange |

**Evidence status** becomes a stored attribute: `cited` (a view's claim cites it), `read` (its text was fetched) or `seen` (only referenced). Today [view.py](../src/pathwayoracle_mcp/evidence/view.py) works it out from whether text is present, and that check breaks once text is dropped.

**In the ledger,** consolidation replaces an exchange's call nodes and text bodies with its episode triplets:
- `returned` edges from its calls are repointed to the episode;
- `returned_by` reports "exchange n (episode)";
- new views resolve consolidated nodes by id, and the panel fetches text again through `resolve_citations` on demand;
- views already shown are stored whole and are unaffected.

**The summary** is deterministic, with no LLM involved. It covers the recipe, top entities by degree, aggregate names, claim labels (marked model-authored) and evidence counts by status.

## 4. Session end

Every remaining exchange is consolidated, the session file is written, and the ledger is cleared.

- **stdio** (Claude Desktop through `docker exec`): when Claude Desktop closes, stdin closes and the process exits. The flush runs from the server's shutdown hook (lifespan exit), with an `atexit` fallback.
- **HTTP:** there's no end signal. An idle sweep flushes ledgers with no call for `PATHWAYORACLE_SESSION_IDLE` (default 30 min).
- **Eviction:** `Ledgers.get` currently drops the oldest ledger past `max_sessions` without saving it. It has to flush before it pops.

## 5. Long-term store

```
/cache/memory/<session>.json      node-link triplet graph of that session's episodes
```

- **One file per session.** It's rewritten at every exchange boundary (the closed exchange's episode is added; the live ledger is left alone), so a crash or a killed connection loses at most the exchange in progress. Compaction of the live ledger is separate and waits for the budget (section 2). Size doesn't matter.
- **At startup,** every session file is loaded and composed into one `MultiDiGraph`. Shared entity ids connect sessions automatically, so "which sessions touched MDM2" is a neighbourhood lookup.
- **No other index.** Lookup by id is graph traversal. Text matching is a token-overlap scan over episode summaries and entity names, which is enough at this scale.
- **Release:** every episode carries the KG release it was recorded against. Recall output shows it as one line. If it differs from the current release, the model can tell the KG has changed and check it with the normal tools. Old ids are not validated or flagged.
- `evidence/store.py`'s `_SAFE` and path-containment checks apply to session file names.

## 6. Model-facing tools

| tool | input | returns | role |
|---|---|---|---|
| `recall` | `query?`, `ids?`, `limit` | ranked episodes: id, session date, release, recipe, summary, matched ids with status (~40 tokens per row) | quick recollection |
| `recall_episode` | `episode_id`, `ids?` | that episode's triplets (all, or around `ids`), loaded into the current ledger | targeted query |

- **Ranking:** id matches first (cited > read > seen), then text overlap. The current session's own episodes are included, which is how the model gets back exchanges consolidated earlier in the session.
- **Recalled nodes** enter the ledger with `status: recalled` and their origin (session, release). They can be cited, and the panel draws them in a distinct style.
- **`evidence_index`** lists the session's episodes alongside live counts.
- **Recall at the start of every chat** (user, 2026-10-09; replaces "no automatic
  recall"). The first `plan` of a session takes the user's question (`question`,
  optional) and returns the top matching episodes from earlier sessions inside its
  result, with a pointer to `recall_episode`. Later in the chat the model calls `recall` itself, e.g.
  once anchor ids are known.
- `recall` re-reads changed chat files first, so a chat can recall another that is still
  open in a different connection.

## 7. Changes in this repo

| file | change |
|---|---|
| `evidence/ledger.py` | `exchange` replaces `question`; `episode`, `session`, `claim` kinds; `tokens_seen`; explicit evidence `status` |
| `evidence/consolidate.py` (new) | exchange selection (keep last 3), ledger → episode triplets, compaction, deterministic summary |
| `evidence/memory.py` (new) | `MemoryGraph`: load and compose session files, `write_session`, `search`, `episode_subgraph` |
| `evidence/__init__.py` | `Ledgers`: flush on eviction, idle sweep, `flush_all()` |
| `dispatch.py` | exchange detection (plan / idle gap); count `tokens_seen`; trigger consolidation after ingest |
| `evidence/view.py` | read `status` from the node; `recalled` status; fetch text again for consolidated nodes on demand |
| `evidence/ingest.py` | mappers for `recall`, `recall_episode` |
| `handlers/memory.py` (new) | `recall`, `recall_episode` |
| `handlers/evidence.py` | `evidence_index` lists episodes |
| `server.py` | shutdown flush; one `INSTRUCTIONS` line |
| `registry/toolkit.yaml` | `memory` category and the two tools; registry reload (guarded, Toolkit nodes only) |
| `apps/evidence.html` | styles for `recalled` and consolidated evidence |
| `compose.yaml` | `PATHWAYORACLE_LEDGER_BUDGET`, `PATHWAYORACLE_EXCHANGE_GAP`, `PATHWAYORACLE_SESSION_IDLE`, `PATHWAYORACLE_MEMORY_DIR` (default `/cache/memory`) |
| `tests/test_memory.py` (new) | see below |

**Tests** (replaying `tests/fixtures/tool_results.json` as several exchanges):
- exchange detection by `plan` and by idle gap;
- under budget, nothing is consolidated; over budget, everything but the last 3 exchanges is; the last 3 stay intact even when they alone exceed it;
- every consolidated fact is a triplet whose cite and release match the ledger edge it came from; no fact appears that the ledger didn't have;
- views built before and after consolidation resolve the same cites;
- two sessions loaded together share entity nodes and keep separate edges;
- `recall` by id and by text; `recall_episode` loads nodes as `recalled`;
- shutdown and eviction flush; session file names can't escape the memory dir.

## Open

- **A model-written session note** beside the deterministic summary: marked model-authored, used only for text recall, never added as evidence triplets. The user agreed in principle (2026-10-09) but asked to have it re-explained when this is built. Ask then.

## As built (2026-10-10)

Code: `evidence/consolidate.py` (episodes, summary, compaction), `evidence/memory.py`
(`MemoryGraph`: files, search, digest, pointers), `handlers/memory.py` (`recall`,
`recall_episode`), wiring in `dispatch.py`; tests in `tests/test_memory.py` (10).

Where it differs from the design above, and why:

- **Recall is server-driven** (user, 2026-10-09): it is not part of `plan` or any recipe.
  - At connection start, a digest of the 5 most recent episodes from other chats is
    appended to the server instructions (~40 tokens each), which the client puts in
    front of the model.
  - When a tool result contains an entity that other chats worked on, the result gets
    a `memory` field pointing to their episodes: at most 3 entities, 2 episodes each,
    once per entity per chat. Not added to meta or memory tools' results.
  - `recall` / `recall_episode` are listed tools in a `memory` category, for drilling in.
- **Saving at every call**, not at consolidation or session end: the chat's file is
  rewritten after each call (the open exchange's episode is rebuilt; closed ones are
  frozen). A killed connection loses nothing it recorded, so no shutdown hook, idle
  sweep or eviction flush is needed.
- **Exchanges are stored as `question:<n>`** (the existing counter), opened by `plan`
  (`reason: plan`) or an idle gap of 90 s (`reason: idle gap`). Renaming the node kind
  would have touched every view and test for no behavioural gain.
- **Compaction keeps call nodes** (dropping their arguments) instead of repointing
  `returned` edges: they are the panel's provenance trail. It drops text bodies and
  bulky attributes of nodes not touched by the last 3 exchanges; `read` is kept as a
  flag on the node, so views resolve the same before and after.
- **Fact predicates keep the ledger's names** (`describes`, `cites`, `ACTS_ON`, roles),
  so every episode fact matches the ledger edge it came from (tested).
- **Recalled nodes**: `recall_episode` loads triplets through a ledger-only `_triplets`
  field; a node is `recalled` when only `recall_episode` calls returned it. Claims citing
  recalled nodes count as supported; the receipt counts "cites from earlier chats"; the
  panel draws them with a double accent border.
- **Not built yet:** the model-written note (open question above); `evidence_index`
  shows only counts (`episodes_saved`, `exchanges_compacted`), not an episode list.
- **Earlier sessions are not in memory:** ledgers before this deploy were never saved
  (only traces and views were), and episodes cannot be rebuilt from them faithfully.

### Triplet grounding at recall (2026-10-10, second pass)

Found in the first live TP53 chat: the triplets were saved correctly (714 facts, 36
claim→cite links, all 11 cited edges present), but the model never saw them. Summaries
were the only layer that reached it, so recall could not ground anything. Fixes:

- **`recall_episode` starts from the claims.** Every claim of the episode is listed with
  the triplets it rested on when it was made: claim → cite → the fact triplets carrying
  that cite.
  - An edge is shown as `TP53 -ACTS_ON(+)-> BAX (regulation; R-HSA-3700984 <name>)`.
  - A reaction is shown as each role with the entity that plays it and that entity's
    genes, e.g. `CATALYST p-S1981-ATM [ATM]`, so a complex's role is not read as each
    member's.
  - A passage is shown with the gene it describes.
  - The other triplets come after the claims, one line per hub
    (`[136] MDM2, ATM, … -ACTS_ON-> TP53`), with members the claims rest on named first.
  - `ids` narrows the output to the claims and triplets around those entities.
  - Previously it returned the first 60 of 714 facts in insertion order. None of the
    cited edges were in them, and the claim→cite links were skipped.
  - Budget: an episode has a handful of claims, so all of them are rendered. The TP53
    episode (9 claims, 714 triplets) costs about 2.2k tokens, or about 1.1k narrowed to
    MDM2. If episodes grow far beyond this, selection (e.g. by similarity to the
    question) would be the next step. It isn't needed yet.
- **`recall` rows** add `size` ("9 claims, 714 triplets"). With `ids`, they add
  `claims_on_ids`: the claims resting on those entities. That is a traversal of the
  triplet graph, not a text match on the summary.
- **Memory calls are not the recalling exchange's facts.** Triplets loaded by
  `recall_episode` belong to the episode they came from. Re-recording them had copied
  the whole episode into the recalling one. An exchange that only used memory tools and
  concluded nothing saves no episode.
- **Several `plan` calls before any other call are one exchange** (`recipe: "fine_grained,
  mechanistic"`). Parallel plans had split a question into a plan-only episode and the
  real one.
- **The summary is built from the episode graph alone**, so saved episodes can be
  re-summarised (`scripts/migrate_memory.py`). It now:
  - lists every tool (the top-4 cut had hidden half of them);
  - counts graph cites ("claims cite 11 edge, 11 reaction") next to passage statuses;
  - ranks entities by how much the claims rest on them. Each claim contributes 1 per
    directly cited entity or edge end, and 1/n for one of n participants sharing a role
    in a cited reaction. Ties are broken by fact degree without participant→gene links.
    Ubiquitin and proteasome subunits no longer head the list.
- **Dates are UTC and say so** (`2026-10-10 06:02Z`). The container has no time zone,
  and an unlabelled UTC date read as tomorrow in the user's evening.

