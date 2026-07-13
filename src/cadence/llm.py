"""Thin LLM access layer.

Cadence uses an LLM only at the edges — parsing the request and drafting
agenda/handover text. Every call site has a deterministic fallback, so a
missing key or API hiccup degrades quality but never breaks the demo.

Provider detection: ANTHROPIC_API_KEY first, then OPENAI_API_KEY, else none.
"""

from __future__ import annotations

import os
import sys
from typing import TypeVar

from pydantic import BaseModel

MODEL = os.environ.get("CADENCE_MODEL", "claude-opus-4-8")
OPENAI_MODEL = os.environ.get("CADENCE_OPENAI_MODEL", "gpt-4o-mini")
# hard cap so a slow API call can never stall a Slack response; fall back instead
LLM_TIMEOUT = float(os.environ.get("CADENCE_LLM_TIMEOUT", "12"))

T = TypeVar("T", bound=BaseModel)


def provider() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return None


def log(message: str) -> None:
    print(f"[cadence] {message}", file=sys.stderr, flush=True)


def parse_structured(system: str, user_text: str, schema: type[T]) -> T | None:
    """One LLM call returning a validated instance of `schema`, or None."""
    which = provider()
    try:
        if which == "anthropic":
            import anthropic

            client = anthropic.Anthropic(timeout=LLM_TIMEOUT)
            response = client.messages.parse(
                model=MODEL,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": user_text}],
                output_format=schema,
            )
            return response.parsed_output
        if which == "openai":
            from openai import OpenAI

            client = OpenAI(timeout=LLM_TIMEOUT)
            # structured outputs: the SDK enforces the pydantic schema server-side
            parse = getattr(client.chat.completions, "parse", None) or client.beta.chat.completions.parse
            completion = parse(
                model=OPENAI_MODEL,
                max_completion_tokens=1024,
                response_format=schema,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_text},
                ],
            )
            return completion.choices[0].message.parsed
    except Exception as exc:  # noqa: BLE001 - any failure -> deterministic fallback
        log(f"LLM structured call failed ({type(exc).__name__}: {exc}); using fallback parser")
    return None


def draft(system: str, user_text: str) -> str | None:
    """Short free-text drafting call (agenda, handover brief), or None."""
    which = provider()
    try:
        if which == "anthropic":
            import anthropic

            client = anthropic.Anthropic(timeout=LLM_TIMEOUT)
            response = client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": user_text}],
            )
            if response.stop_reason == "refusal":
                return None
            return next((b.text for b in response.content if b.type == "text"), None)
        if which == "openai":
            from openai import OpenAI

            client = OpenAI(timeout=LLM_TIMEOUT)
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=1024,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_text},
                ],
            )
            return response.choices[0].message.content
    except Exception as exc:  # noqa: BLE001
        log(f"LLM draft call failed ({type(exc).__name__}: {exc}); using deterministic fallback")
    return None
