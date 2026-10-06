"""`!ask`: SYKE as a general-purpose assistant, with follow-ups by replying."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from .config import Settings

ASK_TIMEOUT_SECONDS = 45
ASK_MAX_TOKENS = 1000
HISTORY_TURNS = 10
MAX_QUESTION = 1500

SYSTEM_PROMPT = """You are SYKE, an AI assistant living in a Discord server. Answer whatever people ask: \
facts, explanations, homework help, code, advice, opinions, ideas, translations, maths.

Be accurate and genuinely helpful first. Your personality is witty, confident and a little sarcastic, \
like the smartest person in the group chat, but never let a joke replace the actual answer.
- Keep answers short: a few sentences or a tight list. Go longer only when asked or when the \
question really needs it (code, step-by-step help).
- Use Discord markdown (**bold**, lists, `code`, ``` code blocks).
- If you don't know or it's after your knowledge cutoff, say so instead of guessing. You can't \
browse the internet or see images.
- Never @mention anyone, and refuse anything hateful, sexual involving minors, or dangerous.

Today is {today}. You are talking in {where}; the person asking is {asker}."""


class AskUnavailable(RuntimeError):
    """No AI provider is configured."""


def system_prompt(asker: str, where: str) -> str:
    today = datetime.now(timezone.utc).strftime("%A %d %B %Y")
    return SYSTEM_PROMPT.format(today=today, where=where, asker=asker)


async def _openai(settings: Settings, system: str, messages: list[dict]) -> str:
    async with httpx.AsyncClient(timeout=ASK_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json={
                "model": settings.openai_model,
                "temperature": 0.7,
                "max_tokens": ASK_MAX_TOKENS,
                "messages": [{"role": "system", "content": system}, *messages],
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def _anthropic(settings: Settings, system: str, messages: list[dict]) -> str:
    async with httpx.AsyncClient(timeout=ASK_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": settings.anthropic_api_key, "anthropic-version": "2023-06-01"},
            json={
                "model": settings.anthropic_model,
                "max_tokens": ASK_MAX_TOKENS,
                "temperature": 0.7,
                "system": system,
                "messages": messages,
            },
        )
        resp.raise_for_status()
        return "".join(b.get("text", "") for b in resp.json()["content"])


async def ask(settings: Settings, question: str, history: list[dict] | None = None,
              asker: str = "someone", where: str = "a Discord server") -> str:
    """The answer to `question`, continuing `history` ([{"role", "content"}, ...]). Raises on failure."""
    if settings.ai_provider == "none":
        raise AskUnavailable
    messages = [*(history or [])[-HISTORY_TURNS:], {"role": "user", "content": question[:MAX_QUESTION]}]
    call = _openai if settings.ai_provider == "openai" else _anthropic
    answer = (await call(settings, system_prompt(asker, where), messages)).strip()
    if not answer:
        raise ValueError("empty answer")
    return answer
