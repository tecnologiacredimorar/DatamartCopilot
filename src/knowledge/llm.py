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
    """Call AI and parse JSON from the response."""
    full_prompt = prompt + "\n\nResponda SOMENTE com JSON válido, sem texto antes ou depois."
    result = call_ai(full_prompt, system, max_tokens)

    # Try direct parse
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        pass

    # Try to extract JSON block from markdown
    match = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", result, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find any JSON object/array
    match = re.search(r"(\{.*\}|\[.*\])", result, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    logger.warning("Could not parse JSON from AI response, returning raw")
    return {"_raw": result}


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
