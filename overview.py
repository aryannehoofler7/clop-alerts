#!/usr/bin/env python3
"""Parse a named panel out of an overview.php page.

overview.php renders several panels -- Resources, Buildings, Weapons, Armor -- and they share one
row shape: a right-aligned name cell followed by a cell whose ``<span>`` holds the value.

    <td style="text-align: right;">Apples</td><td><span class="text-success">226</span></td>

``PanelParser`` arms only after the ``panel-heading`` div whose text matches the heading it was
given, and stops at that panel's ``</table>``, so the identically-shaped sibling panels are not
picked up. Two details of the real markup are handled by falling out of the rules above rather than
by special cases:

* the Resources panel's leading icon cell (``<td style="width: 16px;"><img/></td>``, present unless
  the nation set ``hideicons``) has no ``text-align: right``, so it is not mistaken for the name;
* the trailing centred cells (Generated / Used / Loss / ...) and the Buildings panel's form buttons
  contain further ``<span>``s, but they arrive after the value span has been captured and so are
  ignored.

``buildings.py`` and ``stockpiles.py`` both read overview through this; neither depends on the other.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import List, Optional, Sequence, Tuple


class PanelParser(HTMLParser):
    """Collect ``[(name, value_text), ...]`` from the overview panel headed ``heading``."""

    def __init__(self, heading: str) -> None:
        super().__init__(convert_charrefs=True)
        self._heading = heading
        self._in_heading = False
        self._heading_buf: List[str] = []
        self._pending_table = False
        self._in_table = False
        self._capture: Optional[str] = None  # "name" | "value" | None
        self._name: Optional[str] = None
        self._value: Optional[str] = None
        self._buf: List[str] = []
        self.found = False
        self.rows: List[Tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        attr = dict(attrs)
        if tag == "div" and (attr.get("class") or "") == "panel-heading":
            self._in_heading = True
            self._heading_buf = []
            return
        if self._pending_table and tag == "table":
            self._pending_table = False
            self._in_table = True
            return
        if not self._in_table:
            return
        if tag == "tr":
            self._name = self._value = None
        elif tag == "td" and "text-align: right" in (attr.get("style") or "") and self._name is None:
            self._capture = "name"
            self._buf = []
        elif tag == "span" and self._name is not None and self._value is None:
            self._capture = "value"
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._in_heading:
            self._heading_buf.append(data)
        elif self._capture is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._in_heading:
            self._in_heading = False
            if "".join(self._heading_buf).strip() == self._heading:
                self._pending_table = True
                self.found = True
            return
        if not self._in_table:
            return
        if tag == "td" and self._capture == "name":
            self._name = "".join(self._buf).strip()
            self._capture = None
        elif tag == "span" and self._capture == "value":
            self._value = "".join(self._buf).strip()
            self._capture = None
        elif tag == "tr":
            if self._name and self._value is not None:
                self.rows.append((self._name, self._value))
        elif tag == "table":
            self._in_table = False


def parse_panel(html: str, heading: str) -> List[Tuple[str, str]]:
    """Return ``[(name, value_text), ...]`` for the overview panel headed ``heading``.

    An unknown heading yields an empty list rather than raising -- callers decide whether an empty
    panel is a problem.
    """
    parser = PanelParser(heading)
    parser.feed(html)
    return parser.rows


def panel_present(html: str, heading: str) -> bool:
    """Whether the page carries the panel headed ``heading`` at all.

    The distinction matters more than it looks. overview.php renders every panel heading
    unconditionally, so a heading with an empty table means the nation genuinely has nothing of
    that kind -- but a *missing* heading means this is not an overview page: a PHP fatal after the
    header flushed, a maintenance page, a truncated response. ``parse_panel`` returns an empty list
    for both, and a caller that reads the second as "holds nothing" would happily zero the sheet.
    """
    parser = PanelParser(heading)
    parser.feed(html)
    return parser.found
