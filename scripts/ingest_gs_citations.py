#!/usr/bin/env python3
"""ingest-gs-citations runner.

Parses ~/Downloads/{NN}_{slug}_citations.md files (output of the Claude Chrome
extension) and merges into scholar/<slug>/citation/<tag>/citations.yaml.

Usage:
  uv run --with pyyaml scripts/ingest_gs_citations.py
  uv run --with pyyaml scripts/ingest_gs_citations.py --downloads-dir ~/Downloads
  uv run --with pyyaml scripts/ingest_gs_citations.py --paper hidisc_2303.01605 --dry-run
"""

import argparse
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SLUG = "xinhaihou_umich"
TODAY = str(date.today())


def default_exports_dir(scholar_dir):
    """Prefer the in-project gs_exports/ dir; fall back to ~/Downloads/ for fresh exports."""
    project = scholar_dir / "gs_exports"
    if project.exists():
        return project
    return Path.home() / "Downloads"


def _str_repr(dumper, data):
    style = "|" if ("\n" in data or len(data) > 100) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


yaml.add_representer(str, _str_repr)


def load_yaml(p):
    return yaml.safe_load(Path(p).read_text())


def save_yaml(p, data):
    Path(p).write_text(yaml.dump(data, sort_keys=False, allow_unicode=True, width=100))


# ---------- Markdown parsing ----------


def parse_gs_markdown(md_path):
    """Parse one GS export file. Returns (cited_paper_title, citations_list).

    Each citation is a dict with: title, authors_line, html_url, pdf_url.
    """
    text = md_path.read_text(errors="replace")
    # Cited-paper title (line starting with `# Papers Citing "..."`)
    m = re.search(r'^#\s+Papers Citing\s+"([^"]+)"', text, re.M)
    cited_title = m.group(1).strip() if m else None

    # Each entry: line starting with `N. <title>` (title may or may not be wrapped in **)
    citations = []
    # Find all entry blocks: number + title to next number-line (or EOF).
    # Title can be `**title**` (older extension format) or plain `title` (newer format).
    entry_pattern = re.compile(
        r"^\s*(\d+)\.\s+(?:\*\*(.*?)\*\*|(.*?))\s*\n((?:(?!^\s*\d+\.\s+).)*)",
        re.M | re.S,
    )
    for em in entry_pattern.finditer(text):
        num = int(em.group(1))
        title = (em.group(2) or em.group(3) or "").strip()
        body = em.group(4)
        if not title:
            continue
        # authors_line is the first `- ` bullet that's not HTML/PDF
        authors_line = None
        html_url = None
        pdf_url = None
        for line in body.splitlines():
            s = line.strip()
            if not s.startswith("-"):
                continue
            content = s[1:].strip()
            if content.startswith("HTML:"):
                html_url = content.split("HTML:", 1)[1].strip()
            elif content.startswith("PDF:"):
                pdf_url = content.split("PDF:", 1)[1].strip()
            elif authors_line is None:
                authors_line = content
        citations.append({
            "n": num,
            "title": title,
            "authors_line": authors_line,
            "html_url": html_url,
            "pdf_url": pdf_url,
        })
    return cited_title, citations


# ---------- Title fuzzy match ----------


def _norm(s):
    s = s.lower()
    s = s.replace("ﬁ", "fi").replace("ﬂ", "fl").replace("ﬀ", "ff").replace("ﬃ", "ffi")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def title_match(probe, candidate):
    """Returns True if probe title matches candidate title fuzzily."""
    p, c = _norm(probe), _norm(candidate)
    if not p or not c:
        return False
    if p == c:
        return True
    pt, ct = set(p.split()), set(c.split())
    pl, cl = p.split(), c.split()
    if len(pl) < 4 or len(cl) < 4:
        return False
    # Tier 1: 6+ consecutive words shared (substring)
    n = min(6, len(pl))
    if " ".join(pl[:n]) in c:
        return True
    if " ".join(cl[:n]) in p:
        return True
    # Tier 2: ≥75% word overlap (handles abbreviations like "CNS" vs "central nervous system")
    overlap = pt & ct
    smaller = min(len(pt), len(ct))
    if smaller >= 5 and len(overlap) / smaller >= 0.75:
        return True
    return False


# ---------- URL parsing for arxiv_id / doi ----------


RX_ARXIV = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
RX_DOI_NATURE = re.compile(r"nature\.com/articles/([a-z0-9]+-\d+-\d+(?:-[a-z0-9]+)*)", re.I)
RX_DOI_GENERIC = re.compile(r"doi\.org/(10\.\d{4,9}/[^\s?#&]+)", re.I)
# Most publisher landing/PDF URLs include the DOI in /doi/{abs,pdf,full,epdf}/ paths
RX_DOI_PUBLISHER = re.compile(r"/doi/(?:abs/|pdf/|full/|epdf/|pdfdirect/)?(10\.\d{4,9}/[^\s?#&]+?)(?=[?#]|/full|/pdf|$)", re.I)
RX_PMC = re.compile(r"pmc\.ncbi\.nlm\.nih\.gov/articles/(PMC\d+)", re.I)


