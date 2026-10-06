import json
import re
from typing import Callable, Optional, TypeVar

from pydantic import BaseModel

from analyst.llm import PipelineError, call_llm

T = TypeVar("T", bound=BaseModel)


def extract_json(text: str) -> dict:
    """Pull the first complete JSON object out of an LLM reply."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)

    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in the reply.")

    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start : i + 1])
    raise ValueError("The JSON object is incomplete (unbalanced braces).")


def call_json(
    task: str,
    messages: list[dict],
    model: type[T],
    validate: Optional[Callable[[T], None]] = None,
    max_attempts: int = 3,
) -> T:
    """Call the LLM and return a validated pydantic object.
    On a bad reply, feed the error back to the model and try again."""
    msgs = list(messages)
    last_err: Exception | None = None

    for _ in range(max_attempts):
        raw = call_llm(task, msgs) or ""
        try:
            obj = model.model_validate(extract_json(raw))
            if validate:
                validate(obj)          # should raise ValueError describing the problem
            return obj
        except ValueError as e:        # covers JSON errors and pydantic ValidationError
            last_err = e
            msgs = msgs + [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": f"That reply was not valid: {e}\n"
                    "Return ONLY the corrected JSON object, nothing else.",
                },
            ]

    raise PipelineError(
        f"Task '{task}' did not return valid JSON after {max_attempts} attempts: {last_err}"
    )