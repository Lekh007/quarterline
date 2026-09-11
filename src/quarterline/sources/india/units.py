"""Indian reported-scale handling (SPEC 27 "rupees vs thousands/lakhs/crores").

Two distinct things must never be conflated:

1. **XBRL instance values are full rupees.** In the SEBI ``in-capmkt`` Integrated
   Finance Ind AS instances the ``LevelOfRounding`` trait (e.g. ``"Crores"``)
   describes the *presentation* scale of the human-readable rendering only; the
   fact value ``339860000000`` IS ₹33,986 Cr in full rupees
   (docs/india_source_audit.md §3, "Scale semantics"). Normalization therefore
   passes XBRL values through unchanged — the trait is carried as metadata, never
   applied as a multiplier.

2. **Display strings carry an explicit or declared scale.** ``"₹1,234 Cr"`` is
   self-describing (``Cr`` suffix); ``"1,234.5"`` is only meaningful once a
   declared scale (from a ``"₹ in Crores"`` style header) is supplied. A display
   string with neither an explicit suffix nor a declared scale is ambiguous and
   must raise, never guess.

Per-share values (EPS, face value) must NEVER inherit a lakh/crore multiplier:
``normalize_amount(..., per_share=True)`` is an unconditional passthrough. The
``per_share`` flag comes from the concept mapping (``concept_map``), not from
guessing at the tag name.
"""

from __future__ import annotations

import re
from decimal import Decimal

#: Standard Indian units.
LAKH = Decimal(100000)
CRORE = Decimal(10000000)

#: ``LevelOfRounding`` vocabulary (IFIndAs) -> multiplier in rupees. The value of
#: a fact is already in full rupees; this table documents what the *display*
#: scale means and is used only for presentation formatting.
ROUNDING_TRAIT_SCALES: dict[str, Decimal] = {
    "units": Decimal(1),
    "tens": Decimal(10),
    "hundreds": Decimal(100),
    "thousands": Decimal(1000),
    "lakhs": LAKH,
    "millions": Decimal(1000000),
    "crores": CRORE,
    "billions": Decimal(1000000000),
}

#: Explicit scale suffixes accepted on display strings.
_SUFFIX_PATTERN = re.compile(
    r"(?P<suffix>crores?|cr\.?|lakhs?|lacs?|l\b|k\b|thousands?)",
    re.IGNORECASE,
)

_NUMBER_PATTERN = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

_LEADING_CURRENCY = re.compile(r"^[₹Rs.\s]+", re.IGNORECASE)

_PARENTHESISED = re.compile(r"^\((.*)\)$")


class AmbiguousScaleError(ValueError):
    """A display amount had no explicit suffix and no declared scale."""


def scale_for_rounding_trait(trait: str | None) -> Decimal | None:
    """Multiplier implied by a ``LevelOfRounding`` trait (presentation only)."""
    if trait is None:
        return None
    return ROUNDING_TRAIT_SCALES.get(trait.strip().lower())


def normalize_amount(
    value: Decimal | int | str,
    *,
    per_share: bool = False,
) -> Decimal:
    """Normalize an XBRL-instance value: exact passthrough, never rescaled.

    ``in-capmkt`` fact values are full rupees (or rupees per share); the
    ``LevelOfRounding`` trait is presentation metadata carried alongside, not a
    multiplier. ``per_share=True`` makes the passthrough contract explicit for
    EPS-class concepts so a crore multiplier can never leak into them.
    """
    del per_share  # passthrough either way; the flag exists to force the choice
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value).strip())


def parse_display_amount(
    text: str,
    *,
    declared_scale: Decimal | None = None,
) -> Decimal:
    """Parse a human display string into full rupees.

    Accepted forms (whitespace/Indian or western digit grouping tolerated):

    - ``"₹1,234 Cr"`` / ``"Rs 1,234 crore"`` / ``"1,234.5 L"`` — explicit suffix wins.
    - ``"1,234.5"`` — requires ``declared_scale`` (from a detected header).
    - ``"(1,234)"`` — accounting negative.

    Raises :class:`AmbiguousScaleError` when there is no explicit suffix and no
    declared scale — ambiguity is surfaced, never guessed.
    """
    cleaned = text.strip()
    negative = False
    paren = _PARENTHESISED.match(cleaned)
    if paren:
        negative = True
        cleaned = paren.group(1)
    cleaned = _LEADING_CURRENCY.sub("", cleaned).strip()

    scale: Decimal | None = None
    suffix_match = _SUFFIX_PATTERN.search(cleaned)
    if suffix_match:
        word = suffix_match.group("suffix").lower().rstrip(".")
        scale = {
            "crore": CRORE,
            "crores": CRORE,
            "cr": CRORE,
            "lakh": LAKH,
            "lakhs": LAKH,
            "lac": LAKH,
            "lacs": LAKH,
            "l": LAKH,
            "thousand": Decimal(1000),
            "thousands": Decimal(1000),
        }.get(word)
        cleaned = (cleaned[: suffix_match.start()] + cleaned[suffix_match.end() :]).strip()
    elif declared_scale is not None:
        scale = declared_scale
    else:
        raise AmbiguousScaleError(
            f"display amount has no explicit scale suffix and no declared scale: {text!r}"
        )

    number_match = _NUMBER_PATTERN.search(cleaned)
    if number_match is None:
        raise ValueError(f"no number found in display amount: {text!r}")
    digits = number_match.group(0).replace(",", "")
    value = Decimal(digits) * scale
    return -value if negative else value


def format_crores(value: Decimal) -> str:
    """Render full rupees in the Indian presentation scale: 339860000000 -> "₹33,986 Cr"."""
    return _format_indian_grouped(value / CRORE, "Cr")


def format_millions(value: Decimal) -> str:
    """Render full rupees in millions: 524698000000 -> "₹5,24,698 Mn".

    Used for the MARUTI/SUNPHARMA instances whose ``LevelOfRounding`` trait is
    ``Millions`` — the DISPLAY scale mirrors the source's own presentation unit
    (their Reg-33 PDFs say "Rs in million"); the STORED value is still exact
    full rupees and is never rescaled.
    """
    return _format_indian_grouped(value / ROUNDING_TRAIT_SCALES["millions"], "Mn")


def _format_indian_grouped(amount: Decimal, suffix: str) -> str:
    """Quantize to an integer, group Indian style, prefix sign and add suffix."""
    rounded = amount.quantize(Decimal(1))
    # Indian digit grouping (last 3, then pairs): 178650 -> 1,78,650.
    sign = "-" if rounded < 0 else ""
    digits = str(abs(rounded))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    return f"{sign}₹{digits} {suffix}"
