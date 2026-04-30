#!/usr/bin/env python3
"""Move PDFs that Claude Chrome downloaded into the right citing_papers slot.

After running the Chrome extension on chrome_download_queue.md, PDFs land in
~/Downloads/ with publisher-default filenames (e.g., 1-s2.0-S2666914525002325-main.pdf).
This script:
  1. Scans ~/Downloads/ for recently-added PDFs
  2. Extracts identifiers (PII / DOI suffix / etc.) from each filename
  3. Looks up which citing paper that identifier belongs to (via citations.yaml)
  4. Moves the PDF into citing_papers/<slug>/source/paper.pdf

Usage:
  uv run --with pyyaml scripts/move_chrome_downloads.py
  uv run --with pyyaml scripts/move_chrome_downloads.py --dry-run
  uv run --with pyyaml scripts/move_chrome_downloads.py --downloads-dir ~/Downloads --since-hours 48
"""

import argparse
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SLUG = "xinhaihou_umich"
DEFAULT_DOWNLOADS = Path.home() / "Downloads"


def load_yaml(p):
    return yaml.safe_load(Path(p).read_text())


# ---------- URL → fingerprint extraction ----------

# Each returns one or more fingerprint strings that should appear in the downloaded filename.
URL_FINGERPRINTS = [
    # ScienceDirect: pii/SXXXXXXXXXX → filename contains SXXXXXXXXXX
    (re.compile(r"/pii/(S[\dA-Z]+)"), lambda m: [m.group(1)]),
    # Nature: articles/<doi-suffix> → filename usually contains the suffix
    (re.compile(r"nature\.com/articles/([sa-z\d\-]+)", re.I),
     lambda m: [m.group(1), m.group(1).replace("-", "_")]),
    # Cell Press: S\d+-\d+\(\d+\)\d+-\d+ → also as digits-only (filename pattern: PIIS<digits>)
    # URL: S2666-1667(25)00627-6 → digits: 2666166725006276
    (re.compile(r"cell\.com/.*?/?(S\d{4}-\d+\(\d+\)\d{5}-[\dX])", re.I),
     lambda m: [m.group(1), re.sub(r"\D", "", m.group(1))]),
    # RSC: pubs.rsc.org/en/content/articlepdf/<year>/<journ>/<articleid>
    (re.compile(r"pubs\.rsc\.org/[^/]+/content/article(?:pdf|html)/[^/]+/[^/]+/([\w]+)", re.I),
     lambda m: [m.group(1).lower(), m.group(1).upper()]),
    # ACS: doi/(abs|pdf|full)/10.1021/<suffix>
    (re.compile(r"pubs\.acs\.org/doi/(?:abs|pdf|full)?/?10\.1021/([\w.]+)", re.I),
     lambda m: [m.group(1), m.group(1).replace(".", "")]),
    # Springer link: /article/10.1007/<suffix>
    (re.compile(r"link\.springer\.com/(?:article|content/pdf)/10\.\d{4}/([\w-]+)", re.I),
     lambda m: [m.group(1)]),
    # OUP / academic.oup.com: /article(?:-abstract|-pdf)?/.../<doi-or-id>
    (re.compile(r"academic\.oup\.com/[^/]+/article(?:-abstract|-pdf)?/.*?/(\d+)/([\w-]+)", re.I),
     lambda m: [m.group(2)]),
    # Wiley: /doi/(?:abs|pdf|full)/10.\d+/<suffix>
    (re.compile(r"onlinelibrary\.wiley\.com/doi/(?:abs|pdf|full|epdf)?/?10\.\d{4,9}/([\w.-]+)", re.I),
     lambda m: [m.group(1)]),
    # Tandfonline: /doi/(?:abs|pdf|full)/10.<reg>/<id>
    (re.compile(r"tandfonline\.com/doi/(?:abs|pdf|full|epdf)?/?10\.\d{4,9}/([\w.-]+)", re.I),
     lambda m: [m.group(1)]),
    # IEEE: /document/<id> or /iel/<num>
    (re.compile(r"ieeexplore\.ieee\.org/(?:abstract/document|document|iel\d?)/(\d+)", re.I),
     lambda m: [m.group(1)]),
    # Lancet (cell.com)
    (re.compile(r"thelancet\.com/journals/[^/]+/article/(PII)?(S\d+-\d+\(\d+\)\d+-\d+)", re.I),
     lambda m: [m.group(2)]),
    # bioRxiv / medRxiv
    (re.compile(r"(?:bio|med)rxiv\.org/content/(?:10\.\d+/)?([\d.]+(?:v\d+)?)", re.I),
     lambda m: [m.group(1)]),
    # Frontiers: anything/articles/10.3389/<journ>.<year>.<articleid>(/full)?
    # Filenames look like fonc-15-1585891.pdf so emit both DOI suffix forms
    (re.compile(r"frontiersin\.org/.*?articles?/10\.3389/(\w+)\.(\d{4})\.(\d+)", re.I),
     lambda m: [f"{m.group(1)}.{m.group(2)}.{m.group(3)}", m.group(3), f"{m.group(1)}-{m.group(3)}"]),
    # MDPI: ISSN-based URLs /<issn>/<vol>/<issue>/<articleid> → filename like cancers-17-03584.pdf
    # Article id is 4-5 digits; padded to 5 in filename. Emit raw + padded variants.
    (re.compile(r"mdpi\.com/(\d{4}-\d{4})/(\d+)/\d+/(\d+)", re.I),
     lambda m: [m.group(3), f"-{int(m.group(2)):d}-{int(m.group(3)):05d}", f"-{int(m.group(2)):d}-{m.group(3)}"]),
    # MDPI alt: /<journal-slug>/<vol>/<issue>/<articleid>
    (re.compile(r"mdpi\.com/(?:journal/)?([a-z]+)/(\d+)/\d+/(\d+)", re.I),
     lambda m: [m.group(3), f"{m.group(1)}-{int(m.group(2)):d}-{int(m.group(3)):05d}", f"{m.group(1)}-{int(m.group(2)):d}-{m.group(3)}"]),
    # Theranostics: thno.org/v<vol>p<page>.htm or .pdf → filename pattern thnov<vol>p<page>.pdf
    (re.compile(r"thno\.org/v(\d+)p(\d+)", re.I),
     lambda m: [f"thnov{m.group(1)}p{m.group(2)}", f"v{m.group(1)}p{m.group(2)}"]),
    # arxiv
    (re.compile(r"arxiv\.org/(?:pdf|abs)/(\d{4}\.\d{4,5})", re.I),
     lambda m: [m.group(1)]),
    # PMC
    (re.compile(r"pmc\.ncbi\.nlm\.nih\.gov/articles/(PMC\d+)", re.I), lambda m: [m.group(1)]),
    # AIP / RSC / IOP — DOI tail
    (re.compile(r"(?:pubs\.aip|pubs\.rsc|iopscience\.iop)\.org/.*/(10\.\d{4,9}/[\w.-]+)", re.I),
     lambda m: [m.group(1).split("/")[-1]]),
    # Generic DOI fallback
    (re.compile(r"doi\.org/(10\.\d{4,9}/[\w.\-/]+)", re.I),
     lambda m: [m.group(1).split("/")[-1]]),
    # OPG (Optica)
    (re.compile(r"opg\.optica\.org/(?:viewmedia|abstract)\.cfm\?uri=([\w-]+)", re.I),
     lambda m: [m.group(1)]),
    # SPIE
    (re.compile(r"spiedigitallibrary\.org/conference-proceedings-of-spie/(\d+/\d+)/", re.I),
     lambda m: [m.group(1).replace("/", "-")]),
    # Thieme: products/ejournals/(html|pdf)/10.<reg>/<id>(.pdf)?
    (re.compile(r"thieme-connect\.com/products/ejournals/(?:html|pdf)/10\.\d+/([\w.-]+?)(?:\.pdf)?$", re.I),
     lambda m: [m.group(1)]),
    # JNS (thejns.org)
    (re.compile(r"thejns\.org/[^/]+/view/journals/[^/]+/\d+/\d+/article-([\w-]+)", re.I),
     lambda m: [m.group(1)]),
    # Preprints.org: /manuscript/<id>/<v?> → filename like preprints<id>.v<n>.pdf
    (re.compile(r"preprints\.org/manuscript/([\d.]+)", re.I),
     lambda m: [m.group(1), f"preprints{m.group(1)}"]),
    # HAL (theses.hal.science / hal.science): /tel-XXXXXXXX or /hal-XXXXXXXX
    (re.compile(r"hal\.science/((?:tel|hal)-\d+)", re.I), lambda m: [m.group(1)]),
]


