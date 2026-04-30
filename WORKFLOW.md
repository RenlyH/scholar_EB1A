# Citation pipeline workflow

End-to-end workflow for building a scholar's citation graph and meaningfulness classification. Skills live in `.claude/skills/`; runners live in `scripts/`.

## Pipeline

```
┌─────────────────┐
│  setup-scholar  │  (auto, required)  →  papers.yaml + folder skeleton
└────────┬────────┘
         ▼
┌──────────────────────────┐
│  ingest-gs-citations *   │  (manual + auto, OPTIONAL but HIGHLY ENCOURAGED)
│  - User runs Claude      │
│    Chrome extension      │
│    on each GS profile    │
│    "Cited by" page       │
│  - Drops markdown into   │
│    ~/Downloads/          │
│  - Runner merges into    │
│    citations.yaml        │
└──────────┬───────────────┘
           ▼
┌──────────────────────┐
│  discover-citations  │  (auto, required)  →  pulls S2 contexts/intents/isInfluential
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│  classify-citations  │  (auto, required)  →  labels by reading S2 contexts
└──────────┬───────────┘
           ▼
┌──────────────────────────┐
│  reclassify-from-source  │  (auto, recommended)  →  downloads citing papers,
│                          │   finds \cite{} sites, reclassifies on real text
└──────────────────────────┘
```

`ingest-gs-citations` is technically optional but provides ~25% more citation graph coverage and pre-resolved OA download URLs. **Strongly run it** if you can.

## Why each step

- **setup-scholar:** establishes the canonical paper list from Google Scholar (the scholar's curated truth).
- **ingest-gs-citations:** brings in the citation graph from GS (more comprehensive than S2) and OA download URLs (closes paywall gaps).
- **discover-citations:** S2 has the only useful citation-context API. We need its `contexts`, `intents`, `isInfluential` fields for the initial classification pass.
- **classify-citations:** label each citation with the meaningfulness taxonomy (drive_by / acknowledgment / baseline / methodology / inspired_by).
- **reclassify-from-source:** for any citation the first pass couldn't resolve confidently, download the citing paper itself and re-classify on real surrounding text.

## Concrete commands

```bash
# 1. Set up scholar (one-time per scholar)
#    [Currently no runner — done via skill / manual config]

# 2. (User step) Run Claude Chrome extension on each GS "Cited by" page.
#    Files land in ~/Downloads/{NN}_{slug}_citations.md.
#    Move them into the project so they're tracked:
mv ~/Downloads/*_citations.md xinhaihou_umich/gs_exports/

# 3. Ingest those files (auto-detects xinhaihou_umich/gs_exports/)
uv run --with pyyaml scripts/ingest_gs_citations.py

# 4. Pull S2 metadata (currently done via skill / manual S2 API calls)

# 5. Classify (currently done via skill — Claude reads contexts)

# 6. Reclassify from source — automated download + reclassification
uv run --with pyyaml --with pypdf scripts/reclassify_from_source.py

# 7. Generate the Chrome download queue for what curl couldn't fetch
uv run --with pyyaml scripts/generate_chrome_download_queue.py
# → produces xinhaihou_umich/chrome_download_queue.md
#   Slim — just URLs grouped by host. Feed to Claude Chrome.
#   Chrome saves PDFs to ~/Downloads/ with publisher-default filenames.

# 8. Move downloaded PDFs into the right citing_papers slots
uv run --with pyyaml scripts/move_chrome_downloads.py
# → matches each PDF by URL fingerprint (PII / DOI / etc. in filename),
#   moves to citing_papers/<slug>/source/paper.pdf

# 9. Re-run reclassify_from_source.py — picks up newly-cached PDFs (idempotent)
uv run --with pyyaml --with pypdf scripts/reclassify_from_source.py
```

## Validation results (2026-04-27, scholar=xinhaihou_umich)

After full pipeline run on 18 papers:

- **243 citations** in the graph (was 182 before GS ingest; +61 from GS extension)
- **60 substantive** engagements (acknowledgment + baseline + methodology + inspired_by)
- **32 from_source** reclassifications (real text, not just S2 contexts)
- **47 source_unavailable** (paywalled — needs UMich library or won't be obtainable)
- **106 needs_review** (overlaps with source_unavailable; these are entries pending source data)

## Paywall handling

Some citing papers can't be downloaded programmatically (paywalled clinical journals, biorxiv/medrxiv with Cloudflare bot challenges). For these:

1. Run the runner; entries get marked `source_unavailable`
2. (Optional) Generate `manual_download_queue.md` listing them with publisher URLs
3. User opens publisher URL in browser (UMich SSO handles authentication)
4. User saves PDF to the expected `citing_papers/<slug>/source/paper.pdf` path
5. Re-run the runner; it picks up the new sources automatically

## Skill descriptions (for invocation)

| Skill | Type | Trigger |
|---|---|---|
| `setup-scholar` | required | Per new scholar |
| `ingest-gs-citations` | optional++ | When `~/Downloads/*_citations.md` files exist |
| `discover-citations` | required | After paper list is set |
| `classify-citations` | required | After contexts are pulled |
| `reclassify-from-source` | recommended | After first classification pass leaves needs_review entries |

## Onboarding a new scholar — checklist

Lessons learned from `xinhaihou_umich` (18 papers, 243 citations). Use this as a per-scholar bootstrap script.

**Inputs to collect from user upfront:**
- Google Scholar URL (or `user=XYZ` ID)
- ORCID
- Affiliation (drives folder name `<lastname>_<institution>/`)
- **PI + 3-5 frequent collaborators** — needed for lab-self-cite detection during significance ranking. Save into `profile.yaml` as `collaborators: [...]`.

**Reuse the existing `.env` S2 API key** — single key works for any scholar.

**Expected metrics (baseline from Xinhai's run):**
| Metric | Value |
|---|---|
| GS / S2 citation count gap | ~25% (GS extension closes it) |
| `needs_review` after S2 contexts only | ~50% |
| `needs_review` after reclassify-from-source | ~37% |
| `drive_by` share | ~40-50% |
| substantive (acknowledgment+methodology+baseline+inspired_by) | ~25% |
| `inspired_by`-grade external cites for mid-career scholar | 5-10 |

**When ranking "top N most significant citations":**
1. Compute three labels per citation: `self_cite` (citing paper's tag is in scholar's `papers.yaml`), `lab_self_cite` (PI/collaborator overlap with citing-paper authors), `external`. The S2 `is_self_citation` flag alone is insufficient.
2. Web-search the candidate citing papers to verify peer-reviewed venue. arxiv-listed venue ≠ published; many arxiv preprints are later accepted at MICCAI/CVPR/etc. but the YAML still says "arXiv.org".
3. Tier the output: peer-reviewed top venue (Nature/CVPR/MICCAI main) > peer-reviewed journal/workshop > preprint only.
4. Sort: external > lab_self > self; within each, by classification rank then venue tier.

**Common pitfalls observed:**
- biorxiv/medrxiv hit Cloudflare bot challenge → defer to Chrome extension
- Frontiers GS-extension URLs may be PMC mirrors (DOI-based fingerprinting needed)
- MDPI uses ISSN-based URLs, not journal-slug
- Slug collisions when multiple papers share `<surname>_yx` pattern — use MD5-hash of title for fallback (handled by `scripts/move_chrome_downloads.py`)
- `is_self_citation` from S2 is unreliable; cross-check via scholar's own `papers.yaml`
