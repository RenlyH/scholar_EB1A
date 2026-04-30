#!/usr/bin/env python3
"""reclassify-from-source runner.

Automates the pipeline described in `.claude/skills/reclassify-from-source/SKILL.md`:

  build queue → dedupe → download → parse bib → find cite sites → classify
              → update citations.yaml (with cross-paper propagation)

Run from the repo root:

  uv run --with pyyaml --with pypdf scripts/reclassify_from_source.py
  uv run --with pyyaml --with pypdf scripts/reclassify_from_source.py --paper hidisc_2303.01605
  uv run --with pyyaml --with pypdf scripts/reclassify_from_source.py --max-downloads 5 --dry-run

This is v1: tex path is fully automatic; PDF path attempts download + extraction
but degrades to `source_no_match` for hard cases. Substantive (non-cluster)
citations get rich `source_contexts` attached but keep `[needs_review]` tags
unless they match a high-confidence keyword pattern.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import yaml

# ---------- Constants ----------

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SLUG = "xinhaihou_umich"
NAME_VARIANTS = {"Xinhai Hou", "X. Hou", "X Hou"}
SCHOLAR_S2_AUTHOR_ID = "1784658702"
TODAY = str(date.today())

USER_AGENT = "Mozilla/5.0 (compatible; scholar-reclassify/0.1)"
ARXIV_SRC_TPL = "https://arxiv.org/src/{arxiv_id}"
NATURE_PDF_TPL = "https://www.nature.com/articles/{suffix}.pdf"

# Drive-by detection (auto-tag without LLM)
RX_BRACKET_CLUSTER = re.compile(r"\[\d+\s*[-–,]\s*\d+(?:\s*[-–,]\s*\d+)*\]")
RX_NATBIB_CITE = re.compile(r"\\cite[a-z]*\{([^}]+)\}")
RX_SUPERSCRIPT_CLUSTER = re.compile(r"(?<=\D)\d+(?:[-–,]\s*\d+){2,}(?!\d)")  # e.g., 9-11,15,16

# Substantive auto-classification keyword cues (high confidence)
RX_METHODOLOGY = re.compile(
    r"\b(follow(?:s|ing|ed)?|we (?:use|adopt|apply|reuse|build on|based on)|"
    r"trained\s+on|hyperparameters?\s+(?:from|follow)|"
    r"loss\s+(?:from|of)|inherit(?:s|ed|ing)?|architecture\s+(?:from|of))\b",
    re.I,
)
RX_BASELINE = re.compile(
    r"\b(comparing\s+(?:with|to|against)|compared?\s+(?:with|to|against)|"
    r"as\s+a\s+baseline|baseline\s+method|outperform(?:s|ed)?|"
    r"benchmark\s+against|state-of-the-art)\b",
    re.I,
)
RX_INSPIRED = re.compile(
    r"\b(inspired\s+by|extend(?:s|ing|ed)?|building\s+on|builds?\s+upon|"
    r"motivated\s+by|generaliz(?:e|ation)\s+of|special\s+case\s+of|"
    r"becomes\s+equivalent\s+to)\b",
    re.I,
)
RX_ACK = re.compile(
    r"\b((?:[A-Z][a-zA-Z]+\s+)?et\s+al\.|[A-Z][a-zA-Z]+\s+and\s+colleagues|"
    r"prior\s+work|previous\s+work|recent(?:ly)?|introduced|proposed|"
    r"showed|demonstrated|reported|developed)\b",
    re.I,
)

# Reference-list start markers
# Liberal — Nature-style PDFs often interleave column text after the header.
# We accept "References" or "Bibliography" at the start of a line, optionally followed
# by extra text from an adjacent column.
REFS_START_RX = re.compile(r"^[\s]*(References|Bibliography|REFERENCES)\b", re.M)

# ---------- YAML loading/saving (preserve multiline strings) ----------


def _str_repr(dumper, data):
    style = "|" if ("\n" in data or len(data) > 100) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


yaml.add_representer(str, _str_repr)


def load_yaml(p):
    return yaml.safe_load(Path(p).read_text())


def save_yaml(p, data):
    Path(p).write_text(yaml.dump(data, sort_keys=False, allow_unicode=True, width=100))


# ---------- Data ----------


class CitingPaper:
    """One unique citing paper, possibly cited by multiple Xinhai papers."""

    def __init__(self, s2_paper_id, title, arxiv_id, doi, year, authors, is_self_cite,
                 gs_html_url=None, gs_pdf_url=None, pmcid=None):
        self.s2_paper_id = s2_paper_id
        self.title = title or ""
        self.arxiv_id = arxiv_id
        self.doi = doi
        self.year = year
        self.authors = authors or []
        self.is_self_cite = is_self_cite
        self.gs_html_url = gs_html_url
        self.gs_pdf_url = gs_pdf_url
        self.pmcid = pmcid
        # Targets: list of (xinhai_tag, xinhai_arxiv_id, xinhai_doi, xinhai_title)
        self.targets = []

    @property
    def dedupe_key(self):
        """Stable key for deduplication; prefers s2_paper_id, falls back to URL/title."""
        return self.s2_paper_id or self.gs_html_url or f"title:{self.title[:80]}"

    def slug(self):
        """Storage tag for citing_papers/<slug>/."""
        import hashlib
        first = self.authors[0].split()[-1].lower() if self.authors else "anon"
        first = re.sub(r"[^a-z0-9]+", "", first)[:12]
        if self.arxiv_id:
            suffix = self.arxiv_id
        elif self.doi:
            suffix = self.doi.split("/")[-1]
        else:
            ident = (self.title or self.gs_pdf_url or self.gs_html_url or "").strip()
            suffix = "h" + hashlib.md5(ident.encode("utf-8")).hexdigest()[:8] if ident else f"y{self.year or 'x'}"
        suffix = re.sub(r"[^a-zA-Z0-9._-]+", "_", suffix)[:30]
        return f"{first}_{suffix}"


class TargetPaper:
    """A Xinhai paper that's a target of citation."""

    def __init__(self, tag, s2_paper_id, arxiv_id, doi, title):
        self.tag = tag
        self.s2_paper_id = s2_paper_id
        self.arxiv_id = arxiv_id
        self.doi = doi
        self.title = title


