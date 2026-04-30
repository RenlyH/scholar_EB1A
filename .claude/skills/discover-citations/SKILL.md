---
name: discover-citations
description: Use this skill when the user wants to find which papers cite a scholar's papers and capture the citation context. Reads scholar/<slug>/papers.yaml, hits Semantic Scholar /paper/{id}/citations for each paper, writes scholar/<slug>/citation/<tag>/citations.yaml with citing-paper metadata, S2 signals (isInfluential, intents), and the actual surrounding-text contexts. Reserves a `classification` field for a later fine-grained pass. Does NOT classify — that's classify-citations.
---

# discover-citations

Pull citation data for every paper in a scholar's manifest. Output: per-paper `citation/<tag>/citations.yaml` files with enough metadata that a downstream classifier (manual or LLM) can sort drive-by citations from substantive ones.

## Why this is hard, and why we use Semantic Scholar

Google Scholar reports the highest citation counts but has no API and blocks scrapers. Every other source under-counts somewhat.

For our purpose — distinguishing "related work [14-16]" mentions from substantive engagement — we don't actually need GS's count. We need the **surrounding sentences**. Semantic Scholar is the only free option that gives that:

- `contexts`: actual sentence(s) around each in-text citation
- `isInfluential`: their pre-trained classifier (Valenzuela et al. 2015) flagging "non-incidental" citations
- `intents`: coarse tags `background` / `methodology` / `result` (sparse — only ~30% of citations have any)

S2 will miss some citations — typically clinical-journal citations not indexed in CS-leaning sources. That gap is acceptable; document it in `stats.total_citations_gs_at_fetch` so the user can see how much was lost.

OpenAlex also has citation data but no contexts. OpenCitations / COCI similar. S2 is the only one that gives the in-text snippets, so it's the primary source.

## Inputs

- A scholar slug: e.g. `xinhaihou_umich`. Defaults to the only scholar present in `scholar/` if there's just one.
- Optional: a single paper tag (e.g. `hidisc_2303.01605`) to limit the run to one paper.
- Optional: `--refresh` flag to re-fetch even if `citations.yaml` exists (otherwise skip already-populated paper folders).

## Step 1 — Load scholar context

```python
profile = yaml.safe_load(open(f"scholar/{slug}/profile.yaml"))
papers  = yaml.safe_load(open(f"scholar/{slug}/papers.yaml"))["papers"]
api_key = read_env(f"scholar/.env", "SEMANTIC_SCHOLAR_API_KEY")
```

`profile.yaml` gives the S2 author ID and name variants — needed for self-citation detection.

## Step 2 — For each paper, fetch citations

Endpoint:

```
GET https://api.semanticscholar.org/graph/v1/paper/{paper_id}/citations
  ?fields=contexts,intents,isInfluential,
          citingPaper.paperId,citingPaper.title,citingPaper.year,
          citingPaper.authors,citingPaper.venue,citingPaper.externalIds,
          citingPaper.citationCount,citingPaper.abstract
  &limit=100
```

Note: `tldr` is **not** allowed as a sub-field on `citingPaper` — request will fail with `Unrecognized or unsupported fields: [tldr]`. Use `abstract` instead; LLM classifiers can handle that fine.

**Pagination.** If `data.next` is non-null, paginate with `offset=`. The API caps `limit` at 100. Most papers under 1000 citations are one or two requests.

**Rate limits.** With API key (`x-api-key` header): ~100 req/sec. Without: ~1 req/sec. Either way, sleep enough between calls to avoid 429s on the burst — `time.sleep(0.05)` between paged requests is plenty with a key.

Skip papers that don't have an `s2_paper_id` in `papers.yaml`. (Some clinical-only papers may not be indexed by S2 at all — note them in the run summary, don't crash.)

## Step 3 — Detect self-citations

A self-citation = the citing paper has the scholar as an author. Check:

```python
def is_self_cite(authors, profile):
    s2_id = profile["ids"]["semantic_scholar_author"]
    name_variants = {profile["name"], abbreviate(profile["name"])}  # e.g. "X. Hou"
    for a in authors or []:
        if a.get("authorId") == s2_id: return True
        if (a.get("name") or "").strip() in name_variants: return True
    return False
```

Self-citations are not "bad" — they're often the most substantive (the scholar reusing their own method). But the user should be able to filter them, so flag them clearly.

## Step 4 — Write citations.yaml

Schema (one file per cited paper, at `scholar/<slug>/citation/<tag>/citations.yaml`):

```yaml
cited_paper:
  tag: <tag>
  title: <title>
  s2_paper_id: <hex>
  arxiv_id: <id or null>

source: semantic_scholar
fetched_at: <YYYY-MM-DD>

stats:
  total_citations_s2: <int>
  total_citations_gs_at_fetch: <int or null>   # from papers.yaml note or GS scrape, if available
  influential_s2: <int>
  with_contexts: <int>
  self_citations: <int>

citations:
  - s2_paper_id: <hex>
    title: <str>
    year: <int>
    venue: <str>
    arxiv_id: <id or null>
    doi: <id or null>
    authors: [<name>, ...]
    citation_count_of_citing: <int>
    is_self_citation: <bool>
    s2_signals:
      is_influential: <bool>
      intents: [background | methodology | result]
    contexts:
      - <full surrounding-sentence string>
      - ...
    abstract: <str or null>
    classification:
      tags: null              # filled by classify-citations
      rationale: null
      method: null            # "manual" | "llm:<model-id>"
      classified_at: null
```

**Sort order:** influential first, then by `len(contexts)` descending, then by year descending. This puts the most signal-rich citations at the top so the user can scan them first.

## Step 5 — Run summary

After processing, report:

- Papers processed (count)
- Papers skipped (no s2_paper_id)
- Total S2 citations across all papers
- Sum of self-citations
- Sum of influential
- Any 429s or other errors retried

Recommend running `classify-citations` next.

## What this skill does NOT do

- **Does not classify** citations as drive-by / baseline / inspired-by — that's `classify-citations` (the next skill). The `classification` field is a stub for that.
- **Does not de-duplicate** preprint vs. published version. If the same lab cites a paper twice (once in arxiv, once in the conference version), both appear. Up to the classifier or a follow-up dedup pass to handle.
- **Does not pull GS counts.** If `total_citations_gs_at_fetch` is desired, copy from a manual GS check or a one-off scrape — don't bake GS scraping into this skill.
- **Does not download the citing paper's source.** That's a possible future pass if the user wants tex-level analysis (e.g. find the actual `\cite{}` location). For now, S2 contexts are enough signal for classification.

## On the "meaningfulness" classifier

S2's `isInfluential` is a useful but coarse first cut. Hand-checking on `hidisc_2303.01605` (validation run, 2026-04-27):

- 2 of 17 flagged influential — both clearly substantive (HSAT paper has 10 contexts referring to HiDisc method directly; HLSS uses HiDisc as background and follows its hyperparameters).
- Some non-influential citations are also substantive — e.g. "Step-Calibrated Diffusion" with `intents: [methodology]` explicitly names HiDisc as the method. So `isInfluential=false` does not mean "drive-by" — it means S2's classifier didn't flag it. Keep all citations; let the downstream classifier decide.

The downstream `classify-citations` skill should use the contexts (and abstract as fallback) as input, not just the S2 signals.
