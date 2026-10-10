MDM2 controls TP53 through a negative feedback loop. It binds TP53, blocks its transactivation domain and ubiquitinates it for proteasomal degradation, and TP53 in turn switches on the MDM2 gene. Stress signals release TP53 by blocking MDM2. In cancer, too much MDM2 silences TP53 that is not itself mutated.

### How the regulation works
- **The core mechanism.** MDM2 is an E3 ligase that ubiquitinates TP53 for proteasomal degradation, and it also binds TP53's activation domain [qdrant:Q00987#0, reaction:R-HSA-6804879].
  - UniProt cites two studies for this. One isn't in the literature store (PMID 29681526).
  - The other, **PMID 21317885**, shows that the E4 ligase UBE4B joins MDM2 and promotes TP53 polyubiquitination and degradation [pubmed:21317885].
- **Binding and the feedback loop.**
  - **PMID 9131587** maps the p53-binding site on MDM2 [pubmed:9131587].
  - **PMID 8064888** describes MDM2 inhibiting p53 transactivation in a dose-dependent way, and p53 transactivating the MDM2 gene [pubmed:8064888].
- **MDMX (MDM4) as partner.** **PMID 10827196** finds that MDMX does not degrade p53 itself. Its RING domain pairs with MDM2's, which stabilises both MDM2 and p53 while keeping p53 transcriptionally silent. This holds a pool of inactive p53 ready for stress [pubmed:10827196, qdrant:O15151#2].

### Conditions that release TP53 from MDM2
- **DNA damage.** After DNA damage, p53 is phosphorylated at its N-terminal serines. **PMID 10202144** shows that phosphorylation at Ser20 blocks MDM2 binding, and that a p53 which can't be phosphorylated there is degraded faster [pubmed:10202144].
- **Ribosome stress.** **PMID 23874713** shows that the ribosomal proteins RPL37, RPS15 and RPS20 bind MDM2 and inhibit its ligase activity. This stabilises p53 and also lowers MDMX levels [pubmed:23874713].
- **RB.** In **PMID 10078201**, RB binding to MDM2 blocks MDM2's degradation of p53 and restores p53-driven apoptosis, though not p53-driven transcription [pubmed:10078201].
- **Recorded in the graph:** p14ARF (CDKN2A) inhibits "MDM2 ubiquitinates TP53" [reaction:R-HSA-6804879], and PHF20 inhibits "MDM2 binds TP53" [reaction:R-HSA-5633460].

### Diseases
- **Sarcomas.**
  - **PMID 1614537** cloned human MDM2, mapped it to 12q13-14, and found it amplified in over a third of 47 sarcomas, including bone and soft-tissue tumours. Amplification lets these tumours escape p53 control [pubmed:1614537].
  - **PMID 8064888** studied the relationship between MDM2 amplification and TP53 status in sarcomas [pubmed:8064888].
- **Many tumour types.** **PMID 9671804** pooled 3,889 tumours across 28 types.
  - MDM2 was amplified in 7% overall. The highest rates were soft-tissue tumours (20%), osteosarcoma (16%) and oesophageal carcinoma (13%).
  - Amplification and p53 mutation rarely occurred together: 29 of 33 MDM2-amplified tumours had normal p53 [pubmed:9671804].
- **Glioblastoma.** **PMID 10074928** found alternatively spliced MDM2 lacking the p53-binding domain in 7 of 16 GBM cultures. Its presence correlated with stabilised normal p53, even where MDM2 was also amplified [pubmed:10074928].
- **Brain tumours (medulloblastoma and ependymoma).** TP53 is rarely mutated in these tumours but often inactive. **PMID 21317885** links that to amplification and overexpression of UBE4B, which drives MDM2-mediated degradation of p53 [pubmed:21317885].
- **Lung adenocarcinoma risk.** **PMID 24175836** studied 764 patients and 983 controls, all Chinese women who don't smoke.
  - The MDM2 SNP309 GG genotype raised risk (adjusted OR 1.68).
  - Combined with p53 Pro/Pro (Arg72Pro), the odds ratio was 2.66.
  - Cooking-oil fumes, fuel smoke and passive smoking further raised risk in carriers [pubmed:24175836].

Everything above comes from the retrieved abstracts and function text. Drug approaches that block MDM2 binding, such as nutlins, are well known but didn't come up in these searches, so they are left out.
