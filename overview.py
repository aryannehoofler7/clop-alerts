#!/usr/bin/env python3
"""Read the overview.php page: parse a named panel out of it, and decide whether to trust it.

overview.php renders several panels -- Resources, Buildings, Weapons, Armor -- and they share one
row shape: a right-aligned name cell followed by a cell whose ``<span>`` holds the value.

    <td style="text-align: right;">Apples</td><td><span class="text-success">226</span></td>

Three readings of that value side are available, all arming through the same heading rule:
``parse_panel`` takes the first ``<span>`` of the first value cell, ``parse_panel_text`` takes that
whole cell (the Nation panel's per-tick figures need it), and ``parse_panel_cells`` takes every
value cell in the row (the Resources panel's Generated / Used / Net / Ticks-Worth columns).

``PanelParser`` arms only after the ``panel-heading`` div whose text matches the heading it was
given, and stops at that panel's ``</table>``, so the identically-shaped sibling panels are not
picked up. Two details of the real markup are handled by falling out of the rules above rather than
by special cases:

* the Resources panel's leading icon cell (``<td style="width: 16px;"><img/></td>``, present unless
  the nation set ``hideicons``) has no ``text-align: right``, so it is not mistaken for the name;
* the trailing centred cells (Generated / Used / Loss / ...) and the Buildings panel's form buttons
  contain further ``<span>``s, but they arrive after the value span has been captured and so are
  ignored.

The trust half lives here rather than in its own module because the policy is expressed entirely
in the vocabulary of this one -- panels, headings, whether the page finished -- and because
``buildings.py`` and ``stockpiles.py`` both need it while neither may depend on the other.

The ``panel-heading`` class is compared exactly, not by token. overview.php renders favourite
actions on this same page as ``class="panel-heading h4"`` with a user-chosen label, so a looser
match would let a favourite action named "Resources" pass for the Resources panel. Failing closed
is the point: keep the comparison exact.

``buildings.py`` and ``stockpiles.py`` both read overview through this; neither depends on the other.

``require_valid_overview`` is meant to be called before anything is written anywhere: its messages
tell the reader nothing has been. Keep it that way -- call it as soon as the page is fetched.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any, List, Optional, Sequence, Tuple


#: How much of a row's value side to capture. ``span`` takes the first ``<span>`` of the first value
#: cell (Resources, Buildings); ``cell`` takes that whole cell (the Nation panel's per-tick figures);
#: ``cells`` takes every value cell in the row (the Resources panel's Generated/Used/Net/Ticks-Worth
#: columns). All three arm through the same heading rule.
MODES = ("span", "cell", "cells")


class PanelParser(HTMLParser):
    """Collect ``[(name, value), ...]`` from the overview panel headed ``heading``.

    ``value`` is a string in ``span`` and ``cell`` mode, and a list of the row's value cells in
    ``cells`` mode.
    """

    def __init__(self, heading: str, mode: str = "span") -> None:
        super().__init__(convert_charrefs=True)
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
        self._heading = heading
        self._mode = mode
        self._in_heading = False
        self._heading_buf: List[str] = []
        self._pending_table = False
        self._in_table = False
        self._capture: Optional[str] = None  # "name" | "value" | "cell" | None
        self._name: Optional[str] = None
        self._value: Optional[str] = None
        self._cells: List[str] = []
        self._buf: List[str] = []
        self.found = False
        self.rows: List[Tuple[str, Any]] = []

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
            self._cells = []
        elif tag == "td" and "text-align: right" in (attr.get("style") or "") and self._name is None:
            self._capture = "name"
            self._buf = []
        elif (
            tag == "td"
            and self._name is not None
            and self._mode in ("cell", "cells")
            # "cell" wants only the first value cell; "cells" wants every one of them.
            and (self._mode == "cells" or self._value is None)
        ):
            self._capture = "cell"
            self._buf = []
        elif (
            self._mode == "span"
            and tag == "span"
            and self._name is not None
            and self._value is None
        ):
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
        elif tag == "td" and self._capture == "cell":
            # The whole cell, tags stripped and whitespace collapsed. Nested tags contribute their
            # text and do not end capture -- only this cell's own </td> does.
            text = " ".join("".join(self._buf).split())
            self._cells.append(text)
            if self._value is None:
                self._value = text
            self._capture = None
        elif tag == "span" and self._capture == "value":
            self._value = "".join(self._buf).strip()
            self._capture = None
        elif tag == "tr":
            if self._mode == "cells":
                if self._name and self._cells:
                    self.rows.append((self._name, list(self._cells)))
            elif self._name and self._value is not None:
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


def parse_panel_text(html: str, heading: str) -> List[Tuple[str, str]]:
    """Return ``[(name, whole_cell_text), ...]`` for the panel headed ``heading``.

    ``parse_panel`` captures a value cell's *first* ``<span>``, which is right for Resources and
    Buildings and wrong for the Nation panel: its per-tick figure is a second span in the same cell
    (``overview.php:82-86``), and under Alicorn Elite / Transponyism the game emits a bare
    ``(Ascending)`` with no span at all (``overview.php:50-60``). Whole-cell text is the only shape
    that covers all three. As a side effect it also reads the span-less cells -- Government Type,
    Economic Type -- which ``parse_panel`` drops.

    This shares ``PanelParser`` rather than being a second parser on purpose: the "arm only on a
    ``panel-heading`` div whose text matches exactly" rule lives there, and it is what stops a
    favourite action named ``Nation`` impersonating the Nation panel.
    """
    parser = PanelParser(heading, mode="cell")
    parser.feed(html)
    return parser.rows


def parse_panel_cells(html: str, heading: str) -> List[Tuple[str, List[str]]]:
    """Return ``[(name, [cell_text, ...]), ...]`` -- every value cell of every row.

    The Resources panel is seven columns wide (Qty, Generated, Used, Loss, Net, Ticks-Worth after
    the name), and ``parse_panel`` only ever sees the first. This is how the later columns are read.

    The panel's ``<thead>`` row comes back like any other, as
    ``("Resource", ["Qty", "Generated", "Used", "Loss", "Net", "Ticks-Worth"])`` -- which is what
    lets a caller find a column *by its heading* instead of counting cells. Do that rather than
    indexing a fixed position; the game has changed this table's shape before.

    The leading icon cell has no ``text-align: right`` so it is never mistaken for the name, and it
    is dropped in both the header row and the data rows -- so the columns line up whether or not the
    nation has ``hideicons`` set.
    """
    parser = PanelParser(heading, mode="cells")
    parser.feed(html)
    return parser.rows


def panel_present(html: str, heading: str) -> bool:
    """Whether the page carries a panel headed ``heading`` at all.

    Distinct from ``parse_panel`` returning no rows, which only means the panel is empty. See
    ``require_valid_overview`` for why that difference decides whether the page can be trusted.
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
      (grep ``<div class="panel-heading">Resources</div>``), so a missing heading proves this is
      not an overview page at all -- a PHP fatal after ``header.php`` flushed, a maintenance page,
      a redirect somewhere else. Checked in page order, so the first complaint tells you how far
      the response actually got.
    * **The page finished.** ``footer.php`` ends every page with ``</html>``. Without it the
      response was cut off -- and a cut *after* the Buildings heading passes the check above while
      losing every building row, which would zero them and report it as a routine correction.
      The test is deliberately *ends-with* rather than *contains*, so that a ``</html>`` inside a
      comment or an attribute cannot make a truncated page look finished. The cost is that anything
      appended after ``footer.php`` -- a PHP notice from a shutdown handler, something a proxy
      injects -- also fails it. If you are staring at a complete-looking page and this keeps
      firing, that is what to look for.
    * **Not both panels empty.** ``backend_overview.php`` fills buildings and resources from a
      single query, and on PHP 5.4 a failed query makes ``mysqli_fetch_array`` warn rather than
      fatal, so the page renders whole with both tables empty. Either panel may legitimately be
      empty on its own -- a new nation owns no buildings -- but both at once is that one query
      having failed.
      One case this refuses is legitimate: a brand-new nation before its first tick has no
      ``resources`` rows and owns nothing, so its page really is doubly empty. It would be blocked
      until the first tick gives it something. That is the accepted cost of catching a dead query.
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
    # Deliberately names the two panels rather than looping REQUIRED_PANELS: this check is about
    # those two specifically sharing one query, so a third required panel must not join it.
    if not parse_panel(html, "Resources") and not parse_panel(html, "Buildings"):
        raise OverviewError(
            "overview.php lists no resources and no buildings at all. On this game that is what a "
            "failed database query looks like, not an empty nation. Nothing was written to the "
            "sheet."
        )
