import sys

if sys.version_info >= (3, 8):
    from typing import Final
else:
    from typing_extensions import Final  # pragma: no cover

from ..geometry import Spacing

VALID_VISIBILITY: Final = {"visible", "hidden"}
VALID_DISPLAY: Final = {"block", "none"}
VALID_BORDER: Final = {
    "none",
    "hidden",
    "round",
    "solid",
    "double",
    "dashed",
    "heavy",
    "inner",
    "outer",
    "hkey",
    "vkey",
    "tall",
    "wide",
}
VALID_EDGE: Final = {"top", "right", "bottom", "left"}
VALID_LAYOUT: Final = {"dock", "vertical", "grid"}

VALID_BOX_SIZING: Final = {"border-box", "content-box"}
VALID_OVERFLOW: Final = {"scroll", "hidden", "auto"}


NULL_SPACING: Final = Spacing(0, 0, 0, 0)