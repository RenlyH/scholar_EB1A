# Citation refresh — Xinhai Hou (GS v3UWeDMAAAAJ)

Run: 2026-07-19. GS profile total at refresh time: **308** citations, h-index 10.
Export corpus went **231 → 307** entries (**+76 new citing papers**).

## Result per file

| file | before | after | new |
|---|---:|---:|---:|
| 01_foundation_models_glioma | 101 | 132 | +31 |
| 02_opensrh | 23 | 24 | +1 |
| 03_cns_lymphoma (incl. medRxiv row) | 22 | 24 | +2 |
| 04_hierarchical_discriminative | 22 | 23 | +1 |
| 05_valproic_acid | 19 | 20 | +1 |
| 06_self_supervised_wsi | 11 | 16 | +5 |
| 07_neuroimaging_health_system | 8 | 16 | +8 |
| 08_step_calibrated_diffusion | 6 | 6 | 0 (not rescraped — count unchanged) |
| 09_scalable_3d_medical | 4 | 13 | +9 |
| 10_correct_multi_agent | 3 | 9 | +6 |
| 11_super_resolution_biomedical | 3 | 4 | +1 |
| 12_health_system_learning | 2 | 4 | +2 |
| 13_foreground_virtual_staining | 2 | 2 | 0 (not rescraped) |
| 14_protocol_single_cell | 1 | 1 | 0 (not rescraped) |
| **15_codev** (new) | — | 11 | +11 |
| **16_intelligent_histology** (new) | — | 1 | +1 |
| **17_spinal_tumor_abstract** (new) | — | 1 | +1 |
| | **231** | **307** | **+76** |

## How to do the next refresh

See **WORKFLOW.md → "Refreshing gs_exports for a scholar already set up"**.
Tooling: `scripts/gs_cited_by_scrape.js` (browser) + `scripts/merge_gs_exports.py`
(disk). The gotchas learned on this run — GS re-clustering, `[PDF]` badge
prefixes breaking dedupe, CAPTCHA pacing, one-paper-many-rows — are written up
there, not here.

Command used for this run:

```bash
uv run scripts/merge_gs_exports.py --slug xinhaihou_umich \
  --fold-in 18_cns_lymphoma_medrxiv_citations.md=03_cns_lymphoma_citations.md
```

## Still open

- **CNS lymphoma** now has three GS rows (Neuro-Oncology 15 cites, a 0-cite
  duplicate row, medRxiv preprint 11). Both live rows were scraped and merged
  into `03`, de-duplicated by title → 24 unique.
- **Nature Medicine "Health system learning *enables* generalist neuroimaging
  models"** (2026, 0 cites) is on the profile but absent from `papers.yaml`.
  It's the journal version of arXiv 2511.18640 (`12`). Needs a tag, or an
  explicit merge with `neuroimaging_generalist_2511.18640`.
- `raman_spinal_2026` (npj Digital Medicine) has 0 citations — no export.
- Next pipeline step not yet run: `scripts/ingest_gs_citations.py` to merge these
  into `citation/<tag>/citations.yaml`, then discover → classify.
