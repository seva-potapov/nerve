"""V74: a turn's usage split by model (``session_usage_models``).

``session_usage`` has one row per turn, labelled with the main loop's
model, while its ``cost_usd`` covers every model the turn used: the
sub-agents and the CLI's own side calls are billed to the same turn. This
table keeps the split. One row is one model of one turn:

- ``usage_id``: the turn (``session_usage.id``). NULL is allowed so a
  failed turn-row write doesn't lose the split.
- ``session_id``: the chat; ``delete_session`` removes the rows with it.
- ``model``: the id the CLI keyed its ``modelUsage`` entry by, and
  ``canonical_model``, the id it priced it as (may differ for aliases and
  provider ids). ``provider``: the CLI's ``provider`` (``firstParty``,
  ``bedrock``, ...) or ``codex``.
- token columns and ``web_search_requests``: this turn's share, i.e. the
  difference between two of the CLI's cumulative snapshots.
- ``cost_usd``: the CLI's ``costUSD`` for the model in this turn.
- ``created_at``: when the turn ended (UTC, SQLite format, like
  ``session_usage``); the retention sweep prunes by it.

``CREATE ... IF NOT EXISTS`` makes a second run do nothing.
"""

from __future__ import annotations

import logging

import aiosqlite

logger = logging.getLogger(__name__)

SQL = """
CREATE TABLE IF NOT EXISTS session_usage_models (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    usage_id              INTEGER REFERENCES session_usage(id) ON DELETE CASCADE,
    session_id            TEXT NOT NULL,
    model                 TEXT NOT NULL,
    canonical_model       TEXT,
    provider              TEXT,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    web_search_requests   INTEGER NOT NULL DEFAULT 0,
    cost_usd              REAL NOT NULL DEFAULT 0,
    created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_session_usage_models_created
    ON session_usage_models(created_at, model);
CREATE INDEX IF NOT EXISTS idx_session_usage_models_usage
    ON session_usage_models(usage_id);
CREATE INDEX IF NOT EXISTS idx_session_usage_models_session
    ON session_usage_models(session_id);
"""


async def up(db: aiosqlite.Connection) -> None:
    await db.executescript(SQL)
    logger.info("v074: session_usage_models ready")
