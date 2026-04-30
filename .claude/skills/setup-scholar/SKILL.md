---
name: setup-scholar
description: Use this skill when the user wants to set up a new scholar in scholar/ — given a Google Scholar URL, an ORCID, or just a name+affiliation. Resolves cross-platform IDs, builds a canonical paper list from Google Scholar (ground truth), enriches with Semantic Scholar metadata (paperId + arxiv_id + DOI), and creates the publications/, digest/, citation/ folder skeleton. Does NOT ingest papers — that's the next skill.
---

# setup-scholar

Bootstrap a scholar's directory under `scholar/<slug>/` so per-paper skills (ingest-paper, discover-citations) have a manifest to work from.

## What this skill does

1. Resolves the scholar across Google Scholar / ORCID / Semantic Scholar / OpenAlex.
2. Fetches the canonical paper list from Google Scholar (ground truth — manages their own profile).
3. Cross-references with Semantic Scholar to attach `paperId`, `arxiv_id`, `doi` to each paper.
4. Writes `profile.yaml` and `papers.yaml`.
5. Creates empty `publications/<tag>/`, `digest/<tag>/`, `citation/<tag>/` folders for every paper.

It does **not** download paper sources — that's `ingest-paper`'s job. This skill produces the manifest that downstream skills iterate over.

## Why Google Scholar is the source of truth

S2 and OpenAlex both maintain author-disambiguated profiles, but for any moderately common Chinese/Korean/Indian name they conflate multiple authors. (For Xinhai Hou, both S2 and OpenAlex mixed in a materials-science author from Kunming.) The scholar curates their own GS profile, so it's the cleanest signal of "what's mine."

GS itself has no API and aggressively blocks scrapers. We hit it exactly once per scholar (just the profile page) — that's tolerated. We do NOT use GS for citation discovery (that's S2's job — see `discover-citations` skill).

## Inputs

One of:
- Google Scholar URL: `https://scholar.google.com/citations?user=<id>&hl=en`
- ORCID: `0000-0000-0000-0000`
- Name + affiliation: `Jane Doe, MIT`

Strongly prefer the GS URL. ORCID is useful for cross-checking. Name+affiliation alone works but takes more disambiguation.

## Step 1 — Pick the slug

`<firstname><lastname>_<affiliation_short>`, lowercase, no separators inside the name parts. Examples: `xinhaihou_umich`, `janedoe_mit`, `liuwei_tsinghua`.

If the user gives a different style, follow theirs.

## Step 2 — Read S2 API key

`SEMANTIC_SCHOLAR_API_KEY` lives in `scholar/.env` (gitignored). Load it before any S2 call. Without it, fall back to anon S2 (~1 req/sec — fine for 18 papers, painful when fanning out to citations later).

```bash
set -a; . scholar/.env; set +a
```

## Step 3 — Fetch the Google Scholar profile

Hit `https://scholar.google.com/citations?user=<id>&hl=en&cstart=0&pagesize=100` with a real browser User-Agent. Parse `<tr class="gsc_a_tr">` rows: each gives title, authors, venue, year, citation count.

If the scholar has >100 papers, paginate with `cstart=100`, etc. Most don't.

If the input is ORCID or name-only (no GS URL), search S2/OpenAlex first to find the scholar's GS ID (sometimes available in OpenAlex's `ids` field), then fetch GS as above. If no GS profile exists, fall back to S2 author papers as the canonical list (mark this in `profile.yaml` so future runs know).

## Step 4 — Resolve external IDs

In parallel:
- **OpenAlex author**: `https://api.openalex.org/authors?search=<name>` → pick best match by affiliation overlap.
- **Semantic Scholar author**: `https://api.semanticscholar.org/graph/v1/author/search?query=<name>` (with API key) → pick by paperCount + paper-title overlap with GS list.

Record both IDs in `profile.yaml` even if conflated — they're useful for cross-reference. Add a comment when conflation is detected.

## Step 5 — Enrich each GS paper with S2 metadata

For each paper from GS, search S2:
```
GET /graph/v1/paper/search?query=<title-keywords>&fields=title,year,externalIds,authors&limit=3
```

Match the top result by:
- Title similarity (high)
- Year (within ±1)
- Author-name overlap (Xinhai or X. Hou must appear)

Extract `paperId`, `externalIds.ArXiv`, `externalIds.DOI`. If S2 doesn't have it (rare for ML, common for clinical journals), leave fields null.

Some GS entries are conference abstracts or duplicates of preprints. Keep them as separate entries — they have distinct citation graphs and the user might want to track each.

## Step 6 — Pick a tag for each paper

Convention (mirrors `ingest-paper`):
- `{short_slug}_{arxiv_id}` if arxiv-available, e.g. `hidisc_2303.01605`
- `{short_slug}_{year}` otherwise, e.g. `glioma_foundation_2024`

Slug rules:
- 1–4 lowercase words separated by underscores
- Derived from the paper's central concept, NOT the full title
- Use the paper's own short name when it has one (HiDisc → `hidisc`, OpenSRH → `opensrh`, CodeV → `codev`)

Tags must be unique within the scholar's `papers.yaml`.

## Step 7 — Write profile.yaml

```yaml
name: <full name>
affiliation: <institution full name>
affiliation_short: <slug suffix used>
slug: <full slug>

ids:
  google_scholar: <id>
  google_scholar_url: <url>
  orcid: <id or null>
  semantic_scholar_author: "<id>"   # quoted — these are numeric strings
  openalex_author: <id>

# Note any conflation observed.

date_created: <YYYY-MM-DD>
```

## Step 8 — Write papers.yaml

```yaml
papers:
  - tag: <tag>
    title: <title>
    year: <year>
    venue: <venue>
    arxiv_id: "<id>" | null      # quoted — preserves leading zeros / dots
    doi: <doi> | null
    s2_paper_id: <hex>
    role: first_author | co_author
  - ...
```

Order by GS default (citation count, descending) — that's how the user thinks about their own papers.

## Step 9 — Create folder skeleton

```bash
for tag in $(yq '.papers[].tag' papers.yaml); do
  mkdir -p publications/$tag digest/$tag citation/$tag
done
```

Three top-level folders, one subfolder per paper tag. Empty for now — `ingest-paper` will populate `publications/<tag>/source/`, etc.

## Output

Report to the user:
- The chosen slug and folder path
- Total papers found in GS
- Count with arxiv source available (these can use the tex pipeline)
- Count without arxiv source (will need PDF ingestion)
- Any conflation warnings from S2/OpenAlex
- Recommended next step: run `ingest-paper` per-paper, prioritizing arxiv ones first

## What this skill does NOT do

- Download paper PDFs or tex sources (→ `ingest-paper`)
- Fetch citing papers (→ `discover-citations`)
- Read/summarize papers (→ `ingest-paper` Pass 1)
