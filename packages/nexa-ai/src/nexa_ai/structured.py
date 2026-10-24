"""Structured output: schema checks on the way in, validation on the way out.

Whatever the provider promises, the gateway validates every structured answer itself (JSON
Schema draft 2020-12) before handing it to a caller as `parsed`. Unvalidated model output is
never treated as data (architecture doc 12.6).

The schema is tenant-supplied, untrusted input:

- Validators are built on an empty `referencing.Registry`, so resolving a reference never
  performs network or file I/O. Only local references (`#/...`) are accepted.
- `$id`, `$schema`, `$anchor` and `$dynamicAnchor` are refused: an inline response schema
  never needs them, and `$id` would re-base relative references onto a remote origin.
- `pattern` and `patternProperties` are refused in Phase 04. Python's `re` has no timeout, so
  a catastrophic pattern applied to model output could pin the event loop.
- Every reference must resolve inside the schema, and recursive schemas are refused: a cycle
  of references can make validation recurse without bound, and Anthropic's structured
  outputs do not support recursion either.
- Size, depth and node count are capped. `format` stays an annotation: no format checker is
  configured, so it is never executed.

Anything the validator cannot resolve is the schema's fault, reported as AI_INVALID_REQUEST
before any provider is called.
"""

import json
from typing import Any
from urllib.parse import unquote

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable

from nexa_ai.errors import AIInvalidRequest

MAX_SCHEMA_BYTES = 32 * 1024
MAX_SCHEMA_DEPTH = 24
MAX_SCHEMA_NODES = 2_000
MAX_SCHEMA_REFERENCES = 100
# Reference evaluations a schema may imply once every reference is followed (see
# _check_references): bounds validation time for schemas whose references fan out.
MAX_SCHEMA_EXPANSION = 1_000
_ERROR_LIMIT = 500

Pointer = tuple[str, ...]

_FORBIDDEN_KEYWORDS = frozenset(
    {"$id", "$schema", "$anchor", "$dynamicAnchor", "pattern", "patternProperties"}
)
_REFERENCE_KEYWORDS = frozenset({"$ref", "$dynamicRef"})
# Keywords whose value maps names (chosen by the caller) to subschemas.
_NAMED_SCHEMAS = frozenset({"properties", "$defs", "definitions", "dependentSchemas"})
# Keywords whose value is data, not schema: never inspected for keywords.
_DATA_KEYWORDS = frozenset({"enum", "const", "default", "examples"})

# No `retrieve` callback: anything that is not local or a built-in meta-schema is Unresolvable.
_OFFLINE_REGISTRY: Registry[Any] = Registry()


class OutputMismatch(Exception):
    """The model's answer is not valid JSON or does not match the schema.

    `reason` quotes the offending values and is only ever sent back to the model in the
    repair turn. `summary` names the failing keywords without any value and is what logs
    and error records carry, because model output often echoes the prompt."""

    def __init__(self, reason: str, summary: str) -> None:
        super().__init__(summary)
        self.reason = reason[:_ERROR_LIMIT]
        self.summary = summary[:_ERROR_LIMIT]


def check_schema(schema: dict[str, Any]) -> None:
    """Refuse unsafe, invalid or oversized schemas before any provider call."""
    if len(json.dumps(schema)) > MAX_SCHEMA_BYTES:
        raise AIInvalidRequest("The response schema is too large.")
    if schema.get("type") != "object":
        raise AIInvalidRequest("The response schema must describe a JSON object.")
    references: list[tuple[Pointer, Pointer]] = []
    _walk(schema, (), depth=0, budget=[MAX_SCHEMA_NODES], references=references)
    _check_references(schema, references)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise AIInvalidRequest(f"The response schema is invalid: {exc.message[:200]}") from exc


def parse_structured(text: str, schema: dict[str, Any]) -> Any:
    """Parse and validate; raise OutputMismatch with a reason suitable for a repair prompt."""
    try:
        value = json.loads(_strip_fences(text))
    except json.JSONDecodeError as exc:
        raise OutputMismatch(
            f"not valid JSON: {exc.msg} at position {exc.pos}", "invalid JSON"
        ) from exc
    try:
        errors = sorted(_validator(schema).iter_errors(value), key=lambda e: list(e.path))
    except (Unresolvable, RecursionError) as exc:  # check_schema rules these out up front
        raise AIInvalidRequest("The response schema cannot be evaluated.") from exc
    if errors:
        raise OutputMismatch(
            "; ".join(_describe(error) for error in errors[:5]),
            f"{len(errors)} schema error(s): "
            + ", ".join(sorted({str(error.validator) for error in errors})),
        )
    return value


