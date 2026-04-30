#!/usr/bin/env python3
"""Generate a download queue for Claude Chrome to process.

Reads citations.yaml across all of the scholar's papers; for each citation that
is still `tags: [needs_review]` and has a URL we couldn't fetch programmatically,
emits an entry the user can feed to the Claude Chrome extension to download via
their browser session.

Output: scholar/<slug>/chrome_download_queue.md

Usage:
  uv run --with pyyaml scripts/generate_chrome_download_queue.py
"""

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SLUG = "xinhaihou_umich"


def load_yaml(p):
    return yaml.safe_load(Path(p).read_text())


# Hosts where curl is reliably blocked but a browser session works
CHROME_REQUIRED_HOSTS = {
    "biorxiv.org", "www.biorxiv.org",
    "medrxiv.org", "www.medrxiv.org",
    "researchsquare.com", "www.researchsquare.com",
    "nature.com", "www.nature.com",  # paywalled Nature journals
    "cell.com", "www.cell.com",
    "linkinghub.elsevier.com", "sciencedirect.com", "www.sciencedirect.com",
    "academic.oup.com", "thelancet.com", "www.thelancet.com",
    "wiley.com", "onlinelibrary.wiley.com",
    "tandfonline.com", "www.tandfonline.com",
    "thejns.org",
    "pubs.acs.org",
    "pubs.aip.org",
    "ascopubs.org",
    "iopscience.iop.org",
    "thieme-connect.com", "www.thieme-connect.com",
    "synapse.koreamed.org",
    "spiedigitallibrary.org", "www.spiedigitallibrary.org",
    "ieeexplore.ieee.org",
    "ehost.ebscohost.com", "search.ebscohost.com",
    "advanced.onlinelibrary.wiley.com",
    "spj.science.org",
}

# Hosts where direct curl usually works (so listing here = something else went wrong)
CURL_FRIENDLY_HOSTS = {
    "arxiv.org", "www.arxiv.org",
    "openreview.net",
    "mdpi.com", "www.mdpi.com",
    "frontiersin.org", "www.frontiersin.org",
    "link.springer.com", "springer.com",
    "pmc.ncbi.nlm.nih.gov",
    "openalex.org",
    "researchgate.net", "www.researchgate.net",
    "freidok.uni-freiburg.de",
}


def host_of(url):
    if not url:
        return None
    try:
        return urlparse(url).hostname or None
    except Exception:
        return None


