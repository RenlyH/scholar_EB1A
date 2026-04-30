#!/usr/bin/env python3
"""discover-citations runner.

For each paper in scholar/<slug>/papers.yaml with an s2_paper_id, fetches
citation metadata (contexts, intents, isInfluential, abstract, etc.) from
Semantic Scholar and merges into citation/<tag>/citations.yaml. Existing
entries are matched by DOI / arXiv / fuzzy title; new entries are appended.

Usage:
  uv run --with pyyaml scripts/discover_citations.py --slug yunjiazhang_wisc
  uv run --with pyyaml scripts/discover_citations.py --slug yunjiazhang_wisc --paper reactable_2310.00815
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TODAY = str(date.today())
S2_BASE = "https://api.semanticscholar.org/graph/v1"

FIELDS = ",".join([
    "contexts", "intents", "isInfluential",
    "citingPaper.paperId", "citingPaper.title", "citingPaper.year",
    "citingPaper.authors", "citingPaper.venue", "citingPaper.externalIds",
    "citingPaper.citationCount", "citingPaper.abstract",
])


def _str_repr(dumper, data):
    style = "|" if ("\n" in data or len(data) > 100) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


yaml.add_representer(str, _str_repr)


def load_yaml(p):
    return yaml.safe_load(Path(p).read_text())


def save_yaml(p, data):
    Path(p).write_text(yaml.dump(data, sort_keys=False, allow_unicode=True, width=100))


def read_env(env_path, key):
    if not Path(env_path).exists():
        return None
    for line in Path(env_path).read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _norm(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def title_match(probe, candidate):
    p, c = _norm(probe), _norm(candidate)
    if not p or not c:
        return False
    if p == c:
        return True
    pt, ct = set(p.split()), set(c.split())
    smaller = min(len(pt), len(ct))
    if smaller >= 5 and len(pt & ct) / smaller >= 0.75:
        return True
    return False


def s2_get(url, api_key, retries=3):
    headers = {"x-api-key": api_key} if api_key else {}
    req = urllib.request.Request(url, headers=headers)
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 ** i)
                continue
            if e.code == 404:
                return {"data": [], "next": None, "_404": True}
            raise
    raise RuntimeError(f"S2 retries exhausted for {url}")


def fetch_all_citations(paper_id, api_key):
    """Page through /paper/{id}/citations until exhausted."""
    out = []
    offset = 0
    while True:
        url = f"{S2_BASE}/paper/{paper_id}/citations?fields={FIELDS}&limit=100&offset={offset}"
        d = s2_get(url, api_key)
        if d.get("_404"):
            return None  # paper not in S2
        items = d.get("data") or []
        out.extend(items)
        nxt = d.get("next")
        if nxt is None:
            break
        offset = nxt
        time.sleep(0.05)
    return out


def is_self_cite(authors, profile):
    s2_id = (profile.get("ids") or {}).get("semantic_scholar_author")
    name = profile.get("name") or ""
    parts = name.split()
    surname = parts[-1].lower() if parts else ""
    first = parts[0] if parts else ""
    abbrev = f"{first[0]}. {parts[-1]}" if parts else ""
    name_variants = {name, abbrev, f"{first[0]} {parts[-1]}" if parts else ""}
    collabs = set(profile.get("collaborators") or [])
    for a in authors or []:
        if s2_id and a.get("authorId") == s2_id:
            return True, False
        an = (a.get("name") or "").strip()
        if an in name_variants:
            return True, False
        if an in collabs:
            return False, True
    return False, False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--paper", default=None, help="Limit to one paper tag")
    ap.add_argument("--refresh", action="store_true", help="Re-fetch even if S2 already merged")
    args = ap.parse_args()

    scholar_dir = REPO_ROOT / args.slug
    if not scholar_dir.exists():
        scholar_dir = REPO_ROOT / "scholar" / args.slug
    if not scholar_dir.exists():
        print(f"ERROR: scholar dir not found", file=sys.stderr); sys.exit(1)

    api_key = read_env(REPO_ROOT / ".env", "SEMANTIC_SCHOLAR_API_KEY") or os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    profile = load_yaml(scholar_dir / "profile.yaml")
    papers = load_yaml(scholar_dir / "papers.yaml")["papers"]

    total_s2 = total_self = total_lab = total_inf = total_ctx = 0
    skipped = []

    for p in papers:
        if args.paper and p["tag"] != args.paper:
            continue
        s2_id = p.get("s2_paper_id")
        if not s2_id:
            print(f"  SKIP {p['tag']}: no s2_paper_id"); skipped.append(p["tag"]); continue
        yml_path = scholar_dir / "citation" / p["tag"] / "citations.yaml"
        yml_path.parent.mkdir(parents=True, exist_ok=True)
        if yml_path.exists():
            doc = load_yaml(yml_path)
        else:
            doc = {
                "cited_paper": {"tag": p["tag"], "title": p["title"],
                                "s2_paper_id": s2_id, "arxiv_id": p.get("arxiv_id")},
                "source": "semantic_scholar",
                "fetched_at": TODAY,
                "stats": {},
                "citations": [],
            }

        if doc.get("source") == "semantic_scholar+gs_extension" and not args.refresh:
            print(f"  SKIP {p['tag']}: already has S2 data (use --refresh)"); continue

        print(f"  FETCH {p['tag']} (s2={s2_id[:12]}…)…", end="", flush=True)
        items = fetch_all_citations(s2_id, api_key)
        if items is None:
            print(" 404 (not in S2)"); skipped.append(p["tag"]); continue
        print(f" {len(items)} citations")

        existing = doc.get("citations") or []
        # Indices for fast lookup
        by_doi = {(c.get("doi") or "").lower(): c for c in existing if c.get("doi")}
        by_arxiv = {(c.get("arxiv_id") or "").lower(): c for c in existing if c.get("arxiv_id")}
        by_s2 = {c.get("s2_paper_id"): c for c in existing if c.get("s2_paper_id")}

        added = updated = 0
        for it in items:
            cp = it.get("citingPaper") or {}
            ext = cp.get("externalIds") or {}
            doi = ext.get("DOI")
            arxiv = ext.get("ArXiv")
            s2_pid = cp.get("paperId")
            authors = [a.get("name") for a in (cp.get("authors") or [])]
            self_c, lab_c = is_self_cite(cp.get("authors") or [], profile)
            contexts = it.get("contexts") or []
            intents = it.get("intents") or []
            is_inf = bool(it.get("isInfluential"))

            # Find existing entry
            match = None
            if s2_pid and s2_pid in by_s2: match = by_s2[s2_pid]
            elif doi and doi.lower() in by_doi: match = by_doi[doi.lower()]
            elif arxiv and arxiv.lower() in by_arxiv: match = by_arxiv[arxiv.lower()]
            else:
                # Fuzzy title match
                t = cp.get("title") or ""
                for c in existing:
                    if title_match(t, c.get("title") or ""):
                        match = c; break

            if match:
                match["s2_paper_id"] = s2_pid or match.get("s2_paper_id")
                if doi and not match.get("doi"): match["doi"] = doi
                if arxiv and not match.get("arxiv_id"): match["arxiv_id"] = arxiv
                if cp.get("year") and not match.get("year"): match["year"] = cp.get("year")
                if cp.get("venue") and not match.get("venue"): match["venue"] = cp.get("venue")
                if authors and not match.get("authors"): match["authors"] = authors
                if cp.get("citationCount") is not None: match["citation_count_of_citing"] = cp.get("citationCount")
                match["is_self_citation"] = match.get("is_self_citation") or self_c
                if lab_c: match["is_lab_self_citation"] = True
                match["s2_signals"] = {"is_influential": is_inf, "intents": intents}
                match["contexts"] = contexts
                if cp.get("abstract"): match["abstract"] = cp.get("abstract")
                # Mark source as combined
                match.setdefault("source", "semantic_scholar")
                updated += 1
            else:
                new = {
                    "s2_paper_id": s2_pid,
                    "title": cp.get("title"),
                    "year": cp.get("year"),
                    "venue": cp.get("venue"),
                    "arxiv_id": arxiv,
                    "doi": doi,
                    "pmcid": None,
                    "authors": authors,
                    "citation_count_of_citing": cp.get("citationCount"),
                    "is_self_citation": self_c,
                    "is_lab_self_citation": lab_c,
                    "s2_signals": {"is_influential": is_inf, "intents": intents},
                    "contexts": contexts,
                    "abstract": cp.get("abstract"),
                    "source": "semantic_scholar",
                    "classification": {
                        "tags": ["needs_review"],
                        "rationale": None,
                        "method": None,
                        "classified_at": None,
                    },
                }
                existing.append(new)
                added += 1

        # Update stats
        inf_count = sum(1 for c in existing if (c.get("s2_signals") or {}).get("is_influential"))
        ctx_count = sum(1 for c in existing if c.get("contexts"))
        self_count = sum(1 for c in existing if c.get("is_self_citation"))
        doc["source"] = "semantic_scholar+gs_extension" if doc.get("source") == "gs_extension" or any(c.get("source") == "gs_extension" for c in existing) else "semantic_scholar"
        doc["fetched_at"] = TODAY
        doc.setdefault("stats", {})
        doc["stats"]["total_citations_s2"] = len(items)
        doc["stats"]["total_citations_combined"] = len(existing)
        doc["stats"]["influential_s2"] = inf_count
        doc["stats"]["with_contexts"] = ctx_count
        doc["stats"]["self_citations"] = self_count
        cnt = Counter()
        for c in existing:
            for t in (c.get("classification") or {}).get("tags") or []:
                cnt[t] += 1
        doc["stats"]["classification_distribution"] = dict(cnt)

        # Sort
        def k(c):
            sig = (c.get("s2_signals") or {})
            return (not sig.get("is_influential", False),
                    -len(c.get("contexts") or []),
                    -(c.get("year") or 0))
        existing.sort(key=k)
        doc["citations"] = existing

        save_yaml(yml_path, doc)
        print(f"     -> added={added} updated={updated} influential={inf_count} with_contexts={ctx_count} self={self_count}")
        total_s2 += len(items); total_inf += inf_count; total_ctx += ctx_count; total_self += self_count

    print(f"\n=== summary ===")
    print(f"  total_s2_citations: {total_s2}")
    print(f"  influential: {total_inf}  with_contexts: {total_ctx}  self: {total_self}")
    if skipped:
        print(f"  skipped: {', '.join(skipped)}")


if __name__ == "__main__":
    main()