# ---------- Step 1: Build queue ----------


def build_queue(scholar_dir, only_paper=None, include_borderline=False):
    """Return (queue, target_index)

    queue: dict keyed by citing s2_paper_id → CitingPaper (with .targets populated)
    target_index: dict keyed by xinhai s2_paper_id → TargetPaper
    """
    citation_dir = scholar_dir / "citation"
    papers = load_yaml(scholar_dir / "papers.yaml")["papers"]

    target_index = {
        p["s2_paper_id"]: TargetPaper(p["tag"], p["s2_paper_id"], p.get("arxiv_id"), p.get("doi"), p["title"])
        for p in papers
    }

    queue = {}
    for paper in papers:
        if only_paper and paper["tag"] != only_paper:
            continue
        yml = citation_dir / paper["tag"] / "citations.yaml"
        if not yml.exists():
            continue
        doc = load_yaml(yml)
        for cit in doc.get("citations", []):
            cls = cit.get("classification") or {}
            tags = cls.get("tags") or []
            method = cls.get("method")
            qualifies = (
                tags == ["needs_review"]
                or (include_borderline and method == "auto_heuristic")
            )
            if not qualifies:
                continue
            cp = CitingPaper(
                cit.get("s2_paper_id"),
                cit.get("title"),
                cit.get("arxiv_id"),
                cit.get("doi"),
                cit.get("year"),
                cit.get("authors"),
                cit.get("is_self_citation", False),
                gs_html_url=cit.get("gs_html_url"),
                gs_pdf_url=cit.get("gs_pdf_url"),
                pmcid=cit.get("pmcid"),
            )
            key = cp.dedupe_key
            if key not in queue:
                queue[key] = cp
            queue[key].targets.append(target_index[paper["s2_paper_id"]])
    return queue, target_index


# ---------- Step 2: Get or download source ----------


