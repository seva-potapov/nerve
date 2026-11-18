import { Fragment, useState } from 'react';
import { ChevronDown, ChevronRight } from '../ui/icons';

/** Per-token rates in $ per million tokens (web search: $ per request). */
export interface Rates {
  input: number;
  output: number;
  cache_read: number;
  cache_write_5m: number;
  cache_write_1h?: number;
  web_search?: number;
}

/** One model of `usage.effective_pricing.models` (nerve/db/model_pricing.py). */
export interface EffectivePricingModel {
  model: string;
  provider?: string | null;
  turns: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  web_search_requests: number;
  billed_usd: number;
  list_usd: number | null;
  billed_over_list: number | null;
  blended_usd_per_mtok: number | null;
  price_key: string | null;
  price_source: string | null;
  rates: Rates | null;
  cli_rates: Rates | null;
  effective_rates: Rates | null;
  long_prompt_tier: { above_prompt_tokens: number; multiplier: number } | null;
  flags: string[];
}

export interface EffectivePricing {
  models: EffectivePricingModel[];
  billed_total_usd: number;
  attributed_usd: number;
  unattributed_usd: number;
  coverage: number | null;
  turns: number;
  turns_attributed: number;
  ratio_tolerance: number;
  cli_price_cache: boolean;
}

const FLAG_TEXT: Record<string, string> = {
  unpriced: 'Not in Nerve’s price table',
  only_cli_has_price: 'The CLI has a price for it, Nerve does not',
  price_sources_disagree: 'Nerve’s table and the CLI’s price cache disagree',
  billed_differs_from_list: 'Billed differs from list: the CLI may have priced it as another model',
  long_prompt_tier: 'Has a higher price for long prompts, applied per request: list is a lower bound',
};

function usd(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined) return '—';
  return `$${n.toFixed(digits)}`;
}

