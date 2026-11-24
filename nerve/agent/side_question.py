"""``/btw``: a side question about a session, answered now and outside it.

A message sent to a session waits for its running turn, and its answer
becomes part of the conversation. A side question does neither: it asks the
model directly, with the session's recent transcript as context, through
Nerve's own Messages API client (the one session titles use). The answer goes
back to the person and is not added to the session.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from nerve.agent.plan_summary import _api_client_usable, _describe
from nerve.db.usage import estimate_cost_from_totals
from nerve.utils.llm import response_text

if TYPE_CHECKING:
    from nerve.config import NerveConfig

logger = logging.getLogger(__name__)

# The transcript sent as context: the last messages, cut from the oldest end.
MAX_MESSAGES = 40
MAX_CONTEXT_CHARS = 60_000
MAX_MESSAGE_CHARS = 6_000
MAX_QUESTION_CHARS = 4_000
MAX_TOKENS = 2_000
TIMEOUT = 120.0

_INSTRUCTIONS = (
    "You answer a side question that a person asks about an ongoing agent "
    "session. The transcript is the session so far; the agent may still be "
    "working. Answer from the transcript, briefly and directly, and say so "
    "when it doesn't tell. You cannot run tools or change anything, and your "
    "answer is not added to the session."
)


class SideQuestionError(Exception):
    """The side question could not be asked or answered; the message says why."""


def _transcript(messages: list[dict[str, Any]]) -> str:
    """The messages as ``Person:``/``Agent:`` paragraphs, oldest first, cut
    from the oldest end to :data:`MAX_CONTEXT_CHARS`."""
    parts: list[str] = []
    for m in messages:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        text = (m.get("content") or "").strip()
        if not text:
            continue
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[: MAX_MESSAGE_CHARS // 2] + "\n[…]\n" + text[-MAX_MESSAGE_CHARS // 2:]
        parts.append(f"{'Person' if role == 'user' else 'Agent'}: {text}")
    kept: list[str] = []
    size = 0
    for part in reversed(parts):
        if size + len(part) > MAX_CONTEXT_CHARS and kept:
            break
        kept.append(part)
        size += len(part) + 2
    return "\n\n".join(reversed(kept))


def side_model(config: NerveConfig, session: dict[str, Any]) -> str:
    """The session's model when it is a Claude model, else ``agent.model``."""
    model = (session.get("model") or "").strip()
    return model if model.startswith("claude") else config.agent.model


async def ask_side_question(
    config: NerveConfig,
    session: dict[str, Any],
    messages: list[dict[str, Any]],
    question: str,
) -> dict[str, Any]:
    """Answer *question* about *session* from its *messages* (oldest first).

    Returns ``{"answer", "model", "input_tokens", "output_tokens",
    "cost_usd"}``; ``cost_usd`` is the list-price estimate of this one call.
    Raises :class:`SideQuestionError` with a message for the person.
    """
    question = (question or "").strip()
    if not question:
        raise SideQuestionError("Ask a question: /btw <question>")
    if not _api_client_usable(config):
        raise SideQuestionError(
            "/btw needs Nerve's own API access (an API key, Bedrock or the proxy); "
            "a subscription token only runs the agent."
        )
    model = side_model(config, session)
    transcript = _transcript(messages) or "(the session has no messages yet)"
    content = (
        f"<transcript>\n{transcript}\n</transcript>\n\n"
        f"<question>\n{question[:MAX_QUESTION_CHARS]}\n</question>"
    )
    try:
        async with config.create_async_anthropic_client(
            timeout=TIMEOUT, max_retries=1,
        ) as client:
            response = await asyncio.wait_for(
                client.messages.create(
                    model=model,
                    max_tokens=MAX_TOKENS,
                    system=_INSTRUCTIONS,
                    messages=[{"role": "user", "content": content}],
                ),
                TIMEOUT,
            )
    except TimeoutError as e:
        raise SideQuestionError(f"No answer within {TIMEOUT:g} s.") from e
    except Exception as e:  # noqa: BLE001 — the person gets the kind of failure
        logger.info("Side question on %s failed: %s", session.get("id"), _describe(e))
        raise SideQuestionError(f"The model call failed: {_describe(e)}.") from e
    answer = response_text(response).strip()
    if not answer:
        raise SideQuestionError("The model gave no answer.")
    usage = getattr(response, "usage", None)
    tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
    tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
    cost = estimate_cost_from_totals(
        {"total_input": tokens_in, "total_output": tokens_out}, model,
    )
    return {
        "answer": answer,
        "model": model,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "cost_usd": cost,
    }