def repair_instruction(reason: str) -> str:
    """The extra user turn sent on the single repair attempt."""
    return (
        "Your previous answer did not match the required JSON schema: "
        f"{reason}. Reply again with only a JSON document that matches the schema exactly, "
        "with no explanation and no code fences."
    )


def _validator(schema: dict[str, Any]) -> Draft202012Validator:
    return Draft202012Validator(schema, registry=_OFFLINE_REGISTRY)


def _walk(
    node: Any,
    location: Pointer,
    *,
    depth: int,
    budget: list[int],
    references: list[tuple[Pointer, Pointer]],
) -> None:
    """Check keywords and limits, and collect (where, points-to) for every reference."""
    budget[0] -= 1
    if budget[0] < 0:
        raise AIInvalidRequest("The response schema is too complex.")
    if depth > MAX_SCHEMA_DEPTH:
        raise AIInvalidRequest("The response schema is nested too deeply.")
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _DATA_KEYWORDS:
                continue
            if key in _NAMED_SCHEMAS and isinstance(value, dict):
                for name, subschema in value.items():
                    _walk(
                        subschema,
                        (*location, str(key), str(name)),
                        depth=depth + 2,
                        budget=budget,
                        references=references,
                    )
                continue
            if key in _FORBIDDEN_KEYWORDS:
                raise AIInvalidRequest(f"The response schema may not use '{key}'.")
            if key in _REFERENCE_KEYWORDS:
                if not (isinstance(value, str) and value.startswith("#")):
                    raise AIInvalidRequest(
                        "The response schema may only use local '#/...' references."
                    )
                references.append((location, _parse_pointer(value)))
                if len(references) > MAX_SCHEMA_REFERENCES:
                    raise AIInvalidRequest("The response schema has too many references.")
                continue
            _walk(
                value, (*location, str(key)), depth=depth + 1, budget=budget, references=references
            )
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _walk(
                item, (*location, str(index)), depth=depth + 1, budget=budget, references=references
            )


def _parse_pointer(reference: str) -> Pointer:
    fragment = unquote(reference[1:])
    if not fragment:
        return ()
    if not fragment.startswith("/"):
        raise AIInvalidRequest("The response schema may only use '#/...' JSON pointers.")
    return tuple(part.replace("~1", "/").replace("~0", "~") for part in fragment[1:].split("/"))


def _check_references(schema: dict[str, Any], references: list[tuple[Pointer, Pointer]]) -> None:
    """Every reference must point at a subschema; no chain of references may loop.

    Evaluating the subschema at `target` evaluates every reference located inside it, so a
    reference at L points to T, and T contains another reference, and so on: a cycle in that
    graph means validation can recurse forever."""
    for _, target in references:
        node: Any = schema
        for part in target:
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                node = node[int(part)]
            else:
                raise AIInvalidRequest(
                    "The response schema has a reference that cannot be resolved."
                )
        if not isinstance(node, dict):
            raise AIInvalidRequest("The response schema has a reference that cannot be resolved.")

    def inside(location: Pointer, target: Pointer) -> bool:
        return location[: len(target)] == target

    following = {
        i: [j for j, (location, _) in enumerate(references) if inside(location, target)]
        for i, (_, target) in enumerate(references)
    }
    state: dict[int, int] = {}  # 1 = on the current path, 2 = finished

    def visit(i: int) -> None:
        state[i] = 1
        for j in following[i]:
            if state.get(j) == 1:
                raise AIInvalidRequest("Recursive response schemas are not supported.")
            if j not in state:
                visit(j)
        state[i] = 2

    for i in following:
        if i not in state:
            visit(i)

    # Without a loop, references can still fan out: two references to a target that holds two
    # references to the next one double the work at every level, and the validator does not
    # memoize. Count the reference evaluations each reference implies and refuse a schema
    # whose total would make validation slow.
    expanded: dict[int, int] = {}

    def cost(i: int) -> int:
        if i not in expanded:
            expanded[i] = min(MAX_SCHEMA_EXPANSION + 1, 1 + sum(cost(j) for j in following[i]))
        return expanded[i]

    if sum(cost(i) for i in following) > MAX_SCHEMA_EXPANSION:
        raise AIInvalidRequest("The response schema's references expand too much.")


def _describe(error: ValidationError) -> str:
    path = "/".join(str(part) for part in error.absolute_path) or "(root)"
    return f"{path}: {error.message[:160]}"


def _strip_fences(text: str) -> str:
    """Models sometimes wrap JSON in ```json fences despite instructions."""
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    return stripped.strip()
