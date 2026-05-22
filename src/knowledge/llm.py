from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Generator

logger = logging.getLogger(__name__)

# Model used for all focused knowledge-base calls (< 4000 tokens output)
LLM_MODEL = os.getenv("LLM_MODEL", "anthropic/claude-opus-4-7")
MAX_OUTPUT_TOKENS = 4000


def _get_api_key() -> str:
    return os.getenv("ANTHROPIC_API_KEY", "")


def call_ai(prompt: str, system: str = "", max_tokens: int = MAX_OUTPUT_TOKENS) -> str:
    """Single focused AI call via LiteLLM. Max 4000 tokens output."""
    try:
        import litellm
        litellm.suppress_debug_info = True

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        response = litellm.completion(
            model=LLM_MODEL,
            messages=messages,
            max_tokens=max_tokens,
            api_key=_get_api_key(),
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        logger.error("LiteLLM call failed: %s", e)
        raise


def call_ai_json(prompt: str, system: str = "", max_tokens: int = MAX_OUTPUT_TOKENS) -> dict[str, Any]:
    """Call AI and parse JSON from the response.

    Tries four extraction strategies in order:
    1. Direct json.loads on the stripped response
    2. Strip markdown fences (```json ... ```) then parse
    3. Brace-counting to find the outermost {...} object
    4. Bracket-counting to find the outermost [...] array
    Raises ValueError with the raw response excerpt if all strategies fail.
    """
    full_prompt = (
        prompt
        + "\n\nIMPORTANTE: Responda SOMENTE com JSON válido."
        " Não use markdown fences (```). Não escreva nada antes ou depois do JSON."
    )
    raw = call_ai(full_prompt, system, max_tokens)
    stripped = raw.strip()

    # 1. Direct parse
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # 2. Strip markdown fences
    clean = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.MULTILINE)
    clean = re.sub(r"\s*```\s*$", "", clean, flags=re.MULTILINE).strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass

    # 3. Brace-count to find outermost {...}
    start = stripped.find("{")
    if start != -1:
        depth = 0
        for i, ch in enumerate(stripped[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(stripped[start : i + 1])
                    except json.JSONDecodeError:
                        break

    # 4. Bracket-count to find outermost [...]
    start = stripped.find("[")
    if start != -1:
        depth = 0
        for i, ch in enumerate(stripped[start:], start):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(stripped[start : i + 1])
                    except json.JSONDecodeError:
                        break

    logger.error("JSON parse failed. Raw response (first 500 chars):\n%s", raw[:500])
    raise ValueError(
        f"A IA não retornou JSON válido. Início da resposta: {raw[:300]}"
    )


def call_ai_stream(
    prompt: str,
    system: str = "",
    max_tokens: int = MAX_OUTPUT_TOKENS,
) -> Generator[str, None, None]:
    """Streaming AI call via LiteLLM."""
    try:
        import litellm
        litellm.suppress_debug_info = True

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        response = litellm.completion(
            model=LLM_MODEL,
            messages=messages,
            max_tokens=max_tokens,
            api_key=_get_api_key(),
            stream=True,
        )
        for chunk in response:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
    except Exception as e:
        logger.error("LiteLLM stream failed: %s", e)
        raise
