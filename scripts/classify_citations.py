#!/usr/bin/env python3
"""classify-citations runner — auto-tag pass.

Applies safe regex/heuristic auto-tags to each citation in
scholar/<slug>/citation/<tag>/citations.yaml:

  - bracket-cluster contexts -> [drive_by]   (per skill rule 2)
  - empty contexts + empty abstract -> [needs_review]
  - otherwise: leaves classification stub as-is for follow-up classification

Does NOT do LLM-based substantive classification. The remaining "have-contexts"
entries should be handled by an interactive classify pass (the Claude agent
reading the contexts) or by reclassify-from-source on actual citing-paper text.

Usage:
  uv run --with pyyaml scripts/classify_citations.py --slug yunjiazhang_wisc
  uv run --with pyyaml scripts/classify_citations.py --slug yunjiazhang_wisc --re-classify
"""

import argparse
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TODAY = str(date.today())


def _str_repr(dumper, data):
    style = "|" if ("\n" in data or len(data) > 100) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


yaml.add_representer(str, _str_repr)


def load_yaml(p): return yaml.safe_load(Path(p).read_text())
def save_yaml(p, d): Path(p).write_text(yaml.dump(d, sort_keys=False, allow_unicode=True, width=100))


# Bracket-cluster patterns
RX_NUM_CLUSTER = re.compile(r"\[\s*\d+\s*[-–,]\s*\d+(?:\s*[,;-–]\s*\d+)*\s*\]")
RX_NUM_RANGE = re.compile(r"\[\s*\d+\s*[-–]\s*\d+\s*\]")
RX_AUTHOR_CLUSTER = re.compile(r"\([^)]*?(?:\d{4}[a-z]?[;,]\s*){2,}[A-Z][^)]*\d{4}[^)]*\)")  # 3+ author refs in one paren
RX_YEAR_TAG_TABLE = re.compile(r"(?:[A-Z][A-Za-z\-]+\s+\d{4}\s+\[\d+\][\s,]*){3,}")


def is_bracket_cluster(ctx):
    if not ctx:
        return False
    s = ctx.strip()
    # Numeric range like [14-16]
    if RX_NUM_RANGE.search(s):
        return True
    # Numeric multi-cluster like [5,12,14] (3+ numbers)
    for m in re.finditer(r"\[([^\[\]]+)\]", s):
        inner = m.group(1)
        nums = re.findall(r"\d+", inner)
        if len(nums) >= 3:
            return True
        # Range inside: [14-16]
        if re.search(r"\d+\s*[-–]\s*\d+", inner):
            return True
    # Author cluster
    if RX_AUTHOR_CLUSTER.search(s):
        return True
    # Year-tag table pattern
    if RX_YEAR_TAG_TABLE.search(s):
        return True
    return False


def all_contexts_drive_by(contexts):
    if not contexts:
        return False
    return all(is_bracket_cluster(c) for c in contexts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--paper", default=None)
    ap.add_argument("--re-classify", action="store_true",
                    help="Overwrite existing classifications (default: skip non-null tags)")
    args = ap.parse_args()

    scholar_dir = REPO_ROOT / args.slug
    if not scholar_dir.exists():
        scholar_dir = REPO_ROOT / "scholar" / args.slug
    if not scholar_dir.exists():
        print("ERROR: scholar dir not found", file=sys.stderr); sys.exit(1)

    papers = load_yaml(scholar_dir / "papers.yaml")["papers"]
    grand = Counter()
    grand_total = 0

    for p in papers:
        if args.paper and p["tag"] != args.paper:
            continue
        yml = scholar_dir / "citation" / p["tag"] / "citations.yaml"
        if not yml.exists():
            continue
        doc = load_yaml(yml)
        cits = doc.get("citations") or []
        n_drive = n_review = n_skipped = n_kept = 0
        for c in cits:
            cls = c.setdefault("classification", {})
            tags = cls.get("tags") or []
            # If already substantively classified (not just needs_review) and not --re-classify, keep
            if not args.re_classify and tags and tags != ["needs_review"]:
                n_kept += 1; continue

            contexts = c.get("contexts") or []
            abstract = (c.get("abstract") or "").strip()

            if all_contexts_drive_by(contexts):
                cls["tags"] = ["drive_by"]
                cls["rationale"] = "All S2 contexts are bracket-cluster mentions (e.g., [14-16] or 3+ refs grouped)."
                cls["method"] = "rule:bracket_cluster"
                cls["classified_at"] = TODAY
                n_drive += 1
            elif not contexts and not abstract:
                cls["tags"] = ["needs_review"]
                cls["rationale"] = "No S2 contexts and no abstract — defer to reclassify-from-source."
                cls["method"] = "rule:no_signal"
                cls["classified_at"] = TODAY
                n_review += 1
            else:
                # Leave as-is (still needs_review with the gs_extension rationale, or null)
                if not tags:
                    cls["tags"] = ["needs_review"]
                    cls["rationale"] = "Has contexts/abstract but not auto-classifiable; awaits LLM/manual pass."
                    cls["method"] = "rule:deferred"
                    cls["classified_at"] = TODAY
                n_skipped += 1

        # Recompute distribution
        cnt = Counter()
        for c in cits:
            for t in (c.get("classification") or {}).get("tags") or []:
                cnt[t] += 1
        doc.setdefault("stats", {})
        doc["stats"]["classified"] = sum(1 for c in cits if (c.get("classification") or {}).get("tags"))
        doc["stats"]["classification_distribution"] = dict(cnt)
        save_yaml(yml, doc)
        print(f"  {p['tag']:32s} drive_by={n_drive:3d} no_signal={n_review:3d} deferred={n_skipped:3d} kept={n_kept:3d}")
        grand.update(cnt); grand_total += len(cits)

    print(f"\n=== distribution across all papers ===")
    print(f"  total citations: {grand_total}")
    for k, v in sorted(grand.items(), key=lambda x: -x[1]):
        print(f"  {k:20s} {v}")


if __name__ == "__main__":
    main()