def extract_ids(html_url, pdf_url):
    arxiv_id = None
    doi = None
    pmcid = None
    for url in [u for u in [html_url, pdf_url] if u]:
        if not arxiv_id:
            m = RX_ARXIV.search(url)
            if m:
                arxiv_id = m.group(1)
        if not doi:
            m = RX_DOI_GENERIC.search(url) or RX_DOI_PUBLISHER.search(url)
            if m:
                doi = m.group(1)
            elif "nature.com/articles" in url:
                m = RX_DOI_NATURE.search(url)
                if m:
                    doi = f"10.1038/{m.group(1)}"
        if not pmcid:
            m = RX_PMC.search(url)
            if m:
                pmcid = m.group(1)
    return arxiv_id, doi, pmcid


# ---------- Authors line parsing ----------


def parse_authors_line(line):
    """Best-effort split of '- A Smith, B Jones - Journal Name, 2024 - domain.com'.

    Returns (authors_list, venue, year).
    """
    if not line:
        return [], None, None
    # GS uses NBSP (\xa0) around the dash separators, not ASCII space — normalize first.
    line = line.replace("\xa0", " ").replace(" ", " ")
    parts = [p.strip() for p in re.split(r"\s+-\s+", line)]
    authors_str = parts[0] if parts else ""
    venue_year = parts[1] if len(parts) > 1 else None

    # Authors: split by comma, drop "..." or "et al."
    authors = []
    for a in re.split(r",\s*", authors_str):
        a = a.strip()
        if not a or a in {"…", "..."} or a.lower().startswith("et al"):
            continue
        authors.append(a)

    # Venue + year
    venue = None
    year = None
    if venue_year:
        ym = re.search(r"\b(19|20)\d{2}\b", venue_year)
        if ym:
            year = int(ym.group(0))
            venue = venue_year[: ym.start()].strip().rstrip(",")
        else:
            venue = venue_year
    return authors, venue, year


# ---------- Self-cite heuristic ----------


def is_self_citation(authors, scholar_name_variants):
    surname = next(iter(scholar_name_variants)).split()[-1].lower()
    for a in authors:
        toks = re.split(r"\s+", a.strip())
        if not toks:
            continue
        # GS format like "X Hou" or "Hou, X" or "Xinhai Hou"
        if any(t.lower() == surname for t in toks):
            return True
    return False


# ---------- Per-paper merge ----------


def merge_citations_yaml(yml_path, gs_citations, scholar_name, scholar_name_variants, dry_run=False):
    """Update or add entries in citations.yaml. Returns (added, updated, unchanged)."""
    doc = load_yaml(yml_path)
    existing = doc.get("citations", [])
    added = updated = unchanged = 0

    # Index existing by normalized title
    norm_idx = {_norm(c.get("title") or ""): c for c in existing if c.get("title")}

    for gs in gs_citations:
        title = gs["title"]
        norm = _norm(title)
        match = norm_idx.get(norm)
        if not match:
            # Try fuzzy match
            for ct in existing:
                if title_match(title, ct.get("title") or ""):
                    match = ct
                    break

        if match:
            changed = False
            if gs["html_url"] and match.get("gs_html_url") != gs["html_url"]:
                match["gs_html_url"] = gs["html_url"]
                changed = True
            if gs["pdf_url"] and match.get("gs_pdf_url") != gs["pdf_url"]:
                match["gs_pdf_url"] = gs["pdf_url"]
                changed = True
            if changed:
                updated += 1
            else:
                unchanged += 1
        else:
            # Add new entry
            authors, venue, year = parse_authors_line(gs["authors_line"])
            arxiv_id, doi, pmcid = extract_ids(gs["html_url"], gs["pdf_url"])
            new_entry = {
                "s2_paper_id": None,  # unknown — could be backfilled later
                "title": title,
                "year": year,
                "venue": venue,
                "arxiv_id": arxiv_id,
                "doi": doi,
                "pmcid": pmcid,
                "authors": authors,
                "citation_count_of_citing": None,
                "is_self_citation": is_self_citation(authors, scholar_name_variants),
                "s2_signals": {"is_influential": False, "intents": []},
                "contexts": [],
                "abstract": None,
                "gs_html_url": gs["html_url"],
                "gs_pdf_url": gs["pdf_url"],
                "source": "gs_extension",
                "classification": {
                    "tags": ["needs_review"],
                    "rationale": "Discovered via GS Chrome extension; S2 had not surfaced this cite. No contexts available — pending reclassify-from-source.",
                    "method": "gs_extension",
                    "classified_at": TODAY,
                },
            }
            existing.append(new_entry)
            added += 1

    # Refresh stats
    doc.setdefault("stats", {})
    doc["stats"]["total_citations_gs_export"] = len(gs_citations)
    doc["stats"]["total_citations_added_via_gs"] = (
        doc["stats"].get("total_citations_added_via_gs", 0) + added
    )
    doc["stats"]["total_citations_s2"] = len(existing)  # combined now
    cnt = Counter()
    for c in existing:
        ct = (c.get("classification") or {}).get("tags") or []
        for t in ct:
            cnt[t] += 1
    doc["stats"]["classification_distribution"] = dict(cnt)

    # Sort
    def _sort_key(c):
        sig = (c.get("s2_signals") or {})
        return (
            not sig.get("is_influential", False),
            -len(c.get("contexts") or []),
            -(c.get("year") or 0),
        )

    existing.sort(key=_sort_key)
    doc["citations"] = existing

    if not dry_run:
        save_yaml(yml_path, doc)
    return added, updated, unchanged