def http_get(url, out_path, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(out_path, "wb") as f:
            shutil.copyfileobj(resp, f)
        return True, None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        return False, str(e)


def _save_pdf_if_real(url, pdf_path):
    """Download a URL into pdf_path; return True only if it's a real PDF."""
    ok, err = http_get(url, pdf_path)
    if not ok or pdf_path.stat().st_size < 10000:
        if pdf_path.exists():
            pdf_path.unlink()
        return False, err or "too_small"
    with open(pdf_path, "rb") as f:
        head = f.read(8)
    if not head.startswith(b"%PDF"):
        pdf_path.unlink()
        return False, "not_a_pdf"
    return True, None


def _try_pmc_pdf(pmcid_or_url, pdf_path):
    """Resolve a PMC URL to its actual PDF link by scraping the landing page."""
    pmcid = pmcid_or_url
    m = re.search(r"PMC\d+", pmcid_or_url)
    if m:
        pmcid = m.group(0)
    landing = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
    try:
        req = urllib.request.Request(landing, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return False, str(e)
    # Find the actual PDF filename: pdf/<filename>.pdf
    m = re.search(r'href="(/articles/' + re.escape(pmcid) + r'/pdf/[^"]+\.pdf)"', html)
    if not m:
        m = re.search(r'(pdf/[^"\']+\.pdf)', html)
        if not m:
            return False, "pmc_pdf_link_not_found"
    pdf_url = "https://pmc.ncbi.nlm.nih.gov" + m.group(1) if m.group(1).startswith("/") else f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/{m.group(1)}"
    return _save_pdf_if_real(pdf_url, pdf_path)


def get_or_download_source(citing, scholar_dir, target_index_by_pid):
    """Return (source_dir, source_format) or (None, reason).

    Priority order:
      1. Cached source (already on disk)
      2. arxiv tex (citing.arxiv_id)
      3. gs_pdf_url (pre-resolved OA mirror from Chrome extension)
      4. pmcid → PMC PDF (via landing-page scrape)
      5. Nature OA suffix pattern (legacy fallback)
    """
    # Special case: citing paper IS one of Xinhai's own publications → use publications/
    own = next((t for t in target_index_by_pid.values() if t.s2_paper_id == citing.s2_paper_id), None) if citing.s2_paper_id else None
    if own:
        publication_source = scholar_dir / "publications" / own.tag / "source"
        if any(publication_source.glob("*.tex")):
            return publication_source, "tex"

    citing_tag = citing.slug()
    base_dir = (
        scholar_dir / "publications" / own.tag if own else scholar_dir / "citing_papers" / citing_tag
    )
    src = base_dir / "source"
    src.mkdir(parents=True, exist_ok=True)

    # Already cached?
    if any(src.glob("*.tex")):
        return src, "tex"
    if (src / "paper.pdf").exists():
        return src, "pdf"

    # Tier 1: arxiv tex (cleanest for matching \cite keys)
    if citing.arxiv_id:
        tarball = src / "source.tar.gz"
        ok, err = http_get(ARXIV_SRC_TPL.format(arxiv_id=citing.arxiv_id), tarball)
        if ok and tarball.stat().st_size > 1000:
            try:
                with tarfile.open(tarball) as t:
                    t.extractall(src)
                if any(src.glob("*.tex")) or any(src.glob("**/*.tex")):
                    return src, "tex"
            except Exception:
                pass

    # Tier 2: gs_pdf_url (pre-resolved OA mirror; usually works)
    if citing.gs_pdf_url:
        pdf_path = src / "paper.pdf"
        # PMC URLs need landing-page scrape
        if "pmc.ncbi.nlm.nih.gov" in citing.gs_pdf_url:
            ok, err = _try_pmc_pdf(citing.gs_pdf_url, pdf_path)
            if ok:
                return src, "pdf"
        else:
            ok, err = _save_pdf_if_real(citing.gs_pdf_url, pdf_path)
            if ok:
                return src, "pdf"

    # Tier 3: PMC via pmcid field
    if citing.pmcid:
        pdf_path = src / "paper.pdf"
        ok, err = _try_pmc_pdf(citing.pmcid, pdf_path)
        if ok:
            return src, "pdf"

    # Tier 4: legacy Nature OA suffix pattern
    OA_NATURE_PREFIXES = ("s41467-", "s41746-", "s41598-", "s43856-", "s41377-", "s43018-")
    if citing.doi and citing.doi.startswith("10.1038/"):
        suffix = citing.doi.split("/", 1)[1]
        if any(suffix.startswith(p) for p in OA_NATURE_PREFIXES):
            pdf_path = src / "paper.pdf"
            for url in [
                f"https://www.nature.com/articles/{suffix}_reference.pdf",
                NATURE_PDF_TPL.format(suffix=suffix),
            ]:
                ok, err = _save_pdf_if_real(url, pdf_path)
                if ok:
                    return src, "pdf"

    return None, "no_source_available"


# ---------- Step 3: Parse bibliography (tex) ----------


def _norm_for_match(s):
    """Normalize text for fuzzy matching: lowercase, strip punctuation, collapse whitespace."""
    s = s.lower()
    s = s.replace("ﬁ", "fi").replace("ﬂ", "fl").replace("ﬀ", "ff").replace("ﬃ", "ffi")
    # Replace any non-alphanumeric with single space
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_bib_tex(source_dir, target_index):
    """Find bibitem keys in tex source for any of our target papers.

    Returns: dict {target_tag: bibkey}
    """
    bibs = list(source_dir.glob("**/*.bib")) + list(source_dir.glob("**/*.bbl"))
    if not bibs:
        # Sometimes inline in main.tex
        bibs = list(source_dir.glob("**/*.tex"))
    text = "\n".join(b.read_text(errors="replace") for b in bibs)

    # Extract all entries: @article{key, ...} or \bibitem{key} ...
    entries = []
    # bibtex style
    for m in re.finditer(r"@\w+\{([^,\s]+)\s*,([\s\S]*?)\n\}", text):
        entries.append((m.group(1).strip(), m.group(2)))
    # \bibitem style (in .bbl)
    for m in re.finditer(r"\\bibitem(?:\[[^\]]*\])?\{([^}]+)\}([\s\S]*?)(?=\\bibitem|$)", text):
        entries.append((m.group(1).strip(), m.group(2)))

    found = {}
    for tag, target in [(t.tag, t) for t in target_index]:
        target_arxiv = (target.arxiv_id or "").lower()
        target_doi = (target.doi or "").lower()
        # Build fuzzy-match probes: 6 consecutive words from the title (normalized)
        title_norm = _norm_for_match(target.title)
        title_tokens = title_norm.split()
        probes = []
        if len(title_tokens) >= 6:
            probes.append(" ".join(title_tokens[:6]))
        if len(title_tokens) >= 5:
            probes.append(" ".join(title_tokens[:5]))
        # Distinctive single tokens (unusual ≥6-char words make good anchors)
        # E.g. "intraoperative", "neurosurgery"
        rare = [t for t in title_tokens if len(t) >= 8][:3]
        for tag2, target in [(t.tag, t) for t in target_index]:
            pass  # placeholder
        for key, body in entries:
            body_lower = body.lower()
            if target_arxiv and target_arxiv in body_lower:
                found[tag] = key
                break
            if target_doi and target_doi in body_lower:
                found[tag] = key
                break
            body_norm = _norm_for_match(body)
            matched = False
            for probe in probes:
                if probe in body_norm:
                    matched = True
                    break
            if matched:
                found[tag] = key
                break
    return found


# ---------- Step 4: Find cite sites (tex) ----------


def find_cite_sites_tex(source_dir, bibkey):
    """Find every \\cite[a-z]*{...key...} site in tex source.

    Returns: list of dicts with keys: file, line, paragraph, sentence, co_cited
    """
    sites = []
    pattern = re.compile(r"\\cite[a-z*]*\{([^}]*\b" + re.escape(bibkey) + r"\b[^}]*)\}")
    for tex in sorted(source_dir.glob("**/*.tex")):
        try:
            content = tex.read_text(errors="replace")
        except Exception:
            continue
        lines = content.splitlines()
        for lineno, line in enumerate(lines, 1):
            for m in pattern.finditer(line):
                keys = [k.strip() for k in m.group(1).split(",")]
                co_cited = [k for k in keys if k != bibkey]
                # Reconstruct paragraph (the surrounding non-empty lines)
                start = lineno - 1
                while start > 0 and lines[start - 1].strip():
                    start -= 1
                end = lineno
                while end < len(lines) and lines[end].strip():
                    end += 1
                paragraph = "\n".join(lines[start:end]).strip()
                sites.append({
                    "file": str(tex.relative_to(source_dir)),
                    "line": lineno,
                    "paragraph": paragraph[:2000],
                    "sentence": line.strip()[:1000],
                    "co_cited": co_cited,
                    "n_keys_in_brace": len(keys),
                })
    return sites


# ---------- Step 4b: Find cite sites (PDF) ----------


def pdftotext(pdf_path, out_path):
    """Convert PDF to text using pdftotext or pypdf fallback."""
    if shutil.which("pdftotext"):
        try:
            subprocess.run(["pdftotext", "-layout", str(pdf_path), str(out_path)], check=True, timeout=60)
            return out_path.read_text(errors="replace")
        except Exception:
            pass
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf_path))
        text = "\n\n".join(p.extract_text() for p in reader.pages)
        out_path.write_text(text)
        return text
    except Exception as e:
        return ""


