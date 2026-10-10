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
