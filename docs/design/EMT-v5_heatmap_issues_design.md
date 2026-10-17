# EMT v5 — The heatmap as the data-quality surface: issues first, missing half-hours, one width

*Draft, 17 Oct 2026; §6 decided the same day, one point to confirm. The user, after #196: "work on the heat map import/export bills and unifying
it into the data management tools … start by filtering the heat maps by days with problems (or the
ability to collapse days with issues). Issues are basically anything that is not green … include
missing blocks as a new colour (black?) and also include gaps between periods. The current heat
maps have a bug with the current month not being the same width as historical months. Eventually
we'll integrate deleting blocks and periods and gap filling and backfilling."*

This is the first step of the data-tools direction (data tools design §1, BL-88): before the
heatmap can carry the tools, it has to show where the problems are, including the half-hours that
have no block at all. Every line below was read in `web/templates/heatmap_module.html` and
`web/server.py`, or queried on `blocks-37` (the 17 Oct soak backup); nothing is from a docstring.

---

## 1. What the heatmap does today

- **Rows are the days that have blocks.** `monthGrid` builds a day only when a block lands in it,
  and `drawMonth` draws `Object.keys(p.grid)`. A day with no blocks has **no row**: a gap of a day
  or a month closes up and disappears.
- **A missing half-hour inside a day is transparent.** Its slot stays `null`, `settleColour`
  returns `rgba(0,0,0,0)`, and the cell shows the page background. The same as a future half-hour
  of today, and on the clocks-forward day the same as the hour that does not exist.
- **The grid has 48 columns** (`slots()`) whatever the day. On the clocks-forward day two columns
  have no half-hour; on the clocks-back day two half-hours share a column ("clocks-back: summed").
- **The month list** comes from `/api/periods?kind=month`: `bounds.earliest` / `bounds.latest`,
  truncated to the **day**.
- **"To look at"** (the day totals) counts cells of rank 2 or more in `CAT_RANK`: amber and red.
  Teal (`applied_emt_split`), blue (corrected) and grey (`none`, `imported`, `no_agreement`) are
  rank 0–1.
- **Cell size** is measured per month panel: `cellSize` takes that panel's `.hm-row` width when
  it is drawn, clamped to 6–22 px. A `ResizeObserver` on the root redraws every drawn panel when
  the root's width changes by 8 px or more.

On `blocks-37` there is **nothing missing**: 59,024 main-meter blocks from 5 Jun 2023 23:00 UTC to
17 Oct 14:30 UTC, every half-hour present; `deleted_ranges` is empty; one config period. The
features below need a test database with holes in it (a delete on the soak, or a fixture).

## 2. Missing half-hours — a state of their own

**Proposal.** A half-hour is **missing** when it exists in local time, lies inside the data's
span, and has no main-meter block:

- **inside the span:** from the first block to the last *finished* half-hour. Not before the data
  begins, not today's in-progress or future half-hours. The span needs the first block's start
  exactly, not `bounds.earliest`'s day — `/api/periods` gains `bounds.earliest_block`;
- **exists in local time:** the grid is built from the UTC half-hours of the month, mapped to local
  slots, so the clocks-forward day's missing hour is no half-hour at all, not a missing one;
- **every day of the span gets a row**, with or without blocks. A day with no blocks is a row of
  missing cells, so a gap shows as a band, not a seam.

It shows on **every view**, not only the bills: on kWh and carbon too, since a hole in the data is
a hole whatever is measured. It counts toward "to look at".

**Deleted, or never recorded?** `deleted_ranges` records a user's delete (a tombstone, which gap
fill skips). A half-hour inside one was removed on purpose; one outside was never recorded. The
proposal draws them differently (question (c)).

## 3. Issues first — collapse the days with nothing to look at

**Proposal: collapse, not hide.** A view's "Issues only" toggle (bills and, with §2, every view)
folds each run of clean days into one slim row, `12 days — all final`, which expands on a click.
Dates stay continuous, and a month with nothing to look at becomes one line. Hiding the clean
rows would put two problem days next to each other with nothing to say how far apart they are.

