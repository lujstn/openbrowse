"""Agent-controlled, schema-validated output store — the single answer surface.

Built empty from the task's ``outputSchema`` before the agent runs, so the finished
shape is visible and waiting from turn zero; the agent fills it in place with
validated writes rather than assembling one blob at the end. Every mutating write is
checked against the per-item / top-level Pydantic model live, so an item that does
not fit the schema is rejected with a message the agent can act on.

Kept free of any browser-use import so it is unit-testable on its own; the
browser-use action wrappers that expose it to the agent live in ``tools.py``.
"""

from __future__ import annotations

import json
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel, TypeAdapter, ValidationError


def _peel_optional(annotation: Any) -> Any:
    """Strip an ``Optional``/``Union[..., None]`` wrapper to its single real type."""
    if get_origin(annotation) is Union:
        non_null = [a for a in get_args(annotation) if a is not type(None)]
        if len(non_null) == 1:
            return non_null[0]
    return annotation


def _is_list(annotation: Any) -> bool:
    return get_origin(_peel_optional(annotation)) is list


def _item_model_of(annotation: Any) -> type[BaseModel] | None:
    """The ``BaseModel`` element type of a ``list[Item]`` field annotation, if any."""
    inner = _peel_optional(annotation)
    if get_origin(inner) is not list:
        return None
    args = get_args(inner)
    if not args:
        return None
    elem = _peel_optional(args[0])
    if isinstance(elem, type) and issubclass(elem, BaseModel):
        return elem
    return None


def _empty_for(annotation: Any) -> Any:
    inner = _peel_optional(annotation)
    if get_origin(inner) is list:
        return []
    if isinstance(inner, type) and issubclass(inner, BaseModel):
        return {}
    return None


def _is_empty_value(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _first_error(exc: ValidationError) -> str:
    errs = exc.errors()
    if not errs:
        return str(exc)
    parts = []
    for err in errs[:4]:
        loc = ".".join(str(x) for x in err.get("loc", ())) or "(root)"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts)


class OutputStore:
    """Holds the answer the agent is building, validated against the output schema."""

    def __init__(self, output_model: type[BaseModel]) -> None:
        self._model = output_model
        self._array_field: str | None = None
        self._item_model: type[BaseModel] | None = None
        for name, field in output_model.model_fields.items():
            if _is_list(field.annotation):
                self._array_field = name
                self._item_model = _item_model_of(field.annotation)
                break
        self._data: dict[str, Any] = {
            name: _empty_for(field.annotation)
            for name, field in output_model.model_fields.items()
        }

    @property
    def output_model(self) -> type[BaseModel]:
        return self._model

    @property
    def array_field(self) -> str | None:
        return self._array_field

    @property
    def item_model(self) -> type[BaseModel] | None:
        return self._item_model

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def is_empty(self) -> bool:
        return all(_is_empty_value(v) for v in self._data.values())

    def read_output(self) -> str:
        return json.dumps(self._data, indent=2, default=str)

    def add_item(self, item: Any) -> tuple[bool, str]:
        if not self._array_field:
            return False, "This output has no list to add items to; use set_field."
        if not isinstance(item, dict):
            return False, "add_item expects an object of field/value pairs."
        clean, err = self._validate_item(item)
        if err:
            return False, err
        arr = self._data[self._array_field]
        arr.append(clean)
        return True, f"Added item #{len(arr) - 1} to '{self._array_field}' ({len(arr)} total)."

    def update_item(self, index: Any, fields: Any) -> tuple[bool, str]:
        if not self._array_field:
            return False, "This output has no list to update."
        try:
            index = int(index)
        except (TypeError, ValueError):
            return False, f"index must be an integer, got {index!r}."
        arr = self._data[self._array_field]
        if not 0 <= index < len(arr):
            upper = len(arr) - 1 if arr else 0
            return False, f"No item at index {index}. Valid range: 0..{upper}."
        if not isinstance(fields, dict):
            return False, "update_item expects an object of field/value pairs to merge."
        base = arr[index] if isinstance(arr[index], dict) else {}
        clean, err = self._validate_item({**base, **fields})
        if err:
            return False, err
        arr[index] = clean
        return True, f"Updated item #{index} ({', '.join(fields) or 'nothing'})."

    def set_field(self, key: str, value: Any) -> tuple[bool, str]:
        if key not in self._model.model_fields:
            known = ", ".join(self._model.model_fields)
            return False, f"'{key}' is not an output field. Fields: {known}."
        if key == self._array_field:
            return False, f"'{key}' is a list; use add_item/update_item instead."
        adapter = TypeAdapter(self._model.model_fields[key].annotation)
        try:
            validated = adapter.validate_python(value)
        except ValidationError as exc:
            return False, f"'{key}' rejected: {_first_error(exc)}"
        self._data[key] = adapter.dump_python(validated, mode="json")
        return True, f"Set '{key}'."

    def search_output(self, query: str) -> str:
        q = (query or "").strip().lower()
        if not q:
            return self.read_output()
        matches: dict[str, Any] = {}
        for name, value in self._data.items():
            if name == self._array_field and isinstance(value, list):
                hits = [
                    {"index": i, "item": it}
                    for i, it in enumerate(value)
                    if q in json.dumps(it, default=str).lower()
                ]
                if hits:
                    matches[name] = hits
            elif q in json.dumps({name: value}, default=str).lower():
                matches[name] = value
        return json.dumps(matches, indent=2, default=str)

    def item_missing_fields(self, index: int) -> list[str]:
        """The item-model fields still empty on the array item at ``index`` — the
        drill-in nudge for a stub added from listing data.
        """
        if not self._array_field or self._item_model is None:
            return []
        arr = self._data.get(self._array_field) or []
        if not 0 <= index < len(arr):
            return []
        item = arr[index]
        if not isinstance(item, dict):
            return []
        return [
            name
            for name in self._item_model.model_fields
            if _is_empty_value(item.get(name))
        ]

    def item_count(self) -> int:
        if not self._array_field:
            return 0
        arr = self._data.get(self._array_field)
        return len(arr) if isinstance(arr, list) else 0

    def empty_fields(self) -> list[str]:
        """Schema fields still empty but plausibly fillable, for the completeness gate."""
        out: list[str] = []
        for name, value in self._data.items():
            if name != self._array_field and _is_empty_value(value):
                out.append(f"{name} (not set)")
        if self._array_field:
            arr = self._data[self._array_field]
            if not arr:
                out.append(f"{self._array_field} (list is empty)")
            elif self._item_model is not None:
                total = len(arr)
                for fname in self._item_model.model_fields:
                    missing = sum(
                        1
                        for it in arr
                        if not isinstance(it, dict) or _is_empty_value(it.get(fname))
                    )
                    if missing:
                        out.append(
                            f"{fname} — empty on {missing} of {total} {self._array_field}"
                        )
        return out

    def _validate_item(self, item: dict) -> tuple[dict | None, str | None]:
        if self._item_model is None:
            return item, None
        try:
            validated = self._item_model.model_validate(item)
        except ValidationError as exc:
            return None, f"item rejected: {_first_error(exc)}"
        return validated.model_dump(mode="json"), None