def extract_fingerprints(url):
    """Return a list of substrings that, if found in a downloaded filename, identify this URL."""
    if not url:
        return []
    out = []
    for rx, fn in URL_FINGERPRINTS:
        m = rx.search(url)
        if m:
            out.extend(fn(m))
    # Universal fallback: last URL path segment (minus query/fragment/extension)
    # Many publishers name PDFs after the last URL segment (e.g., 978-3-031-98022-0_15.pdf)
    parsed = urlparse(url)
    if parsed.path:
        last = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        last = re.sub(r"\.\w{2,5}$", "", last)  # strip extension
        if last and len(last) >= 6 and not last.isdigit():  # avoid trivial numbers
            out.append(last)
    return [f for f in out if f and len(f) >= 4]


def slug_title_tokens(s):
    """Reduce a string (filename or title) to a clean lowercase token sequence for substring match."""
    if not s:
        return ""
    s = s.lower()
    s = s.replace("ﬁ", "fi").replace("ﬂ", "fl").replace("ﬀ", "ff").replace("ﬃ", "ffi")
    s = s.replace("‐", "-").replace("–", "-").replace("—", "-")
    s = re.sub(r"\(\d+\)$", "", s)  # drop trailing "(1)", "(2)" duplicate-marker
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def title_match(filename_norm, citation_title):
    """True if 5+ consecutive title words appear in the normalized filename.

    Also handles run-together filenames (e.g., 'ComparativereviewofstimulatedRaman...')
    by checking the de-spaced version of both sides.
    """
    if not citation_title:
        return False
    title_norm = slug_title_tokens(citation_title)
    title_tokens = title_norm.split()
    # Try probes starting at any position (not just word 0). Some publishers
    # reorder title words in filenames (e.g., 'Whole Slide Image Survey' from 'Survey on Whole Slide Image').
    if len(title_tokens) >= 5:
        for start in range(min(4, len(title_tokens))):
            for n in (min(8, len(title_tokens) - start), min(6, len(title_tokens) - start), 5):
                if n < 5: continue
                probe = " ".join(title_tokens[start:start + n])
                if probe in filename_norm:
                    return True
    # Run-together fallback: de-space both sides and check ≥30-char title prefix
    title_squashed = title_norm.replace(" ", "")
    fname_squashed = filename_norm.replace(" ", "")
    if len(title_squashed) >= 30 and title_squashed[:40] in fname_squashed:
        return True
    # Truncated-filename fallback (e.g., ProQuest "LLM4SCM_A_Framework_for_Intel.pdf"
    # truncates "Intelligent..."). If filename's leading 20+ squashed chars are a
    # prefix of the title's squashed form, accept the match.
    if len(fname_squashed) >= 20 and title_squashed.startswith(fname_squashed[:25]):
        return True
    # Short-title fallback (e.g., "Learning Patterns in Configuration" — 4 tokens):
    # accept if the entire short title appears as a substring in the filename.
    if len(title_squashed) >= 18 and title_squashed in fname_squashed:
        return True
    # Non-English-title fallback: when the citation title contains non-ASCII
    # characters (Korean, Chinese, etc.) the ASCII-only portion is usually a
    # short English keyword (e.g., "PDTune", "Least-to-Most"). If the filename
    # starts with that ASCII keyword, accept.
    has_nonascii = any(ord(ch) > 127 for ch in citation_title)
    if has_nonascii:
        # Take leading ASCII run from the title (alphanumeric only)
        ascii_prefix = ""
        for ch in citation_title.lower():
            if ch.isalnum() or ch in "-_:":
                ascii_prefix += ch
            else:
                if ascii_prefix and ord(ch) > 127:
                    break
                if ascii_prefix and not ch.isspace():
                    ascii_prefix = ""  # reset on punctuation
                if not ch.strip():
                    if ascii_prefix:
                        break
                else:
                    pass
        ascii_squashed = re.sub(r"[^a-z0-9]+", "", ascii_prefix)
        if len(ascii_squashed) >= 5 and fname_squashed.startswith(ascii_squashed):
            return True
    # Content-word overlap fallback: ≥4 distinct long tokens (≥5 chars) must overlap.
    # Strong signal even when filename uses only a subset of title words.
    title_long = {t for t in title_tokens if len(t) >= 5}
    fname_long = {t for t in filename_norm.split() if len(t) >= 5}
    if len(title_long & fname_long) >= 4:
        return True
    return False


