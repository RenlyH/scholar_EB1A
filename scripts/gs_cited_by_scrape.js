/* Google Scholar "Cited by" scraper — browser-side helpers.
 *
 * Install by evaluating this whole file in the page context of a GS author
 * profile (Claude: mcp__claude-in-chrome__javascript_tool; human: devtools
 * console). Then drive it with __counts / __scrape / __dl.
 *
 * Usage
 * -----
 *   __counts()                         // title -> live cited-by count, for the delta check
 *   await __scrape('Foundation models for fast')      // scrape that row's citing papers
 *   __dl('01_foundation_models_glioma_citations.md',  // write markdown to ~/Downloads
 *        'Full Paper Title', 'Author list. Venue, Year.')
 *   await __resume()                   // continue after a CAPTCHA, keeps what was collected
 *   __reset()                          // drop accumulated state before an unrelated scrape
 *
 * Design notes — each of these exists because of a bug we actually hit:
 *
 * - Pages are pulled with fetch(&num=20&start=N), NOT by clicking "Next".
 *   No navigation means these helpers and sessionStorage survive the whole run.
 * - Progress is written to sessionStorage after EVERY page. A CAPTCHA mid-run
 *   then costs one page instead of the entire scrape.
 * - Titles are stripped of Scholar's [PDF]/[HTML]/[BOOK] badge. h3.textContent
 *   is "\n  [PDF] Real Title", so the regex MUST allow leading whitespace —
 *   otherwise ~20% of entries are stored as "[PDF] Title", which silently
 *   fails to dedupe against the same paper already in the file.
 * - Default 5s between pages. At 1.8s Scholar CAPTCHA'd after ~120 fetches.
 * - Never return hrefs through the tool output channel; it blocks on URLs with
 *   query strings. Return counts only, and ship the URLs via the file download.
 */

window.__PROFILE_ROWS = () => [...document.querySelectorAll('#gsc_a_b .gsc_a_tr')];

/* title -> live cited-by count for every row on the profile.
 * Safe to return through tool output: no URLs. */
window.__counts = function () {
  return JSON.stringify(window.__PROFILE_ROWS().map(r => ({
    t: r.querySelector('.gsc_a_at')?.textContent.slice(0, 70),
    c: parseInt(r.querySelector('.gsc_a_c a')?.textContent || '0', 10) || 0
  })), null, 1);
};

/* Scrape all citing papers for the row whose title contains SUB.
 * `occurrence` picks among duplicate rows (GS lists preprint + journal
 * versions of the same paper separately — CNS lymphoma has three rows). */
window.__scrape = async function (SUB, occurrence, delayMs) {
  const rows = window.__PROFILE_ROWS().filter(x =>
    x.querySelector('.gsc_a_at').textContent.toLowerCase().includes(SUB.toLowerCase()));
  if (!rows.length) return 'NO ROW MATCH';
  const row = rows[occurrence || 0];
  if (!row) return `NO ROW AT occurrence=${occurrence} (only ${rows.length} match)`;
  const a = row.querySelector('a.gsc_a_ac');
  if (!a || !a.href) return 'NO CITED-BY LINK (0 citations)';
  sessionStorage.setItem('__base', a.href);
  sessionStorage.setItem('__all', '[]');
  return await window.__resume(delayMs);
};

window.__resume = async function (delayMs) {
  const base = sessionStorage.getItem('__base');
  if (!base) return 'NO BASE SAVED — call __scrape first';
  const all = JSON.parse(sessionStorage.getItem('__all') || '[]');
  let start = all.length, pages = 0, note = 'done';
  while (pages < 30) {
    const r = await fetch(`${base}&num=20&start=${start}`, { credentials: 'include' });
    const t = await r.text();
    const doc = new DOMParser().parseFromString(t, 'text/html');
    const items = [...doc.querySelectorAll('#gs_res_ccl_mid .gs_r.gs_or')];
    if (!items.length) {
      // Only trust this as a CAPTCHA signal when there are also no results:
      // ordinary result pages ship recaptcha code in their JS bundle.
      if (/recaptcha|gs_captcha/i.test(t)) note = `CAPTCHA at start=${start} — solve it in the browser, then __resume()`;
      break;
    }
    items.forEach(el => all.push({
      title: (el.querySelector('h3')?.textContent || '').replace(/^\s*\[[A-Z]+\]\s*/, '').trim(),
      meta: el.querySelector('.gs_a')?.textContent.trim() || '',
      htmlLink: el.querySelector('h3 a')?.getAttribute('href') || '',
      pdfLink: el.querySelector('.gs_or_ggsm a')?.getAttribute('href') || ''
    }));
    sessionStorage.setItem('__all', JSON.stringify(all));  // save every page
    pages++; start += 20;
    if (items.length < 20) break;
    await new Promise(z => setTimeout(z, delayMs || 5000));
  }
  return `${note} | total=${all.length} pagesThisRun=${pages}`;
};

/* Render what was collected as markdown and download it to ~/Downloads. */
window.__dl = function (filename, header, orig) {
  const all = JSON.parse(sessionStorage.getItem('__all') || '[]');
  let md = `# Papers Citing "${header}"\n\nSource: Google Scholar\nOriginal paper: ${orig}\n\n`
         + `## Citing Papers (${all.length})\n\n`;
  all.forEach((x, i) => {
    md += `${i + 1}. **${x.title}**\n   - ${x.meta.replace(/\s+/g, ' ')}\n`;
    if (x.htmlLink) md += `   - HTML: ${x.htmlLink}\n`;
    if (x.pdfLink) md += `   - PDF: ${x.pdfLink}\n`;
    md += '\n';
  });
  const blob = new Blob([md], { type: 'text/markdown' });
  const u = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = u; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(u), 2000);
  return `wrote ${filename} (${all.length} entries)`;
};

window.__reset = function () {
  sessionStorage.removeItem('__all');
  sessionStorage.removeItem('__base');
  return 'reset';
};

/* Cheap probe: are we CAPTCHA-blocked right now? Returns a result count. */
window.__probe = async function () {
  const r = await fetch('https://scholar.google.com/scholar?q=glioma&hl=en', { credentials: 'include' });
  const d = new DOMParser().parseFromString(await r.text(), 'text/html');
  return `results=${d.querySelectorAll('#gs_res_ccl_mid .gs_r.gs_or').length} (0 => still blocked)`;
};

'gs_cited_by_scrape helpers installed';
