---
name: classify-citations
description: Use this skill when the user wants to classify citations of a scholar's paper into the meaningfulness taxonomy (drive_by / acknowledgment / baseline / methodology / inspired_by). Reads scholar/<slug>/citation/<tag>/citations.yaml, fills in the `classification` block per citation using the surrounding-text contexts as the primary signal, and writes the file back. Multi-label — a citation can be both `baseline` and `methodology`. Run after `discover-citations`.
---

# classify-citations

Decide what each citation is *actually doing* with the cited paper. The whole point: separate the dozens of `[14-16]` drive-by mentions from the few citations that genuinely engage with the work.

## Why this is a separate skill from discover-citations

`discover-citations` is data collection — pure API plumbing. `classify-citations` is judgment — reading sentences and deciding what kind of engagement they represent. Splitting them lets you re-pull data without re-classifying, and re-classify (with a refined taxonomy or better model) without re-pulling.

## Inputs

- A scholar slug (default: only one in `scholar/`).
- Optional: a paper tag to limit the run.
- Optional: `--re-classify` to overwrite existing classifications. Default is to skip citations with `classification.tags != null`.

## Step 1 — The taxonomy

Multi-label. A citation gets a list of tags from this set:

| Tag | Trigger |
|-----|---------|
| `drive_by` | Cited paper appears in a bracket cluster (`[14-16]`, `[5,12,14]`, `[1,18,21,...]`) with no individual discussion. The cluster groups the cited paper with several others under a general claim. |
| `acknowledgment` | Citing paper individually identifies and *characterizes* the cited paper substantively (e.g., "Smith et al. proposed X to address Y"). It's discussed, not just listed. But it is not used, extended, or compared against. |
| `baseline` | Citing paper uses the cited paper as an experimental comparison — a row in a results table, an ablation, a benchmark entry. The citing paper is claiming to outperform / match / contrast with it. |
| `methodology` | Citing paper directly *reuses* a specific technique, loss, training setting, hyperparameter choice, architectural component, or evaluation protocol from the cited paper as a building block of their own method. |
| `inspired_by` | Citing paper *extends* or *builds on* the cited paper's core idea. The cited paper is framed as the conceptual seed for the citing work. |
| `needs_review` | Sentinel — used when contexts are empty AND abstract is uninformative. Not a real category; flags for human inspection. |

**Mutual exclusion:** `drive_by` is exclusive — if it's drive-by, by definition there's no substantive engagement, so no other tag applies. The other tags can co-occur:

- `[methodology, inspired_by]` — extends and reuses
- `[baseline, methodology]` — uses HiDisc's training framework AND compares against it (HSAT does this)
- `[baseline, acknowledgment]` — compared against and discussed substantively
- `[methodology, acknowledgment]` — reuses a component and gives it a substantive write-up

**Self-citations** are NOT a separate category. The author engaging with their own prior work can do any of these. Use `is_self_citation` (already populated by `discover-citations`) to filter the view, not the tag.

## Step 2 — Decision rules (apply in order)

For each citation in `citations.yaml` where `classification.tags == null`:

1. **No signal?** If `contexts` is empty AND (`abstract` is null/empty OR uninformative for this cite) → `tags: [needs_review]`. Don't guess.

   **Title-substring rescue.** Before defaulting to `needs_review`, check if the citing paper's title contains the cited paper's short slug (e.g., "FastGlioma", "OpenSRH", "HiDisc", "DPO"). If yes, presume `[acknowledgment]` even with empty contexts — editorials and commentaries often have weak text-extraction but the title alone signals substantive engagement. Validation example: `glioma_foundation_2024` was cited by an editorial titled *"Navigating in the dark: Tailoring the extent of resection in gliomas with FastGlioma"* — zero S2 contexts but unambiguously substantive.

2. **Bracket-cluster check (auto-tag).** If every context matches one of these patterns, → `tags: [drive_by]`. Stop.
   - Numeric bracket cluster of 3+ refs: `[5,12,14]`, `[5, 12, 14]`, `[14-16]`, `[1,18,21,31,...]`
   - Year-tag table: repeated `<Method> <YYYY> [N]` patterns like `OpenSRH 2022 [50] HiDisc 2023 [51] SPT 2024 [52]`
   - Author-list cluster: 3+ author refs in one parenthesis, e.g. `(Smith et al., 2020; Jones et al., 2021; ...)`
   - Single bracket where the surrounding sentence does not name the cited paper or its concept (e.g., a sentence about pathologist accuracy with `[N]` attached)

   This is the safest auto-tag: validation across 116 citations had no false positives in this category. A regex pre-filter can do this without LLM judgment.

