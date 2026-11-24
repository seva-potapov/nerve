"""``/btw``: a side question about a session, answered now and outside it.

A message sent to a session waits for its running turn, and its answer
becomes part of the conversation. A side question does neither: it asks the
model directly, with the session's recent transcript as context, and the
answer goes back to the person without being added to the session.

It goes through Nerve's own Messages API client (the one session titles
use) when that has a credential and the API takes the call. Otherwise it
asks through a one-shot Claude CLI run with no tools, no settings files and
no MCP servers: the CLI runs with the credential the agent itself uses, so a
key that refuses plain API calls, or a subscription token, still answers.
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


# The one-shot CLI run's reasoning effort: a side answer should come quickly.
CLI_EFFORT = "medium"


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


async def _ask_direct(config: NerveConfig, model: str, content: str) -> dict[str, Any]:
    """Ask through Nerve's own Messages API client. Raises what the client raises."""
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
    usage = getattr(response, "usage", None)
    tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
    tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
    return {
        "answer": response_text(response).strip(),
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "cost_usd": estimate_cost_from_totals(
            {"total_input": tokens_in, "total_output": tokens_out}, model,
        ),
    }


async def _ask_cli(model: str, content: str) -> dict[str, Any]:
    """Ask through a one-shot Claude CLI run: no tools, no settings files, no
    MCP servers, in an empty directory. Raises :class:`SideQuestionError`."""
    import shutil
    import tempfile

    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    cwd = tempfile.mkdtemp(prefix="nerve-btw-")
    options = ClaudeAgentOptions(
        model=model, system_prompt=_INSTRUCTIONS, max_turns=1, tools=[],
        mcp_servers={}, strict_mcp_config=True, setting_sources=[], cwd=cwd,
        effort=CLI_EFFORT,
    )
    texts: list[str] = []
    result: Any = None

    async def _run() -> None:
        nonlocal result
        async for message in query(prompt=content, options=options):
            if isinstance(message, AssistantMessage):
                texts.extend(b.text for b in message.content if isinstance(b, TextBlock))
            elif isinstance(message, ResultMessage):
                result = message

    try:
        await asyncio.wait_for(_run(), TIMEOUT)
    except TimeoutError as e:
        raise SideQuestionError(f"No answer within {TIMEOUT:g} s.") from e
    except SideQuestionError:
        raise
    except Exception as e:  # noqa: BLE001 — the person gets the kind of failure
        raise SideQuestionError(f"The model call failed: {_describe(e)}.") from e
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    if result is not None and getattr(result, "is_error", False):
        raise SideQuestionError(
            f"The model call failed: {getattr(result, 'subtype', None) or 'error'}.",
        )
    usage = getattr(result, "usage", None) or {}
    return {
        "answer": "".join(texts).strip(),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cost_usd": float(getattr(result, "total_cost_usd", None) or 0.0),
    }


async def ask_side_question(
    config: NerveConfig,
    session: dict[str, Any],
    messages: list[dict[str, Any]],
    question: str,
) -> dict[str, Any]:
    """Answer *question* about *session* from its *messages* (oldest first).

    Returns ``{"answer", "model", "input_tokens", "output_tokens",
    "cost_usd", "via"}``; ``via`` is ``"api"`` or ``"cli"``, and ``cost_usd``
    is the CLI's figure, or the list-price estimate of the API call.
    Raises :class:`SideQuestionError` with a message for the person.
    """
    question = (question or "").strip()
    if not question:
        raise SideQuestionError("Ask a question: /btw <question>")
    model = side_model(config, session)
    transcript = _transcript(messages) or "(the session has no messages yet)"
    content = (
        f"<transcript>\n{transcript}\n</transcript>\n\n"
        f"<question>\n{question[:MAX_QUESTION_CHARS]}\n</question>"
    )
    result: dict[str, Any] | None = None
    via = "api"
    if _api_client_usable(config):
        try:
            result = await _ask_direct(config, model, content)
        except TimeoutError as e:
            raise SideQuestionError(f"No answer within {TIMEOUT:g} s.") from e
        except Exception as e:  # noqa: BLE001 — refused (429, 401, …): try the CLI
            logger.info(
                "Side question on %s: the API client failed (%s); asking through the CLI",
                session.get("id"), _describe(e),
            )
    if result is None:
        via = "cli"
        result = await _ask_cli(model, content)
    if not result.get("answer"):
        raise SideQuestionError("The model gave no answer.")
    return {**result, "model": model, "via": via}
