import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { EffectivePricingTable, type EffectivePricing, type EffectivePricingModel } from './EffectivePricingTable';

const RATES = { input: 4, output: 20, cache_read: 0.2, cache_write_5m: 5, cache_write_1h: 8, web_search: 0.01 };

function model(over: Partial<EffectivePricingModel>): EffectivePricingModel {
  return {
    model: 'claude-opus-5-5', provider: 'firstParty', turns: 3,
    input_tokens: 10, output_tokens: 1_000_000, cache_read_tokens: 0, cache_creation_tokens: 0,
    web_search_requests: 0, billed_usd: 20, list_usd: 20, billed_over_list: 1,
    blended_usd_per_mtok: 20, price_key: 'opus-5-5', price_source: 'nerve',
    rates: RATES, cli_rates: RATES, effective_rates: RATES, long_prompt_tier: null, flags: [],
    ...over,
  };
}

function data(models: EffectivePricingModel[], over: Partial<EffectivePricing> = {}): EffectivePricing {
  return {
    models, billed_total_usd: 30, attributed_usd: 25, unattributed_usd: 5, coverage: 0.8333,
    turns: 6, turns_attributed: 5, ratio_tolerance: 0.02, cli_price_cache: true, ...over,
  };
}

describe('EffectivePricingTable', () => {
  it('shows billed, list, ratio and coverage', () => {
    render(<EffectivePricingTable data={data([model({})])} />);
    expect(screen.getByText('claude-opus-5-5')).toBeInTheDocument();
    expect(screen.getByTestId('ratio-claude-opus-5-5')).toHaveTextContent('1.000');
    expect(screen.getByText(/Attributed to a model: 83%/)).toBeInTheDocument();
    expect(screen.getByText('Unattributed $5.00')).toBeInTheDocument();
  });

  it('marks a ratio outside the tolerance and an unpriced model', () => {
    render(<EffectivePricingTable data={data([
      model({ model: 'claude-fable-5-2', billed_usd: 10, billed_over_list: 0.5, flags: ['billed_differs_from_list'] }),
      model({ model: 'claude-new-1', list_usd: null, billed_over_list: null, rates: null,
        effective_rates: null, price_source: null, price_key: null, flags: ['unpriced'] }),
    ])} />);
    expect(screen.getByTestId('ratio-claude-fable-5-2').className).toContain('text-amber-400');
    expect(screen.getByTestId('ratio-claude-new-1')).toHaveTextContent('—');
    expect(screen.getByText('unpriced')).toBeInTheDocument();
  });

  it('opens a row to show the rates and what the flags mean', () => {
    render(<EffectivePricingTable data={data([
      model({ flags: ['long_prompt_tier'] }),
    ])} />);
    fireEvent.click(screen.getByText('claude-opus-5-5'));
    expect(screen.getByText('CLI price cache')).toBeInTheDocument();
    expect(screen.getByText(/list is a lower bound/)).toBeInTheDocument();
  });

  it('renders nothing without models', () => {
    const { container } = render(<EffectivePricingTable data={data([])} />);
    expect(container).toBeEmptyDOMElement();
  });
});