def find_refs_section(text):
    """Find the start of the References section. Handles column-interleaved layouts where
    'References' may not be at the start of a line."""
    # Try strict line-start first
    m = re.search(r"^[\s]*(References|Bibliography|REFERENCES)\b", text, re.M)
    if m:
        return m
    # Lenient: anywhere, but only if followed within ~200 chars by a numbered ref or surname pattern
    for m in re.finditer(r"\b(References|Bibliography|REFERENCES)\b", text):
        tail = text[m.end(): m.end() + 500]
        if re.search(r"\b\d+\.\s+[A-Z]", tail) or re.search(r"\b[A-Z][a-z]+,\s*[A-Z]\.\s", tail):
            return m
    return None


def find_pdf_refnum(text, target, debug=False):
    """Determine the reference number for `target` in the citing paper's bibliography.

    Returns int or None.
    """
    target_doi = (target.doi or "").lower()
    target_arxiv = (target.arxiv_id or "").lower()
    title_norm = _norm_for_match(target.title)
    title_tokens = title_norm.split()
    probes = []
    if len(title_tokens) >= 6:
        probes.append(" ".join(title_tokens[:6]))
    if len(title_tokens) >= 5:
        probes.append(" ".join(title_tokens[:5]))

    m = find_refs_section(text)
    if not m:
        if debug:
            print(f"        [debug] no References header found in {len(text)} chars")
        return None
    refs_text = text[m.end():]

    # Heuristic 1: refs numbered "12. <entry>" or "[12] <entry>".
    # Slice each entry's body from its header to the next header so multi-line
    # entries are captured without bleeding into the next ref.
    headers = list(re.finditer(
        r"^\s*(?:\[\s*(\d+)\s*\]|(\d+)\.)\s+",
        refs_text, re.M,
    ))
    for i, hm in enumerate(headers):
        num = int(hm.group(1) or hm.group(2))
        start = hm.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(refs_text)
        body_norm = _norm_for_match(refs_text[start:end])
        if target_arxiv and target_arxiv in body_norm:
            return num
        if target_doi and target_doi.replace(".", " ") in body_norm:
            return num
        for probe in probes:
            if probe in body_norm:
                return num

    # Heuristic 2: count entries in order (when leading numbers are stripped)
    entries = []
    for line in refs_text.splitlines()[:1500]:
        s = line.strip()
        if not s:
            continue
        if re.match(r"^\s*(Acknowledgements|Author contributions|Competing interests|Funding|Peer review|Supplementary)\b", s):
            break
        if re.match(r"^([A-Z][a-z]+,?\s|[A-Z]\.\s|[0-9]+\.\s)", s):
            entries.append(s)
        elif entries:
            entries[-1] += " " + s

    for i, e in enumerate(entries, 1):
        body_norm = _norm_for_match(e)
        if target_arxiv and target_arxiv in body_norm:
            return i
        if target_doi and target_doi.replace(".", " ") in body_norm:
            return i
        for probe in probes:
            if probe in body_norm:
                return i

    if debug:
        print(f"        [debug] no match: probes={probes}, {len(entries)} entries scanned, refs_text[:100]={refs_text[:100]!r}")
    return None


