# Roadmap — active log

Updated as work lands. `[x]` done, `[~]` in progress, `[ ]` to do, `[!]` blocked.
Newest decisions at the top of each section.

## Evidence view (provenance graph)

Design: the server **records** every tool result in a per-session evidence
ledger; the model **chooses** what to show by referencing ids it already holds;
the server **renders** only what it recorded, so the view cannot show evidence
that was never retrieved. Rendering is an MCP Apps panel (Cytoscape.js).

- [x] **Phase 0 — rendering spike.** Claude Desktop renders the MCP Apps panel:
      handshake, tool-result delivery, Cytoscape.js from a CDN under the declared
      CSP, hover, and live cola physics (drag a node, the rest respond).
      `src/pathwayoracle_mcp/apps/`. Tool `evidence_panel_demo` is a spike — remove
      once Phase 3 replaces it.
- [x] **Phase 1 — evidence ledger.** `src/pathwayoracle_mcp/evidence/`. Per-session
      NetworkX `MultiDiGraph` on KG_Memory's backend design (mention-counting
      upserts, `returned` back-links call -> node, namespace index by question /
      call), revised: facts only from tool results, entities keyed by domain ids,
      one edge per (relation, reaction), typed nodes (entity / evidence /
      aggregate / call / question), refuses edges to unknown nodes. Mappers for
      every content tool (`ingest.py`); `plan` opens a question; failed calls are
      recorded; `execute_tool` is not double-counted; recording never fails a
      call. Fed from `dispatch.Toolkit.call`. Tests: `tests/test_ledger.py`
      (14, against real results in `tests/fixtures/`, regenerate with
      `scripts/capture_fixtures.py`). Deployed; not yet visible to the model.
  - Found while building it: resolving complexes to genes credits each member
    with the complex's role (MDM2 "inhibits" its own ubiquitination of TP53 via
    the p14ARF:MDM2 complex). `gene_reactions.by_regulator` now returns reaction
    ids and `via` (the acting entity) — part of the v2 registry, not yet loaded.
- [x] **Phase 2 — `show_evidence` + `evidence_index`.** Token-first design:
      the model sends ids (claims = short label + cite ids; or scope=question
      with no ids) and gets a one-line receipt (~40 tokens); the full view goes
      to the panel through the app-only `evidence_view_data` tool (visibility
      ["app"]) and never enters context. Cites resolve as fetched / referenced
      (id seen, content never read) / unknown (never drawn, flagged). Node cap
      (default 40) keeps cited and included nodes and folds the rest into
      per-label groups; explicit `collapse` groups. `evidence_index` (unlisted)
      returns compact strings. Listed definition ~300 tokens. Python tools that
      the Neo4j registry lacks are taken from the bundled YAML at runtime
      (logged). Tests: `tests/test_view.py` (7). Deployed.
  - [x] Confirmed in Claude Desktop (2026-10-09, TP53 question): the panel's
        `tools/call` to `evidence_view_data` succeeds (the graph only draws after
        it), and the model never called that tool itself. 7 claims, all supported.
  - [ ] Optional: a recipe step / instruction suggesting show_evidence at the end
        of an answer (the user prefers the model decides when).
- [x] **Phase 3 — the real panel.** `apps/evidence.html`, one file for both the
      MCP Apps panel and the standalone page. Numbered claims (hover lights their
      evidence, click pins + zooms); details pane on click with full text, source,
      read status and the provenance trail (`returned_by`: the tool calls that
      produced it); groups expand on click (members + edges ship in the view);
      legend toggles per category; toolbar: fit, pause physics, fullscreen
      (`ui/request-display-mode`), PNG/JSON (`ui/download-file`, else a link),
      open standalone page (`ui/open-link`). Unread evidence drawn dashed; edge
      labels only on cited edges. Reaction participants linked to the session's
      gene nodes (`in_entity`), so a protein entity and its gene no longer look
      like duplicates. `evidence_panel_demo` spike removed.
  - [ ] Try in Claude Desktop: fullscreen, downloads, open page, group expand,
        details pane (not exercisable in the headless render check).
