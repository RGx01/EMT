"""
A meter's chart style: its bar colour and its rate line (31 Oct 2026).

Each channel that draws a bar can carry a style the user chose on Settings: the main meter's
import (the house), its export, and each device's import. `bar_color` is `#rrggbb`; `rate_line`
is 'solid' or 'dashed'. Unset, a meter keeps the fixed palette colour it had by position, and its
rate line keeps its old style (export dashed, the rest solid).

The rate line's colour is not chosen: it is the bar colour moved a fixed step toward white (a dark
bar) or toward black (a light one), so it always reads against its own bar. The settings page
draws its preview with the same rule (`rateLineColor` in meter_config.html).

A colour reaches the pages inside HTML and CSS, so anything that is not exactly `#rrggbb` is
dropped — at the write (`block_store._write_meters`) and again at the read (`build_meter_styles`).
"""
import re

PALETTE = [
    "#1f77b4",  # blue       → main electricity meter
    "#e377c2",  # pink       → first sub-meter
    "#ff7f0e",  # orange     → second sub-meter
    "#7f7f7f",  # grey
    "#8c564b",  # brown
    "#bcbd22",  # lime
    "#d62728",  # red
    "#9467bd",  # purple
    "#17becf",  # cyan
]

RATE_LINES = ("solid", "dashed")
RATE_MIX = 0.3                       # how far the rate line moves from its bar, toward white or black

_HEX = re.compile(r"#[0-9a-fA-F]{6}")


def clean_color(v):
    """`#rrggbb` (lower case), or None for anything else."""
    return v.lower() if isinstance(v, str) and _HEX.fullmatch(v) else None


def clean_rate_line(v):
    """'solid' or 'dashed', or None for anything else."""
    return v if v in RATE_LINES else None


def rate_line_color(bar: str) -> str:
    """The rate line's colour for a bar colour: mixed RATE_MIX of the way toward white when the bar
    is dark (perceived lightness under half), toward black when it is light. Rounded half up, as
    the page's JavaScript does."""
    c = clean_color(bar) or "#333333"
    r, g, b = (int(c[i:i + 2], 16) for i in (1, 3, 5))
    target = 255 if (0.299 * r + 0.587 * g + 0.114 * b) / 255 < 0.5 else 0
    return "#" + "".join("%02x" % int(x + (target - x) * RATE_MIX + 0.5) for x in (r, g, b))


def apply_styles(store, period_id: int, styles) -> int:
    """BL-101 PR 4: chart styles chosen in the Charts page's bar colours modal, written straight to the
    channels of the config period `period_id` — [(meter_id, channel, bar_color | None,
    rate_line | None)], None clearing it to the default. The engine thread's store (the route hands
    this to `_engine_write`); no engine restart, as a config save would. Values are cleaned again
    here. Returns the channels written."""
    n = 0
    for meter_id, channel, bar, line in styles:
        cur = store._conn.execute(
            "UPDATE meter_channels SET bar_color = ?, rate_line = ? WHERE channel = ? AND meter_id = "
            "(SELECT id FROM meters WHERE config_period_id = ? AND meter_id = ?)",
            (clean_color(bar), clean_rate_line(line), channel, period_id, meter_id))
        n += cur.rowcount or 0
    if not getattr(store, "_in_batch", False):
        store._conn.commit()
    return n