def find_cite_sites_pdf(text, refnum):
    """Find body cites of [N] / superscript N / (Year, N) for a given reference number."""
    # Truncate at References section
    m = find_refs_section(text)
    body = text[: m.start()] if m else text

    sites = []
    # Pattern: superscript or bracket cluster including refnum
    # Look for things like "diagnoses9-11,15,16" or "[5,12,14]"
    refnum_str = str(refnum)
    for line in body.splitlines():
        # Cluster pattern e.g., 9-11,15,16
        for m in re.finditer(r"(\d+(?:[-–,]\s*\d+){1,})", line):
            cluster = m.group(1)
            nums = expand_cluster(cluster)
            if refnum in nums:
                # Reconstruct paragraph (surrounding non-blank lines)
                sites.append({
                    "sentence": line.strip()[:1000],
                    "paragraph": line.strip()[:2000],
                    "co_cited": [str(n) for n in nums if n != refnum],
                    "n_keys_in_brace": len(nums),
                    "match_pattern": cluster,
                })
        # Also match `[N]` or trailing single `N` (less reliable; only match if surrounded by sentence-ending punct)
        for m in re.finditer(r"\[" + refnum_str + r"\]|(?<=[a-z])" + refnum_str + r"(?=[\s.,;)])", line):
            sites.append({
                "sentence": line.strip()[:1000],
                "paragraph": line.strip()[:2000],
                "co_cited": [],
                "n_keys_in_brace": 1,
                "match_pattern": m.group(0),
            })
    return sites


def expand_cluster(s):
    """Expand a cluster like '9-11,15,16' into [9,10,11,15,16]."""
    nums = []
    for part in re.split(r",\s*", s):
        if "-" in part or "–" in part:
            a, b = re.split(r"[-–]", part, 1)
            try:
                nums.extend(range(int(a), int(b) + 1))
            except ValueError:
                continue
        else:
            try:
                nums.append(int(part))
            except ValueError:
                continue
    return nums


# ---------- Step 5: Classify ----------


