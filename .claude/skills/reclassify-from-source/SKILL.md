---
name: reclassify-from-source
description: Use this skill when the user wants to upgrade citation classifications from S2-context-based to source-text-based. For each citation marked needs_review (or any citation the user wants to verify) in scholar/<slug>/citation/<tag>/citations.yaml, downloads the citing paper's source via ingest-paper, locates the actual \cite{} site for the cited paper, extracts a richer surrounding-text context, and re-runs classification. Closes the dominant failure mode of classify-citations on biomedical/clinical papers (~42% needs_review baseline).
---

# reclassify-from-source

The citation pipeline has three steps: discover (pull metadata from S2), classify (label by reading S2 contexts), reclassify-from-source (this skill — go to ground truth when S2 contexts are missing or weak).

S2's automatic context extraction is uneven. Validation across 18 of Xinhai's papers showed 76/181 citations (42%) ended up tagged `needs_review` because S2 surfaced no surrounding text. The fix is to download the citing paper itself and read the actual `\cite{}` location.

## When to run this skill

Default trigger: any citation with `classification.tags == ["needs_review"]`.

Optional broader scope:
- Citations where `classification.method == "auto_heuristic"` (drive_by from bracket regex) — usually solid but a sample audit can validate.
- Citations the user manually flags as borderline.
- Re-running after adding a new substantive tag to the taxonomy.

## Inputs

- A scholar slug (default: only one in `scholar/`).
- Optional: a paper tag to limit the run.
- Optional: `--include-borderline` to also re-process auto-classified citations.
- Optional: `--max-downloads N` to cap how many citing papers get fetched (so the user can run incrementally).

## Recommended prerequisite

Run `ingest-gs-citations` first if the user has GS Chrome extension exports in `~/Downloads/`. That skill adds `gs_pdf_url` fields that this skill prefers as a download path — bypasses many publisher paywalls via OA mirrors. Without it, we fall back to arxiv-only + Nature OA patterns.

## Storage convention

Citing papers go in `scholar/<slug>/citing_papers/<citing_tag>/` — **parallel to but separate from** `publications/`, which holds the scholar's own work. Keeping them in different trees prevents conflating "papers the scholar wrote" with "papers that cite the scholar."

The `<citing_tag>` follows the standard `ingest-paper` convention: `{short_slug}_{arxiv_id}` if available, else `{short_slug}_{year}`.

```
scholar/xinhaihou_umich/
├── publications/<paper_tag>/              # Xinhai's own papers
├── digest/<paper_tag>/
├── citation/<paper_tag>/citations.yaml
└── citing_papers/<citing_tag>/            # papers that cite Xinhai
    └── source/                            # populated by ingest-paper
```

**Edge case: the citing paper is also one of the scholar's own papers.** Active research groups self-cite frequently — Xinhai's "Intelligent histology" cites several of his earlier works; CodeV cites his neuroimaging papers. When the citing paper appears in `papers.yaml`, ingest it to `publications/<tag>/source/` (the canonical home for the scholar's work) rather than duplicating into `citing_papers/`. Before downloading, always check `publications/<paperId-or-arxiv-resolved-tag>/source/` first. The `source_paper_tag` field in the classification block can point to either tree.

## Step 1 — Build the work queue

For each scholar paper, read `citation/<tag>/citations.yaml` and collect every citation entry where `classification.tags == ["needs_review"]` (or matches the broader scope flag).

Two important deduplications:
1. **By citing s2_paper_id.** A single citing paper may appear in multiple of Xinhai's `citations.yaml` files (e.g., "Intelligent histology" cites several of Xinhai's papers). Download once, then look up each `\cite{}` site separately within the same source.
2. **By failed downloads.** Maintain a small `scholar/<slug>/citing_papers/.failed.yaml` so we don't retry papers we know are unretrievable (paywalled clinical journals, dead links).

## Step 2 — Download each citing paper