**What a clean day is** — question (a). The user's words: *anything that is not green*. On the
Import bill that is:

| colour | states | an issue? |
|---|---|---|
| green | `applied` | no |
| teal | `applied_emt_split` — Octopus's cost, EMT's split | yes (a) |
| light amber / amber | `estimate_dcc`, `estimate_cad`, `known` | yes |
| red | `overdue`, `differs`, `flagged` (incl. `split_missing`) | yes |
| blue | `corrected` — the user's own correction | yes (a) |
| grey | `imported` (CSV history) | no (a) |
| grey | `none` (nothing imported) | no — to confirm (a) |
| black (new) | missing | yes |

and on the Export bill `waiting`, `overdue`, `no_agreement` and missing are issues; `applied`,
`imported` and (to confirm) `none` are not (a). The rule lives in one place (`CAT_RANK`'s threshold,
today rank ≥ 2), so the toggle, the day totals and the future quality strip (BL-89) agree.

**The selection still works across a collapsed run:** a drag from a problem day above it to one
below covers the folded days too, as the range they are.

## 4. One width for every month (the bug)

*Found from the user's screenshot (17 Oct 2026, the Export bill on a desktop browser).* The
current month's cells end at x ≈ 1380, September's at ≈ 1388; its "To look at" bars span
1409–1548, September's 1403–1556. Both months are at the 22 px cell maximum, so `cellSize` is not
it. The difference is **the axes**: only the first panel (`p.first`, which is the current month
because the list is newest first) draws the 45° time labels across the top of its grid and the
0–60 labels over its totals. Chart.js shrinks a chart's plot area to fit tick labels that overhang
its edges — the last rotated label, "23:30", hangs past the right — so the axis panel's cells and
bars come out a few pixels narrower than every other month's.

**Proposal:** draw both axes once, in a header above the months (the time labels over the grid,
the totals' scale over the totals), outside the month charts. Every month's chart is then
configured the same and draws to the same width. While there, one cell size for the whole heatmap,
measured from the root rather than per panel, so months drawn at different moments (the first at
once, the rest as they scroll near) cannot disagree either; and a redraw on any width change, not
only one of 8 px or more. The test draws two months, one first, and asserts the same plot width.

## 5. Order

1. **The width** (§4) — alone, small, a bug.
2. **Missing half-hours** (§2): `bounds.earliest_block`; the grid from UTC half-hours (every day a
   row, the clocks days right); the missing colour; tombstoned half-hours apart.
3. **Issues first** (§3): the one "is it clean" rule, the collapse toggle, the selection across a
   fold.
4. **Then the tools on the selection** (data tools design §1): gap fill on missing half-hours
   first — the black cells are what it fills — then backfill, then delete (with BL-90's soft
   delete), each with the re-fetch's shape: select → preview → apply after a required backup.

## 6. Decided (17 Oct 2026, the user)

**(a) What is an issue: everything not green except CSV history.** Teal (Octopus's cost, EMT's
split), blue (the user's correction), grey `no_agreement`, light amber, amber, red and missing are
issues; green `applied` and grey `imported` (CSV history) are not. *One to confirm*: grey `none`
(nothing imported, nothing exported). On the Export bill every night half-hour is `none` — the
screenshot's grey left third — so counting it would make every day an issue and nothing would
collapse there. The proposal: `none` is not an issue, like CSV history (§3's table).

**(b) The missing colour: black, diagonally hatched**, on both themes. Not on the clocks-forward
day's missing hour: those half-hours do not exist (§2).

**(c) A deleted half-hour is a gap like any other** — missing, black-hatched, an issue. EMT's gap
detection already counts the gaps a user made; the tooltip still says when one was deleted, from
`deleted_ranges`.

**(d) Between periods, a stretch with no blocks is a gap**: every day of the span has a row (§2),
so it shows as rows of missing half-hours, which are issues and never fold away.

**(e) Collapse and expand** (§3).