# ---------- Main ----------


def run(scholar_dir, downloads_dir, only_paper=None, dry_run=False):
    papers = load_yaml(scholar_dir / "papers.yaml")["papers"]
    profile = load_yaml(scholar_dir / "profile.yaml")
    scholar_name = profile.get("name", "")
    name_variants = {scholar_name, scholar_name.split()[-1]}

    md_files = sorted(downloads_dir.glob("*_citations.md"))
    print(f"Found {len(md_files)} GS export files in {downloads_dir}")

    matched_files = 0
    total_added = total_updated = total_unchanged = 0

    for md in md_files:
        cited_title, citations = parse_gs_markdown(md)
        if not cited_title:
            print(f"  SKIP {md.name}: no '# Papers Citing' header")
            continue
        # Match to a paper in papers.yaml
        target = None
        for p in papers:
            if title_match(cited_title, p["title"]):
                target = p
                break
        if not target:
            print(f"  SKIP {md.name}: cited paper '{cited_title[:60]}' not in papers.yaml")
            continue
        if only_paper and target["tag"] != only_paper:
            continue
        matched_files += 1
        yml = scholar_dir / "citation" / target["tag"] / "citations.yaml"
        if not yml.exists():
            yml.parent.mkdir(parents=True, exist_ok=True)
            yml.write_text(yaml.dump({
                "cited_paper": {
                    "tag": target["tag"], "title": target["title"],
                    "s2_paper_id": target["s2_paper_id"], "arxiv_id": target.get("arxiv_id"),
                },
                "source": "gs_extension",
                "fetched_at": TODAY,
                "stats": {},
                "citations": [],
            }, sort_keys=False, allow_unicode=True, width=100))
        added, updated, unchanged = merge_citations_yaml(
            yml, citations, scholar_name, name_variants, dry_run=dry_run
        )
        total_added += added
        total_updated += updated
        total_unchanged += unchanged
        print(f"  {md.name:55s} → {target['tag']:42s} | parsed={len(citations):3d} added={added:3d} updated={updated:3d} unchanged={unchanged:3d}")

    print(f"\n=== summary ===")
    print(f"  files matched: {matched_files}/{len(md_files)}")
    print(f"  total parsed: {total_added + total_updated + total_unchanged}")
    print(f"  added (S2 had missed): {total_added}")
    print(f"  updated (gs_*_url attached): {total_updated}")
    print(f"  unchanged (already had gs urls): {total_unchanged}")
    if dry_run:
        print(f"  (dry run — no files written)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", default=DEFAULT_SLUG)
    ap.add_argument("--downloads-dir", default=None,
                    help="Directory of *_citations.md files (default: <scholar>/gs_exports/ if it exists, else ~/Downloads/)")
    ap.add_argument("--paper", default=None, help="Limit to one paper tag")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    scholar_dir = REPO_ROOT / "scholar" / args.slug
    if not scholar_dir.exists():
        alt = REPO_ROOT / args.slug
        if alt.exists():
            scholar_dir = alt
        else:
            print(f"ERROR: scholar directory not found", file=sys.stderr)
            sys.exit(1)

    if args.downloads_dir:
        downloads = Path(args.downloads_dir).expanduser()
    else:
        downloads = default_exports_dir(scholar_dir)
    if not downloads.exists():
        print(f"ERROR: gs exports dir not found: {downloads}", file=sys.stderr)
        sys.exit(1)
    print(f"Reading GS exports from {downloads}")

    run(scholar_dir, downloads, only_paper=args.paper, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
