"""Field metadata structures.

The wire format encodes values according to per-field metadata: the type
(FieldType enum), the size in bytes, the floor key of a homogeneous block
and the span. Real metadata is discovered at runtime via ENUMERATE_FIELDS
(PROTOCOL §4.5), but for simple SET_FIELDS use cases we hardcode the
display-module schema we already know.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    FIELD_TYPE_BOOLEAN,
    FIELD_TYPE_ENUM,
    FIELD_TYPE_FLOAT,
    FIELD_TYPE_INT,
    FIELD_TYPE_RAW,
    FIELD_TYPE_TIME,
    FIELD_TYPE_UINT,
    FIELD_TYPE_UTF8_CHAR,
    FIELD_TYPE_UTF8_STRING,
)


@dataclass(frozen=True)
class FieldSpec:
    """Describes one homogeneous block of fields keyed [floor .. floor+span).

    The first five fields are what the wire protocol itself needs; the
    rest are metadata the firmware reports via ENUMERATE_FIELDS and that
    `pylumicube.discovery` fills in. They default to "unknown" so the
    hardcoded display schema (and any hand-written spec table) stays
    terse.
    """

    floor: int
    span: int
    type: int   # FieldType enum
    size: int   # bytes per value (0 = variable)
    name: str = ""
    module: str = ""            # module namespace (e.g. "light_sensor")
    units: str = ""             # e.g. "celcius"
    min_value: int | None = None
    max_value: int | None = None
    gettable: bool = True
    settable: bool = False
    system: bool = False        # firmware-internal; not published by default
    debug: bool = False

    @property
    def signed(self) -> bool:
        """True for the FieldType values the protocol encodes as signed.

        PROTOCOL §4.7: RAW/UINT are unsigned; ENUM/BOOLEAN/INT/TIME are
        signed integers on the wire (the distinction only matters for
        values with the top bit set).
        """
        return self.type in (FIELD_TYPE_ENUM, FIELD_TYPE_BOOLEAN,
                             FIELD_TYPE_INT, FIELD_TYPE_TIME)

    @property
    def is_float(self) -> bool:
        return self.type == FIELD_TYPE_FLOAT

    @property
    def is_string(self) -> bool:
        return self.type == FIELD_TYPE_UTF8_STRING

    def covers(self, key: int) -> bool:
        return self.floor <= key < self.floor + self.span


def display_specs() -> dict[int, FieldSpec]:
    """Display module schema for the `cube` base board, verified at runtime
    via ENUMERATE_FIELDS. Floor key -> FieldSpec.

    The cube node has multiple modules (microphone, speaker, screen, etc.)
    so display fields don't start at key 0 — they live at offset 256.

    The firmware allocates `led_colour` with span = MAX_LEDS-1 = 999, even
    though the physical panel is only 192 LEDs (24x8). Writes past LED 191
    are accepted but discarded.

    | Key range  | Field                    | Size | Type    |
    | 256        | brightness               | 1    | UINT    |
    | 257..265   | panel_*, refresh, gamma  | 1-2  | UINT/BOOL |
    | 266..1264  | led_colour (192 LEDs)    | 3    | UINT    |
    | 1265       | spread_spectrum_period   | 2    | UINT    |
    | 1266       | show                     | 1    | BOOLEAN |
    """
    return {
        256:  FieldSpec(256,  1,   FIELD_TYPE_UINT,    1, "brightness", module="display"),
        257:  FieldSpec(257,  1,   FIELD_TYPE_UINT,    2, "panel_width", module="display"),
        258:  FieldSpec(258,  1,   FIELD_TYPE_UINT,    2, "panel_height", module="display"),
        259:  FieldSpec(259,  1,   FIELD_TYPE_UINT,    2, "refresh_period", module="display"),
        260:  FieldSpec(260,  1,   FIELD_TYPE_UINT,    2, "estimated_current", module="display"),
        261:  FieldSpec(261,  1,   FIELD_TYPE_UINT,    2, "max_current", module="display"),
        262:  FieldSpec(262,  1,   FIELD_TYPE_BOOLEAN, 1, "gamma_correction_enabled", module="display"),
        263:  FieldSpec(263,  1,   FIELD_TYPE_UINT,    1, "gamma_correction_red", module="display"),
        264:  FieldSpec(264,  1,   FIELD_TYPE_UINT,    1, "gamma_correction_green", module="display"),
        265:  FieldSpec(265,  1,   FIELD_TYPE_UINT,    1, "gamma_correction_blue", module="display"),
        266:  FieldSpec(266,  999, FIELD_TYPE_UINT,    3, "led_colour", module="display"),
        1265: FieldSpec(1265, 1,   FIELD_TYPE_UINT,    2, "spread_spectrum_period", module="display"),
        1266: FieldSpec(1266, 1,   FIELD_TYPE_BOOLEAN, 1, "show", module="display"),
    }