Tier order (try each, fall through on failure):

1. **arxiv tex** (`citing.arxiv_id`) — cleanest for `\cite{}` matching. Always preferred when available.
2. **`gs_pdf_url`** (from `ingest-gs-citations`) — pre-resolved OA mirror from the Chrome extension. Usually a Nature `_reference.pdf`, PMC paper, ResearchGate copy, or Frontiers PDF. Bypasses the paywall the publisher's HTML landing page would show.
3. **PMC scrape** (`citing.pmcid`) — fetch the PMC landing page, find the `pdf/<filename>.pdf` link, download. PMC's `/pdf/` shortcut returns HTML, you must scrape the page.
4. **Nature OA suffix** (`citing.doi` for known-OA Nature journals like `s41467-`, `s41746-`, `s43856-`, `s41377-`) — try both `articles/<suffix>.pdf` and `articles/<suffix>_reference.pdf`.

Skip if `scholar/<slug>/citing_papers/<citing_tag>/source/` already has a tex tree or `paper.pdf` (idempotent).

**Cloudflare-protected hosts** (biorxiv, medrxiv, some publishers): JS-based bot challenges. Don't fight them programmatically — fall through to "user manual" via the manual_download_queue.

If the download fails (HTTP error, paywall, malformed source), append to `.failed.yaml` with the citing paper's s2_paper_id, title, and reason. Don't crash the whole run.

## Step 3 — Match \cite{} keys to the target paper

Once a citing paper is ingested, identify which bibliography key(s) refer to Xinhai's paper. Two paths depending on source format:

### tex source

Find the `.bbl` file or the bibliography section in the main tex. Each entry looks like:
```latex
\bibitem{kondepudi2024}
A. Kondepudi, M. Pekmezci, X. Hou, et al.
\newblock Foundation models for fast, label-free detection of glioma infiltration.
\newblock {\em Nature}, 2024.
```

Match by, in order:
1. **Arxiv ID** appears in the bibitem (most papers include `arxiv:2206.08439` or similar). Direct match.
2. **DOI** appears in the bibitem.
3. **Title keywords + author surname**. Use a fuzzy substring match — e.g., for `cns_lymphoma_2024`, search for "primary CNS lymphoma" + "Reinecke". Be lenient with hyphenation, line wrapping, and capitalization.
4. **Title keywords alone** if no author overlap (rare — usually means a different paper with similar title).

If multiple bibitems match, that's a sign the citing paper cites both a preprint and the published version of Xinhai's paper. Treat both as valid sites and combine their contexts.

### PDF source (no tex)

