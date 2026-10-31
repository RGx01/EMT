"""
A tariff's short name for display, from its Octopus tariff code (31 Oct 2026).

Usage Stats' tariff overlay labels each agreement, and the stored agreements carry only the code
(`E-1R-INTELLI-VAR-22-10-14-A`); `product_code` is empty on the agreements discovery returns. The
name is made here, offline — no Octopus call — from the code's product part: the family first
(the more specific before the less: AGILE-OUTGOING before AGILE and OUTGOING, INTELLI-FLUX before
INTELLI and FLUX), then " fixed" for a fixed-term (FIX) product. A product it does not know shows as
its product part, never as a guess.
"""
import re

# (pattern in the product part, name) — first match wins, so the more specific come first
_FAMILIES = (
    (r"AGILE-OUTGOING|OUTGOING-AGILE", "Agile Outgoing"),
    (r"AGILE", "Agile"),
    (r"IOG-SMB", "Intelligent Go (SMB)"),
    (r"INTELLI-FLUX", "Intelligent Flux"),
    (r"INTELLI|IOG", "Intelligent Go"),
    (r"FLUX", "Flux"),
    (r"COSY", "Cosy"),
    (r"(?:^|-)GO(?:-|$)", "Go"),
    (r"OUTGOING", "Outgoing"),
    (r"SILVER|TRACKER", "Tracker"),
    (r"(?:^|-)VAR(?:-|$)", "Flexible"),
    (r"(?:^|-)FIX(?:-|$)", "Fixed"),
)

# E-1R-<product>-<region>, E-2R-… (two-rate), G-1R-… (gas)
_CODE = re.compile(r"^[EG]-\dR-(?P<product>.+)-[A-P]$")


def product_part(code: str) -> str:
    """The product part of a tariff code (`INTELLI-VAR-22-10-14`), or the code as given."""
    m = _CODE.match((code or "").strip().upper())
    return m.group("product") if m else (code or "").strip()


def tariff_name(code: str) -> str:
    """'Intelligent Go', 'Outgoing fixed', … ; the product part for a product it does not know."""
    product = product_part(code)
    up = product.upper()
    for pat, name in _FAMILIES:
        if re.search(pat, up):
            fixed = name != "Fixed" and re.search(r"(?:^|-)FIX(?:-|$)", up)
            return name + (" fixed" if fixed else "")
    return product
