"""Colour contrast, as arithmetic against the real stylesheet.

A dark palette drifts under the WCAG thresholds very easily and never looks
obviously wrong while doing it -- which is exactly why this is computed rather
than judged. The values are parsed out of ``terminal.css`` itself, so editing a
token to something prettier fails here rather than shipping.

Two distinctions matter, and getting them wrong produces either false alarms or
false comfort:

* **Which surface a token actually appears on.** Checking every colour against
  every background is easy and wrong: control borders never sit on the ladder's
  hover tint, so measuring them there invents a failure that cannot occur.
* **Decoration versus component.** WCAG 1.4.11 covers what is "required to
  identify" a control. A hairline dividing two panels identifies nothing, and
  holding it to 3:1 would mean a terminal ruled in bright grey. The edge of an
  input is a different thing, and gets the full requirement.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = Path(__file__).resolve().parents[1] / "dashboard" / "static" / "css" / "terminal.css"

# Text under 18px (and under 14px bold) needs 4.5:1; UI components need 3:1.
# Everything in this terminal is small, so text is held to 4.5 throughout.
SMALL_TEXT = 4.5
COMPONENT = 3.0


def tokens() -> dict[str, str]:
    """Every custom property defined on :root, read from the stylesheet."""
    text = CSS.read_text(encoding="utf-8")
    root = text.split(":root", 1)[1].split("}", 1)[0]
    return {
        name: value
        for name, value in re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", root)
    }


def luminance(colour: str) -> float:
    raw = colour.lstrip("#")
    channels = [int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [
        c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def ratio(foreground: str, background: str) -> float:
    low, high = sorted((luminance(foreground), luminance(background)))
    return (high + 0.05) / (low + 0.05)


# (token, surfaces it is actually drawn on, minimum, what it carries)
CASES = [
    ("ink", ("void", "sunk", "panel", "raised", "hover"), SMALL_TEXT, "body text and prices"),
    ("ink-dim", ("sunk", "panel", "raised", "hover"), SMALL_TEXT, "secondary text and nav"),
    ("ink-faint", ("sunk", "panel", "raised", "hover"), SMALL_TEXT, "table headers, labels"),
    ("amber", ("void", "sunk", "panel", "raised"), SMALL_TEXT, "accent text, active nav"),
    ("up", ("panel", "raised", "hover"), SMALL_TEXT, "positive prices"),
    ("down", ("panel", "raised", "hover"), SMALL_TEXT, "negative prices"),
    ("halt", ("panel", "sunk"), SMALL_TEXT, "halted-session badge"),
    ("closed", ("panel", "sunk"), SMALL_TEXT, "closed-session badge"),
    # Controls live on panels and the sunk header, never on the row-hover tint.
    ("control-edge", ("sunk", "panel"), COMPONENT, "input and button borders"),
    ("amber", ("sunk", "panel", "raised", "hover"), COMPONENT, "the focus ring"),
]


@pytest.mark.parametrize("token,surfaces,minimum,role", CASES)
def test_contrast_meets_wcag(token, surfaces, minimum, role):
    palette = tokens()
    colour = palette[token]
    for surface in surfaces:
        measured = ratio(colour, palette[surface])
        assert measured >= minimum, (
            f"--{token} ({colour}) carries {role} and is {measured:.2f}:1 on "
            f"--{surface} ({palette[surface]}); WCAG needs {minimum}:1"
        )


def test_the_primary_button_label_is_readable():
    """Amber is bright, so its label must be dark -- white on it is 2.0:1."""
    palette = tokens()
    assert ratio("#16120a", palette["amber"]) >= SMALL_TEXT
    assert ratio("#ffffff", palette["amber"]) < SMALL_TEXT, (
        "white on amber would pass this test for the wrong reason"
    )


def test_direction_colours_stay_distinguishable():
    """Green and red are the only carriers of direction, so they must not blur
    into each other for anyone reading them side by side in a ladder."""
    palette = tokens()
    assert ratio(palette["up"], palette["down"]) >= 1.5


def test_every_palette_token_is_a_six_digit_hex():
    """Keeps the parser honest: a token in another notation would be silently
    skipped, and skipping is how a failing colour passes."""
    text = CSS.read_text(encoding="utf-8")
    root = text.split(":root", 1)[1].split("}", 1)[0]
    declared = set(re.findall(r"--([\w-]+):", root))
    parsed = set(tokens())
    non_colour = {"mono", "display", "rail", "tick", "amber-sunk", "up-sunk", "down-sunk"}

    # The type scale lives in the same block, under a `t-` namespace. Adding
    # those seven names to `non_colour` would widen the very hole this test
    # exists to close, so they are checked rather than skipped -- just against
    # a different rule. Every one has to be a whole-pixel length, which is what
    # stops the half-pixel steps creeping back: the stylesheet used to carry
    # 8.5, 9.5, 10.5, 11.5 and 13.5px, and a half-pixel difference is not
    # hierarchy a reader can perceive, only noise they cannot name.
    type_tokens = {name for name in declared if name.startswith("t-")}
    assert type_tokens, "the type scale has gone from :root"
    for name in sorted(type_tokens):
        value = re.search(rf"--{name}:\s*([^;]+);", root).group(1).strip()
        assert re.fullmatch(r"\d+px", value), (
            f"--{name} is {value!r}; the scale is whole pixels only"
        )

    assert declared - parsed - non_colour - type_tokens == set()


def test_no_text_is_smaller_than_the_scale_allows():
    """Ten pixels is the floor, and it is a floor rather than a target.

    The stylesheet reached 71 font-size declarations carrying 19 distinct
    values, nine of them below 12px and the smallest at 8px. Measured in the
    browser on the static shell alone, sixteen elements rendered under 12px,
    including the primary navigation at 11px and every header stat label at
    8.5px -- while the largest text on the page was the 15px brand. An
    interface whose entire type range is 8 to 15px has no hierarchy available
    to it: everything arrives at the reader with the same weight, and half of
    it is too small to read without leaning in.

    Density on a trading screen comes from tight spacing and hairline rules,
    not from shrinking the text until it fits.
    """
    text = CSS.read_text(encoding="utf-8")
    sizes = [float(v) for v in re.findall(r"font-size:\s*([\d.]+)px", text)]
    assert sizes, "no font sizes found; the parser has drifted from the file"

    too_small = sorted({s for s in sizes if s < 10})
    assert not too_small, f"text below the 10px floor: {too_small}"

    fractional = sorted({s for s in sizes if s != int(s)})
    assert not fractional, (
        f"half-pixel sizes are nudges rather than decisions: {fractional}"
    )


def test_the_figures_outrank_their_labels():
    """A number a person came to read must beat the caption describing it.

    Both of these led by less than half a pixel over their own label. The
    header stat was 12px against an 8.5px caption, and in the market rail the
    price was 11.5px against a 9px category tag, so a list a person scans for
    prices gave the price no more presence than the word 'future' beside it.
    """
    text = CSS.read_text(encoding="utf-8")

    def size_of(selector: str) -> str:
        block = text.split(selector, 1)[1].split("}", 1)[0]
        found = re.search(r"font-size:\s*([^;]+);", block)
        assert found, f"{selector} no longer sets a font size"
        return found.group(1).strip()

    # Both resolve through the scale rather than a literal, which is the point:
    # a figure that is sized by hand drifts away from its caption again.
    assert size_of(".stat b {") == "var(--t-lg)"
    assert size_of(".watch .px {") == "var(--t-lg)"