# ---------- Slug derivation (matches reclassify_from_source.py) ----------


import hashlib


def slug_for(citation):
    authors = citation.get("authors") or []
    first = (authors[0].split()[-1].lower() if authors else "anon")
    first = re.sub(r"[^a-z0-9]+", "", first)[:12]
    if citation.get("arxiv_id"):
        suffix = citation["arxiv_id"]
    elif citation.get("doi"):
        suffix = citation["doi"].split("/")[-1]
    else:
        # No stable identifier — hash title or URL so distinct citations get distinct slugs
        ident = (citation.get("title") or citation.get("gs_pdf_url") or citation.get("gs_html_url") or "").strip()
        suffix = "h" + hashlib.md5(ident.encode("utf-8")).hexdigest()[:8] if ident else f"y{citation.get('year') or 'x'}"
    suffix = re.sub(r"[^a-zA-Z0-9._-]+", "_", suffix)[:30]
    return f"{first}_{suffix}"


def has_cached_source(scholar_dir, citation):
    sl = slug_for(citation)
    src = scholar_dir / "citing_papers" / sl / "source"
    if not src.exists():
        return False
    if any(src.glob("*.tex")) or any(src.glob("**/*.tex")):
        return True
    if (src / "paper.pdf").exists():
        with open(src / "paper.pdf", "rb") as f:
            return f.read(8).startswith(b"%PDF")
    return False


