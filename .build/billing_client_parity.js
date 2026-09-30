/*
 * C, Billing tab step 2: the in-browser Billing tab formats EXACTLY what the
 * pre-rendered page showed. Paste into the browser console on the Charts page of an
 * install that still has /charts/daily_usage.html (removed in step 3), with the
 * Billing tab loaded; or run it from an automated browser. Resolves to a report.
 *
 * The suite cannot run JavaScript (no Node on the Mac or the Pi), so the JS port of
 * render_billing_summary and the side panel is checked here, against the page
 * Python rendered, on a real database:
 *
 *   cards    every period of every mode (Bill, Month, Quarter, Year): the module's
 *            card for the period is one of the page's cards;
 *   labels   the picker's text for every period equals the page's <option> text;
 *   compare  vs Prev / vs Last Year pick the same period as the page did;
 *   panels   every day's side panel equals the page's markup.
 *
 * Whitespace between tags is ignored; nothing else is. The page heads its cards with
 * the site from the legacy meters_config.json, so cards are formatted with no site
 * here — the site row is the one thing that legitimately differs. Prints no money.
 */
(async function billingClientParity() {
  const B = window.EMTBilling;
  if (!B || !B._state.inited) return 'Billing tab not loaded';
  const page = await (await fetch(apiUrl('/charts/daily_usage.html'))).text();
  if (page.length < 1000) return 'no pre-rendered page to compare with';
  const doc = new DOMParser().parseFromString(page, 'text/html');
  const norm = (h) => h.replace(/<div class="billing-summary current-period">/, '<div class="billing-summary">')
                       .replace(/>\s+</g, '><').replace(/\s+/g, ' ').trim();
  const cards = new Set((page.match(/\n    <div class="billing-summary(?: current-period)?">[\s\S]*?\n      <\/table>\n    <\/div>/g) || []).map(norm));
  const out = [], bad = [];
  const MODES = {month: 'period-select-month', calmonth: 'period-select-calmonth', quarter: 'period-select-quarter', year: 'period-select-year'};
  const cur = B._state.currency || '£';
  const clean = (t) => t.replace(/^★ Current\s+/, '').replace(/&rarr;/g, '→').trim();

  for (const mode of Object.keys(MODES)) {
    const lst = await B._loadList(mode);
    // labels
    const opts = Array.from(doc.getElementById(MODES[mode]).options).map((o) => clean(o.textContent));
    const mine = lst.shown.map((p) => B._optionLabel(mode, p, cur));
    if (opts.length !== mine.length) bad.push(`${mode}: ${mine.length} periods, page has ${opts.length}`);
    mine.forEach((l, i) => { if (opts[i] !== undefined && l !== opts[i]) bad.push(`${mode} label #${i} differs`); });
    // cards + compare picks
    let n = 0;
    for (const p of lst.shown) {
      const bill = await B._loadBill(mode, p);
      if (!cards.has(norm(B._renderCard(bill.summary, bill.currency, null)))) bad.push(`${mode} ${p.start}: bill card not on the page`);
      n++;
    }
    // compare picks: the page's section for each period names its vs-prev / vs-year columns
    const secs = Array.from(doc.querySelectorAll('.' + mode + '-section'));
    secs.forEach((sec, i) => {
      const p = lst.shown[i]; if (!p) return;
      const head = (v) => { const h = sec.querySelector('.bill-view[data-view="' + v + '"] .bill-view-heading.compare h2');
                           return h ? h.textContent.trim() : null; };
      const want = {prev: head('vs-prev'), year: head('vs-year')};
      // The page's compare heading is the period's label without its total.
      const hd = (q) => q ? B._optionLabel(mode, q, cur).split('  |  ')[0] : null;
      const got = {prev: hd(B._prevOf(mode, lst, p)), year: hd(B._yearOf(mode, lst, p))};
      if (want.prev !== got.prev) bad.push(`${mode} ${p.start}: vs Prev picks ${got.prev}, page ${want.prev}`);
      if (want.year !== got.year) bad.push(`${mode} ${p.start}: vs Last Year picks ${got.year}, page ${want.year}`);
    });
    out.push(`${mode}: ${n} cards, ${mine.length} labels, ${secs.length} compare pairs`);
  }

  // side panels, a calendar month at a time
  const panels = {};
  // Bounded by ANY chart container (grouped sections prefix their ids): looking only
  // for the unprefixed one made each quarter/year panel scan the rest of the page.
  for (const m of page.matchAll(/<div class="chart-summary"><div class="day-label">(\d{4}-\d\d-\d\d)<\/div>[\s\S]*?(?=\s*<div id="[a-z0-9_]*chart_)/g))
    panels[m[1]] = panels[m[1]] || norm(m[0]);
  const months = [...new Set(Object.keys(panels).map((d) => d.slice(0, 7)))];
  let nd = 0;
  for (const mk of months) {
    const byDay = await B._loadMonth(mk);
    for (const day of Object.keys(byDay)) {
      if (!panels[day]) continue;
      nd++;
      if (norm(B._renderPanel(day, byDay[day].panel, byDay[day].chart.currency)) !== panels[day]) bad.push(`${day}: side panel differs`);
    }
  }
  out.push(`panels: ${nd} of ${Object.keys(panels).length} days`);
  out.push(bad.length ? `FAIL (${bad.length})\n  ` + bad.slice(0, 15).join('\n  ') : 'verdict: PASS');
  return out.join('\n');
})();
