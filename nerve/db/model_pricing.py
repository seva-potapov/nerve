"""Per-model usage of a turn and the effective price of each model.

The Claude CLI reports, on every ``result`` message, a ``modelUsage`` map:
tokens and ``costUSD`` for each model it called since the CLI process
started. Like ``total_cost_usd`` it is cumulative per process and starts
from zero in a new process. It is the only complete source: foreground
sub-agents and the CLI's own side calls (on the small model) never reach
the message stream, and the usage on streamed assistant messages is a
snapshot taken before the response ends (checked against CLI 2.1.310).

This module turns two cumulative snapshots into the rows of one turn
(``session_usage_models``, migration v074) and prices the stored rows for
the diagnostics page: the cost the CLI reported ("billed") next to the
same tokens at Nerve's list prices ("list").

The CLI's billed cost is its own estimate, not the invoice. When it does
not know a model it prices it as the default model (seen with a new model
before its price reached the CLI's cache), so a billed/list gap is a
signal, not noise.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from nerve.db.usage import MODEL_PRICING

logger = logging.getLogger(__name__)

# Token fields of the CLI's ModelUsage (camelCase, as the CLI sends them)
# and the column each one is stored in.
_FIELDS: dict[str, str] = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cacheReadInputTokens": "cache_read_tokens",
    "cacheCreationInputTokens": "cache_creation_tokens",
    "webSearchRequests": "web_search_requests",
}

# Models whose price changes above a prompt size, per request. Usage is
# summed per turn here, so the list cost of these models is the price
# below the threshold: a lower bound. The CLI prices each request, so its
# billed cost can sit above list for them. (CLI 2.1.310 catalog.)
LONG_PROMPT_TIERS: dict[str, dict[str, float]] = {
    "haiku-5-5": {"above_prompt_tokens": 100_000, "multiplier": 5.0},
}

# Billed and list disagree by more than this share: flag the model.
RATIO_TOLERANCE = 0.02


def snapshot(model_usage: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Normalise a raw ``modelUsage`` map into ``{model: {...}}``.

    Keeps the token fields, ``costUSD``, ``canonicalModel`` and
    ``provider``. Entries that are not dicts are dropped.
    """
    out: dict[str, dict[str, Any]] = {}
    for model, raw in (model_usage or {}).items():
        if not isinstance(raw, dict):
            continue
        entry: dict[str, Any] = {
            col: int(raw.get(key) or 0) for key, col in _FIELDS.items()
        }
        entry["cost_usd"] = float(raw.get("costUSD") or 0.0)
        for key, col in (("canonicalModel", "canonical_model"), ("provider", "provider")):
            if raw.get(key):
                entry[col] = str(raw[key])
        out[str(model)] = entry
    return out


def _is_reset(cur: dict[str, dict], prev: dict[str, dict]) -> bool:
    """True when *cur* comes from a new CLI process.

    The counters only grow within one process. A model that disappeared,
    or any counter that went down, means the process changed and *cur*
    started from zero.
    """
    for model, p in prev.items():
        c = cur.get(model)
        if c is None:
            return True
        if c["cost_usd"] + 1e-9 < p["cost_usd"]:
            return True
        for col in _FIELDS.values():
            if c[col] < p.get(col, 0):
                return True
    return False


def turn_rows(
    cur: dict[str, dict], prev: dict[str, dict] | None,
) -> list[dict[str, Any]]:
    """The per-model rows of one turn: *cur* minus *prev*.

    Both are :func:`snapshot` results. After a process reset the whole of
    *cur* belongs to this turn (the same rule as ``compute_turn_cost``).
    Models with no change are left out.
    """
    prev = prev or {}
    if _is_reset(cur, prev):
        prev = {}
    rows: list[dict[str, Any]] = []
    for model, c in cur.items():
        p = prev.get(model) or {}
        row: dict[str, Any] = {"model": model}
        for col in _FIELDS.values():
            row[col] = max(c[col] - p.get(col, 0), 0)
        row["cost_usd"] = round(max(c["cost_usd"] - p.get("cost_usd", 0.0), 0.0), 9)
        row["canonical_model"] = c.get("canonical_model")
        row["provider"] = c.get("provider")
        if row["cost_usd"] > 0 or any(row[col] for col in _FIELDS.values()):
            rows.append(row)
    return rows