function tokens(n: number): string {
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(1)}B`;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function rate(n: number | undefined): string {
  return n === undefined ? '—' : `$${Number(n.toFixed(4))}`;
}

function RatesRow({ label, rates }: { label: string; rates: Rates | null }) {
  return (
    <tr>
      <td className="pr-4 text-text-muted">{label}</td>
      <td className="pr-4 text-right tabular-nums">{rates ? rate(rates.input) : '—'}</td>
      <td className="pr-4 text-right tabular-nums">{rates ? rate(rates.output) : '—'}</td>
      <td className="pr-4 text-right tabular-nums">{rates ? rate(rates.cache_read) : '—'}</td>
      <td className="text-right tabular-nums">{rates ? rate(rates.cache_write_5m) : '—'}</td>
    </tr>
  );
}

function Details({ m }: { m: EffectivePricingModel }) {
  return (
    <div className="px-4 py-3 bg-surface/50 text-xs text-text-secondary space-y-3">
      <div className="flex flex-wrap gap-x-6 gap-y-1 tabular-nums">
        <span>Uncached input {tokens(m.input_tokens)}</span>
        <span>Output {tokens(m.output_tokens)}</span>
        <span>Cache read {tokens(m.cache_read_tokens)}</span>
        <span>Cache write {tokens(m.cache_creation_tokens)}</span>
        {m.web_search_requests > 0 && <span>Web searches {m.web_search_requests}</span>}
        <span>Turns {m.turns}</span>
      </div>
      <table className="text-xs">
        <thead>
          <tr className="text-text-muted">
            <th className="pr-4 text-left font-medium">$ per million tokens</th>
            <th className="pr-4 text-right font-medium">Input</th>
            <th className="pr-4 text-right font-medium">Output</th>
            <th className="pr-4 text-right font-medium">Cache read</th>
            <th className="text-right font-medium">Cache write</th>
          </tr>
        </thead>
        <tbody>
          <RatesRow label={`List (${m.price_source ?? 'none'}${m.price_key ? `: ${m.price_key}` : ''})`} rates={m.rates} />
          <RatesRow label="CLI price cache" rates={m.cli_rates} />
          <RatesRow label="Effective (list × billed/list)" rates={m.effective_rates} />
        </tbody>
      </table>
      {m.flags.length > 0 && (
        <ul className="list-disc pl-4 text-amber-400">
          {m.flags.map((f) => <li key={f}>{FLAG_TEXT[f] ?? f}</li>)}
        </ul>
      )}
    </div>
  );
}

/**
 * Billed vs list cost per model over the diagnostics window.
 *
 * "Billed" is what the Claude CLI reported (its own estimate, which can
 * be wrong for a model it does not know yet); "list" is the same tokens at
 * Nerve's price table. Cache writes are priced at the 5-minute rate,
 * because the CLI's per-model usage has no TTL split.
 */
export function EffectivePricingTable({ data }: { data: EffectivePricing }) {
  const [open, setOpen] = useState<string | null>(null);
  if (!data.models.length) return null;
  const tolerance = data.ratio_tolerance ?? 0.02;
  return (
    <div className="border border-border-subtle rounded-lg overflow-hidden mb-3">
      <div className="px-4 py-2 bg-surface text-xs text-text-muted flex flex-wrap gap-x-4">
        <span className="font-medium text-text-secondary">Effective pricing</span>
        <span>
          Attributed to a model: {data.coverage === null ? '—' : `${Math.round(data.coverage * 100)}%`}
          {' '}({data.turns_attributed} of {data.turns} turns)
        </span>
        {data.unattributed_usd > 0 && <span>Unattributed {usd(data.unattributed_usd)}</span>}
        {!data.cli_price_cache && <span>No CLI price cache found</span>}
      </div>
      <table className="w-full text-sm">
        <thead>
          <tr className="bg-surface text-text-muted border-t border-border-subtle">
            <th className="text-left px-4 py-2 font-medium">Model</th>
            <th className="text-right px-4 py-2 font-medium">Billed</th>
            <th className="text-right px-4 py-2 font-medium">List</th>
            <th className="text-right px-4 py-2 font-medium" title="Billed ÷ list">Ratio</th>
            <th className="text-right px-4 py-2 font-medium" title="Billed $ per million tokens of all types">$/M blended</th>
          </tr>
        </thead>
        <tbody>
          {data.models.map((m) => {
            const isOpen = open === m.model;
            const off = m.billed_over_list !== null && Math.abs(m.billed_over_list - 1) > tolerance;
            return (
              <Fragment key={m.model}>
                <tr
                  className="border-t border-border-subtle hover:bg-surface cursor-pointer"
                  onClick={() => setOpen(isOpen ? null : m.model)}
                  aria-expanded={isOpen}
                >
                  <td className="px-4 py-2 font-mono text-text-secondary text-xs">
                    <span className="inline-flex items-center gap-1">
                      {isOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                      {m.model}
                      {m.flags.includes('unpriced') && (
                        <span className="ml-1 px-1 rounded bg-amber-500/20 text-amber-400 text-2xs">unpriced</span>
                      )}
                    </span>
                  </td>
                  <td className="px-4 py-2 text-right text-text-secondary tabular-nums">{usd(m.billed_usd)}</td>
                  <td className="px-4 py-2 text-right text-text-secondary tabular-nums">{usd(m.list_usd)}</td>
                  <td
                    className={`px-4 py-2 text-right tabular-nums ${off ? 'text-amber-400' : 'text-text-dim'}`}
                    data-testid={`ratio-${m.model}`}
                  >
                    {m.billed_over_list === null ? '—' : m.billed_over_list.toFixed(3)}
                  </td>
                  <td className="px-4 py-2 text-right text-text-dim tabular-nums">{usd(m.blended_usd_per_mtok, 3)}</td>
                </tr>
                {isOpen && (
                  <tr className="border-t border-border-subtle">
                    <td colSpan={5} className="p-0"><Details m={m} /></td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