# ---------- Build manifest from citations.yaml ----------


def build_manifest(scholar_dir):
    """Returns a list of dicts: {slug, fingerprints, target_path, urls, title}."""
    papers = load_yaml(scholar_dir / "papers.yaml")["papers"]
    manifest = {}
    for p in papers:
        yml = scholar_dir / "citation" / p["tag"] / "citations.yaml"
        if not yml.exists():
            continue
        doc = load_yaml(yml)
        for c in doc["citations"]:
            cls = c.get("classification") or {}
            if (cls.get("tags") or []) != ["needs_review"]:
                continue
            if has_cached_source(scholar_dir, c):
                continue
            urls = [u for u in [c.get("gs_pdf_url"), c.get("gs_html_url")] if u]
            if not urls and not c.get("doi") and not c.get("arxiv_id") and not c.get("title"):
                continue
            sl = slug_for(c)
            if sl in manifest:
                manifest[sl]["urls"].extend(urls)
                continue
            fingerprints = []
            for url in urls:
                fingerprints.extend(extract_fingerprints(url))
            # Also include DOI/arxiv suffix from citation fields directly (URL extraction can miss)
            if c.get("doi"):
                doi = c["doi"]
                fingerprints.append(doi.split("/")[-1])
                # Publisher-specific patterns from DOI (when URL doesn't expose them):
                m = re.match(r"10\.3389/(\w+)\.(\d{4})\.(\d+)$", doi)  # Frontiers
                if m:
                    fingerprints.extend([m.group(3), f"{m.group(1)}-{m.group(3)}"])
                m = re.match(r"10\.3390/([a-z]+)(\d+)$", doi)  # MDPI: cancers17213584-style
                if m:
                    digits = m.group(2)
                    if len(digits) >= 5:
                        # Last 4-5 digits = article number
                        fingerprints.extend([digits[-4:], digits[-5:], f"{m.group(1)}-{digits[:-5] or digits[:-4]}-{digits[-5:].lstrip('0') or digits[-4:]}"])
            if c.get("arxiv_id"):
                fingerprints.append(c["arxiv_id"])
            manifest[sl] = {
                "slug": sl,
                "title": c.get("title"),
                "urls": urls,
                "fingerprints": list(dict.fromkeys([f for f in fingerprints if f and len(f) >= 4])),
                "target_dir": scholar_dir / "citing_papers" / sl / "source",
            }
    return list(manifest.values())