def classify_from_sites(sites):
    """Return (tags, rationale) given source-extracted cite sites.

    Auto-classifies clear drive_by; substantive cases get heuristic patterns.
    Falls back to needs_review with rich contexts if no rule fires.
    """
    if not sites:
        return ["source_no_match"], "Source downloaded but no cite site found — likely an indexer false-positive in S2."
    # Drive_by: every site has 3+ keys in the brace
    if all(s.get("n_keys_in_brace", 1) >= 3 for s in sites):
        n = sites[0]["n_keys_in_brace"]
        loc = f"{sites[0].get('file','')}{':' + str(sites[0]['line']) if sites[0].get('line') else ''}".lstrip(":")
        return ["drive_by"], (
            f"Source-verified drive_by: cited within a {n}-key cluster"
            f"{' across ' + str(len(sites)) + ' sites' if len(sites) > 1 else ''} (location: {loc}). "
            "No individual discussion."
        )
    # Mixed or single-key cite — try heuristics on the joined paragraphs
    joined = "\n".join(s.get("paragraph", "") for s in sites)
    tags = []
    if RX_BASELINE.search(joined):
        tags.append("baseline")
    if RX_METHODOLOGY.search(joined):
        tags.append("methodology")
    if RX_INSPIRED.search(joined):
        tags.append("inspired_by")
    if not tags and RX_ACK.search(joined):
        tags.append("acknowledgment")
    if tags:
        return tags, (
            f"Source-verified ({len(sites)} site{'s' if len(sites) != 1 else ''}). "
            f"Pattern matches: {', '.join(tags)}. "
            f"Sample: '{joined[:200].strip()}...'"
        )
    return ["needs_review"], (
        f"Source extracted ({len(sites)} site{'s' if len(sites) != 1 else ''}) "
        "but no high-confidence keyword pattern matched. Source contexts attached for human review."
    )


# ---------- Step 6: Update citations.yaml ----------


def update_yaml_for_target(citation_dir, target_tag, citing, classification, tags, rationale, sites, source_paper_tag, bibkey=None, refnum=None):
    """Update or ADD an entry for `citing` in target_tag's citations.yaml."""
    yml = citation_dir / target_tag / "citations.yaml"
    doc = load_yaml(yml)

    # Find existing. Match by s2_paper_id when both are non-null, otherwise
    # fall back to DOI / arxiv_id / fuzzy title (which is the only signal for
    # gs_extension-only citations whose s2_paper_id is None — without this,
    # the next() below grabs the FIRST None-id row, which is almost never the
    # correct one).
    target_entry = None
    if citing.s2_paper_id:
        target_entry = next(
            (c for c in doc["citations"] if c.get("s2_paper_id") == citing.s2_paper_id),
            None,
        )
    if target_entry is None and citing.doi:
        target_entry = next(
            (c for c in doc["citations"] if (c.get("doi") or "").lower() == citing.doi.lower()),
            None,
        )
    if target_entry is None and citing.arxiv_id:
        target_entry = next(
            (c for c in doc["citations"] if (c.get("arxiv_id") or "").lower() == citing.arxiv_id.lower()),
            None,
        )
    if target_entry is None and citing.title:
        t_norm = _norm_for_match(citing.title)
        for c in doc["citations"]:
            if _norm_for_match(c.get("title") or "") == t_norm:
                target_entry = c; break
    added = False
    if not target_entry:
        # Add a stub entry — S2 missed this cite
        target_entry = {
            "s2_paper_id": citing.s2_paper_id,
            "title": citing.title,
            "year": citing.year,
            "venue": None,
            "arxiv_id": citing.arxiv_id,
            "doi": citing.doi,
            "authors": citing.authors,
            "citation_count_of_citing": None,
            "is_self_citation": citing.is_self_cite,
            "s2_signals": {"is_influential": False, "intents": []},
            "contexts": [],
            "abstract": None,
        }
        doc["citations"].append(target_entry)
        added = True

    # Convert sites → source_contexts
    source_contexts = [
        {
            "paragraph": s.get("paragraph"),
            "sentence": s.get("sentence"),
            "co_cited": s.get("co_cited", []),
            "location": (s["file"] + ":" + str(s["line"])) if "file" in s else "(pdf body)",
        }
        for s in sites
    ]

    cls = {
        "tags": tags,
        "rationale": rationale,
        "method": "from_source",
        "classified_at": TODAY,
        "source_paper_tag": source_paper_tag,
    }
    if bibkey:
        cls["bibitem_key"] = bibkey
    if refnum is not None:
        cls["bibitem_number"] = refnum
    co_cited_union = sorted({c for s in sites for c in s.get("co_cited", [])})
    if co_cited_union:
        cls["co_cited_keys"] = co_cited_union
    target_entry["classification"] = cls
    if source_contexts:
        target_entry["source_contexts"] = source_contexts

    # Resort: influential first, then by # contexts desc, then year desc
    doc["citations"].sort(key=lambda x: (
        not (x.get("s2_signals") or {}).get("is_influential", False),
        -len(x.get("contexts") or []),
        -(x.get("year") or 0),
    ))

    # Refresh stats
    cnt = Counter()
    classified = 0
    for c in doc["citations"]:
        ct = (c.get("classification") or {}).get("tags") or []
        if ct:
            classified += 1
            for t in ct:
                cnt[t] += 1
    doc["stats"]["classified"] = classified
    doc["stats"]["classification_distribution"] = dict(cnt)
    if added:
        doc["stats"]["total_citations_added_from_source"] = doc["stats"].get("total_citations_added_from_source", 0) + 1
        doc["stats"]["total_citations_s2"] = doc["stats"].get("total_citations_s2", 0) + 1

    save_yaml(yml, doc)
    return added


