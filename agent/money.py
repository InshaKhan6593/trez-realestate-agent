"""PKR amounts the way Pakistanis say them, and back again."""

from __future__ import annotations

import re

CRORE, LAKH = 10**7, 10**5


def pkr(n: int | None) -> str | None:
    """110000000 -> 'PKR 11 Crore'; 8500000 -> 'PKR 85 Lakh'; 60000 -> 'PKR 60,000'."""
    if n is None:
        return None
    if n >= CRORE:
        return f"PKR {_trim(n / CRORE)} Crore"
    if n >= LAKH:
        return f"PKR {_trim(n / LAKH)} Lakh"
    return f"PKR {n:,}"


def _trim(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


_UNITS = {"crore": CRORE, "cr": CRORE, "karor": CRORE, "crores": CRORE,
          "lakh": LAKH, "lac": LAKH, "lacs": LAKH, "lakhs": LAKH, "laakh": LAKH,
          "thousand": 1000, "hazar": 1000, "hazaar": 1000, "k": 1000}
_AMOUNT = re.compile(
    r"(?:(?:pkr|rs\.?)\s*)?(\d+(?:[.,]\d+)*)\s*(crores?|cr|karor|lakhs?|lacs?|laakh|thousand|hazaa?r|k)\b"
    r"|(?:pkr|rs\.?)\s*(\d[\d,]*)",
    re.IGNORECASE,
)


def amounts_in(text: str) -> list[int]:
    """Money amounts written in a reply: '4.5 crore', '85 lakh', 'PKR 60,000'."""
    out = []
    for m in _AMOUNT.finditer(text or ""):
        if m.group(1):
            number = float(m.group(1).replace(",", ""))
            out.append(round(number * _UNITS[m.group(2).lower()]))
        else:
            out.append(int(m.group(3).replace(",", "")))
    return out


def same_amount(said: int, actual: int) -> bool:
    """Equal as displayed: crore and lakh are shown to 2 decimals."""
    step = CRORE // 100 if actual >= CRORE else LAKH // 100 if actual >= LAKH else 1
    return abs(said - actual) <= step // 2 or said == actual
