#!/usr/bin/env python3
"""Merge freshly-scraped GS cited-by markdown into a scholar's gs_exports/.

    uv run scripts/merge_gs_exports.py --slug xinhaihou_umich --dry-run
    uv run scripts/merge_gs_exports.py --slug xinhaihou_umich

Reads new exports from --src (default ~/Downloads), merges each into the
same-named file under <slug>/gs_exports/, and prints a before/after/new table.

WHY MERGE INSTEAD OF OVERWRITE
------------------------------
A fresh scrape is NOT a superset of the previous one. Google Scholar
re-clusters its "Cited by" sets between queries: on the 2026-07-19 refresh,
today's 130-result scrape of the Nature glioma paper was missing 22 papers
that the existing 101-entry export contained. Overwriting silently deletes
them. So: union by normalized title, keep every entry ever seen, and backfill
HTML/PDF links onto existing entries that lacked them.

Consequence: the union grows past the live GS citation count over time. That
is expected and correct — do not "fix" it by truncating to the GS number.

Safe to re-run; merging is idempotent.
"""
import argparse
import os
import re
import sys

ENTRY = re.compile(r'^(\d+)\.\s+\*\*(.*?)\*\*\s*$', re.M)
HEADER = re.compile(r'## Citing Papers \((\d+)\)')
# Scholar's [PDF]/[HTML]/[BOOK]/[CITATION] badge. Leading \s* is load-bearing:
# h3.textContent is "\n  [PDF] Title", and without it the badge survives into
# the stored title and that entry never dedupes against its own earlier copy.
BADGE = re.compile(r'^\s*\[[A-Z]+\]\s*')

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def norm(title):
    """Dedupe key. Punctuation/case/whitespace-insensitive."""
    return re.sub(r'\W+', ' ', BADGE.sub('', title)).strip().lower()


def parse(path):
    """-> (header_block, [{title, meta, html, pdf}])"""
    text = open(path).read()
    head, _, body = text.partition('## Citing Papers')
    body = body.split('\n', 1)[1] if '\n' in body else ''
    entries, cur = [], None
    for line in body.split('\n'):
        m = ENTRY.match(line)
        if m:
            if cur:
                entries.append(cur)
            cur = {'title': BADGE.sub('', m.group(2)).strip(), 'meta': '', 'html': '', 'pdf': ''}
        elif cur is not None:
            s = line.strip()
            if s.startswith('- HTML:'):
                cur['html'] = s[len('- HTML:'):].strip()
            elif s.startswith('- PDF:'):
                cur['pdf'] = s[len('- PDF:'):].strip()
            elif s.startswith('- ') and not cur['meta']:
                cur['meta'] = s[2:].strip()
    if cur:
        entries.append(cur)
    return head.rstrip('\n'), entries


def render(head, entries):
    out = [head, '', f'## Citing Papers ({len(entries)})', '']
    for i, e in enumerate(entries, 1):
        out.append(f"{i}. **{e['title']}**")
        if e['meta']:
            out.append(f"   - {e['meta']}")
        if e['html']:
            out.append(f"   - HTML: {e['html']}")
        if e['pdf']:
            out.append(f"   - PDF: {e['pdf']}")
        out.append('')
    return '\n'.join(out)


def merge(old_entries, new_entries):
    """Union, old order first. Returns (entries, n_added)."""
    by, order, added = {}, [], 0
    for e in old_entries:                      # existing file may itself hold dupes
        k = norm(e['title'])
        if k not in by:
            by[k] = e
            order.append(k)
    for e in new_entries:
        k = norm(e['title'])
        if k in by:
            for f in ('meta', 'html', 'pdf'):  # backfill links we didn't have before
                if not by[k][f] and e[f]:
                    by[k][f] = e[f]
        else:
            by[k] = e
            order.append(k)
            added += 1
    return [by[k] for k in order], added


def verify(path):
    """Post-write sanity check. Returns a list of problem strings."""
    text = open(path).read()
    hdr = int(HEADER.search(text).group(1))
    ents = ENTRY.findall(text)
    titles = [norm(t) for _, t in ents]
    problems = []
    if hdr != len(ents):
        problems.append(f'header {hdr} != {len(ents)} entries')
    if [int(n) for n, _ in ents] != list(range(1, len(ents) + 1)):
        problems.append('numbering not contiguous')
    if len(set(titles)) != len(titles):
        problems.append(f'{len(titles) - len(set(titles))} duplicate titles')
    if [t for _, t in ents if BADGE.match(t)]:
        problems.append('[PDF]/[HTML] badge left in a title')
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--slug', required=True, help='scholar folder, e.g. xinhaihou_umich')
    ap.add_argument('--src', default=os.path.expanduser('~/Downloads'),
                    help='where the freshly-scraped *_citations.md files are (default ~/Downloads)')
    ap.add_argument('--fold-in', action='append', default=[], metavar='EXTRA=TARGET',
                    help='merge EXTRA into TARGET instead of keeping it standalone, for papers '
                         'GS splits across rows, e.g. 18_cns_lymphoma_medrxiv_citations.md='
                         '03_cns_lymphoma_citations.md. Repeatable.')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    # --slug accepts a bare folder name under the repo, or any path (handy for tests)
    dst = os.path.join(REPO, args.slug, 'gs_exports')
    if not os.path.isdir(dst):
        dst = os.path.join(os.path.abspath(args.slug), 'gs_exports')
    if not os.path.isdir(dst):
        sys.exit(f'no gs_exports/ under {args.slug} (looked in {REPO} and cwd)')
    fold = dict(p.split('=', 1) for p in args.fold_in)

    incoming = sorted(f for f in os.listdir(args.src) if f.endswith('_citations.md'))
    if not incoming:
        sys.exit(f'no *_citations.md in {args.src}')

    rows, problems = [], []
    for fn in incoming:
        if fn in fold:
            continue                                   # handled as part of its target
        _, nents = parse(os.path.join(args.src, fn))
        for extra, target in fold.items():
            if target == fn and os.path.exists(os.path.join(args.src, extra)):
                _, eents = parse(os.path.join(args.src, extra))
                nents, _ = merge(nents, eents)
        out = os.path.join(dst, fn)
        if os.path.exists(out):
            head, oents = parse(out)
            merged, added = merge(oents, nents)
            before = len(oents)
        else:
            head, merged, added, before = parse(os.path.join(args.src, fn))[0], nents, len(nents), 0
        if not args.dry_run:
            open(out, 'w').write(render(head, merged))
            problems += [f'{fn}: {p}' for p in verify(out)]
        rows.append((fn, before, len(merged), added))

    print(f"{'file':<48}{'before':>8}{'after':>8}{'new':>7}")
    tb = ta = tn = 0
    for fn, b, a, n in rows:
        print(f'{fn:<48}{b:>8}{a:>8}{n:>7}')
        tb, ta, tn = tb + b, ta + a, tn + n
    print(f"{'TOTAL':<48}{tb:>8}{ta:>8}{tn:>7}")
    if args.dry_run:
        print('\n(dry run — nothing written)')
    else:
        print('\nintegrity:', '; '.join(problems) if problems else 'ok')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
