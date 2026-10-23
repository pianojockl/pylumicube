"""Backwards-compatible alias for `pylumicube.font`.

The 5x7 bitmap font used by `Display.scroll_text` moved from
`pylumicube.compat.font` to `pylumicube.font` when the text scroller
became part of the native API. This module re-exports it so existing
imports keep working.
"""

from __future__ import annotations

from ..font import (  # noqa: F401
    GLYPH_HEIGHT,
    GLYPH_WIDTH,
    glyph_columns,
    render_text,
)

__all__ = ["GLYPH_HEIGHT", "GLYPH_WIDTH", "glyph_columns", "render_text"]
