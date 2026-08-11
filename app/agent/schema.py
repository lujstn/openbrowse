"""JSON Schema -> Pydantic model converter for Browser Use ``outputSchema``.

Browser Use's V3 API accepts an ``outputSchema`` as a JSON Schema (2020-12) where
optional fields are expressed as ``anyOf: [<type>, {"type": "null"}]`` (what
Zod/Pydantic emit for Optional) and enums as ``{"type": "string", "enum": [...]}``.
``Agent(output_model_schema=...)`` needs a Pydantic model *class*, and the stock
``schema_dict_to_pydantic_model`` helper rejects ``anyOf`` (it is only wired to the
``extract`` action), so this converter builds the model for the typed ``done`` path.

Supported: object / array / primitive types, ``anyOf``/``oneOf`` and ``type``-array
Optional (``[T, null]``), string ``enum`` (as ``Literal``), nested objects and
arrays, ``$ref``/``$defs``, ``additionalProperties`` (loose -> allow, false -> forbid)
and ``description`` preservation. A genuine multi-branch union (more than one
non-null branch) raises :class:`SchemaConversionError`, letting the caller fall
back to prose rather than crash.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, create_model


class SchemaConversionError(ValueError):
    """Raised when a JSON Schema can't be represented as a Pydantic model."""


_PRIMITIVES: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


def _is_null(node: Any) -> bool:
    return isinstance(node, dict) and node.get("type") == "null"


def _safe_identifier(name: str) -> str:
    out = "".join(c if c.isalnum() else "_" for c in name)
    if not out or not (out[0].isalpha() or out[0] == "_"):
        out = f"m_{out}"
    return out


class _Converter:
    def __init__(self, defs: dict[str, Any]):
        self._defs = defs

    def _deref(self, node: Any) -> Any:
        seen = 0
        while isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/"):
                raise SchemaConversionError(f"Unsupported $ref: {ref!r}")
            target = self._defs.get(ref.split("/")[-1])
            if target is None:
                raise SchemaConversionError(f"Unresolved $ref: {ref}")
            merged = dict(target)
            for k, v in node.items():
                if k != "$ref":
                    merged.setdefault(k, v)
            node = merged
            seen += 1
            if seen > 50:
                raise SchemaConversionError("Cyclic $ref chain")
        return node

    def unwrap_nullable(self, node: dict) -> tuple[dict, bool]:
        """Collapse Optional-style unions into (concrete_node, nullable)."""
        node = self._deref(node)
        for key in ("anyOf", "oneOf"):
            if key in node:
                branches = node.get(key)
                if not isinstance(branches, list) or not branches:
                    raise SchemaConversionError(f"Empty/invalid {key}")
                non_null = [self._deref(b) for b in branches if not _is_null(b)]
                has_null = any(_is_null(b) for b in branches)
                if len(non_null) != 1:
                    raise SchemaConversionError(
                        f"Unsupported {key}: {len(non_null)} non-null branches "
                        "(only Optional-style unions are supported)"
                    )
                merged = dict(non_null[0])
                if "description" in node and "description" not in merged:
                    merged["description"] = node["description"]
                inner, inner_nullable = self.unwrap_nullable(merged)
                return inner, (has_null or inner_nullable)

        node_type = node.get("type")
        if isinstance(node_type, list):
            non_null = [t for t in node_type if t != "null"]
            if len(non_null) != 1:
                raise SchemaConversionError(f"Unsupported multi-type: {node_type}")
            collapsed = dict(node)
            collapsed["type"] = non_null[0]
            return collapsed, ("null" in node_type)

        return node, False

    def resolve_type(self, node: dict, name: str) -> Any:
        node = self._deref(node)
        if "enum" in node:
            values = node.get("enum") or []
            if values and all(isinstance(v, str) for v in values):
                return Literal[tuple(values)]  # type: ignore[valid-type]
        node_type = node.get("type")
        if node_type == "object" or "properties" in node:
            return self.build_object(node, name)
        if node_type == "array":
            items = node.get("items")
            if isinstance(items, dict):
                inner_node, inner_nullable = self.unwrap_nullable(items)
                inner_type = self.resolve_type(inner_node, f"{name}Item")
                if inner_nullable:
                    inner_type = Optional[inner_type]
                return list[inner_type]  # type: ignore[valid-type]
            return list
        if node_type in _PRIMITIVES:
            return _PRIMITIVES[node_type]
        return Any

    def build_object(self, node: dict, name: str) -> type[BaseModel]:
        node = self._deref(node)
        properties: dict[str, Any] = node.get("properties") or {}
        required = set(node.get("required") or [])
        additional = node.get("additionalProperties", True)
        extra = "forbid" if additional is False else "allow"

        fields: dict[str, Any] = {}
        for prop_name, prop_schema in properties.items():
            if not prop_name.isidentifier():
                raise SchemaConversionError(
                    f"Property name is not a valid identifier: {prop_name!r}"
                )
            if not isinstance(prop_schema, dict):
                prop_schema = {}
            inner_node, nullable = self.unwrap_nullable(prop_schema)
            py_type = self.resolve_type(
                inner_node, _safe_identifier(f"{name}_{prop_name}")
            )
            description = inner_node.get("description") or prop_schema.get("description")
            in_required = prop_name in required
            if in_required and not nullable:
                default: Any = ...
            elif in_required and nullable:
                py_type = Optional[py_type]
                default = ...
            else:
                py_type = Optional[py_type]
                default = None
            fields[prop_name] = (py_type, Field(default, description=description))

        return create_model(  # type: ignore[call-overload]
            _safe_identifier(name),
            __config__=ConfigDict(extra=extra),
            **fields,
        )

    def build(self, schema: dict, name: str) -> type[BaseModel]:
        node, _ = self.unwrap_nullable(schema)
        if node.get("type") != "object" and "properties" not in node:
            raise SchemaConversionError("Top-level schema must be an object type")
        return self.build_object(node, name)


def json_schema_to_pydantic(
    schema: dict, model_name: str = "OutputSchema"
) -> type[BaseModel]:
    """Build a Pydantic model class from a JSON Schema dict.

    Raises :class:`SchemaConversionError` on constructs we cannot faithfully
    represent (genuine multi-branch unions, non-identifier property names,
    unresolved ``$ref``), so callers can fall back to prose output.
    """
    if not isinstance(schema, dict):
        raise SchemaConversionError("Schema must be a dict")
    defs = schema.get("$defs") or schema.get("definitions") or {}
    return _Converter(defs).build(schema, model_name)
