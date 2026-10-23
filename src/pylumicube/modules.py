"""Module namespace: the daemon-side grouping of node fields (PROTOCOL §5).

The wire protocol only knows about nodes and 16-bit field keys. The
user-facing API talks about *modules*: ``light_sensor.ambient_light``,
``buttons.top_pressed``. The Java daemon builds that mapping from each
field's ``module`` metadata, falling back to the node's preferred name
when a field declares none (`Namespace.build`).

Two firmware quirks are reproduced here:

* A field on the ``cube`` base board that claims the ``buttons`` module is
  renamed to ``system_button``, so it cannot collide with the optional
  add-on button module on the sensor board (which owns ``buttons``).
* ``environment``-style modules are not merged across nodes: the first
  node (lowest id) to claim a field name wins, and later duplicates are
  skipped with a warning, rather than discarding the whole namespace the
  way the Java implementation does.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from .metadata import FieldSpec
from .telemetry import TelemetryStore

log = logging.getLogger(__name__)

#: Node preferred names that mean "the cube base board".
CUBE_NODE_NAMES = ("cube", "com.abstractfoundry.cube")


@dataclass(frozen=True)
class FieldRef:
    """Where a named field lives on the wire."""

    node_id: int
    key: int
    spec: FieldSpec

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def module(self) -> str:
        return self.spec.module


class FieldModule:
    """Read-only view of one module's published fields.

    Values come from the `TelemetryStore` cache, so reading a field is a
    dict lookup — no wire traffic. A field that the node has not published
    yet reads as None (upstream's behaviour); use `has_data()` to tell
    "no telemetry at all yet" from "this field is not published".
    """

    def __init__(self, name: str, fields: Mapping[str, FieldRef],
                 store: TelemetryStore) -> None:
        self._name = name
        self._fields = dict(fields)
        self._store = store

    def __repr__(self) -> str:
        return f"<FieldModule {self._name!r} fields={sorted(self._fields)}>"

    @property
    def name(self) -> str:
        return self._name

    @property
    def field_names(self) -> list[str]:
        return sorted(self._fields)

    @property
    def refs(self) -> dict[str, FieldRef]:
        return dict(self._fields)

    def ref(self, field_name: str) -> FieldRef:
        try:
            return self._fields[field_name]
        except KeyError:
            raise AttributeError(
                f"module {self._name!r} has no field {field_name!r}; "
                f"known fields: {sorted(self._fields)}") from None

    def get(self, field_name: str, default: int | None = None) -> int | None:
        ref = self._fields.get(field_name)
        if ref is None:
            return default
        value = self._store.get(ref.node_id, ref.key, None)
        return default if value is None else value

    def has_data(self) -> bool:
        """True once any node backing this module has published telemetry."""
        return any(self._store.has_data(ref.node_id) for ref in self._fields.values())

    def wait_for(self, field_name: str, timeout: float | None = None,
                 default: int | None = None) -> int | None:
        ref = self.ref(field_name)
        return self._store.wait_for(ref.node_id, ref.key, timeout, default)

    def snapshot(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for field_name, ref in self._fields.items():
            value = self._store.get(ref.node_id, ref.key, None)
            if value is not None:
                out[field_name] = value
        return out

    def __getattr__(self, field_name: str):
        # __getattr__ only fires for attributes we don't have; internal
        # state is stored in _name/_fields/_store, which resolve normally.
        if field_name.startswith("_"):
            raise AttributeError(field_name)
        ref = self._fields.get(field_name)
        if ref is None:
            raise AttributeError(
                f"module {self._name!r} has no field {field_name!r}; "
                f"known fields: {sorted(self._fields)}")
        return self._store.get(ref.node_id, ref.key, None)


def build_namespace(schemas: Mapping[int, Mapping[int, FieldSpec]],
                    node_names: Mapping[int, str],
                    store: TelemetryStore) -> dict[str, FieldModule]:
    """Group every discovered field into ``{module_name: FieldModule}``.

    `schemas` maps node id -> {floor: FieldSpec} (from
    `pylumicube.discovery`); `node_names` maps node id -> preferred name.
    """
    modules: dict[str, dict[str, FieldRef]] = {}
    owners: dict[str, int] = {}

    for node_id in sorted(schemas):
        node_name = node_names.get(node_id, "")
        for spec in sorted(schemas[node_id].values(), key=lambda s: s.floor):
            if not spec.name:
                continue
            module_name = spec.module or node_name
            if not module_name:
                continue
            if module_name == "buttons" and node_name in CUBE_NODE_NAMES:
                module_name = "system_button"
            fields = modules.setdefault(module_name, {})
            if spec.name in fields:
                previous = fields[spec.name]
                if previous.node_id != node_id:
                    log.warning(
                        "module %r field %r claimed by both node %d and node %d; "
                        "keeping node %d", module_name, spec.name,
                        previous.node_id, node_id, previous.node_id)
                continue
            fields[spec.name] = FieldRef(node_id=node_id, key=spec.floor, spec=spec)
            owners.setdefault(module_name, node_id)

    return {name: FieldModule(name, fields, store)
            for name, fields in modules.items()}
