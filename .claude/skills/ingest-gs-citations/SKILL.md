---
name: ingest-gs-citations
description: Use this skill when the user has run the Claude Chrome extension to export Google Scholar's "Cited by" pages and wants to merge that data into the citation pipeline. Reads ~/Downloads/{NN}_{slug}_citations.md files, matches each to one of the scholar's papers, and updates citation/<tag>/citations.yaml — adds new entries for cites S2 missed, attaches gs_html_url and gs_pdf_url to existing entries. Optional but highly encouraged step.
---

# ingest-gs-citations

The Chrome extension produces a richer citation graph than Semantic Scholar (Google Scholar coverage is ~25% higher) and pre-resolves PDF URLs to OA mirrors that bypass paywalls. This skill merges that data into the pipeline.

## When to run

**Strongly encouraged before** `discover-citations` and `reclassify-from-source`. The pipeline still works without it, but you lose:
- ~20% of the citation graph (papers GS surfaces but S2 missed)
- ~50% of the paywall-blocked downloads (GS provides OA mirror URLs)

If `~/Downloads/*_citations.md` files exist when this skill is invoked but the pipeline hasn't been told about them, surface a one-line prompt: "Want me to ingest these first?"

## Inputs

- A scholar slug (default: only one in `scholar/`)
- Directory of GS-export markdown files (default: `~/Downloads/`)
- Pattern for matching: `{NN}_{slug}_citations.md`

## File format (produced by the Chrome extension)

```markdown
# Papers Citing "<Xinhai's paper title>"

Source: Google Scholar (Xinhai Hou's profile)
Original paper: <author list and venue>

## Citing Papers (N)

1. **<Title>**
   - <Authors> - <Venue>, <Year> - <domain>
   - HTML: <url>
   - PDF: <url>

2. ...
```

Some entries:
- Have only HTML, no PDF
- Have title `**` (empty) — skip these
- Have non-Latin titles (Chinese papers) — preserve as-is

## Step 1 — Match each markdown to a scholar paper

The filename prefix `{NN}_` aligns with `papers.yaml` row order (sorted by citation count, descending). But don't trust the prefix blindly — also parse the `# Papers Citing "<title>"` header line and fuzzy-match against `papers.yaml` titles. If the prefix and title disagree, trust the title.

If a markdown file can't be matched to any scholar paper, log a warning and skip — don't guess.

## Step 2 — Parse each citation entry

Extract for each numbered item:
- `title`: the bolded text after `N. **`
- `authors_venue`: the `- <text>` line (best-effort split into authors / venue / year)
- `gs_html_url`: from `- HTML: <url>`
- `gs_pdf_url`: from `- PDF: <url>` (may be absent)

Skip entries with empty title.

## Step 3 — Match each citation to existing citations.yaml or add new

For each scholar paper's `citations.yaml`:

1. Build an index of existing citations by normalized title.
2. For each parsed GS citation:
   - Try to match by **fuzzy title** (lowercase, strip punctuation, ≥6 consecutive words match) against existing entries.
   - **If matched**: attach `gs_html_url` and `gs_pdf_url` to the existing entry. Don't overwrite other fields.
   - **If not matched**: ADD a new citation entry with:
     ```yaml
     s2_paper_id: null              # unknown — could be backfilled later by S2 lookup
     title: <gs title>
     year: <parsed year or null>
     venue: <parsed venue or null>
     arxiv_id: null                 # parsed from gs URL if present
     doi: null                      # parsed from gs URL if present
     authors: [<list>]              # best-effort split
     citation_count_of_citing: null
     is_self_citation: <heuristic — does scholar's surname appear?>
     s2_signals: {is_influential: false, intents: []}
     contexts: []
     abstract: null
     gs_html_url: <url>
     gs_pdf_url: <url or null>
     classification: {tags: [needs_review], rationale: "Discovered via GS Chrome extension; S2 had not surfaced this cite. No contexts available — pending reclassify-from-source.", method: gs_extension, classified_at: <date>}
     ```

3. Try to extract `arxiv_id` / `doi` from the URLs:
   - arxiv: `arxiv.org/abs/<id>` or `arxiv.org/pdf/<id>`
   - DOI: `doi.org/<doi>` or `dx.doi.org/<doi>` or domain-specific patterns (e.g., `nature.com/articles/<doi-suffix>` → reconstruct `10.1038/<suffix>`)

## Step 4 — Refresh stats and write back

Update `stats.total_citations_gs_extension`, `stats.total_citations_added_via_gs`, etc. Distribution counts shouldn't change much (most new entries land in needs_review until reclassify runs).

## Step 5 — Run summary

Report:
- Files processed (e.g., 14/14)
- Citations added that S2 had missed
- gs_pdf_url attached to existing entries
- Any markdown files that couldn't be matched to a scholar paper

Recommend next: `reclassify-from-source` (which now has new download URLs to try).

## Idempotency

Re-running this skill should be safe:
- Already-attached `gs_pdf_url` on an entry: don't change unless URL differs.
- Already-added `gs_extension` entries: skip duplicate adds (match by title).
- New files in `~/Downloads/`: pick them up.

## What this skill does NOT do

- **Does not classify** — the new entries get `tags: [needs_review]`. Classification happens in `classify-citations` or `reclassify-from-source`.
- **Does not download papers** — that's `reclassify-from-source` (which can now use `gs_pdf_url`).
- **Does not infer S2 paperId** for new entries. A future skill could backfill via S2 search by title; for now, `s2_paper_id: null` is the marker that this came from GS not S2.
