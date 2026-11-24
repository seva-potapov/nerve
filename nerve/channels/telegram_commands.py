"""Telegram's handlers for the chat commands added with ``/status``:
``/status``, ``/cost``, ``/diag``, ``/btw``, ``/rename``, ``/fork``,
``/retry``, ``/model``, ``/effort``, ``/ps``, ``/files``, ``/file`` and
``/transcript`` (see :mod:`nerve.commands`).

A mixin of :class:`~nerve.channels.telegram.TelegramChannel`: the handlers
use its addressing (a topic is a session of its own), its delivery path and
its topics. Their buttons send ``cmd…:`` callback data, which
:meth:`_handle_command_button` answers.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import logging
import os
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from nerve.agent.effort import EFFORT_LEVELS, normalize_effort
from nerve.agent.side_question import SideQuestionError
from nerve.channels.command_data import PERIODS, CommandRefused
from nerve.channels.command_text import (
    cost_text,
    diag_text,
    files_text,
    format_bytes,
    format_usd,
    period_cost_text,
    ps_text,
    status_text,
)

logger = logging.getLogger(__name__)

# Telegram bots may send files up to 50 MB.
MAX_SEND_BYTES = 50 * 1024 * 1024
# /file lists at most this many entries of a folder.
MAX_LISTED = 40
# Buttons per /files answer.
MAX_FILE_BUTTONS = 20
# File tokens kept for buttons (callback data holds 64 bytes, a path more).
MAX_FILE_TOKENS = 500
# Telegram's message limit, with room for a footer.
MAX_TEXT = 4000


def _short_model(model_id: str) -> str:
    return model_id[len("claude-"):] if model_id.startswith("claude-") else model_id


def _normalize(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def match_model(query: str, models: list[str]) -> tuple[str | None, list[str]]:
    """The model ``/model <query>`` names, as the web composer matches it: an
    id (any case), the id without ``claude-``, or the unique start of either.
    Returns ``(match, candidates)``; *candidates* lists the models a query
    that names more than one could mean."""
    q = query.strip().lower()
    if not q:
        return None, []
    for m in models:
        if m.lower() == q:
            return m, []
    n = _normalize(q)
    names = {m: (_normalize(m), _normalize(_short_model(m))) for m in models}
    exact = [m for m, ns in names.items() if n in ns]
    if len(exact) == 1:
        return exact[0], []
    if exact:
        return None, exact
    prefix = [m for m, ns in names.items() if any(x.startswith(n) for x in ns)]
    if len(prefix) == 1:
        return prefix[0], []
    return None, prefix


def _command_text(message: Any) -> str:
    """The text after the command, newlines kept (``context.args`` splits on
    every whitespace)."""
    text = getattr(message, "text", None) or ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _is_excluded(rel_parts: tuple[str, ...], is_dir: bool, patterns: list[str]) -> bool:
    """The Files page's matching, for a Nerve without it: a pattern without
    ``/`` matches a name at any depth, one with ``/`` the path from the
    workspace root, a trailing ``/`` folders only; every folder above the
    path is tested too."""
    from fnmatch import fnmatchcase

    for i, name in enumerate(rel_parts):
        sub = "/".join(rel_parts[: i + 1])
        component_is_dir = is_dir if i == len(rel_parts) - 1 else True
        for raw in patterns:
            pat = raw.strip()
            dir_only = pat.endswith("/")
            pat = pat.strip("/")
            if not pat or (dir_only and not component_is_dir):
                continue
            if fnmatchcase(sub if "/" in pat else name, pat):
                return True
    return False


def _token(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8", "surrogateescape")).hexdigest()[:12]


class TelegramCommandsMixin:
    """The handlers; ``self`` is the :class:`TelegramChannel`."""

    # ------------------------------------------------------------------ #
    #  Helpers                                                            #
    # ------------------------------------------------------------------ #

    async def _command_session(self, update: Any) -> str | None:
        """The session a command in this chat or topic is about; answers
        "No active session." when there is none."""
        session_id = await self.router.get_last_session(self._address(update).channel_key)
        if not session_id:
            await update.message.reply_text("No active session.")
        return session_id

    async def _reply_long(self, update: Any, text: str, **kwargs: Any) -> None:
        """Reply with *text*, cut to Telegram's limit."""
        if len(text) > MAX_TEXT:
            text = text[: MAX_TEXT - 1] + "…"
        await update.message.reply_text(text, **kwargs)

    def _file_tokens(self) -> collections.OrderedDict[str, str]:
        tokens = self.__dict__.get("_cmd_file_tokens")
        if tokens is None:
            tokens = self.__dict__["_cmd_file_tokens"] = collections.OrderedDict()
        return tokens

    def _remember_path(self, path: str) -> str:
        """A short token for *path* (or a model id, an agent id), for a
        button's callback data: it holds 64 bytes."""
        tokens = self._file_tokens()
        token = _token(path)
        tokens[token] = path
        tokens.move_to_end(token)
        while len(tokens) > MAX_FILE_TOKENS:
            tokens.popitem(last=False)
        return token

    def _workspace(self) -> Path:
        return Path(self.config.workspace).resolve()

    def _exclude_patterns(self) -> list[str]:
        """The Files page's exclude patterns (``files.patterns``: its
        defaults and ``files.exclude``), when this Nerve has them."""
        files = getattr(self.config, "files", None)
        if files is None:
            return []
        patterns = getattr(files, "patterns", None)
        if patterns is None:
            patterns = getattr(files, "exclude", None) or []
        return [str(p) for p in patterns]

    def _excluded(self, rel: str, is_dir: bool = False) -> bool:
        """Whether a workspace path, or a folder above it, is hidden by the
        Files page's patterns."""
        patterns = self._exclude_patterns()
        if not patterns:
            return False
        try:
            from nerve.gateway.routes.workspace import is_excluded
        except ImportError:
            is_excluded = _is_excluded
        return is_excluded(Path(rel).parts, is_dir, patterns)

    def _workspace_path(self, raw: str) -> Path | None:
        """*raw* (workspace-relative or absolute) inside the workspace, or
        None when it points outside it or is excluded."""
        ws = self._workspace()
        candidate = Path(raw).expanduser()
        target = (candidate if candidate.is_absolute() else ws / candidate).resolve()
        if target != ws and ws not in target.parents:
            return None
        rel = "." if target == ws else str(target.relative_to(ws))
        if rel != "." and self._excluded(rel, target.is_dir()):
            return None
        return target

    async def _send_path(self, chat_id: int, thread_id: int | None, path: Path) -> str | None:
        """Send one file as a document; returns why not, or None when sent."""
        from nerve.channels.telegram import _in_thread

        try:
            size = path.stat().st_size
        except OSError:
            return "That file no longer exists."
        if size > MAX_SEND_BYTES:
            return f"{path.name} is {format_bytes(size)}: Telegram takes files up to 50 MB."
        try:
            with open(path, "rb") as f:
                await self._app.bot.send_document(
                    chat_id=chat_id, document=f, filename=path.name, **_in_thread(thread_id),
                )
        except Exception as exc:  # noqa: BLE001 — the person gets the reason
            logger.warning("Could not send %s: %s", path, exc)
            return f"Could not send {path.name}: {type(exc).__name__}."
        return None

    def _folder_view(self, folder: Path) -> tuple[str, InlineKeyboardMarkup]:
        """A folder's entries as text and buttons: folders first, then files."""
        ws = self._workspace()
        rel = "." if folder == ws else str(folder.relative_to(ws))
        entries = []
        try:
            for entry in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                entry_rel = str(entry.relative_to(ws))
                if self._excluded(entry_rel, entry.is_dir()):
                    continue
                entries.append(entry)
        except OSError as exc:
            return f"Can't read {rel}: {type(exc).__name__}.", InlineKeyboardMarkup([])
        rows = []
        if folder != ws:
            rows.append([InlineKeyboardButton("⬆ ..", callback_data=f"cmdfile:{self._remember_path(str(folder.parent))}")])
        for entry in entries[:MAX_LISTED]:
            if entry.is_dir():
                label = f"📁 {entry.name}"
            else:
                try:
                    label = f"📄 {entry.name} ({format_bytes(entry.stat().st_size)})"
                except OSError:
                    label = f"📄 {entry.name}"
            rows.append([InlineKeyboardButton(label[:60], callback_data=f"cmdfile:{self._remember_path(str(entry))}")])
        more = len(entries) - MAX_LISTED
        text = f"{rel}/ ({len(entries)} entries)" + (f", first {MAX_LISTED} shown" if more > 0 else "")
        if not entries:
            text = f"{rel}/ is empty."
        return text, InlineKeyboardMarkup(rows)

    # ------------------------------------------------------------------ #
    #  /status /cost /diag                                                #
    # ------------------------------------------------------------------ #

    async def _handle_status(self, update: Any, context: Any) -> None:
        """Handle /status — the session's model, effort, work, context and cost."""
        self._touch()
        if not self._is_authorized(update.effective_user.id):
            return
        session_id = await self._command_session(update)
        if not session_id:
            return
        info = await self.router.status_info(session_id)
        if info is None:
            await update.message.reply_text("No active session.")
            return
        await self._reply_long(update, status_text(info, self.config.timezone))

    async def _handle_cost(self, update: Any, context: Any) -> None:
        """Handle /cost [today|week|month] — the session's or every session's spend."""
        self._touch()
        if not self._is_authorized(update.effective_user.id):
            return
        period = (context.args[0].lower() if context.args else "")
        if period:
            if period not in PERIODS:
                await update.message.reply_text(
                    f"Unknown period “{period}”. Use /cost, /cost today, /cost week or /cost month.",
                )
                return
            info = await self.router.period_cost_info(period, self.config.timezone)
            await self._reply_long(update, period_cost_text(info))
            return
        session_id = await self._command_session(update)
        if not session_id:
            return
        info = await self.router.cost_info(session_id)
        if info is None:
            await update.message.reply_text("No active session.")
            return
        await self._reply_long(update, cost_text(info))

    async def _handle_diag(self, update: Any, context: Any) -> None:
        """Handle /diag — the running Nerve's live state (/doctor checks the config)."""
        self._touch()
        if not self._is_authorized(update.effective_user.id):
            return
        info = await self.router.diag_info(self.config.timezone)
        pending = None
        try:
            webhook = await self._app.bot.get_webhook_info()
            pending = getattr(webhook, "pending_update_count", None)
        except Exception as exc:  # noqa: BLE001 — the rest of /diag still helps
            logger.debug("get_webhook_info failed: %s", exc)
        info["telegram"] = {"live": True, "pending": pending}
        await self._reply_long(update, diag_text(info, self.config.timezone))

    # ------------------------------------------------------------------ #
    #  /btw                                                               #
    # ------------------------------------------------------------------ #

    async def _handle_btw(self, update: Any, context: Any) -> None:
        """Handle /btw <question> — answered now from the transcript, not added to the session."""
        self._touch()
        if not self._is_authorized(update.effective_user.id):
            return
        question = _command_text(update.message)
        if not question:
            await update.message.reply_text("Usage: /btw <question>")
            return
        session_id = await self._command_session(update)
        if not session_id:
            return
        address = self._address(update)
        typing = asyncio.create_task(self._keep_typing(address.target))
        try:
            result = await self.router.side_question(session_id, question)
        except SideQuestionError as e:
            await update.message.reply_text(str(e))
            return
        finally:
            typing.cancel()
        footer = (
            f"\n\n(btw · {_short_model(result['model'])} · {format_usd(result['cost_usd'])}"
            " · not added to the session)"
        )
        from nerve.channels.base import OutboundMessage

        await self.send(OutboundMessage(
            target=address.target, text=self.format_response(result["answer"] + footer),
        ))

    # ------------------------------------------------------------------ #
    #  /rename /fork /retry /transcript                                   #
    # ------------------------------------------------------------------ #

    async def _rename_topics(self, session_id: str, title: str) -> None:
        """Rename the session's topics after *title*."""
        from nerve.channels.telegram import _address_of_key, _topic_name

        if self._app is None:
            return
        for key in await self.router.channel_keys_for_session(session_id):
            topic = _address_of_key(key)
            if topic is None or topic.thread_id is None:
                continue
            try:
                await self._app.bot.edit_forum_topic(
                    chat_id=topic.chat_id, message_thread_id=topic.thread_id,
                    name=_topic_name(title),
                )
            except Exception as exc:  # noqa: BLE001 — the session keeps its new title
                logger.info("Could not rename topic %s: %s", key, exc)

    async def _handle_rename(self, update: Any, context: Any) -> None:
        """Handle /rename <title> — the session's title, and its topics."""
        self._touch()
        if not self._is_authorized(update.effective_user.id):
            return
        title = " ".join(_command_text(update.message).split())
        if not title:
            await update.message.reply_text("Usage: /rename <title>")
            return
        session_id = await self._command_session(update)
        if not session_id:
            return
        try:
            await self.router.update_session(session_id, {"title": title})
        except CommandRefused as e:
            await update.message.reply_text(str(e))
            return
        await self._rename_topics(session_id, title)
        await update.message.reply_text(f"Renamed to “{title}”.")

    async def _handle_fork(self, update: Any, context: Any) -> None:
        """Handle /fork [title] — the session forked into a new one: in a new
        topic when the chat has topics, else this chat switches to it. Sent as
        a reply, the fork starts at the replied-to message."""
        from nerve.channels.telegram import _reply_of

        self._touch()
        if not self._is_authorized(update.effective_user.id):
            return
        address = self._address(update)
        session_id = await self._command_session(update)
        if not session_id:
            return
        source = session_id
        at: int | None = None
        replied = _reply_of(update.message)
        if replied is not None:
            route = await self.router.reply_route_entry("telegram", address.chat_id, replied.message_id)
            if route is None:
                await update.message.reply_text(
                    "Nerve has no record of that message (it's older than the record, "
                    "or not from a session), so it can't fork there. /fork alone forks the whole chat.",
                )
                return
            source = route["session_id"]
            at = await self.router.latest_answer_at(source, route["created_at"])
            if at is None:
                await update.message.reply_text("Nothing to fork there: the session had no answer yet.")
                return
        elif self.router.is_running(session_id):
            # Fork from before the running turn, not from half of it.
            at = await self.router.latest_answer_at(session_id)
        title = " ".join(_command_text(update.message).split()) or None
        try:
            fork = await self.router.fork_session(source, at_message_id=at, title=title, source="telegram")
        except ValueError as e:
            await update.message.reply_text(str(e))
            return
        fork_id = fork["id"]
        name = fork.get("title") or fork_id
        where = " from that message" if replied is not None else (
            " from before the running turn" if at is not None else ""
        )
        if self._chat_may_have_topics(update.effective_chat):
            opened = await self.open_session_topic(fork_id, chat_id=address.chat_id)
            if not opened.error:
                await update.message.reply_text(f"Forked{where} into a new topic: {name}")
                return
            logger.info("No topic for fork %s: %s", fork_id, opened.error)
        await self.router.switch_session(address.channel_key, fork_id)
        await update.message.reply_text(
            f"Forked{where}: this chat is now on {fork_id} ({name}). /session {source} goes back.",
        )

    async def _handle_retry(self, update: Any, context: Any) -> None:
        """Handle /retry — the session's last message from you, sent again."""
        self._touch()
        if not self._is_authorized(update.effective_user.id):
            return
        session_id = await self._command_session(update)
        if not session_id:
            return
        if self.router.is_running(session_id):
            await update.message.reply_text("A turn is running: /stop it first, or wait for it.")
            return
        last = await self.router.last_user_message(session_id)
        text = (last or {}).get("content", "").strip()
        if not text:
            await update.message.reply_text("Nothing to retry: this session has no message from you.")
            return
        preview = text if len(text) <= 80 else text[:79] + "…"
        note = " Attachments aren't sent again." if last.get("has_attachments") else ""
        await update.message.reply_text(f"Sending again: “{preview}”.{note}")
        await self._deliver_message(update, text, session_id=session_id)

    async def _handle_transcript(self, update: Any, context: Any) -> None:
        """Handle /transcript — the session's whole conversation as a file."""
        from nerve.channels.telegram import (
            _TRANSCRIPT_MAX_MESSAGES,
            _in_thread,
            _transcript_filename,
            build_session_transcript,
        )

        self._touch()
        if not self._is_authorized(update.effective_user.id):
            return
        session_id = await self._command_session(update)
        if not session_id:
            return
        session = await self.router.get_session(session_id)
        if not session:
            await update.message.reply_text("That session is no longer available.")
            return
        messages = await self.router.get_session_messages(session_id, limit=_TRANSCRIPT_MAX_MESSAGES)
        document = build_session_transcript(session, messages, self.config.timezone)
        address = self._address(update)
        await self._app.bot.send_document(
            chat_id=address.chat_id, document=document.encode("utf-8"),
            filename=_transcript_filename(session), **_in_thread(address.thread_id),
        )

    # ------------------------------------------------------------------ #
    #  /model /effort                                                     #
    # ------------------------------------------------------------------ #

    async def _session_models(self, session: dict) -> list[str]:
        """The models a session can switch to: its backend's."""
        listing = await self.router.list_models()
        backend = session.get("backend") or "claude"
        for option in listing.get("options") or []:
            if option.get("id") == backend:
                return [m for m in option.get("models") or [] if isinstance(m, str)]
        providers = ("openai",) if backend == "codex" else ("anthropic", "ollama")
        return [m["id"] for m in listing.get("models") or [] if m.get("provider") in providers]

    async def _set_model(self, session_id: str, model: str) -> str:
        try:
            await self.router.update_session(session_id, {"model": model})
        except CommandRefused as e:
            return str(e)
        return f"Model set to {model} for this session, from the next turn."

    async def _handle_model(self, update: Any, context: Any) -> None:
        """Handle /model [name] — show the models as buttons, or switch to one."""
        self._touch()
        if not self._is_authorized(update.effective_user.id):
            return
        session_id = await self._command_session(update)
        if not session_id:
            return
        session = await self.router.get_session(session_id) or {}
        models = await self._session_models(session)
        current = session.get("model") or self.config.agent.model
        query = _command_text(update.message)
        if not query:
            rows = [
                [InlineKeyboardButton(
                    ("✓ " if m == current else "") + _short_model(m),
                    callback_data=f"cmdmodel:{session_id}:{self._remember_path(m)}",
                )]
                for m in models
            ]
            await update.message.reply_text(
                f"Model: {current}. Pick another:", reply_markup=InlineKeyboardMarkup(rows),
            )
            return
        match, candidates = match_model(query, models)
        if match is None:
            offered = candidates or models
            what = "matches more than one model" if candidates else "is not a model here"
            await update.message.reply_text(f"“{query}” {what}: {', '.join(offered)}")
            return
        if match == current:
            await update.message.reply_text(f"Already on {match}.")
            return
        await update.message.reply_text(await self._set_model(session_id, match))

    async def _effort_levels(self, model: str) -> list[str]:
        listing = await self.router.list_models()
        levels = (listing.get("effort_levels") or {}).get(model)
        return list(levels) if levels is not None else list(EFFORT_LEVELS)

    async def _set_effort(self, session_id: str, level: str | None) -> str:
        try:
            await self.router.update_session(session_id, {"effort": level})
        except CommandRefused as e:
            return str(e)
        if level is None:
            return f"Effort back to the default ({self.config.agent.effort}) for this session."
        return f"Effort set to {level} for this session, from the next turn."

    async def _handle_effort(self, update: Any, context: Any) -> None:
        """Handle /effort [level] — show the levels as buttons, or set one."""
        self._touch()
        if not self._is_authorized(update.effective_user.id):
            return
        session_id = await self._command_session(update)
        if not session_id:
            return
        session = await self.router.get_session(session_id) or {}
        model = session.get("model") or self.config.agent.model
        levels = await self._effort_levels(model)
        current = session.get("effort")
        query = _command_text(update.message).lower()
        if not query:
            if not levels:
                await update.message.reply_text(f"{model} takes no effort setting.")
                return
            rows = [[
                InlineKeyboardButton(("✓ " if level == current else "") + level,
                                     callback_data=f"cmdeffort:{session_id}:{level}")
                for level in levels
            ], [InlineKeyboardButton(
                ("✓ " if current is None else "") + f"default ({self.config.agent.effort})",
                callback_data=f"cmdeffort:{session_id}:default",
            )]]
            shown = current or f"default ({self.config.agent.effort})"
            await update.message.reply_text(f"Effort: {shown}. Pick another:", reply_markup=InlineKeyboardMarkup(rows))
            return
        if query == "default":
            level = None
        else:
            try:
                level = normalize_effort(query)
            except ValueError:
                level = "?"
            if level not in levels:
                await update.message.reply_text(
                    f"Unknown effort “{query}”. Use one of: {', '.join(levels)}, default.",
                )
                return
        if level == current:
            await update.message.reply_text("Already at that effort.")
            return
        await update.message.reply_text(await self._set_effort(session_id, level))

    # ------------------------------------------------------------------ #
    #  /ps                                                                #
    # ------------------------------------------------------------------ #

    async def _ps_view(self, session_id: str) -> tuple[str, InlineKeyboardMarkup]:
        info = await self.router.ps_info(session_id)
        rows = []
        n = 0
        for agent in info.get("agents") or []:
            n += 1
            rows.append([InlineKeyboardButton(
                f"⏹ {n}. {(agent.get('label') or 'agent')[:40]}",
                callback_data=f"cmdps:a:{session_id}:{self._remember_path(str(agent['id']))}",
            )])
        for proc in info.get("processes") or []:
            n += 1
            rows.append([InlineKeyboardButton(
                f"⏹ {n}. pid {proc['pid']}",
                callback_data=f"cmdps:p:{session_id}:{proc['pid']}:{proc.get('start_ticks') or 0}",
            )])
        rows.append([InlineKeyboardButton("↻ Refresh", callback_data=f"cmdps:r:{session_id}")])
        return ps_text(info), InlineKeyboardMarkup(rows)

    async def _handle_ps(self, update: Any, context: Any) -> None:
        """Handle /ps — the session's agents and processes, each with a stop button."""
        self._touch()
        if not self._is_authorized(update.effective_user.id):
            return
        session_id = await self._command_session(update)
        if not session_id:
            return
        text, markup = await self._ps_view(session_id)
        await update.message.reply_text(text, reply_markup=markup)

    # ------------------------------------------------------------------ #
    #  /files /file                                                       #
    # ------------------------------------------------------------------ #

    async def _handle_files(self, update: Any, context: Any) -> None:
        """Handle /files — the files the session wrote, each a button that sends it."""
        self._touch()
        if not self._is_authorized(update.effective_user.id):
            return
        session_id = await self._command_session(update)
        if not session_id:
            return
        files = await self.router.written_files(session_id)
        rows = []
        for i, f in enumerate(files, 1):
            if not f.get("exists") or len(rows) >= MAX_FILE_BUTTONS:
                continue
            rows.append([InlineKeyboardButton(
                f"📄 {i}. {os.path.basename(f['path'])}"[:60],
                callback_data=f"cmdfile:{self._remember_path(f['abs_path'])}",
            )])
        await self._reply_long(
            update, files_text(files, self.config.timezone),
            reply_markup=InlineKeyboardMarkup(rows) if rows else None,
        )

    async def _handle_file(self, update: Any, context: Any) -> None:
        """Handle /file [path] — send a workspace file, or list a folder (the
        workspace without a path) with a button per entry."""
        self._touch()
        if not self._is_authorized(update.effective_user.id):
            return
        raw = _command_text(update.message) or "."
        target = self._workspace_path(raw)
        if target is None:
            await update.message.reply_text("That path is outside the workspace, or hidden.")
            return
        if target.is_dir():
            text, markup = self._folder_view(target)
            await update.message.reply_text(text, reply_markup=markup)
            return
        if not target.exists():
            await update.message.reply_text(f"No such file: {raw}")
            return
        address = self._address(update)
        problem = await self._send_path(address.chat_id, address.thread_id, target)
        if problem:
            await update.message.reply_text(problem)

    # ------------------------------------------------------------------ #
    #  Buttons                                                            #
    # ------------------------------------------------------------------ #

    async def _handle_command_button(self, query: Any) -> None:
        """Answer the buttons of /model, /effort, /ps, /files and /file."""
        data = query.data or ""
        kind, _, rest = data.partition(":")
        message = query.message
        if kind == "cmdfile":
            path = self._file_tokens().get(rest)
            if path is None:
                await query.answer("That button has expired: send the command again.", show_alert=True)
                return
            target = Path(path)
            if target.is_dir():
                checked = self._workspace_path(path)
                if checked is None:
                    await query.answer("That folder is outside the workspace.", show_alert=True)
                    return
                text, markup = self._folder_view(checked)
                await query.answer()
                await self._safe_edit(query, text, markup)
                return
            address = self._address_of(message.chat.id, message)
            await query.answer(f"Sending {target.name}")
            problem = await self._send_path(address.chat_id, address.thread_id, target)
            if problem:
                await query.answer(problem, show_alert=True)
            return
        if kind == "cmdmodel":
            session_id, _, token = rest.partition(":")
            model = self._file_tokens().get(token)
            if model is None:
                await query.answer("That button has expired: send /model again.", show_alert=True)
                return
            result = await self._set_model(session_id, model)
            await query.answer(result[:190])
            await self._safe_edit(query, result, None)
            return
        if kind == "cmdeffort":
            session_id, _, level = rest.partition(":")
            result = await self._set_effort(session_id, None if level == "default" else level)
            await query.answer(result[:190])
            await self._safe_edit(query, result, None)
            return
        if kind == "cmdps":
            what, _, rest2 = rest.partition(":")
            if what == "r":
                text, markup = await self._ps_view(rest2)
                await query.answer()
                await self._safe_edit(query, text, markup)
                return
            if what == "a":
                session_id, _, token = rest2.partition(":")
                agent_id = self._file_tokens().get(token)
                if agent_id is None:
                    await query.answer("That button has expired: send /ps again.", show_alert=True)
                    return
                try:
                    stopping = await self.router.stop_agent(session_id, agent_id)
                except Exception as exc:  # noqa: BLE001 — the person gets the reason
                    await query.answer(f"Could not stop it: {exc}"[:190], show_alert=True)
                    return
                await query.answer("Stopping" if stopping else "It had already ended")
            elif what == "p":
                session_id, pid, ticks = (rest2.split(":") + ["", "", ""])[:3]
                try:
                    await self.router.signal_process(session_id, int(pid), int(ticks) or None)
                except Exception as exc:  # noqa: BLE001 — not the session's, gone, …
                    await query.answer(f"Could not stop it: {exc}"[:190], show_alert=True)
                    return
                await query.answer("Sent TERM")
            else:
                await query.answer()
                return
            text, markup = await self._ps_view(session_id)
            await self._safe_edit(query, text, markup)
            return
        await query.answer()
