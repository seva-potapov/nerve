"""Per-model usage of a turn and the effective price of each model.

The Claude CLI's ``modelUsage`` (on every ``result`` message) is
cumulative per CLI process, covers sub-agents and the CLI's own side
calls, and restarts from zero in a new process. The engine diffs it into
``session_usage_models`` rows; diagnostics prices them against Nerve's
table and the CLI's own price cache.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from nerve.agent.backends.claude import translate_message
from nerve.agent.backends.events import TurnCompleted
from nerve.agent.engine import AgentEngine
from nerve.db import Database
from nerve.db import model_pricing as mp

from tests.test_engine import _engine_with_scripted_clients, _result_msg


def _mu(**models) -> dict:
    """A raw CLI ``modelUsage`` map: model=(in, out, cache_read, cache_write, cost)."""
    out = {}
    for model, (i, o, cr, cw, cost) in models.items():
        out[model.replace("_", "-")] = {
            "inputTokens": i, "outputTokens": o, "cacheReadInputTokens": cr,
            "cacheCreationInputTokens": cw, "webSearchRequests": 0,
            "costUSD": cost, "contextWindow": 200000, "maxOutputTokens": 32000,
        }
    return out


# ---------------------------------------------------------------------------
# Snapshots and per-turn rows
# ---------------------------------------------------------------------------


class TestTurnRows:
    def test_first_turn_is_whole_snapshot(self):
        cur = mp.snapshot(_mu(claude_haiku_5_5=(2, 3, 0, 15998, 0.002)))
        rows = mp.turn_rows(cur, None)
        assert len(rows) == 1
        assert rows[0]["model"] == "claude-haiku-5-5"
        assert rows[0]["cache_creation_tokens"] == 15998
        assert rows[0]["cost_usd"] == pytest.approx(0.002)

    def test_delta_and_new_model(self):
        # Turn 2 of a real CLI 2.1.310 run: a sub-agent on Sonnet appears.
        prev = mp.snapshot(_mu(claude_haiku_5_5=(2, 3, 0, 15998, 0.0020)))
        cur = mp.snapshot(_mu(
            claude_haiku_5_5=(1180, 196, 29045, 19571, 0.0030),
            claude_sonnet_5_5=(2, 3, 0, 10835, 0.0271),
        ))
        rows = {r["model"]: r for r in mp.turn_rows(cur, prev)}
        assert rows["claude-haiku-5-5"]["input_tokens"] == 1178
        assert rows["claude-haiku-5-5"]["cost_usd"] == pytest.approx(0.0010)
        assert rows["claude-sonnet-5-5"]["cache_creation_tokens"] == 10835
        assert rows["claude-sonnet-5-5"]["cost_usd"] == pytest.approx(0.0271)

    def test_unchanged_model_is_left_out(self):
        snap = mp.snapshot(_mu(
            claude_haiku_5_5=(10, 10, 10, 10, 0.01),
            claude_sonnet_5_5=(5, 5, 5, 5, 0.02),
        ))
        cur = mp.snapshot(_mu(
            claude_haiku_5_5=(20, 20, 20, 20, 0.02),
            claude_sonnet_5_5=(5, 5, 5, 5, 0.02),
        ))
        assert [r["model"] for r in mp.turn_rows(cur, snap)] == ["claude-haiku-5-5"]

    def test_reset_when_counter_goes_down(self):
        prev = mp.snapshot(_mu(claude_haiku_5_5=(1196, 1092, 149696, 21699, 0.0049)))
        cur = mp.snapshot(_mu(claude_haiku_5_5=(2, 4, 12664, 3335, 0.0005)))
        rows = mp.turn_rows(cur, prev)
        assert rows[0]["cache_read_tokens"] == 12664
        assert rows[0]["cost_usd"] == pytest.approx(0.0005)

    def test_reset_when_model_disappears(self):
        prev = mp.snapshot(_mu(
            claude_haiku_5_5=(1, 1, 1, 1, 0.001),
            claude_sonnet_5_5=(9, 9, 9, 9, 0.5),
        ))
        cur = mp.snapshot(_mu(claude_haiku_5_5=(5, 5, 5, 5, 0.004)))
        rows = mp.turn_rows(cur, prev)
        assert rows[0]["input_tokens"] == 5  # whole snapshot, not 5 - 1

    def test_snapshot_keeps_canonical_and_drops_junk(self):
        raw = _mu(claude_opus_5_5=(1, 1, 1, 1, 0.1))
        raw["claude-opus-5-5"]["canonicalModel"] = "claude-opus-5-5"
        raw["claude-opus-5-5"]["provider"] = "firstParty"
        raw["junk"] = "not a dict"
        snap = mp.snapshot(raw)
        assert set(snap) == {"claude-opus-5-5"}
        assert snap["claude-opus-5-5"]["canonical_model"] == "claude-opus-5-5"
        assert snap["claude-opus-5-5"]["provider"] == "firstParty"
        assert mp.snapshot(None) == {}

    def test_single_model_row(self):
        rows = mp.single_model_row(
            "gpt-5.6-terra",
            {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3,
             "cache_creation_input_tokens": 0},
            0.25, provider="codex",
        )
        assert rows == [{
            "model": "gpt-5.6-terra", "canonical_model": None, "provider": "codex",
            "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 3,
            "cache_creation_tokens": 0, "web_search_requests": 0, "cost_usd": 0.25,
        }]
        assert mp.single_model_row(None, {"input_tokens": 1}, 1.0) == []


# ---------------------------------------------------------------------------
# Price sources
# ---------------------------------------------------------------------------


class TestPriceSources:
    def test_nerve_rates_no_fallback(self):
        key, rates = mp.nerve_rates("claude-opus-5-5")
        assert key == "opus-5-5"
        assert rates["input"] == 4 and rates["cache_read"] == 0.20
        assert mp.nerve_rates("claude-something-new") == (None, None)

    def test_load_cli_prices(self, tmp_path):
        path = tmp_path / ".claude.json"
        path.write_text(json.dumps({"additionalModelCostsCache": {
            "claude-fable-5-2": {
                "inputTokens": 8, "outputTokens": 40, "promptCacheWriteTokens": 10,
                "promptCacheWrite1hTokens": 16, "promptCacheReadTokens": 0.2,
                "webSearchRequests": 0.01,
            },
            "broken": {"inputTokens": 1},
        }}))
        prices = mp.load_cli_prices(path)
        assert set(prices) == {"claude-fable-5-2"}
        assert prices["claude-fable-5-2"]["cache_write_1h"] == 16
        assert mp.load_cli_prices(tmp_path / "missing.json") == {}
        (tmp_path / "bad.json").write_text("{")
        assert mp.load_cli_prices(tmp_path / "bad.json") == {}

    def test_cli_config_dir_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        assert mp.cli_config_path() == tmp_path / ".claude.json"

    def test_match_cli_rates_prefix(self):
        cli = {"claude-opus-4-5-20251101": {"input": 5.0}, "claude-opus-4-6": {"input": 5.0}}
        assert mp.match_cli_rates("claude-opus-4-6", cli) is cli["claude-opus-4-6"]
        assert mp.match_cli_rates("claude-opus-4-5", cli) is cli["claude-opus-4-5-20251101"]
        assert mp.match_cli_rates("claude-haiku-5-5", cli) is None
        assert mp.match_cli_rates("x", {}) is None


# ---------------------------------------------------------------------------
# Effective pricing
# ---------------------------------------------------------------------------


def _agg(model, i=0, o=0, cr=0, cw=0, cost=0.0, turns=1, provider=None):
    return {"model": model, "provider": provider, "turns": turns,
            "input_tokens": i, "output_tokens": o, "cache_read_tokens": cr,
            "cache_creation_tokens": cw, "web_search_requests": 0, "cost_usd": cost}


class TestEffectivePricing:
    def test_billed_at_list(self):
        # 1M output on Opus 5.5 = $20 at list.
        out = mp.effective_pricing(
            [_agg("claude-opus-5-5", o=1_000_000, cost=20.0)],
            billed_total=20.0, turns_total=1, turns_attributed=1,
        )
        m = out["models"][0]
        assert m["list_usd"] == pytest.approx(20.0)
        assert m["billed_over_list"] == pytest.approx(1.0)
        assert m["flags"] == []
        assert m["effective_rates"]["output"] == pytest.approx(20.0)
        assert m["blended_usd_per_mtok"] == pytest.approx(20.0)
        assert out["coverage"] == pytest.approx(1.0)
        assert out["unattributed_usd"] == 0

    def test_fallback_priced_model_is_flagged(self):
        # A new model the CLI priced as Opus 5.5: billed is half of list.
        out = mp.effective_pricing(
            [_agg("claude-fable-5-2", o=1_000_000, cost=20.0)],
            billed_total=30.0, turns_total=3, turns_attributed=2,
        )
        m = out["models"][0]
        assert m["billed_over_list"] == pytest.approx(0.5)
        assert "billed_differs_from_list" in m["flags"]
        assert m["effective_rates"]["output"] == pytest.approx(20.0)
        assert out["unattributed_usd"] == pytest.approx(10.0)
        assert out["coverage"] == pytest.approx(20 / 30, abs=1e-4)

    def test_unpriced_and_cli_disagreement(self):
        cli = {
            "claude-new-1": {"input": 1, "output": 2, "cache_read": 0.1,
                             "cache_write_5m": 1.25, "cache_write_1h": 2, "web_search": 0.01},
            "claude-opus-5-5": {"input": 5, "output": 25, "cache_read": 0.5,
                                "cache_write_5m": 6.25, "cache_write_1h": 10, "web_search": 0.01},
        }
        out = mp.effective_pricing(
            [_agg("claude-new-1", o=10, cost=1.0), _agg("claude-opus-5-5", o=10, cost=0.5)],
            billed_total=1.5, turns_total=1, turns_attributed=1, cli_prices=cli,
        )
        by = {m["model"]: m for m in out["models"]}
        assert by["claude-new-1"]["flags"] == ["unpriced", "only_cli_has_price"]
        assert by["claude-new-1"]["list_usd"] is None
        assert "price_sources_disagree" in by["claude-opus-5-5"]["flags"]
        assert out["cli_price_cache"] is True

    def test_long_prompt_tier_flag(self):
        out = mp.effective_pricing(
            [_agg("claude-haiku-5-5", o=1_000_000, cost=0.5)],
            billed_total=0.5, turns_total=1, turns_attributed=1,
        )
        m = out["models"][0]
        assert "long_prompt_tier" in m["flags"]
        assert m["long_prompt_tier"]["above_prompt_tokens"] == 100_000

    def test_codex_model_uses_codex_table(self):
        table = {"gpt-5.6-terra": {"input": 2.5, "cached_input": 0.25, "output": 15.0}}
        out = mp.effective_pricing(
            [_agg("gpt-5.6-terra", i=1_000_000, cost=2.5, provider="codex")],
            billed_total=2.5, turns_total=1, turns_attributed=1, codex_pricing=table,
        )
        m = out["models"][0]
        assert m["price_source"] == "codex.pricing"
        assert m["billed_over_list"] == pytest.approx(1.0)

    def test_empty(self):
        out = mp.effective_pricing([], billed_total=0.0, turns_total=0, turns_attributed=0)
        assert out["models"] == [] and out["coverage"] is None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestStorage:
    async def test_record_and_aggregate(self, db: Database):
        await db.create_session("s1", title="t", source="web", actor=None)
        uid = await db.record_turn_usage("s1", 1, 2, 3, 4, 200_000, cost_usd=0.6,
                                         cost_basis="api_billed")
        assert isinstance(uid, int)
        await db.record_turn_model_usage(uid, "s1", [
            {**_agg("claude-opus-5-5", o=10_000, cost=0.2), "canonical_model": "claude-opus-5-5"},
            {**_agg("claude-sonnet-5-5", o=10_000, cost=0.1), "canonical_model": None},
        ])
        # A turn without a split (older row) counts as unattributed.
        await db.record_turn_usage("s1", 1, 2, 3, 4, 200_000, cost_usd=0.3,
                                   cost_basis="api_billed")
        out = await db.get_effective_pricing(days=7)
        assert {m["model"] for m in out["models"]} == {"claude-opus-5-5", "claude-sonnet-5-5"}
        assert out["turns"] == 2 and out["turns_attributed"] == 1
        assert out["billed_total_usd"] == pytest.approx(0.9)
        assert out["unattributed_usd"] == pytest.approx(0.6)
        opus = next(m for m in out["models"] if m["model"] == "claude-opus-5-5")
        assert opus["list_usd"] == pytest.approx(0.2)

    async def test_delete_session_removes_rows(self, db: Database):
        await db.create_session("s2", title="t", source="web", actor=None)
        uid = await db.record_turn_usage("s2", 1, 1, 1, 1, 200_000, cost_usd=0.1)
        await db.record_turn_model_usage(uid, "s2", [_agg("claude-opus-5-5", o=1, cost=0.1)])
        await db.delete_session("s2")
        async with db.db.execute("SELECT COUNT(*) FROM session_usage_models") as cur:
            assert (await cur.fetchone())[0] == 0


# ---------------------------------------------------------------------------
# Claude translation and the engine
# ---------------------------------------------------------------------------


def test_translate_carries_model_usage():
    raw = _mu(claude_haiku_5_5=(1, 1, 1, 1, 0.001))
    (event,) = [e for e in translate_message(_result_msg("sdk-x", model_usage=raw))
                if isinstance(e, TurnCompleted)]
    assert event.model_usage == raw
    (event,) = [e for e in translate_message(_result_msg("sdk-x"))
                if isinstance(e, TurnCompleted)]
    assert event.model_usage is None


async def _model_rows(db: Database, sid: str) -> list[dict]:
    async with db.db.execute(
        "SELECT usage_id, model, input_tokens, cost_usd FROM session_usage_models "
        "WHERE session_id = ? ORDER BY id", (sid,),
    ) as cur:
        return [dict(r) async for r in cur]


async def _per_turn_engine(db, tmp_path, sid, turns):
    """An engine whose scripted clients play ``turns[n]`` on the n-th turn,
    whether the engine keeps its client between turns or not."""
    engine, _ = await _engine_with_scripted_clients(db, tmp_path, sid, [turns[0]])
    original = engine._get_or_create_client
    count = {"n": 0}

    async def _get(*a, **kw):
        client = await original(*a, **kw)
        if not getattr(client, "_per_turn", False):
            client._per_turn = True
            play = client.receive_turn

            async def receive_turn():
                client._script = turns[min(count["n"], len(turns) - 1)]
                count["n"] += 1
                async for event in play():
                    yield event

            client.receive_turn = receive_turn
        return client

    engine._get_or_create_client = _get
    return engine


@pytest.mark.asyncio
async def test_engine_records_per_model_rows(db, tmp_path):
    t1 = _mu(claude_haiku_5_5=(2, 3, 0, 15998, 0.002))
    t2 = _mu(claude_haiku_5_5=(1180, 196, 29045, 19571, 0.003),
             claude_sonnet_5_5=(2, 3, 0, 10835, 0.0271))
    t3 = _mu(claude_haiku_5_5=(2, 4, 12664, 3335, 0.0005))  # new CLI process
    engine = await _per_turn_engine(db, tmp_path, "pm", [
        [("text", "a"), ("result", "sdk-pm", {"total_cost_usd": 0.002, "model_usage": t1})],
        [("text", "b"), ("result", "sdk-pm", {"total_cost_usd": 0.0301, "model_usage": t2})],
        [("text", "c"), ("result", "sdk-pm", {"total_cost_usd": 0.0005, "model_usage": t3})],
    ])
    for text in ("one", "two", "three"):
        await engine.run("pm", text, source="web", channel="web", actor=None)
    rows = await _model_rows(db, "pm")
    assert [(r["model"], r["input_tokens"]) for r in rows] == [
        ("claude-haiku-5-5", 2),
        ("claude-haiku-5-5", 1178), ("claude-sonnet-5-5", 2),
        ("claude-haiku-5-5", 2),
    ]
    assert sum(r["cost_usd"] for r in rows) == pytest.approx(0.002 + 0.0281 + 0.0005)
    turn_ids = [r["usage_id"] for r in rows]
    assert turn_ids[1] == turn_ids[2] and len(set(turn_ids)) == 3
    meta = json.loads((await db.get_session("pm"))["metadata"])
    assert meta["_sdk_cumulative_model_usage"]["claude-haiku-5-5"]["cache_read_tokens"] == 12664


@pytest.mark.asyncio
async def test_engine_without_model_usage_writes_no_rows(db, tmp_path):
    engine, _ = await _engine_with_scripted_clients(
        db, tmp_path, "pm0", [[("text", "a"), ("result", "sdk-0")]],
    )
    await engine.run("pm0", "hello", source="web", channel="web", actor=None)
    assert await _model_rows(db, "pm0") == []


@pytest.mark.asyncio
async def test_reset_cost_baseline_clears_model_snapshot(db):
    await db.create_session("rb", title="t", source="web", actor=None)
    await db.update_session_metadata("rb", {
        "_sdk_cumulative_cost": 0, "_sdk_cumulative_model_usage": {"m": {}},
    })
    shim = SimpleNamespace(db=db)
    await AgentEngine._reset_cost_baseline(shim, "rb")
    meta = json.loads((await db.get_session("rb"))["metadata"])
    assert meta["_sdk_cumulative_model_usage"] == {}


# ---------------------------------------------------------------------------
# Diagnostics route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDiagnosticsRoute:
    @pytest_asyncio.fixture
    async def client(self, db: Database, bypass_auth, tmp_path, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import nerve.config as cfg_mod
        from nerve.config import NerveConfig
        from nerve.gateway.routes._deps import init_deps
        from nerve.gateway.routes.diagnostics import router

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        cfg_mod._config = NerveConfig(workspace=tmp_path)
        init_deps(engine=SimpleNamespace(), db=db)  # type: ignore[arg-type]
        app = FastAPI()
        app.include_router(router)
        bypass_auth(app)
        yield TestClient(app)
        cfg_mod._config = None

    async def test_effective_pricing_in_payload(self, client, db: Database):
        await db.create_session("d1", title="t", source="web", actor=None)
        uid = await db.record_turn_usage("d1", 0, 0, 0, 0, 200_000, cost_usd=20.0,
                                         cost_basis="api_billed")
        await db.record_turn_model_usage(uid, "d1", [
            _agg("claude-opus-5-5", o=1_000_000, cost=20.0)])
        ep = client.get("/api/diagnostics").json()["usage"]["effective_pricing"]
        assert ep["models"][0]["model"] == "claude-opus-5-5"
        assert ep["models"][0]["billed_over_list"] == pytest.approx(1.0)
        assert ep["cli_price_cache"] is False

    async def test_failure_hides_only_this_block(self, client, db: Database, monkeypatch):
        await db.create_session("d2", title="t", source="web", actor=None)
        await db.record_turn_usage("d2", 1, 1, 1, 1, 200_000, cost_usd=0.1)

        async def boom(*a, **kw):
            raise RuntimeError("query failed")

        monkeypatch.setattr(db, "get_effective_pricing", boom)
        usage = client.get("/api/diagnostics").json()["usage"]
        assert usage["effective_pricing"] is None
        assert usage["last_7d"]["turns"] == 1