def has_cached_source(scholar_dir, citation):
    """Return True if we already have tex or pdf for this citation. Imports unified slug logic."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from move_chrome_downloads import slug_for as _slug_for
    citing_tag = _slug_for(citation)
    src = scholar_dir / "citing_papers" / citing_tag / "source"
    if not src.exists():
        return False
    if any(src.glob("*.tex")) or any(src.glob("**/*.tex")):
        return True
    if (src / "paper.pdf").exists():
        with open(src / "paper.pdf", "rb") as f:
            return f.read(8).startswith(b"%PDF")
    return False


def best_url(citation):
    """Pick the best URL to give to Chrome; prefer PDF over HTML."""
    return citation.get("gs_pdf_url") or citation.get("gs_html_url")


def categorize(url):
    """Bucket: 'auto-retryable' (curl might work, retry runner) | 'chrome-required' | 'no-url'."""
    if not url:
        return "no-url"
    h = host_of(url)
    if h in CHROME_REQUIRED_HOSTS or any(h and h.endswith("." + d) for d in CHROME_REQUIRED_HOSTS):
        return "chrome-required"
    if h in CURL_FRIENDLY_HOSTS:
        return "auto-retryable"
    # Unknown host: default to chrome-required (safer assumption)
    return "chrome-required"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", default=DEFAULT_SLUG)
    args = ap.parse_args()
    scholar_dir = REPO_ROOT / args.slug
    if not scholar_dir.exists():
        scholar_dir = REPO_ROOT / "scholar" / args.slug
    if not scholar_dir.exists():
        print(f"ERROR: scholar dir not found", file=sys.stderr)
        sys.exit(1)

    papers = load_yaml(scholar_dir / "papers.yaml")["papers"]
    citation_dir = scholar_dir / "citation"

    # Dedupe by (gs_pdf_url or gs_html_url or s2_paper_id)
    chrome_entries = {}
    auto_retry_entries = {}
    no_url_entries = {}

    for p in papers:
        yml = citation_dir / p["tag"] / "citations.yaml"
        if not yml.exists():
            continue
        doc = load_yaml(yml)
        for c in doc["citations"]:
            cls = c.get("classification") or {}
            tags = cls.get("tags") or []
            if tags != ["needs_review"]:
                continue
            if has_cached_source(scholar_dir, c):
                continue  # already have it; runner will pick up next time
            url = best_url(c)
            cat = categorize(url)
            key = url or c.get("s2_paper_id") or c.get("title", "")[:80]
            target_bucket = {
                "chrome-required": chrome_entries,
                "auto-retryable": auto_retry_entries,
                "no-url": no_url_entries,
            }[cat]
            if key not in target_bucket:
                target_bucket[key] = {
                    "title": c.get("title"),
                    "year": c.get("year"),
                    "venue": c.get("venue"),
                    "authors": c.get("authors") or [],
                    "best_url": url,
                    "html_url": c.get("gs_html_url"),
                    "pdf_url": c.get("gs_pdf_url"),
                    "doi": c.get("doi"),
                    "arxiv_id": c.get("arxiv_id"),
                    "host": host_of(url),
                    "cites_xinhai": [],
                    "is_self_citation": c.get("is_self_citation", False),
                }
            target_bucket[key]["cites_xinhai"].append(p["tag"])

    # ---- Render Chrome queue (minimal — just URLs, host-grouped) ----
    out = [
        "# Chrome download queue",
        "",
        f"{len(chrome_entries)} URLs to download. Open each in your browser; let it save to ~/Downloads/.",
        "After Chrome finishes, run: `uv run --with pyyaml python3 scripts/move_chrome_downloads.py`",
        "",
    ]

    by_host = defaultdict(list)
    for entry in chrome_entries.values():
        by_host[entry["host"] or "(unknown)"].append(entry)

    for host in sorted(by_host.keys(), key=lambda h: -len(by_host[h])):
        entries = by_host[host]
        out.append(f"## {host} ({len(entries)})")
        out.append("")
        for entry in entries:
            url = entry["pdf_url"] or entry["html_url"]
            if url:
                out.append(url)
        out.append("")

    chrome_path = scholar_dir / "chrome_download_queue.md"
    chrome_path.write_text("\n".join(out))

    # ---- Render auto-retry list (info-only) ----
    out_auto = ["# Auto-retryable downloads", ""]
    out_auto.append(f"{len(auto_retry_entries)} entries on curl-friendly hosts that the runner couldn't fetch on the previous run.")
    out_auto.append("Re-running `reclassify_from_source.py` should pick these up; if they still fail, investigate the URL pattern.")
    out_auto.append("")
    for entry in sorted(auto_retry_entries.values(), key=lambda e: e["host"] or ""):
        out_auto.append(f"- [{entry['host']}] **{entry['title'][:90]}**")
        out_auto.append(f"  URL: {entry['best_url']}")
    auto_path = scholar_dir / "auto_retry_queue.md"
    auto_path.write_text("\n".join(out_auto))

    # ---- Render no-URL list ----
    out_nourl = ["# Papers with no resolvable download URL", ""]
    out_nourl.append(f"{len(no_url_entries)} entries with no PDF or HTML URL — likely need manual title-search to find a copy.")
    out_nourl.append("")
    for entry in no_url_entries.values():
        out_nourl.append(f"- {entry['title']} · cites=`{', '.join(entry['cites_xinhai'])}`")
    nourl_path = scholar_dir / "no_url_queue.md"
    nourl_path.write_text("\n".join(out_nourl))

    print(f"Chrome queue:    {chrome_path} ({len(chrome_entries)} papers)")
    print(f"Auto-retry list: {auto_path} ({len(auto_retry_entries)} papers)")
    print(f"No-URL list:     {nourl_path} ({len(no_url_entries)} papers)")
    by_host_summary = sorted(by_host.items(), key=lambda kv: -len(kv[1]))
    if by_host_summary:
        print(f"\nChrome queue by host:")
        for h, es in by_host_summary[:10]:
            print(f"  {len(es):3d} {h}")


if __name__ == "__main__":
    main()