def single_model_row(
    model: str | None, usage: dict | None, cost: float | None,
    *, provider: str | None = None,
) -> list[dict[str, Any]]:
    """The one row of a turn whose backend reports no per-model split
    (Codex: one model per turn)."""
    if not model or not usage:
        return []
    server_tool = usage.get("server_tool_use") or {}
    return [{
        "model": model,
        "canonical_model": None,
        "provider": provider,
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "cache_creation_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "web_search_requests": int(server_tool.get("web_search_requests") or 0),
        "cost_usd": float(cost or 0.0),
    }]


# ---------------------------------------------------------------------------
# Price sources
# ---------------------------------------------------------------------------

def _rates_from_tuple(t: tuple) -> dict[str, float]:
    p_in, p_out, p_cr, p_5m, p_1h, p_ws = t
    return {"input": p_in, "output": p_out, "cache_read": p_cr,
            "cache_write_5m": p_5m, "cache_write_1h": p_1h, "web_search": p_ws}


def nerve_rates(model: str) -> tuple[str | None, dict[str, float] | None]:
    """Nerve's list rates for *model*: ``(table key, rates)``.

    Unlike ``_get_pricing`` there is no fallback: a model the table does
    not know returns ``(None, None)`` and diagnostics shows it as
    unpriced instead of pricing it as another model.
    """
    m = model.lower()
    for key, prices in MODEL_PRICING.items():
        if key in m:
            return key, _rates_from_tuple(prices)
    return None, None


def codex_rates(model: str, table: dict | None) -> dict[str, float] | None:
    """Rates from ``codex.pricing`` (input, cached input, output)."""
    if not table:
        return None
    from nerve.agent.backends.codex.pricing import match_pricing

    prices = match_pricing(model, table)
    if not prices:
        return None
    return {"input": float(prices.get("input") or 0.0),
            "output": float(prices.get("output") or 0.0),
            "cache_read": float(prices.get("cached_input") or 0.0),
            "cache_write_5m": float(prices.get("input") or 0.0),
            "cache_write_1h": float(prices.get("input") or 0.0),
            "web_search": 0.0}