# ---------- Failure log ----------


def append_failure(failed_path, citing, reason):
    failed_path.parent.mkdir(parents=True, exist_ok=True)
    if failed_path.exists():
        data = load_yaml(failed_path) or {"failed": []}
    else:
        data = {"failed": []}
    if not any(f["s2_paper_id"] == citing.s2_paper_id for f in data["failed"]):
        data["failed"].append({
            "s2_paper_id": citing.s2_paper_id,
            "title": citing.title,
            "arxiv_id": citing.arxiv_id,
            "doi": citing.doi,
            "reason": reason,
            "logged_at": TODAY,
        })
        save_yaml(failed_path, data)


# ---------- Main ----------


def run(scholar_dir, only_paper=None, max_downloads=None, dry_run=False, include_borderline=False):
    citation_dir = scholar_dir / "citation"
    queue, target_index = build_queue(scholar_dir, only_paper=only_paper, include_borderline=include_borderline)
    target_index_by_pid = {t.s2_paper_id: t for t in target_index.values()}
    target_list = list(target_index.values())

    # Sort: arxiv-available first (cheap, reliable tex), then nature OA, then paywalled
    OA_NATURE_PREFIXES = ("s41467-", "s41746-", "s41598-", "s43856-", "s41377-", "s43018-")
    def _priority(cp):
        if cp.arxiv_id: return 0
        if cp.doi and cp.doi.startswith("10.1038/") and any(cp.doi.split("/", 1)[1].startswith(p) for p in OA_NATURE_PREFIXES): return 1
        return 2
    queue_sorted = sorted(queue.values(), key=_priority)

    print(f"Queue: {len(queue)} unique citing papers across {sum(len(c.targets) for c in queue.values())} citation entries")
    n_arxiv = sum(1 for cp in queue_sorted if cp.arxiv_id)
    n_oa_nature = sum(1 for cp in queue_sorted if not cp.arxiv_id and cp.doi and cp.doi.startswith("10.1038/") and any(cp.doi.split("/", 1)[1].startswith(p) for p in OA_NATURE_PREFIXES))
    print(f"  arxiv-available: {n_arxiv} | OA Nature: {n_oa_nature} | other (likely paywalled): {len(queue_sorted) - n_arxiv - n_oa_nature}")
    if dry_run:
        for cp in queue_sorted[: (max_downloads or 50)]:
            tags = [t.tag for t in cp.targets]
            print(f"  [{cp.arxiv_id or cp.doi or '—'}] targets={tags} | {cp.title[:70]}")
        return

    failed_path = scholar_dir / "citing_papers" / ".failed.yaml"
    stats = Counter()
    processed = 0

    for citing in queue_sorted:
        if max_downloads and processed >= max_downloads:
            break
        processed += 1
        print(f"\n[{processed}] {citing.title[:80]}")
        print(f"    arxiv={citing.arxiv_id} doi={citing.doi} → targets={[t.tag for t in citing.targets]}")

        source_dir, fmt = get_or_download_source(citing, scholar_dir, target_index_by_pid)
        if source_dir is None:
            print(f"    SKIP: {fmt}")
            stats["unavailable"] += 1
            for tg in citing.targets:
                # Mark each target's entry with the unavailable status
                yml = citation_dir / tg.tag / "citations.yaml"
                doc = load_yaml(yml)
                ent = next((c for c in doc["citations"] if c["s2_paper_id"] == citing.s2_paper_id), None)
                if ent:
                    ent["classification"] = {
                        "tags": ["needs_review"],
                        "rationale": f"Citing paper unretrievable: {fmt}.",
                        "method": "source_unavailable",
                        "classified_at": TODAY,
                    }
                    save_yaml(yml, doc)
            append_failure(failed_path, citing, fmt)
            continue

        source_paper_tag = source_dir.parent.name
        if fmt == "tex":
            bib_map = parse_bib_tex(source_dir, target_list)
            print(f"    bib_map: {bib_map}")
            for target in citing.targets:
                bibkey = bib_map.get(target.tag)
                if not bibkey:
                    update_yaml_for_target(
                        citation_dir, target.tag, citing,
                        cls=None, tags=["needs_review"],
                        rationale="Source downloaded (tex) but no bibitem matched the cited paper — likely S2 false-positive.",
                        sites=[], source_paper_tag=source_paper_tag,
                    ) if False else None  # hack: avoid the cls=None path below; just skip
                    yml = citation_dir / target.tag / "citations.yaml"
                    doc = load_yaml(yml)
                    ent = next((c for c in doc["citations"] if c["s2_paper_id"] == citing.s2_paper_id), None)
                    if ent:
                        ent["classification"] = {
                            "tags": ["needs_review"],
                            "rationale": "Source downloaded (tex) but no bibitem matched the cited paper — likely S2 false-positive citation.",
                            "method": "source_no_match",
                            "classified_at": TODAY,
                            "source_paper_tag": source_paper_tag,
                        }
                        save_yaml(yml, doc)
                    stats["no_match"] += 1
                    continue
                sites = find_cite_sites_tex(source_dir, bibkey)
                tags, rat = classify_from_sites(sites)
                added = update_yaml_for_target(
                    citation_dir, target.tag, citing,
                    classification=None, tags=tags, rationale=rat, sites=sites,
                    source_paper_tag=source_paper_tag, bibkey=bibkey,
                )
                stats["+".join(tags)] += 1
                if added:
                    stats["added_missing_cite"] += 1
                print(f"      → {target.tag}: tags={tags} sites={len(sites)} bibkey={bibkey} added={added}")
            # Cross-paper propagation: also check OTHER xinhai papers in the bib that aren't yet in targets
            extra = {tag for tag in bib_map if tag not in {t.tag for t in citing.targets}}
            for extra_tag in extra:
                target = next(t for t in target_list if t.tag == extra_tag)
                bibkey = bib_map[extra_tag]
                sites = find_cite_sites_tex(source_dir, bibkey)
                tags, rat = classify_from_sites(sites)
                rat = "[Cross-paper discovery: S2 had not surfaced this cite] " + rat
                added = update_yaml_for_target(
                    citation_dir, extra_tag, citing,
                    classification=None, tags=tags, rationale=rat, sites=sites,
                    source_paper_tag=source_paper_tag, bibkey=bibkey,
                )
                stats["cross_paper_discovered"] += 1
                if added:
                    stats["added_missing_cite"] += 1
                print(f"      → {extra_tag} (DISCOVERED): tags={tags} sites={len(sites)} added={added}")
        elif fmt == "pdf":
            pdf_path = source_dir / "paper.pdf"
            txt_path = source_dir / "paper.txt"
            if not txt_path.exists():
                pdftotext(pdf_path, txt_path)
            text = txt_path.read_text(errors="replace") if txt_path.exists() else ""
            for target in citing.targets:
                refnum = find_pdf_refnum(text, target, debug=True)
                if refnum is None:
                    yml = citation_dir / target.tag / "citations.yaml"
                    doc = load_yaml(yml)
                    ent = next((c for c in doc["citations"] if c["s2_paper_id"] == citing.s2_paper_id), None)
                    if ent:
                        ent["classification"] = {
                            "tags": ["needs_review"],
                            "rationale": "Source downloaded (PDF) but could not match cited paper to a reference number.",
                            "method": "source_no_match",
                            "classified_at": TODAY,
                            "source_paper_tag": source_paper_tag,
                        }
                        save_yaml(yml, doc)
                    stats["no_match"] += 1
                    print(f"      → {target.tag}: NO refnum match")
                    continue
                sites = find_cite_sites_pdf(text, refnum)
                tags, rat = classify_from_sites(sites)
                added = update_yaml_for_target(
                    citation_dir, target.tag, citing,
                    classification=None, tags=tags, rationale=rat, sites=sites,
                    source_paper_tag=source_paper_tag, refnum=refnum,
                )
                stats["+".join(tags)] += 1
                if added:
                    stats["added_missing_cite"] += 1
                print(f"      → {target.tag}: refnum={refnum} tags={tags} sites={len(sites)} added={added}")

    print(f"\n=== run summary ===")
    for k, v in sorted(stats.items(), key=lambda kv: -kv[1]):
        print(f"  {k:30s} {v}")
    print(f"  total processed: {processed}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", default=DEFAULT_SLUG)
    ap.add_argument("--paper", default=None, help="Limit to one of the scholar's papers (by tag)")
    ap.add_argument("--max-downloads", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-borderline", action="store_true",
                    help="Also re-process auto_heuristic-classified entries")
    args = ap.parse_args()

    scholar_dir = REPO_ROOT / "scholar" / args.slug
    if not scholar_dir.exists():
        # Allow running with the scholar at the repo root (not in scholar/)
        alt = REPO_ROOT / args.slug
        if alt.exists():
            scholar_dir = alt
        else:
            print(f"ERROR: scholar directory not found: {scholar_dir}", file=sys.stderr)
            sys.exit(1)

    run(
        scholar_dir,
        only_paper=args.paper,
        max_downloads=args.max_downloads,
        dry_run=args.dry_run,
        include_borderline=args.include_borderline,
    )


if __name__ == "__main__":
    main()
