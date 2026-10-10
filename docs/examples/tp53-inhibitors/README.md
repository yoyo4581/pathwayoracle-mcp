# Which proteins inhibit TP53's stability or activity?

Recipe `mechanistic` · 14 tool calls · 13.2 s in tools ·
evidence view: 41 nodes, 74 edges, 11/11 claims supported ·
release `serving-v20261006193533`

## Answer

The graph records 11 proteins that inhibit TP53. They work in three ways: they mark it for degradation, they add modifications that switch it off, or they repress its gene. MDM2 and MDM4 are the central pair.

### Degradation (stability)
- **MDM2** (Q00987) catalyses "MDM2 ubiquitinates TP53" [reaction:R-HSA-6804879]. It is an E3 ligase whose ubiquitination sends TP53 to the proteasome, and it also blocks TP53's transactivation domain [qdrant:Q00987#0].
- **MDM4** (O15151) is part of the same catalytic complex [reaction:R-HSA-6804879]. It suppresses TP53 transactivation and apoptosis [qdrant:O15151#0, qdrant:O15151#2].
- **RNF34 / RFFL** (Q969K3 / Q8WZ73) ubiquitinate phosphorylated TP53 [reaction:R-HSA-6804441]. Their text says this targets TP53 for proteasomal degradation [qdrant:Q969K3#3].
- **GTSE1** (Q9NYZ3) brings poly-ubiquitinated TP53 to the 26S proteasome for degradation [reaction:R-HSA-8852354].

### Modifications that switch TP53 off (activity)
- **AURKA** (O14965) phosphorylates TP53 at S315 [reaction:R-HSA-6805103], which destabilises it [qdrant:O14965#10].
- **AURKB** (Q96GD4) phosphorylates TP53 at S269 and T284 [reaction:R-HSA-6805126], which represses its transcriptional activity [qdrant:Q96GD4#11].
- **SMYD2** (Q9NRG4) methylates K370 [reaction:R-HSA-3222237], which lowers TP53's DNA binding [qdrant:Q9NRG4#3].
- **SETD8/KMT5A** (Q9NQR1) methylates K382 [reaction:R-HSA-6805740], which represses TP53 target genes [qdrant:Q9NQR1#7].
- **EHMT1/EHMT2** (Q9H9B1 / Q96KQ7) add two methyl groups to K373 [reaction:R-HSA-6805755]. The data records the modification but not its effect [qdrant:Q9H9B1#9], so calling it repressive comes from general knowledge.

### Repressed expression
- **PRDM1** (O75626) represses TP53 expression [reaction:R-HSA-6804188, edge:O75626-ACTS_ON->P04637@R-HSA-6804188].
- **STAT3** (P40763), as its phosphorylated (Y705) dimer, inhibits "Expression of NDN, TP53" [reaction:R-HSA-6789960].

### Recorded as negative, but they protect TP53
- **PHF20** inhibits "MDM2 binds TP53" [edge:Q9BVI0-ACTS_ON->P04637@R-HSA-5633460].
- **p14ARF (CDKN2A)** inhibits MDM2's ubiquitination of TP53 [reaction:R-HSA-6804879].
- **USP7, USP10, USP24 and USP42** remove the ubiquitin.

## Evidence panel

[![Evidence panel](panel.png)](panel.html)

`panel.html` is the panel Claude Desktop renders inline, as a standalone page: open it
in a browser to hover claims, drag nodes and click through to the evidence text.

## Tool calls

| # | tool | via | arguments | ms | cites returned |
|---|---|---|---|---|---|
| 1 | `plan` | listed | `{"recipe": "mechanistic"}` | 1,612 | 0 |
| 2 | `find_entities` | listed | `{"text": "TP53", "labels": ["Gene"], "limit": 1}` | 84 | 1 |
| 3 | `gene_reactions` | execute_tool | `{"uniprot_ids": ["P04637"], "role": "REACTANT", "skip": 0, "limit": 5}` | 56 | 5 |
| 4 | `gene_interactions` | execute_tool | `{"uniprot_ids": ["P04637"], "direction": "in", "sign": "negative", "skip": 0, "limit": 50}` | 8 | 2 |
| 5 | `reaction_details` | execute_tool | `{"reaction_ids": ["R-HSA-6804879", "R-HSA-6804441", "R-HSA-8852354", "R-HSA-6805103", "R-HSA-6805126", "R-HSA…` | 13 | 11 |
| 6 | `read_snippets` | listed | `{"uniprot_id": "Q00987", "query": "ubiquitinates p53 for proteasomal degradation and inhibits its transcripti…` | 9,535 | 1 |
| 7 | `read_snippets` | listed | `{"uniprot_id": "O15151", "query": "inhibits p53 transcriptional activity and stability", "n": 2}` | 132 | 2 |
| 8 | `read_snippets` | listed | `{"uniprot_id": "Q969K3", "query": "ubiquitinates p53 and promotes its degradation", "n": 1}` | 132 | 1 |
| 9 | `read_snippets` | listed | `{"uniprot_id": "O14965", "query": "phosphorylates p53 promoting its MDM2-mediated degradation", "n": 1}` | 301 | 1 |
| 10 | `read_snippets` | listed | `{"uniprot_id": "Q96GD4", "query": "phosphorylates p53 and decreases its transcriptional activity", "n": 1}` | 190 | 1 |
| 11 | `read_snippets` | listed | `{"uniprot_id": "Q9NRG4", "query": "methylates p53 and represses its transcriptional activity", "n": 1}` | 688 | 1 |
| 12 | `read_snippets` | listed | `{"uniprot_id": "Q9NQR1", "query": "monomethylates p53 and represses its transcriptional activity", "n": 1}` | 115 | 1 |
| 13 | `read_snippets` | listed | `{"uniprot_id": "Q9H9B1", "query": "methylates p53 and inactivates its transcriptional activity", "n": 1}` | 282 | 1 |
| 14 | `show_evidence` | listed | `11 claims, themes: Degradation, Inactivating modification, Reduced expression` | 23 | 0 |

Raw records: [`trace.jsonl`](trace.jsonl).