def cli_config_path() -> Path:
    """Where the Claude CLI keeps its global config."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    if base:
        return Path(base) / ".claude.json"
    return Path.home() / ".claude.json"


def load_cli_prices(path: Path | None = None) -> dict[str, dict[str, float]]:
    """The CLI's ``additionalModelCostsCache``: prices the CLI fetched
    from the server, keyed by model id. Read-only; ``{}`` when missing."""
    path = path or cli_config_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    cache = data.get("additionalModelCostsCache") if isinstance(data, dict) else None
    if not isinstance(cache, dict):
        return {}
    out: dict[str, dict[str, float]] = {}
    for model, raw in cache.items():
        if not isinstance(raw, dict):
            continue
        try:
            out[str(model)] = {
                "input": float(raw["inputTokens"]),
                "output": float(raw["outputTokens"]),
                "cache_read": float(raw["promptCacheReadTokens"]),
                "cache_write_5m": float(raw["promptCacheWriteTokens"]),
                "cache_write_1h": float(raw.get("promptCacheWrite1hTokens")
                                        or raw["promptCacheWriteTokens"]),
                "web_search": float(raw.get("webSearchRequests") or 0.0),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return out


def match_cli_rates(model: str, cli: dict[str, dict[str, float]]) -> dict[str, float] | None:
    """The CLI cache entry for *model*: the exact id, else the longest key
    that is a prefix of it (a family id for a dated id), else the
    longest key it is a prefix of."""
    if not cli:
        return None
    if model in cli:
        return cli[model]
    m = model.lower()
    best = ""
    for key in cli:
        k = key.lower()
        if (m.startswith(k) or k.startswith(m)) and len(k) > len(best):
            best = key
    return cli.get(best) if best else None


def _same_rates(a: dict[str, float], b: dict[str, float]) -> bool:
    keys = ("input", "output", "cache_read", "cache_write_5m")
    return all(abs(a[k] - b[k]) < 1e-9 for k in keys)


# ---------------------------------------------------------------------------
# Effective pricing
# ---------------------------------------------------------------------------

def _list_cost(row: dict[str, Any], rates: dict[str, float]) -> float:
    # modelUsage has no 5m/1h split: cache writes are priced at the 5m
    # rate, the lower one.
    return (
        row["input_tokens"] * rates["input"]
        + row["output_tokens"] * rates["output"]
        + row["cache_read_tokens"] * rates["cache_read"]
        + row["cache_creation_tokens"] * rates["cache_write_5m"]
    ) / 1_000_000 + row["web_search_requests"] * rates["web_search"]


def effective_pricing(
    model_rows: list[dict[str, Any]],
    *,
    billed_total: float,
    turns_total: int,
    turns_attributed: int,
    cli_prices: dict[str, dict[str, float]] | None = None,
    codex_pricing: dict | None = None,
) -> dict[str, Any]:
    """Build the ``usage.effective_pricing`` block of ``/api/diagnostics``.

    *model_rows* are aggregated ``session_usage_models`` rows (one per
    model, with ``turns``). *billed_total* is the billed cost of every
    turn in the window (``session_usage``), so the part no model row
    covers (older turns, failed attribution) shows as ``unattributed``.
    """
    cli_prices = cli_prices or {}
    models: list[dict[str, Any]] = []
    attributed = 0.0
    for row in model_rows:
        model = row["model"]
        billed = float(row["cost_usd"] or 0.0)
        attributed += billed
        key, rates = nerve_rates(model)
        source = "nerve" if rates else None
        if rates is None and row.get("provider") == "codex":
            rates = codex_rates(model, codex_pricing)
            source = "codex.pricing" if rates else None
        cli = match_cli_rates(model, cli_prices)
        flags: list[str] = []
        if rates is None:
            flags.append("unpriced")
            if cli is not None:
                flags.append("only_cli_has_price")
        elif cli is not None and not _same_rates(rates, cli):
            flags.append("price_sources_disagree")
        list_cost = _list_cost(row, rates) if rates else None
        ratio = (billed / list_cost) if list_cost else None
        if ratio is not None and abs(ratio - 1) > RATIO_TOLERANCE:
            flags.append("billed_differs_from_list")
        tier = LONG_PROMPT_TIERS.get(key or "")
        if tier:
            flags.append("long_prompt_tier")
        tokens = (row["input_tokens"] + row["output_tokens"]
                  + row["cache_read_tokens"] + row["cache_creation_tokens"])
        effective = None
        if rates and ratio is not None:
            # The CLI bills per model, not per token type: scaling every
            # list rate by billed/list is the only split the data allows.
            effective = {k: round(rates[k] * ratio, 6)
                         for k in ("input", "output", "cache_read", "cache_write_5m")}
        models.append({
            "model": model,
            "provider": row.get("provider"),
            "turns": int(row.get("turns") or 0),
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "cache_read_tokens": row["cache_read_tokens"],
            "cache_creation_tokens": row["cache_creation_tokens"],
            "web_search_requests": row["web_search_requests"],
            "billed_usd": round(billed, 6),
            "list_usd": round(list_cost, 6) if list_cost is not None else None,
            "billed_over_list": round(ratio, 4) if ratio is not None else None,
            "blended_usd_per_mtok": round(billed / tokens * 1e6, 4) if tokens else None,
            "price_key": key,
            "price_source": source,
            "rates": rates,
            "cli_rates": cli,
            "effective_rates": effective,
            "long_prompt_tier": tier,
            "flags": flags,
        })
    models.sort(key=lambda m: -m["billed_usd"])
    unattributed = max(billed_total - attributed, 0.0)
    return {
        "models": models,
        "billed_total_usd": round(billed_total, 6),
        "attributed_usd": round(attributed, 6),
        "unattributed_usd": round(unattributed, 6),
        "coverage": round(attributed / billed_total, 4) if billed_total else None,
        "turns": turns_total,
        "turns_attributed": turns_attributed,
        "ratio_tolerance": RATIO_TOLERANCE,
        "cli_price_cache": bool(cli_prices),
    }
