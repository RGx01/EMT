# EMT v5 — The heatmap as the data-quality surface: issues first, missing half-hours, one width

*Draft, 17 Oct 2026. The user, after #196: "work on the heat map import/export bills and unifying
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
| teal | `applied_emt_split` — Octopus's cost, EMT's split | **(a)** |
| light amber / amber | `estimate_dcc`, `estimate_cad`, `known` | yes |
| red | `overdue`, `differs`, `flagged` (incl. `split_missing`) | yes |
| blue | `corrected` — the user's own correction | **(a)** |
| grey | `none` (nothing imported), `imported` (CSV history) | **(a)** |
| black (new) | missing | yes |

and on the Export bill `waiting`, `overdue` and missing are issues; `applied` is not; `none`,
`imported` and `no_agreement` are grey (a). The rule lives in one place (`CAT_RANK`'s threshold,
today rank ≥ 2), so the toggle, the day totals and the future quality strip (BL-89) agree.

**The selection still works across a collapsed run:** a drag from a problem day above it to one
below covers the folded days too, as the range they are.

## 4. One width for every month (the bug)

*Not reproduced yet: a screenshot, and the browser and device it shows on, would settle it.* What
the code allows:

- **Each panel measures itself when drawn.** The current month is drawn at once ("never make the
  first month wait"), the others as they scroll near. If the page is not laid out yet when the
  first one draws — a hidden tab or pane, `.hm-row` 0 px wide — `cellSize` clamps to 6 px, and
  only a root resize of 8 px or more redraws it.
- **The 8 px threshold** skips any width change under it — a scrollbar appearing as more months
  load is ~15 px on Windows and Linux, 0 on macOS.

**Proposal:** one cell size for the whole heatmap, measured from the root, stored in `state`, and
every panel drawn at it; redrawn when the root's width changes at all (debounced, as now). Months
can then never disagree, whatever order they draw in. The width bug is fixed first, alone, with a
test that draws two months at different moments and asserts one cell size.

## 5. Order

1. **The width** (§4) — alone, small, a bug.
2. **Missing half-hours** (§2): `bounds.earliest_block`; the grid from UTC half-hours (every day a
   row, the clocks days right); the missing colour; tombstoned half-hours apart.
3. **Issues first** (§3): the one "is it clean" rule, the collapse toggle, the selection across a
   fold.
4. **Then the tools on the selection** (data tools design §1): gap fill on missing half-hours
   first — the black cells are what it fills — then backfill, then delete (with BL-90's soft
   delete), each with the re-fetch's shape: select → preview → apply after a required backup.

## 6. Questions for the user

**(a) What is clean?** Green only, as asked, would make teal, blue and grey issues too. Teal is
Octopus's cost with EMT's split (on the soak, for 7 half-hours, the split arrives later); blue is
the user's own correction; grey is "nothing to settle" (no import, CSV history, no export
agreement). The proposal counts them **clean**, so a collapsed day means "nothing to do".

**(b) The missing colour.** Black reads well on the light theme, but the dark theme's background
is `#1a1d27`, near black. The proposal: black with a diagonal hatch on both themes, so it is
neither a colour of the scale nor the background.

**(c) Deleted versus never recorded.** The proposal: missing (never recorded) black-hatched;
deleted (inside a `deleted_ranges` tombstone) the same hatch on grey, its tooltip "deleted
<date>", not an issue — it was meant.

**(d) "Gaps between periods".** Is that the stretches with no blocks (§2 draws them as bands of
missing rows), or something else — between config periods, or between the API import's history
and the live data?

**(e) Collapse, or hide?** The proposal collapses (§3).
