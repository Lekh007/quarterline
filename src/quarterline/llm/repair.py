"""One-shot JSON repair (SPEC §17 "JSON repair", §18 gate re-run rule).

Flow per SPEC §17:
1. Generate once.
2. If the output is invalid — unparseable JSON OR a pydantic schema failure —
   allow exactly ONE repair pass that sends the invalid output plus the schema
   back asking for corrected JSON only.
3. Parse with Pydantic.
4. Semantic validation happens afterwards in the validation gate
   (``generation.validate_generation``) — also re-run after a repair.
5. If still invalid: controlled :class:`GenerationFailure`. No retries, no loop.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from quarterline.llm.base import GenerationProvider, GenerationResult

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class GenerationFailure(RuntimeError):
    """Controlled failure: output remained invalid after the single repair pass."""


REPAIR_INSTRUCTION = (
    "Your previous reply was not valid output for the required JSON schema. "
    "Return ONLY the corrected JSON document — no prose, no markdown fences, "
    "no commentary. Required schema:\n{schema}"
)


@dataclass
class RepairOutcome:
    """Result of generate-once + at-most-one-repair.

    ``first_pass_valid`` records whether the FIRST response already parsed;
    ``repair_used`` records whether the repair pass ran (both feed run
    metrics: JSON validity before/after repair, SPEC §22/§24).
    """

    model: BaseModel
    results: list[GenerationResult] = field(default_factory=list)
    first_pass_valid: bool = True
    repair_used: bool = False
    raw_first_text: str = ""

    @property
    def json_valid_before_repair(self) -> bool:
        return self.first_pass_valid


def _extract_json_text(text: str) -> str | None:
    """Best-effort extraction of the JSON object from a model reply."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    try:
        json.loads(stripped)
        return stripped
    except ValueError:
        pass
    fenced = _FENCED_JSON_RE.search(stripped)
    if fenced:
        try:
            json.loads(fenced.group(1))
            return fenced.group(1)
        except ValueError:
            pass
    # First balanced object spanning from the first '{' to the matching '}'.
    start = stripped.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(stripped)):
            char = stripped[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = stripped[start : index + 1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except ValueError:
                        break
                    # Unparseable balanced region: no further guessing.
    return None


def parse_model_output(
    text: str, model_cls: type[BaseModel]
) -> tuple[BaseModel | None, str | None]:
    """Parse model text into ``model_cls``; return (model, error_reason)."""
    json_text = _extract_json_text(text)
    if json_text is None:
        return None, "output is not parseable JSON"
    try:
        payload = json.loads(json_text)
    except ValueError as exc:  # pragma: no cover - _extract_json_text validated
        return None, f"output is not parseable JSON: {exc}"
    try:
        return model_cls.model_validate(payload), None
    except ValidationError as exc:
        return None, f"schema validation failed: {exc.error_count()} error(s): {exc.errors()[:3]}"


def generate_and_parse(
    provider: GenerationProvider,
    *,
    messages: list[dict],
    model_cls: type[BaseModel],
    json_schema: dict | None = None,
) -> RepairOutcome:
    """Generate once; if parsing fails run exactly one repair pass.

    Raises :class:`GenerationFailure` when the output is still invalid after
    the repair pass (controlled failure; SPEC §25 failure table).
    """
    first = provider.generate(messages, json_schema)
    parsed, reason = parse_model_output(first.text, model_cls)
    if parsed is not None:
        return RepairOutcome(model=parsed, results=[first], first_pass_valid=True)

    repair_messages = list(messages) + [
        {"role": "assistant", "content": first.text},
        {
            "role": "user",
            "content": REPAIR_INSTRUCTION.format(
                schema=json.dumps(json_schema, indent=2) if json_schema else "(see system prompt)"
            ),
        },
    ]
    second = provider.generate(repair_messages, json_schema)
    parsed_second, reason_second = parse_model_output(second.text, model_cls)
    if parsed_second is not None:
        return RepairOutcome(
            model=parsed_second,
            results=[first, second],
            first_pass_valid=False,
            repair_used=True,
            raw_first_text=first.text,
        )
    raise GenerationFailure(
        f"generation output invalid after the single repair pass: {reason_second or reason}"
    )


__all__ = [
    "REPAIR_INSTRUCTION",
    "GenerationFailure",
    "RepairOutcome",
    "generate_and_parse",
    "parse_model_output",
]
