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

## Filename → citation matching (move_chrome_downloads.py)

`move_chrome_downloads.py` is responsible for routing a user's `~/Downloads/*.pdf`
into the correct `citing_papers/<slug>/source/paper.pdf` slot. Real downloads
have wildly inconsistent filenames; the matcher tries strategies in order and
accepts the first that fires. If a download is not landing where expected,
trace through these in order — usually the fix is a new fingerprint rule for
the host or a new filename-shape fallback.

1. **URL fingerprint match.** For every gs_pdf_url / gs_html_url / DOI / arXiv
   on the citation, extract a publisher-specific identifier (PII for Elsevier,
   document-id for IEEE, DOI suffix for ACS/Wiley/Springer, NNT/HAL id for
   theses, etc.) and look for that string inside the lowercased filename.
   Highest precision; tried first. New publishers go in `URL_FINGERPRINTS`.
2. **Title-slug match against the filename** — for ACS/Wiley/Tandfonline
   patterns where the filename is the cleaned title.
   - Standard: ≥5 consecutive title-word probe inside the filename slug.
   - **Truncated-filename fallback**: ProQuest names dissertations
     `Title_Truncated_at_30_chars.pdf` (e.g. `LLM4SCM_A_Framework_for_Intel.pdf`
     where "Intel" is "Intelligent…"). Accept if the filename's first 25
     squashed chars are a prefix of the title's squashed form.
   - **Short-title fallback**: titles with <5 tokens (e.g. "Learning Patterns
     in Configuration") never reach the standard probe loop. Accept if the
     full title squashed form (≥18 chars) appears as a substring in the
     filename.
   - **Non-ASCII title fallback**: Korean/Chinese/Cyrillic citations
     (`PDTune: 파라미터 …`, `Least-to-Most 프롬프트 …`) lose almost all tokens
     in normalization. When the title contains non-ASCII characters, accept
     if the filename starts with the title's leading ASCII keyword.
3. **PDF page-1 fallback.** When neither (1) nor (2) match, open the PDF and
   try fingerprint + title match against its first page text. Catches:
   - Generic filenames like `out.pdf`, `Wang.pdf`.
   - Theses where the filename is an institutional code (`2025TLSES089.pdf`)
     but the title and HAL id are printed on the cover.
   - HAL deposit covers — the actual title is on page 2 because page 1 is the
     HAL banner ("hal id…", "submitted on…", "to cite this version"). The
     extractor combines pages 1+2 in that case.

When you change matching logic, run `--dry-run` first; the manifest size +
"matched + moved" tally tells you immediately if you over-broaden.

## NBSP author-parser trap (ingest_gs_citations.py)

Google Scholar emits non-breaking spaces (`\xa0`) around the dash separator
in author/venue lines: `S Sheoran\xa0-\xa0Procedia Computer Science`. The
original `parse_authors_line` split on the literal `" - "` (ASCII spaces) and
left the venue glued onto the author string, contaminating ~78% of entries
on the yunjiazhang_wisc onboarding run. Cascading effects:

- Wrong author surnames break `slug_for()`, producing `_<doi>` (empty surname)
  or junk surnames like `directions` (last word of the contaminated tail).
- `is_self_citation` and lab-self-cite checks fail because collaborator
  string match doesn't see clean names.
- Year/venue extraction yields nothing because the dash split returned 1 part.

The fix is in place (split on `\s+-\s+` after NBSP→space normalization).
For previously-ingested data, run `scripts/repair_authors.py --slug <slug>`
once — it re-parses each entry's authors_line from `gs_exports/`, updates
the citation rows in place, and renames affected `citing_papers/` folders
(e.g. `_<doi>` → `<surname>_<doi>`).

## Reclassify-from-source matching pitfalls (reclassify_from_source.py)

- **Bracket-numbered bibliographies.** `find_pdf_refnum` originally only
  recognized `15. Author...` format; many CS papers (and most ScienceDirect
  Procedia entries) use `[15] Author...`. Both are now accepted, and ref
  bodies are sliced to the next ref header so multi-line entries no longer
  bleed into the next ref's body.
- **gs_extension-only citations have `s2_paper_id: null`.** When writing a
  reclassification result back to citations.yaml, the writer originally did
  `next(c for c in citations if c.s2_paper_id == citing.s2_paper_id)`. With
  both sides None, that returns the **first** None-id row — almost never the
  one you wanted, so the update silently lands on a different citation. The
  writer now falls back to DOI / arxiv_id / fuzzy-title match when ids are
  null. If a citation's classification stays `[needs_review]` despite the
  citing paper being downloaded and ref number found, this null-match path
  is the first place to check.
- **ProQuest gateway returns the wrong PDF.** `https://search.proquest.com/openview/<hex>/1?...`
  without SSO can return a generic preview that happens to be a *different*
  paper (the dissertation matched by the search context, not the requested
  one). One known case during yunjiazhang_wisc onboarding: B Hu's paper slot
  ended up containing Yunjia's dissertation PDF. If the bib parser reports a
  refnum that contradicts the file's title, suspect this and delete
  `citing_papers/<bad_slug>/source/paper.pdf` before re-running the move.