# ---------- Match downloads ----------


def is_real_pdf(path):
    if not path.exists() or path.stat().st_size < 5000:
        return False
    with open(path, "rb") as f:
        return f.read(8).startswith(b"%PDF")


def find_match(filename_lower, manifest):
    """Return list of manifest entries whose fingerprints OR titles match the filename.

    Tries fingerprint match first (high precision); falls back to title-slug match.
    """
    fingerprint_hits = []
    for entry in manifest:
        for fp in entry["fingerprints"]:
            if fp.lower() in filename_lower:
                fingerprint_hits.append(entry)
                break
    if fingerprint_hits:
        return fingerprint_hits

    # Title-slug fallback (handles ACS / Wiley / Tandfonline filename-as-title patterns)
    fname_norm = slug_title_tokens(filename_lower)
    title_hits = []
    for entry in manifest:
        if title_match(fname_norm, entry.get("title")):
            title_hits.append(entry)
    return title_hits


def extract_pdf_first_page_title(pdf_path):
    """Extract a probable title from the PDF's first page (used when filename is too
    generic, e.g., 'out.pdf', 'Wang.pdf'). Returns a normalized token string.

    Heuristic: take the first ~10 lines of page 1, drop very short/very long lines,
    and join the remaining ones. This catches dissertation title pages, preprint
    headers, conference paper titles."""
    try:
        from pypdf import PdfReader
    except Exception:
        return ""
    try:
        reader = PdfReader(str(pdf_path))
        if not reader.pages:
            return ""
        # If page 1 is just a HAL/repository cover (very common for theses),
        # skip past it. Heuristic: page 1 mentions "hal" / "submitted on" /
        # "to cite this version" repeatedly. In those cases, page 2 has the title.
        page_texts = []
        for i in range(min(3, len(reader.pages))):
            try:
                page_texts.append(reader.pages[i].extract_text() or "")
            except Exception:
                page_texts.append("")
        text = page_texts[0]
        joined = " ".join(page_texts).lower()
        if joined.count("hal") >= 3 or "submitted on" in page_texts[0].lower() or "to cite this version" in page_texts[0].lower():
            text = "\n".join(page_texts)  # combine cover + first content page
    except Exception:
        return ""
    # First non-empty lines (skip headers/journal banners with very short or all-caps short)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    keep = []
    for ln in lines[:30]:
        if 6 <= len(ln) <= 200:
            keep.append(ln)
        if len(keep) >= 8:
            break
    return slug_title_tokens(" ".join(keep).lower())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", default=DEFAULT_SLUG)
    ap.add_argument("--downloads-dir", default=str(DEFAULT_DOWNLOADS))
    ap.add_argument("--since-hours", type=int, default=72,
                    help="Only consider PDFs in Downloads modified within this many hours (default 72)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    scholar_dir = REPO_ROOT / args.slug
    if not scholar_dir.exists():
        scholar_dir = REPO_ROOT / "scholar" / args.slug
    if not scholar_dir.exists():
        print(f"ERROR: scholar dir not found", file=sys.stderr)
        sys.exit(1)

    downloads = Path(args.downloads_dir).expanduser()
    if not downloads.exists():
        print(f"ERROR: downloads dir not found: {downloads}", file=sys.stderr)
        sys.exit(1)

    manifest = build_manifest(scholar_dir)
    print(f"Manifest: {len(manifest)} pending entries with extractable fingerprints")
    if not manifest:
        print("Nothing to match. (All entries already have cached sources, or no entries with extractable URL fingerprints.)")
        return

    cutoff = time.time() - args.since_hours * 3600
    pdf_files = [p for p in downloads.glob("*.pdf") if p.stat().st_mtime >= cutoff]
    print(f"Scanning {len(pdf_files)} recent PDFs in {downloads}")

    moved = 0
    skipped_no_match = []
    skipped_not_pdf = []
    ambiguous = []
    matched_slugs = set()

    for pdf in pdf_files:
        if not is_real_pdf(pdf):
            skipped_not_pdf.append(pdf.name)
            continue
        hits = find_match(pdf.name.lower(), manifest)
        if not hits:
            # Last-resort: extract first-page text from PDF, then try fingerprint
            # match (catches dissertation cover pages with HAL/NNT/DOI ids) AND
            # title match (catches generic filenames whose titles are on page 1).
            pdf_title_tokens = extract_pdf_first_page_title(pdf)
            if pdf_title_tokens:
                # Fingerprint match against page-1 text first (high precision)
                fp_hits = []
                for entry in manifest:
                    for fp in entry["fingerprints"]:
                        if fp.lower() in pdf_title_tokens or fp.lower().replace("-", " ") in pdf_title_tokens:
                            fp_hits.append(entry); break
                if fp_hits:
                    hits = fp_hits
                else:
                    title_hits = []
                    for entry in manifest:
                        if title_match(pdf_title_tokens, entry.get("title")):
                            title_hits.append(entry)
                    if title_hits:
                        hits = title_hits
        if not hits:
            skipped_no_match.append(pdf.name)
            continue
        if len(hits) > 1:
            # Try most-specific fingerprint (longest)
            hits.sort(key=lambda e: -max(len(f) for f in e["fingerprints"]))
        target = hits[0]
        if target["slug"] in matched_slugs:
            # Already matched another file to the same slug — skip
            ambiguous.append((pdf.name, target["slug"]))
            continue
        target_dir = target["target_dir"]
        dest = target_dir / "paper.pdf"
        if dest.exists():
            continue
        action = f"{pdf.name} → {target['slug']}"
        if args.dry_run:
            print(f"  [dry] {action}")
        else:
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(pdf), str(dest))
            print(f"  moved {action}")
        matched_slugs.add(target["slug"])
        moved += 1

    print(f"\n=== summary ===")
    print(f"  matched + moved: {moved}{' (dry run)' if args.dry_run else ''}")
    print(f"  PDFs in Downloads with no match: {len(skipped_no_match)}")
    if skipped_no_match[:5]:
        for n in skipped_no_match[:5]:
            print(f"    - {n}")
        if len(skipped_no_match) > 5:
            print(f"    (+ {len(skipped_no_match) - 5} more)")
    if skipped_not_pdf:
        print(f"  not-actually-PDF: {len(skipped_not_pdf)} (paywall/error pages)")
    if ambiguous:
        print(f"  ambiguous (multiple files mapped to same slug, kept first): {len(ambiguous)}")

    if not args.dry_run and moved:
        print(f"\nNext: re-run reclassify_from_source.py to process the new sources")


if __name__ == "__main__":
    main()