- [x] **Phase 4 — fallbacks.** Clients without MCP Apps get a Mermaid flowchart
      (≤25 nodes, cited first, ~300 tokens) + a standalone link in the
      show_evidence result; Apps clients still get only the one-line receipt.
      Views persist to `/cache/evidence/<session>/<view>.json` (shared volume) and
      the HTTP service serves `GET /evidence/{session}/{view}` (page, view
      embedded) and `….json` — works across processes (Claude Desktop's stdio
      session -> HTTP service). Full view attached to the show_evidence span as
      Langfuse metadata, output unchanged. Path traversal in view ids caught by
      the tests and fixed. Tests: `tests/test_fallbacks.py` (6). Rendered and
      checked in headless Chrome.
- [x] **Connected views from reaction and passage cites** (2026-10-09). A TP53-inhibitor
      answer citing only `reaction:` and `qdrant:` ids drew 19 nodes and 0 edges: passages
      and reactions only meet through genes, and no gene was cited. Now:
  - `build_view` brings the gene a cited passage describes, and the genes taking part
    in a cited reaction (both inherit the claim's theme). Under the cap, participants
    rank by the size of the entity they act through, so specific actors (STAT3 dimer,
    SETD8) are kept before machinery (40 proteasome subunits fold into one group).
    Replayed: 0 -> 73 edges at the default 40-node cap.
  - `reaction_details` records every constituent: each gene of each participant, by
    UniProt id, linked `in_entity` and straight to the reaction (role + `via`). The
    full member lists (sets reach 400 genes) ride in `_members`, a **ledger-only field**:
    dispatch strips `_`-prefixed keys from what the model sees and what is traced;
    `Toolkit.call(private=True)` returns them (fixtures). The model's `genes` stays
    capped at 15, now with `n_genes`. Registry reloaded (KG unchanged).
  - Tests: 6 new (40 total). Fixtures re-captured for reaction_details only.
  - [ ] Seen while re-capturing: `search_proteins` picked a different best snippet for
        one hit (a Pol alpha accessory subunit, chunk 0 vs 1) on identical input -- likely a score tie; make the
        tie-break deterministic (core/retriever is vendored: change upstream).
- [x] **README examples** (2026-10-09). `scripts/export_example.py` turns one question of
      a session into `docs/examples/<slug>/`: README (answer, tool-call table from
      `/cache/traces/calls.jsonl`), `trace.jsonl`, and `panel.html` -- the standalone
      panel (`apps.standalone_page`, now shared with the HTTP route). `panel.png` by
      headless Chrome. Two examples: TP53 inhibitors, MDM2 -> TP53.
  - [x] Image rebuilt with the `standalone_page` refactor (route verified: 200).
- [x] **Theme provenance** (2026-10-09). The user's rule: the model's own grouping is
      fine, but a grouping sourced from the database must be known as such.
      `show_evidence(themes=[{name, cites}])`; the LEDGER decides the source, not the
      model: `database` only when every cite resolves to a grouping retrieved this
      session (common_ontology group, pathway_go_profile row, shared_machinery row,
      Pathway / GO term / Complex / set node), otherwise `model` -- and the receipt
      says why ("2 themes (1 from database)", "... not a grouping"). Panel: solid
      accent box + "from database" badge naming the basis (term, tool) vs dashed box +
      "model's grouping". Server instructions: say in the answer which groupings come
      from a tool. Registry reloaded; tests: 3 new (43 total). README examples
      regenerated (all their themes were the model's).
- [ ] **Phase 5 — measure.** Harness scores: does the model call show_evidence,
      do its views cover its claims. Deprioritised (2026-10-09): in manual testing
      Claude uses show_evidence well.
- [x] **KG_Memory integration — built and deployed (2026-10-10).** `docs/memory.md`
      "As built": episodes saved at every call (`/cache/memory/<session>.json`), compaction
      over 60k tokens keeping the last 3 exchanges, server-driven recall (digest in the
      instructions + `memory` pointers in tool results), `recall` / `recall_episode` as
      listed memory tools outside the recipes. Registry reloaded (34 tools, KG unchanged).
      Tests: 10 new (53 total). Live check in the container (two chats, scratch memory
      dir): digest, pointer on chat B's first result, recall by id, recall_episode.
      Original design notes:
      the last 3 exchanges stay intact; everything older is consolidated into episode
      triplets (NetworkX) once 60k tokens seen is exceeded, and everything at session end;
      one long-term file per session (`/cache/memory/<session>.json`), composed into one
      graph at startup, unused evidence included; episodes carry their KG release;
      `recall` (quick) and `recall_episode` (targeted). Reuses KG_Memory's triplet,
      upsert and namespace ideas, not its code (its bugs: relative-import crash,
      stdout prints, `abatch`, `_handle_get`, `get_entity_info`, hard-coded Qdrant host).
  - [x] Idle-gap default measured from the trace file: within-response max 36.2 s,
        between exchanges >= 208.6 s -> default 90 s.
  - [x] Decided (2026-10-09): session = CHAT, not server process; recall runs at the
        start of every chat (inside the first `plan`).
  - [x] Decided: no chat id in the model's context -- continuity through long-term
        memory. Each exchange's episode is saved at its boundary (compaction separate,
        over budget only); a new session's first `plan` recalls earlier sessions.
  - [~] **Triplet grounding at recall (2026-10-10).** `docs/memory.md` "Triplet
        grounding at recall". In the first live chat the triplets were saved but never
        reached the model:
        - `recall_episode` returned 60 of 714 facts in insertion order and skipped the
          claim→cite links;
        - recall calls re-recorded the episode they loaded as a new one;
        - parallel plans split one question into two episodes;
        - the summary cut tools at 4 and ranked ubiquitin genes as top entities;
        - dates were UTC without saying so.
        Fixed:
        - claim-first grounding (claim → cite → triplets; reactions as entity + genes);
        - hub-grouped remaining triplets;
        - `claims_on_ids` in `recall`;
        - memory calls excluded from episodes;
        - plan-burst exchanges;
        - the summary built from the episode graph;
        - UTC dates.
        Tests: 7 new (60 total), including a regression on this chat's real episode
        (`tests/fixtures/memory_tp53_episode.json.gz`). `scripts/migrate_memory.py`
        re-summarises saved files and drops copied episodes.
        **Deployed (2026-10-10).** Image rebuilt, registry reloaded (34 tools, KG
        unchanged), `/cache/memory` migrated (copied episode 3 dropped, `.bak` kept).
        Live check passed: `recall(ids=[MDM2, TP53])` leads with the MDM2 claims, and
        `recall_episode(ids=[MDM2])` grounds them in reactions R-HSA-6804879 and
        R-HSA-5633460 and the edge TP53→MDM2. Found during the live check and fixed:
        - `claims_on_ids` ranks claims by how many of the ids they rest on, and how
          specifically;
        - a chat that only recalled no longer writes an empty file.
        Left behind: one empty file from before that fix
        (`stdio-2961fe31160b.json`, 77 bytes, harmless).
  - [ ] Verify: one server process per chat or per app (two chats, compare session ids).
  - [ ] Try in Claude Desktop: a new chat shows the digest; MDM2 / TP53 results carry
        pointers to this chat's episodes; recall_episode + show_evidence draws recalled
        evidence.
  - [ ] Open: model-written note per exchange (optional `note` on show_evidence).
  - [ ] Open: model-written session note (re-explain to the user when building).

## Controlled evaluation (`eval/`)

- [x] Harness: `eval/run.py` + `eval/questions.yaml`; scores facts, citations
      (fetched / mentioned-only / unsupported), recipe adherence, tool calls,
      read_cypher, cost; one scored trace per run in Langfuse.
- [!] **Baseline** — 8 of 15 runs done (`eval/results/baseline.jsonl`); stopped
      when the Anthropic API credit ran out. Remaining: q3 r3, q4 ×3, q5 ×3.
      The running image now carries v2 code and the Apps spike, so finish the
      baseline from the backups (baseline seed + code in the session scratchpad)
      or re-run the whole baseline on a rebuilt baseline image.
      Since the abstracts were re-embedded (2026-10-09), the 8 finished runs used
      different literature data than any new run would: a fair baseline-vs-v2
      comparison now needs the whole baseline re-run (15 runs), not just the rest.
- [ ] **v2** — coverage statements, `gene_reactions` by regulator gene,
      `read_cypher`/`describe_schema` off the startup list, leaner `plan`. Edited
      in `toolkit.yaml` + code; **registry not yet reloaded** (Neo4j still holds the
      baseline registry). Run after the baseline, compare.
- [ ] Harder fact checks (negative facts: "PHF20 does not lower TP53"; Q3 rejects
      DPH1/HARS1 as histidine kinases).
- [ ] Ask: which model is selected in Claude Desktop — match it in the harness.
- [ ] Paradigm vs workflow recipes — decide only if v2 still shows drift.

## Toolkit

- [x] Registry in Neo4j (`:Toolkit` nodes), guarded loader, KG fingerprint check.
- [x] Recipes as `plan(recipe)` traversal; mechanistic recipe on gene tools;
      `gene_reactions`, `reaction_details`, `interaction_paths`, `snippet_sources`.
- [x] Tracing → Langfuse (OTLP), session per MCP connection, `PATHWAYORACLE_SESSION`.
- [x] Claude Desktop via `docker exec` (2 s start; no leftover containers).
- [ ] MCP prompts (user-picked recipes in Claude Desktop's "+" menu) — only if
      the model keeps skipping `plan`.
- [x] Registry v2 loaded into Neo4j (2026-10-09; 32 tools, 9 recipes, KG fingerprint unchanged).
- [x] Recipes are paradigms, not checklists: `plan` says "take a step only if the answer
      still needs it"; server instructions ask for the answer first, then the evidence behind it.
- [x] Synthesis stage (2026-10-09): recipes carry `stage: gather | synthesis`; `plan`
      lists only gather recipes and returns the synthesis ones as `to_structure_the_answer`:
      `by_function` (`common_ontology`), `by_system` (`gene_pathways`), `by_interaction`
      (`gene_interactions` within the set), `by_shared_machinery` (new `shared_machinery`:
      genes grouped by the complexes and reactions they share).
  - `common_ontology` is hierarchical: level 1 is a weighted set cover of specific KG terms;
    each higher level joins groups under their most informative shared ancestor and
    absorbs ungrouped genes; `exclude_terms` drops terms that restate the question.
  - `show_evidence` claims take an optional `theme`; the panel draws theme boxes and
    groups claims under theme headers. A cited group brings its member genes into the view.
- [ ] Review: a fresh Claude Desktop run of "Which proteins inhibit TP53's stability or
      activity?" — does it pick a synthesis recipe on its own and lead with the answer?
  - 2026-10-09 run (docs/examples/tp53-inhibitors): led with the answer, but NO synthesis
    call -- the 3 themes were the model's own grouping by reaction verb, so the theme
    boxes rest on uncited labels. An earlier run did call common_ontology +
    shared_machinery (see tests/fixtures). n=2, split 1/1: measure before changing `plan`.
- [ ] Possibly trim `plan` output (~1.2k tokens) and `shared_machinery` (~740 tokens).

## Backend (POKnowledgeBase_ETL) — for the next snapshot, not this repo

- [ ] `ACTS_ON`: one edge per gene pair loses reactions (MDM2 → TP53 keeps only
      SUMOylation); key edges by (pair, reaction) or carry `reaction_ids`.
- [ ] `ACTS_ON`: regulators of reactions whose target sits in a set / gene entity
      get no edge (STAT3 → "Expression of NDN,TP53").
- [ ] Composition edges point part → whole despite their names — document or rename.
- [ ] Drop leftover KEGG constraints (`Function`, `Glycan`, `Ortholog`).
- [ ] Pathway hierarchy edges; precomputed GO term specificity; PubMed ids as a
      payload field; abstract metadata (authors, journal, year) in Zilliz.
- [x] Re-embed the PubMed abstracts with the fine-tuned encoder — done on Colab with
      `colab_reembed/reembed_abstracts.ipynb` (2026-10-09; fp16 failed its 0.999 cosine
      check against fp32, so it ran in fp32; the notebook now falls back automatically).
      STAT3 + p53 check afterwards: top 5 all STAT3-p53 papers, #1 PMID 22303479 (STAT3
      binds and represses the p53 promoter), #2 PMID 16107692.
      Correction: the pre-re-embed ranking was NOT mostly off-topic, as noted here
      earlier — 3 of the 4 hits dismissed by title (25113562, 29174979, 32127023) do
      discuss STAT3 and p53. Re-embedding was for encoder pairing, not a broken ranking.
  - [ ] Re-run eval q2 (literature sourcing) to measure the effect beyond one query.
- [ ] Package `core/` so this repo can drop the vendored copy.
- [ ] Merge the toolkit registry into the published Neo4j snapshot.
