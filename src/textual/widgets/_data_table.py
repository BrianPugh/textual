from __future__ import annotations

from dataclasses import dataclass, field
from itertools import chain
import sys
from typing import ClassVar, Generic, NamedTuple, TypeVar, cast

from rich.console import RenderableType
from rich.padding import Padding
from rich.protocol import is_renderable
from rich.segment import Segment
from rich.style import Style
from rich.text import Text, TextType

from .. import events
from .._cache import LRUCache
from .._segment_tools import line_crop
from .._types import Lines
from ..geometry import clamp, Region, Size, Spacing
from ..reactive import Reactive
from .._profile import timer
from ..scroll_view import ScrollView
from ..widget import Widget
from .. import messages


if sys.version_info >= (3, 8):
    from typing import Literal
else:
    from typing_extensions import Literal

CursorType = Literal["cell", "row", "column"]
CELL: CursorType = "cell"
CellType = TypeVar("CellType")


def default_cell_formatter(obj: object) -> RenderableType | None:
    """Format a cell in to a renderable.

    Args:
        obj (object): Data for a cell.

    Returns:
        RenderableType | None: A renderable or None if the object could not be rendered.
    """
    if isinstance(obj, str):
        return Text.from_markup(obj)
    if not is_renderable(obj):
        return None
    return cast(RenderableType, obj)


@dataclass
class Column:
    """Table column."""

    label: Text
    width: int
    visible: bool = False
    index: int = 0


@dataclass
class Row:
    """Table row."""

    index: int
    height: int
    y: int
    cell_renderables: list[RenderableType] = field(default_factory=list)


@dataclass
class Cell:
    """Table cell."""

    value: object


class Coord(NamedTuple):
    """An object to represent the cordinate of a cell within the data table."""

    row: int
    column: int

    def left(self) -> Coord:
        """Get coordinate to the left."""
        row, column = self
        return Coord(row, column - 1)

    def right(self) -> Coord:
        """Get coordinate to the right."""
        row, column = self
        return Coord(row, column + 1)

    def up(self) -> Coord:
        """Get coordinate above."""
        row, column = self
        return Coord(row - 1, column)

    def down(self) -> Coord:
        """Get coordinate below."""
        row, column = self
        return Coord(row + 1, column)