The bibliography is harder to parse. Use:
1. Convert PDF to text (`pdftotext` is reliable; `pypdf` is a fallback).
2. Locate the `References` / `Bibliography` section.
3. Match by arxiv ID, DOI, or `title + first_author_surname` substring search.
4. Determine the reference number (the `[N]` index in the citing paper's numbering).

If the bibliography number can't be confidently determined, mark the citation `[needs_review]` with rationale "could not match cited paper to any bibitem in the source" and move on. Don't guess.

**PDF extraction gotchas observed in validation:**

- **Reference numbers can disappear** when refs are laid out in columns. In a Nature Comms PDF, refs 1–17 came out as just `"1.\n2.\n3.\n..."` (numbers in one column block) followed by `"<entry>\n<entry>\n..."` (entries in another). When the explicit `^N\.` prefix is missing, count entries from the start of the References section to determine each ref's number.
- **Body superscript cites are ambiguous.** `text11` could be a citation, a page number, a supplementary table number, or part of a chemical formula. Heuristics that worked:
  - Citations usually appear as runs like `text9–11,15,16` (comma-separated, possibly with en-dashes for ranges, no whitespace between text and digit).
  - Citations cluster mid-sentence, not at the start of headings, table captions, or figure labels.
  - When in doubt, search for the specific `[N,M,P]` pattern around domain-relevant prose ("prior studies", "we follow", etc.).
- **Ligature normalization.** Nature-style PDFs use Unicode ligatures (`ﬁ`, `ﬂ`) that may not match plain `fi`/`fl` searches. Normalize before matching titles.
- **Two-column layouts** can interleave text from adjacent columns when extracted naïvely. Use `pdftotext -layout` if standard extraction produces garbled text mid-paragraph.

If you encounter any of these, prefer to spend a few minutes with the raw PDF (open it, find the cite manually) over building elaborate parsers. The skill is meant to handle the common case automatically and the long-tail manually.

## Step 4 — Extract the actual citation site(s)

Once you have the bibitem key (tex) or the reference number (PDF), find every place it's used:

### tex
```bash
grep -n "\\\\cite[a-z]*{[^}]*kondepudi2024[^}]*}" main.tex
```
Match `\cite`, `\citep`, `\citet`, `\cite*`, `\citeyear`, etc. The key may appear alongside other keys: `\cite{kondepudi2024, jiang2023}` — that's a bracket-cluster pattern, important to preserve.

### PDF text
Search for the reference number `[N]` or `(Surname, YYYY)` depending on citation style. Be careful with collisions — `[12]` may appear in many places, including non-citation contexts (figures, tables). Use surrounding "et al." / parentheses / square-bracket conventions to confirm.

For each match, extract:
- **The full sentence** containing the cite (cross-sentence boundaries via `.`/`!`/`?` followed by capitalization).
- **One sentence before and after** for paragraph context (helps disambiguate brief mentions).
- **The other co-cited keys** if present (`\cite{a, b, c}` — list `b` and `c` in metadata; signals drive-by even if the surrounding text looks substantive).

Save these as `source_contexts` on the citation entry.

## Step 5 — Re-classify with richer text

Run the same taxonomy from `classify-citations`. The `source_contexts` are usually 3–5× longer than S2 contexts and include the surrounding paragraph, so the classification rules apply more cleanly.

Two new patterns this richer text often surfaces:

- **"In Section 3 we follow the [protocol from N]"** — locking down `methodology` claims that S2 contexts didn't capture.
- **"Our work differs from [N] in that we ..."** — usually `inspired_by + acknowledgment` (extension framing).

Update the citation's `classification` block:

```yaml
classification:
  tags: [<refreshed tags>]
  rationale: <new rationale referencing source_contexts>
  method: from_source
  classified_at: <YYYY-MM-DD>
  source_paper_tag: <citing_tag>          # link to where we stored the source
  bibitem_key: <kondepudi2024>            # tex case
  bibitem_number: 21                       # PDF case
  co_cited_keys: [jiang2023, lyu2024]    # other keys cited in the same brace, if any
```

Also add the extracted text:

```yaml
source_contexts:
  - paragraph: <full paragraph containing the cite>
    sentence: <the specific sentence>
    co_cited: [jiang2023, lyu2024]
```

This is additive — the original S2 `contexts` field stays. Future passes can compare S2 vs source extraction to estimate S2's recall.

## Step 6 — Handle the genuinely unretrievable

For citations whose citing paper:
- Couldn't be downloaded (paywalled, dead link, no OA version), OR
- Was downloaded but no bibitem could be matched (rare, indicates an indexer false-positive in S2 — the paper doesn't actually cite the target)

Update the classification with a more specific status:

```yaml
classification:
  tags: [needs_review]
  rationale: "Citing paper unretrievable: <reason>" | "No matching bibitem found — likely S2 false-positive citation."
  method: source_unavailable | source_no_match
  classified_at: <YYYY-MM-DD>
```

Don't fan out indefinitely on these. The user can manually inspect a list of `source_unavailable` cases later.

## Step 6.5 — Cross-paper propagation (a free win)

Once you've downloaded a citing paper and parsed its bibliography, **search for ALL of the scholar's papers in that bibliography**, not just the one that triggered the download. A single citing paper often cites multiple of the scholar's works — for active research groups, intro paragraphs commonly bundle 3–5 of the lab's prior papers in one `\citep{...}` cluster. One download yields data for many `citations.yaml` files.

For each scholar paper found in the bib:

1. **If `citations.yaml` already has this citing paper as an entry**: update its classification + add `source_contexts`. (Standard reclassify path.)
2. **If `citations.yaml` does NOT have this citing paper**: ADD a new entry. S2's `discover-citations` missed it — opportunistically close the discover gap. Mark the entry with `source: from_source_extraction` (not `semantic_scholar`) and bump `stats.total_citations_added_from_source` so the user can see the discovery delta.

This path is high-yield for the scholar's own internal citation graph — validation showed CodeV's intro alone cited 4 of Xinhai's papers in one parenthetical, and S2 had only surfaced 3 of those 4.

## Pattern: self-curated related-work bundle

When a scholar cites several of their own group's papers in a single bracket cluster, that's almost always **drive_by** — an "we've also been working on related stuff" parenthetical, not substantive engagement. Detect this when the same citing paper shows up across multiple of the scholar's `citations.yaml` files at the SAME source location (`location` field matches). Flag in the rationale so the user sees the pattern explicitly. Don't auto-elevate self-bundle drive-bys to `acknowledgment` just because they're self-cites — the surrounding paragraph determines the tag, not the author overlap.

## Step 7 — Run summary

Report:
- Citations targeted (queue size after dedup)
- Citing papers downloaded (new / cached / failed)
- Citations reclassified successfully
- Tag distribution before vs after — show how `needs_review` count dropped and where those citations migrated to
- Remaining `needs_review` count and breakdown (`source_unavailable` vs `source_no_match` vs unprocessed)

A useful presentation:
```
needs_review:    76 → 12  (-64)
acknowledgment:  38 → 71  (+33)
methodology:     20 → 38  (+18)
drive_by:        52 → 65  (+13)
baseline:         4 →  9  (+5)
inspired_by:      3 →  4  (+1)
```

## Why arxiv source matters more here than for ingest-paper

In `ingest-paper`, tex is preferred because it makes the paper easier to *read*. In `reclassify-from-source`, tex is preferred because `\cite{}` keys are explicit and matchable — PDF text extraction loses the citation marker structure (`[N]` becomes ambiguous, especially in survey papers with hundreds of refs). So when a citing paper has both arxiv and DOI, **always prefer arxiv**, even if the published version is more recent.

## What this skill does NOT do

- **Does not change the taxonomy.** Same five substantive tags + drive_by + needs_review. The skill just upgrades the data quality, not the labels.
- **Does not re-run S2 citation discovery.** If a citation is missing entirely from S2, that's `discover-citations` territory — re-run that first with `--refresh`.
- **Does not analyze the citing paper deeply.** We extract one or a few citation sites, not the full paper. Don't do Pass 1 / Pass 2 on the citing paper unless explicitly asked — that's `ingest-paper` + `deep-read-paper`.
- **Does not auto-classify reverse citations.** "Papers Xinhai cites" is a different graph; this skill is one-directional.

## Implementation order

When invoked, run roughly:

1. Build queue (Step 1) — fast, just yaml reads.
2. Surface counts to user: "Will attempt to download N unique citing papers (M after caching). Confirm?" The download is the slow + bandwidth-using step; checkpoint here.
3. Download in batches, respecting `--max-downloads` cap.
4. Match + extract per citation (Steps 3–4) — this is where most of the per-citation effort goes.
5. Reclassify (Step 5) — same logic as `classify-citations`.
6. Update citations.yaml + write summary.

Resume-friendly: the skill should be safe to re-run if interrupted. Cached sources (Step 2 idempotent), already-reclassified entries (skip if `method == from_source`), and `.failed.yaml` (skip retries) all make this work.
