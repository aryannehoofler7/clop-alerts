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

The ``panel-heading`` class is compared exactly, not by token. overview.php renders favourite
actions on this same page as ``class="panel-heading h4"`` with a user-chosen label, so a looser
match would let a favourite action named "Resources" pass for the Resources panel. Failing closed
is the point: keep the comparison exact.

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


class OverviewError(RuntimeError):
    """The page is not a complete, normal render of overview.php."""


#: The panels overview.php always renders, in the order they appear on the page.
REQUIRED_PANELS = ("Resources", "Buildings")


def require_valid_overview(html: str) -> None:
    """Raise ``OverviewError`` unless ``html`` is a complete, normal overview.php page.

    Every check here closes one way a broken page could be mistaken for a nation that simply owns
    and holds nothing. That mistake is the worst outcome available to this tool: it zeroes the
    shared sheet and stamps it freshly verified, hiding the very failure the timestamp exists to
    expose.

    * **Both panel headings present.** ``overview.php`` emits them from unconditional heredocs
      (lines 111 and 214), so a missing heading proves this is not an overview page at all -- a PHP
      fatal after ``header.php`` flushed, a maintenance page, a redirect somewhere else. Checked in
      page order, so the first complaint tells you how far the response actually got.
    * **The page finished.** ``footer.php`` ends every page with ``</html>``. Without it the
      response was cut off -- and a cut *after* the Buildings heading passes the check above while
      losing every building row, which would zero them and report it as a routine correction.
    * **Not both panels empty.** ``backend_overview.php`` fills buildings and resources from a
      single query, and on PHP 5.4 a failed query makes ``mysqli_fetch_array`` warn rather than
      fatal, so the page renders whole with both tables empty. Either panel may legitimately be
      empty on its own -- a new nation owns no buildings -- but both at once is that one query
      having failed.
    """
    for heading in REQUIRED_PANELS:
        if not panel_present(html, heading):
            raise OverviewError(
                f"overview.php has no {heading} panel, so this is not a normal overview page. "
                "Nothing was written to the sheet."
            )
    if not html.rstrip().endswith("</html>"):
        raise OverviewError(
            "overview.php stopped part-way, so the response was cut off before the page finished. "
            "Nothing was written to the sheet."
        )
    if not parse_panel(html, "Resources") and not parse_panel(html, "Buildings"):
        raise OverviewError(
            "overview.php lists no resources and no buildings at all. On this game that is what a "
            "failed database query looks like, not an empty nation. Nothing was written to the "
            "sheet."
        )