class DataTable(ScrollView, Generic[CellType], can_focus=True):

    CSS = """
    DataTable {
        background: $surface;
        color: $text-surface;       
    }
    DataTable > .datatable--header {        
        text-style: bold;
        background: $primary;
        color: $text-primary;
    }
    DataTable > .datatable--fixed {
        text-style: bold;
        background: $primary;
        color: $text-primary;
    }

    DataTable > .datatable--odd-row {
        
    }

    DataTable > .datatable--even-row {
        background: $primary 10%;
    }

    DataTable >  .datatable--cursor {
        background: $secondary;
        color: $text-secondary;
    }

    .-dark-mode DataTable > .datatable--even-row {
        background: $primary 15%;
    }

    DataTable > .datatable--highlight {
        background: $secondary 20%;
    }
    """

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "datatable--header",
        "datatable--fixed",
        "datatable--odd-row",
        "datatable--even-row",
        "datatable--highlight",
        "datatable--cursor",
    }

    def __init__(
        self,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.columns: list[Column] = []
        self.rows: dict[int, Row] = {}
        self.data: dict[int, list[CellType]] = {}
        self.row_count = 0

        self._y_offsets: list[tuple[int, int]] = []

        self._row_render_cache: LRUCache[
            tuple[int, int, Style, int, int], tuple[Lines, Lines]
        ]
        self._row_render_cache = LRUCache(1000)

        self._cell_render_cache: LRUCache[tuple[int, int, Style, bool, bool], Lines]
        self._cell_render_cache = LRUCache(10000)

        self._line_cache: LRUCache[
            tuple[int, int, int, int, int, int, Style], list[Segment]
        ]
        self._line_cache = LRUCache(1000)

        self._line_no = 0

    show_header = Reactive(True)
    fixed_rows = Reactive(0)
    fixed_columns = Reactive(0)
    zebra_stripes = Reactive(False)
    header_height = Reactive(1)
    show_cursor = Reactive(True)
    cursor_type = Reactive(CELL)

    cursor_cell: Reactive[Coord] = Reactive(Coord(0, 0), repaint=False)
    hover_cell: Reactive[Coord] = Reactive(Coord(0, 0), repaint=False)

    @property
    def hover_row(self) -> int:
        return self.hover_cell.row

    @property
    def hover_column(self) -> int:
        return self.hover_cell.column

    @property
    def cursor_row(self) -> int:
        return self.cursor_cell.row

    @property
    def cursor_column(self) -> int:
        return self.cursor_cell.column

    def _clear_caches(self) -> None:
        self._row_render_cache.clear()
        self._cell_render_cache.clear()
        self._line_cache.clear()
        self._styles_cache.clear()

    def get_row_height(self, row_index: int) -> int:
        if row_index == -1:
            return self.header_height
        return self.rows[row_index].height

    async def on_styles_updated(self, message: messages.StylesUpdated) -> None:
        self._clear_caches()
        self.refresh()

    def watch_show_header(self, show_header: bool) -> None:
        self._clear_caches()

    def watch_fixed_rows(self, fixed_rows: int) -> None:
        self._clear_caches()

    def watch_zebra_stripes(self, zebra_stripes: bool) -> None:
        self._clear_caches()

    def watch_hover_cell(self, old: Coord, value: Coord) -> None:
        self.refresh_cell(*old)
        self.refresh_cell(*value)

    def watch_cursor_cell(self, old: Coord, value: Coord) -> None:
        self.refresh_cell(*old)
        self.refresh_cell(*value)

    def validate_cursor_cell(self, value: Coord) -> Coord:
        row, column = value
        row = clamp(row, 0, self.row_count - 1)
        column = clamp(column, self.fixed_columns, len(self.columns) - 1)
        return Coord(row, column)

    def _update_dimensions(self) -> None:
        """Called to recalculate the virtual (scrollable) size."""
        total_width = sum(column.width for column in self.columns)
        self.virtual_size = Size(
            total_width,
            len(self._y_offsets) + (self.header_height if self.show_header else 0),
        )

    def _get_cell_region(self, row_index: int, column_index: int) -> Region:
        if row_index not in self.rows:
            return Region(0, 0, 0, 0)
        row = self.rows[row_index]
        x = sum(column.width for column in self.columns[:column_index])
        width = self.columns[column_index].width
        height = row.height
        y = row.y
        if self.show_header:
            y += self.header_height
        cell_region = Region(x, y, width, height)
        return cell_region

    def add_column(self, label: TextType, *, width: int = 10) -> None:
        """Add a column to the table.

        Args:
            label (TextType): A str or Text object containing the label (shown top of column)
            width (int, optional): Width of the column in cells. Defaults to 10.
        """
        text_label = Text.from_markup(label) if isinstance(label, str) else label
        self.columns.append(Column(text_label, width, index=len(self.columns)))
        self._update_dimensions()
        self.refresh()

    def add_row(self, *cells: CellType, height: int = 1) -> None:
        """Add a row.

        Args:
            height (int, optional): The height of a row (in lines). Defaults to 1.
        """
        row_index = self.row_count
        self.data[row_index] = list(cells)
        self.rows[row_index] = Row(row_index, height, self._line_no)

        for line_no in range(height):
            self._y_offsets.append((row_index, line_no))

        self.row_count += 1
        self._line_no += height
        self._update_dimensions()
        self.refresh()

    def refresh_cell(self, row_index: int, column_index: int) -> None:
        if row_index < 0 or column_index < 0:
            return
        region = self._get_cell_region(row_index, column_index)
        if not self.window_region.overlaps(region):
            return
        region = region.translate(-self.scroll_offset)
        self.refresh(region)

    def _get_row_renderables(self, row_index: int) -> list[RenderableType]:
        """Get renderables for the given row.

        Args:
            row_index (int): Index of the row.

        Returns:
            list[RenderableType]: List of renderables
        """

        if row_index == -1:
            row = [column.label for column in self.columns]
            return row

        data = self.data.get(row_index)
        empty = Text()
        if data is None:
            return [empty for _ in self.columns]
        else:
            return [default_cell_formatter(datum) or empty for datum in data]

    def _render_cell(
        self,
        row_index: int,
        column_index: int,
        style: Style,
        width: int,
        cursor: bool = False,
        hover: bool = False,
    ) -> Lines:
        """Render the given cell.

        Args:
            row_index (int): Index of the row.
            column_index (int): Index of the column.
            style (Style): Style to apply.
            width (int): Width of the cell.

        Returns:
            Lines: A list of segments per line.
        """
        if hover:
            style += self.component_styles["datatable--highlight"].node.rich_style
        if cursor:
            style += self.component_styles["datatable--cursor"].node.rich_style
        cell_key = (row_index, column_index, style, cursor, hover)
        if cell_key not in self._cell_render_cache:
            style += Style.from_meta({"row": row_index, "column": column_index})
            height = (
                self.header_height if row_index == -1 else self.rows[row_index].height
            )
            cell = self._get_row_renderables(row_index)[column_index]
            lines = self.app.console.render_lines(
                Padding(cell, (0, 1)),
                self.app.console.options.update_dimensions(width, height),
                style=style,
            )
            self._cell_render_cache[cell_key] = lines
        return self._cell_render_cache[cell_key]

    def _render_row(
        self,
        row_index: int,
        line_no: int,
        base_style: Style,
        cursor_column: int = -1,
        hover_column: int = -1,
    ) -> tuple[Lines, Lines]:
        """Render a row in to lines for each cell.

        Args:
            row_index (int): Index of the row.
            line_no (int): Line number (on screen, 0 is top)
            base_style (Style): Base style of row.

        Returns:
            tuple[Lines, Lines]: Lines for fixed cells, and Lines for scrollable cells.
        """

        cache_key = (row_index, line_no, base_style, cursor_column, hover_column)

        if cache_key in self._row_render_cache:
            return self._row_render_cache[cache_key]

        render_cell = self._render_cell

        if self.fixed_columns:
            fixed_style = self.component_styles["datatable--fixed"].node.rich_style
            fixed_style += Style.from_meta({"fixed": True})
            fixed_row = [
                render_cell(row_index, column.index, fixed_style, column.width)[line_no]
                for column in self.columns[: self.fixed_columns]
            ]
        else:
            fixed_row = []

        if row_index == -1:
            row_style = self.component_styles["datatable--header"].node.rich_style
        else:
            if self.zebra_stripes:
                component_row_style = (
                    "datatable--odd-row" if row_index % 2 else "datatable--even-row"
                )
                row_style = self.component_styles[component_row_style].node.rich_style
            else:
                row_style = base_style

        scrollable_row = [
            render_cell(
                row_index,
                column.index,
                row_style,
                column.width,
                cursor=cursor_column == column.index,
                hover=hover_column == column.index,
            )[line_no]
            for column in self.columns
        ]

        row_pair = (fixed_row, scrollable_row)
        self._row_render_cache[cache_key] = row_pair
        return row_pair

    def _get_offsets(self, y: int) -> tuple[int, int]:
        """Get row number and line offset for a given line.

        Args:
            y (int): Y coordinate relative to screen top.

        Returns:
            tuple[int, int]: Line number and line offset within cell.
        """
        if self.show_header:
            if y < self.header_height:
                return (-1, y)
            y -= self.header_height
        if y > len(self._y_offsets):
            raise LookupError("Y coord {y!r} is greater than total height")
        return self._y_offsets[y]

    def _render_line(
        self, y: int, x1: int, x2: int, base_style: Style
    ) -> list[Segment]:
        """Render a line in to a list of segments.

        Args:
            y (int): Y coordinate of line
            x1 (int): X start crop.
            x2 (int): X end crop (exclusive).
            base_style (Style): Style to apply to line.

        Returns:
            list[Segment]: List of segments for rendering.
        """

        width = self.size.width

        try:
            row_index, line_no = self._get_offsets(y)
        except LookupError:
            return [Segment(" " * width, base_style)]
        cursor_column = (
            self.cursor_column
            if (self.show_cursor and self.cursor_row == row_index)
            else -1
        )
        hover_column = self.hover_column if (self.hover_row == row_index) else -1

        cache_key = (y, x1, x2, width, cursor_column, hover_column, base_style)
        if cache_key in self._line_cache:
            return self._line_cache[cache_key]

        fixed, scrollable = self._render_row(
            row_index,
            line_no,
            base_style,
            cursor_column=cursor_column,
            hover_column=hover_column,
        )
        fixed_width = sum(column.width for column in self.columns[: self.fixed_columns])

        fixed_line: list[Segment] = list(chain.from_iterable(fixed)) if fixed else []
        scrollable_line: list[Segment] = list(chain.from_iterable(scrollable))

        segments = fixed_line + line_crop(scrollable_line, x1 + fixed_width, x2, width)
        segments = Segment.adjust_line_length(segments, width, style=base_style)
        simplified_segments = list(Segment.simplify(segments))

        self._line_cache[cache_key] = simplified_segments
        return segments

    def render_line(self, y: int) -> list[Segment]:
        """Render a line of content.

        Args:
            y (int): Y Coordinate of line.

        Returns:
            list[Segment]: A rendered line.
        """
        width, height = self.size
        scroll_x, scroll_y = self.scroll_offset
        fixed_top_row_count = sum(
            self.get_row_height(row_index) for row_index in range(self.fixed_rows)
        )
        if self.show_header:
            fixed_top_row_count += self.get_row_height(-1)

        style = self.rich_style

        if y >= fixed_top_row_count:
            y += scroll_y

        return self._render_line(y, scroll_x, scroll_x + width, style)

    def render_lines(self, crop: Region) -> Lines:
        """Render the widget in to lines.

        Args:
            crop (Region): Region within visible area to.

        Returns:
            Lines: A list of list of segments
        """
        lines = self._styles_cache.render_widget(self, crop)
        return lines

    def on_mouse_move(self, event: events.MouseMove):
        meta = event.style.meta
        if meta:
            try:
                self.hover_cell = Coord(meta["row"], meta["column"])
            except KeyError:
                pass

    async def on_key(self, event) -> None:
        await self.dispatch_key(event)

    def _get_cell_border(self) -> Spacing:
        top = self.header_height if self.show_header else 0
        top += sum(
            self.rows[row_index].height
            for row_index in range(self.fixed_rows)
            if row_index in self.rows
        )
        left = sum(column.width for column in self.columns[: self.fixed_columns])
        return Spacing(top, 0, 0, left)

    def _scroll_cursor_in_to_view(self, animate: bool = False) -> None:
        region = self._get_cell_region(self.cursor_row, self.cursor_column)
        spacing = self._get_cell_border()
        self.scroll_to_region(region, animate=animate, spacing=spacing)

    def on_click(self, event: events.Click) -> None:
        meta = self.get_style_at(event.x, event.y).meta
        if meta:
            self.cursor_cell = Coord(meta["row"], meta["column"])
            self._scroll_cursor_in_to_view()

    def key_down(self, event: events.Key):
        self.cursor_cell = self.cursor_cell.down()
        event.stop()
        event.prevent_default()
        self._scroll_cursor_in_to_view()

    def key_up(self, event: events.Key):
        self.cursor_cell = self.cursor_cell.up()
        event.stop()
        event.prevent_default()
        self._scroll_cursor_in_to_view()

    def key_right(self, event: events.Key):
        self.cursor_cell = self.cursor_cell.right()
        event.stop()
        event.prevent_default()
        self._scroll_cursor_in_to_view(animate=True)

    def key_left(self, event: events.Key):
        self.cursor_cell = self.cursor_cell.left()
        event.stop()
        event.prevent_default()
        self._scroll_cursor_in_to_view(animate=True)