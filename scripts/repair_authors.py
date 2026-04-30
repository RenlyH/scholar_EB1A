#!/usr/bin/env python3
"""One-time repair pass: fix author/venue/year fields contaminated by the old
NBSP-blind parser in ingest_gs_citations.py.

Walks every citations.yaml under scholar/<slug>/citation/, finds entries whose
`authors` look contaminated (single string containing the venue dash separator
or a NBSP), re-parses by re-running each gs_exports/<NN>_*.md file through the
patched parser, and updates the citation entry by title match.

Also renames citing_papers/<slug>/ folders whose slug was derived from the
contaminated authors (e.g., `_<doi>` → `<surname>_<doi>`).

Usage:
  uv run --with pyyaml scripts/repair_authors.py --slug yunjiazhang_wisc
  uv run --with pyyaml scripts/repair_authors.py --slug yunjiazhang_wisc --dry-run
"""

import argparse
import hashlib
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

# Reuse the patched parser
sys.path.insert(0, str(Path(__file__).parent))
from ingest_gs_citations import parse_gs_markdown, parse_authors_line, _norm, title_match


def _str_repr(dumper, data):
    style = "|" if ("\n" in data or len(data) > 100) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


yaml.add_representer(str, _str_repr)


def slug_for(citation):
    """Mirrors move_chrome_downloads.py / reclassify_from_source.py slug rules."""
    authors = citation.get("authors") or []
    if authors and authors[0] and authors[0].split():
        first = authors[0].split()[-1].lower()
    else:
        first = "anon"
    first = re.sub(r"[^a-z0-9]+", "", first)[:12]
    if citation.get("arxiv_id"):
        suffix = citation["arxiv_id"]
    elif citation.get("doi"):
        suffix = citation["doi"].split("/")[-1]
    else:
        ident = (citation.get("title") or citation.get("gs_pdf_url") or citation.get("gs_html_url") or "").strip()
        suffix = "h" + hashlib.md5(ident.encode("utf-8")).hexdigest()[:8] if ident else "yx"
    suffix = re.sub(r"[^a-zA-Z0-9._-]+", "_", suffix)[:30]
    return f"{first}_{suffix}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    scholar_dir = REPO_ROOT / args.slug
    if not scholar_dir.exists():
        scholar_dir = REPO_ROOT / "scholar" / args.slug
    if not scholar_dir.exists():
        print("ERROR: scholar dir not found", file=sys.stderr); sys.exit(1)

    papers = yaml.safe_load((scholar_dir / "papers.yaml").read_text())["papers"]

    # Build lookup: cited-paper title → citation_dir, then within it title → entry
    # First, parse each gs_exports/*.md and build (cited_paper_title) → list of citations
    exports_dir = scholar_dir / "gs_exports"
    md_files = sorted(exports_dir.glob("*_citations.md"))
    print(f"Reading {len(md_files)} gs_exports files…")

    # Per-paper tag → list of (title, authors, venue, year) reparsed from MD
    repairs_by_tag = {}
    for md in md_files:
        cited_title, citations = parse_gs_markdown(md)
        if not cited_title:
            continue
        # match to a paper tag
        target = None
        for p in papers:
            if title_match(cited_title, p["title"]):
                target = p; break
        if not target:
            continue
        for gs in citations:
            authors, venue, year = parse_authors_line(gs["authors_line"])
            repairs_by_tag.setdefault(target["tag"], {})[_norm(gs["title"])] = {
                "authors": authors, "venue": venue, "year": year,
            }

    total_seen = total_repaired = 0
    rename_plan = []  # list of (old_slug, new_slug)
    seen_old_slugs = set()
    for p in papers:
        yml = scholar_dir / "citation" / p["tag"] / "citations.yaml"
        if not yml.exists():
            continue
        doc = yaml.safe_load(yml.read_text())
        repairs = repairs_by_tag.get(p["tag"], {})
        changed = 0
        for c in doc.get("citations") or []:
            total_seen += 1
            t = _norm(c.get("title") or "")
            if not t or t not in repairs:
                continue
            r = repairs[t]
            old_authors = c.get("authors") or []
            old_year = c.get("year")
            old_venue = c.get("venue")
            # Detect contamination: any author contains "\xa0", is "20xx", or whole entry has " - "
            contaminated = (
                any("\xa0" in (a or "") or "  " in (a or "") for a in old_authors)
                or any(isinstance(a, str) and a.isdigit() and len(a) == 4 for a in old_authors)
                or (len(old_authors) == 1 and " - " in (old_authors[0] or ""))
            )
            if not contaminated and old_authors:
                continue
            old_slug = slug_for(c)
            c["authors"] = r["authors"]
            if r.get("venue") and not old_venue:
                c["venue"] = r["venue"]
            if r.get("year") and not old_year:
                c["year"] = r["year"]
            new_slug = slug_for(c)
            if old_slug != new_slug and old_slug not in seen_old_slugs:
                rename_plan.append((old_slug, new_slug))
                seen_old_slugs.add(old_slug)
            changed += 1; total_repaired += 1
        if changed:
            print(f"  {p['tag']:32s} repaired={changed}/{len(doc.get('citations') or [])}")
            if not args.dry_run:
                yml.write_text(yaml.dump(doc, sort_keys=False, allow_unicode=True, width=100))

    # Rename citing_papers/ folders
    citing_papers = scholar_dir / "citing_papers"
    if citing_papers.exists():
        renamed = skipped = collisions = 0
        for old, new in rename_plan:
            old_dir = citing_papers / old
            new_dir = citing_papers / new
            if not old_dir.exists():
                skipped += 1; continue
            if new_dir.exists():
                # Collision — keep old as-is to preserve any source data already there.
                collisions += 1; continue
            if not args.dry_run:
                old_dir.rename(new_dir)
            renamed += 1
        print(f"\n=== folder rename ===")
        print(f"  rename plan: {len(rename_plan)} (deduped from {total_repaired} repaired)")
        print(f"  renamed: {renamed} | skipped (no old folder): {skipped} | collisions: {collisions}")

    print(f"\n=== summary ===")
    print(f"  citations seen: {total_seen}")
    print(f"  citations repaired: {total_repaired}")
    if args.dry_run:
        print("  (dry run — no files written)")


if __name__ == "__main__":
    main()