3. **Otherwise, read the contexts** and assign one or more substantive tags. Common patterns:

   - "We compare against [N]" / "Table 1 shows [N]" / "...as a baseline [N]" → `baseline`
   - "We use [N]'s training schedule" / "following [N]" / "loss from [N]" / "hyperparameters follow [N]" → `methodology`
   - "Building on [N]" / "extending [N]" / "[N] is the foundation" → `inspired_by`
   - "[N] proposed X" / "[N] showed Y" / "[N] introduced Z" with no further use → `acknowledgment`

   When a context says "becomes equivalent to [N]" or "is a special case of [N]" → that's both `methodology` and `acknowledgment`.

4. **Don't over-trust S2 signals.** Validation on `hidisc_2303.01605` showed:
   - `isInfluential=true` correctly identifies very substantive cites, but misses some (e.g., "becomes equivalent to HiDisc-Slide" was flagged false).
   - `intents` is sparse and sometimes wrong (paper tagged `methodology` was actually a bracket cluster).

   Treat both as features, not labels. **Read the contexts.**

## Step 3 — Write back

For each classified citation:

```yaml
classification:
  tags: [<tag>, ...]
  rationale: <1-2 sentences referring to specific evidence in the contexts>
  method: manual | "llm:<model>" | hybrid
  classified_at: <YYYY-MM-DD>
```

Rationale rules:
- Quote or reference the specific context that drove the decision.
- One sentence is usually enough. Two if multi-label and the second tag needs separate justification.
- Don't restate the taxonomy definition.

## Step 4 — Update stats block

After classifying, recompute and write:

```yaml
stats:
  classified: <int>
  classification_distribution:
    drive_by: <int>
    acknowledgment: <int>
    baseline: <int>
    methodology: <int>
    inspired_by: <int>
    needs_review: <int>
```

Tag counts may sum to more than `total_citations_s2` (multi-label).

## Step 5 — Run summary

Report:
- Citations classified
- Distribution across tags
- Citations marked `needs_review` (these are the ones the user should manually inspect or where we should fetch the citing paper's tex via `ingest-paper` to get richer context)
- Any disagreements with `isInfluential` worth flagging (e.g., "5 citations classified as drive_by despite S2 marking them with intent tags — `isInfluential` is too loose for our taxonomy")

## Implementation notes

**How the classification is actually done.** When this skill is invoked, Claude (the agent) reads the citations.yaml, classifies each citation by reading the contexts directly, and writes the file back. No external API call required — the LLM doing the work is the same model running the skill.

**For scaling to many papers in batch.** If a scholar has hundreds of papers and thousands of citations, it's more efficient to batch via the Anthropic Messages API with prompt caching:
- Cache the taxonomy definition + decision rules as system prompt
- Send batches of 20–50 citations per call
- Parse JSON-formatted responses

This is a future optimization. For one scholar with <50 papers and <500 citations total, the inline approach is fine and produces better rationales (Claude can re-read the cited paper's summary if it needs to).

**When to re-classify.** If the taxonomy changes, or a better model becomes available, run with `--re-classify`. Keep `classified_at` so you can tell which entries are stale.

## What this skill does NOT do

- **Does not fetch new citations.** Run `discover-citations --refresh` first if needed.
- **Does not download the citing paper's source.** For `needs_review` citations, the natural follow-up is `reclassify-from-source`: ingest the citing paper via `ingest-paper`, locate the actual `\cite{}` site for the target paper, and re-classify on real surrounding text. Validation showed `needs_review` is ~49% of citations on biomedical papers (S2 weakly indexes clinical journals and very recent papers), so this follow-up is a planned-but-not-yet-built next pass — not optional polish.
- **Does not aggregate across papers.** Cross-paper analysis (e.g., "which scholars cite Xinhai's work most substantively?") is a different downstream task.

## Notes on dataset papers

When the cited paper is primarily a *dataset* paper (like OpenSRH), most citations look like "we use the OpenSRH dataset for evaluation" rather than method comparisons. The existing `methodology` definition covers this — it explicitly includes "evaluation protocol" reuse — so dataset adoption gets tagged `methodology`. If finer granularity is needed later, consider adding a `uses_dataset` sub-tag, but for now `methodology` is the canonical bucket.

The classification distribution is itself a useful signal: a paper whose citations are dominated by `methodology` is making impact via dataset/benchmark adoption; a paper dominated by `acknowledgment` is making impact via discussion/awareness; one with high `baseline+methodology` overlap is being treated as a comparable method. Surface this distribution in any per-paper rendering.
