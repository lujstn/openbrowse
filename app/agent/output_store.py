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
import re
from typing import Any, Literal, Union, get_args, get_origin

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


def _literal_choices(annotation: Any) -> tuple[str, ...] | None:
    """The string choices of a ``Literal[...]`` field annotation (optionally wrapped
    in Optional), or None when the field is not a string-literal enum.
    """
    inner = _peel_optional(annotation)
    if get_origin(inner) is Literal:
        args = get_args(inner)
        if args and all(isinstance(a, str) for a in args):
            return args
    return None


def _coerce_scalar(value: Any, annotation: Any) -> Any:
    """Forgiving pre-validation coercion: trim string whitespace, and map a string
    case-insensitively onto a ``Literal`` enum choice ('Hybrid ' -> 'HYBRID') so an
    obviously-right value is never rejected over casing.
    """
    if not isinstance(value, str):
        return value
    value = value.strip()
    choices = _literal_choices(annotation)
    if choices and value not in choices:
        folded = value.lower()
        for choice in choices:
            if choice.lower() == folded:
                return choice
    return value


_TOKEN_STOPWORDS = {"at", "is", "the", "a", "of", "in", "on", "id", "url"}


def _name_tokens(name: str) -> set[str]:
    """Meaningful lowercase tokens of a camelCase/snake_case identifier, for fuzzy
    matching a schema field against a raw key (postedAt <-> datePosted).
    """
    parts = re.findall(r"[A-Za-z][a-z0-9]*", re.sub(r"[_\-]", " ", name))
    return {p.lower() for p in parts if len(p) > 1} - _TOKEN_STOPWORDS


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
        self._absent: dict[str, str] = {}

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

    def read_output(self, offset: int = 0, limit: int | None = None) -> str:
        """The output as JSON; ``offset``/``limit`` window the item array so a read
        of a large store never depends on a full dump (upstream layers truncate
        long tool results, silently hiding the tail).
        """
        if limit is None and not offset:
            return json.dumps(self._data, indent=2, default=str)
        if not self._array_field:
            return json.dumps(self._data, indent=2, default=str)
        arr = self._data.get(self._array_field) or []
        total = len(arr)
        offset = max(0, int(offset))
        end = total if limit is None else min(total, offset + max(0, int(limit)))
        windowed = dict(self._data)
        windowed[self._array_field] = arr[offset:end]
        windowed["_window"] = (
            f"showing {self._array_field}[{offset}:{end}] of {total}; "
            "call read_output with offset/limit for the rest"
        )
        return json.dumps(windowed, indent=2, default=str)

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

    def update_many(self, updates: Any) -> tuple[bool, str]:
        """Apply a list of ``{"index": n, "fields": {...}}`` merges in one call.
        Reports per-entry failures without aborting the rest.
        """
        if not isinstance(updates, list) or not updates:
            return False, (
                "update_items expects a non-empty list of {index, fields} objects."
            )
        applied = 0
        failures: list[str] = []
        for i, entry in enumerate(updates):
            if not isinstance(entry, dict) or "index" not in entry:
                failures.append(f"entry {i}: must be an object with index and fields")
                continue
            ok, msg = self.update_item(entry.get("index"), entry.get("fields"))
            if ok:
                applied += 1
            else:
                failures.append(f"entry {i}: {msg}")
        summary = f"Applied {applied} of {len(updates)} updates."
        if failures:
            summary += " Failed: " + "; ".join(failures)
        return applied > 0, summary

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
        annotation = self._model.model_fields[key].annotation
        adapter = TypeAdapter(annotation)
        try:
            validated = adapter.validate_python(_coerce_scalar(value, annotation))
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
            if name not in self._absent and _is_empty_value(item.get(name))
        ]

    def mark_absent(self, field: str, reason: str) -> tuple[bool, str]:
        """Record that ``field`` was looked for on the source and is genuinely not
        published there, so the completeness gate and nudges stop counting it as
        unfinished work. Accepts item fields and top-level fields.
        """
        field = (field or "").strip()
        reason = (reason or "").strip()
        known = set(self._model.model_fields)
        if self._item_model is not None:
            known |= set(self._item_model.model_fields)
        if field not in known:
            return False, f"'{field}' is not a schema field. Fields: {', '.join(sorted(known))}."
        if not reason:
            return False, "mark_absent needs a short reason saying where you looked."
        self._absent[field] = reason
        return True, (
            f"Marked '{field}' as absent from the source ({reason}). It no longer "
            "counts as unfinished; values you do find can still be written to it."
        )

    @property
    def absent_fields(self) -> dict[str, str]:
        return dict(self._absent)

    def item_count(self) -> int:
        if not self._array_field:
            return 0
        arr = self._data.get(self._array_field)
        return len(arr) if isinstance(arr, list) else 0

    def empty_fields(self) -> list[str]:
        """Schema fields still empty but plausibly fillable, for the completeness gate.
        Fields marked absent via ``mark_absent`` are settled and never listed.
        """
        out: list[str] = []
        for name, value in self._data.items():
            if name == self._array_field or name in self._absent:
                continue
            if _is_empty_value(value):
                out.append(f"{name} (not set)")
        if self._array_field:
            arr = self._data[self._array_field]
            if not arr:
                out.append(f"{self._array_field} (list is empty)")
            elif self._item_model is not None:
                total = len(arr)
                for fname in self._item_model.model_fields:
                    if fname in self._absent:
                        continue
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

    def coverage_summary(self) -> str:
        """A compact fill-state readout: item count, then item fields grouped as
        full / partial / empty / marked-absent, then unset top-level fields. This is
        the one-glance answer to "what is still missing", so verification never
        needs a full read_output dump.
        """
        parts: list[str] = []
        top_unset = [
            name
            for name, value in self._data.items()
            if name != self._array_field
            and name not in self._absent
            and _is_empty_value(value)
        ]
        if self._array_field:
            arr = self._data.get(self._array_field) or []
            total = len(arr)
            parts.append(f"{self._array_field}: {total} item(s)")
            if total and self._item_model is not None:
                full: list[str] = []
                partial: list[str] = []
                empty: list[str] = []
                for fname in self._item_model.model_fields:
                    if fname in self._absent:
                        continue
                    filled = sum(
                        1
                        for it in arr
                        if isinstance(it, dict) and not _is_empty_value(it.get(fname))
                    )
                    if filled == total:
                        full.append(fname)
                    elif filled:
                        partial.append(f"{fname} {filled}/{total}")
                    else:
                        empty.append(fname)
                if full:
                    parts.append("filled on all: " + ", ".join(full))
                if partial:
                    parts.append("partial: " + ", ".join(partial))
                if empty:
                    parts.append("empty on all: " + ", ".join(empty))
        if self._absent:
            parts.append("marked absent: " + ", ".join(sorted(self._absent)))
        if top_unset:
            parts.append("top-level not set: " + ", ".join(top_unset))
        return "Coverage — " + "; ".join(parts) + "."

    def extra_key_hints(self) -> list[str]:
        """Detect raw captured keys that look like they fill an empty schema field
        (extra.datePosted vs an empty postedAt), so the gate can point at a bulk
        promotion instead of sending the agent back to re-read pages.
        """
        if not self._array_field or self._item_model is None:
            return []
        arr = self._data.get(self._array_field) or []
        if not arr:
            return []
        total = len(arr)
        empty_fields = [
            fname
            for fname in self._item_model.model_fields
            if fname not in self._absent
            and any(
                isinstance(it, dict) and _is_empty_value(it.get(fname)) for it in arr
            )
        ]
        if not empty_fields:
            return []

        raw_keys: set[str] = set()
        for it in arr:
            if not isinstance(it, dict):
                continue
            for value in it.values():
                if isinstance(value, dict):
                    raw_keys.update(k for k in value if isinstance(k, str))
                elif isinstance(value, list):
                    for entry in value:
                        if isinstance(entry, dict):
                            key = entry.get("key") or entry.get("name")
                            if isinstance(key, str):
                                raw_keys.add(key)
        hints: list[str] = []
        for fname in empty_fields:
            ftokens = _name_tokens(fname)
            if not ftokens:
                continue
            for key in sorted(raw_keys):
                if key in self._item_model.model_fields:
                    continue
                if ftokens & _name_tokens(key):
                    missing = sum(
                        1
                        for it in arr
                        if isinstance(it, dict) and _is_empty_value(it.get(fname))
                    )
                    hints.append(
                        f"captured key '{key}' looks like it fills '{fname}' "
                        f"(empty on {missing} of {total}) — promote it with "
                        "update_items in one step instead of re-reading pages"
                    )
                    break
        return hints

    def _validate_item(self, item: dict) -> tuple[dict | None, str | None]:
        if self._item_model is None:
            return item, None
        coerced = {
            name: (
                _coerce_scalar(value, self._item_model.model_fields[name].annotation)
                if name in self._item_model.model_fields
                else value
            )
            for name, value in item.items()
        }
        try:
            validated = self._item_model.model_validate(coerced)
        except ValidationError as exc:
            return None, f"item rejected: {_first_error(exc)}"
        return validated.model_dump(mode="json"), None
