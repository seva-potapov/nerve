import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { useEscStopStore } from '../stores/escStopStore';
import { useUIStore } from '../stores/uiStore';
import { ShortcutsModal } from './ShortcutsModal';

describe('ShortcutsModal', () => {
  it('documents the agents shortcuts', () => {
    render(<ShortcutsModal />);
    act(() => { useUIStore.getState().toggleShortcutsModal(); });
    const dialog = screen.getByRole('dialog', { name: 'Keyboard shortcuts' });
    expect(within(dialog).getByText('Toggle the agents panel')).toBeInTheDocument();
    expect(within(dialog).getByText('Open agents (empty message box)')).toBeInTheDocument();
    expect(within(dialog).getByText('Agents panel')).toBeInTheDocument();
    expect(within(dialog).getByText('x x')).toBeInTheDocument();
    expect(within(dialog).getByText('Back to the message box')).toBeInTheDocument();
  });
});

describe('ShortcutsModal: Esc to stop', () => {
  beforeEach(() => {
    localStorage.clear();
    useEscStopStore.setState({ double: true, windowMs: 1000, armedUntil: 0 });
    useUIStore.setState({ shortcutsModalOpen: true });
  });

  it('shows the double press and its window, and changes them', () => {
    render(<ShortcutsModal />);
    const dialog = screen.getByRole('dialog', { name: 'Keyboard shortcuts' });
    const stopKey = () => within(dialog).getByText('Stop generation').nextElementSibling;
    expect(stopKey()).toHaveTextContent('Esc Esc');
    const box = within(dialog).getByRole('checkbox', { name: 'Stop needs a second Esc' });
    expect(box).toBeChecked();
    const field = within(dialog).getByRole('spinbutton', { name: /within/ });
    expect(field).toHaveValue(1000);

    fireEvent.change(field, { target: { value: '1500' } });
    fireEvent.blur(field);
    expect(useEscStopStore.getState().windowMs).toBe(1500);
    fireEvent.change(field, { target: { value: '50' } });
    fireEvent.keyDown(field, { key: 'Enter' });
    expect(useEscStopStore.getState().windowMs).toBe(200);
    expect(field).toHaveValue(200);

    fireEvent.click(box);
    expect(useEscStopStore.getState().double).toBe(false);
    expect(stopKey()?.textContent).toBe('Esc');
    expect(field).toBeDisabled();
  });
});
