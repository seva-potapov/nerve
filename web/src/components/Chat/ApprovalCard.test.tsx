import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useChatStore } from '../../stores/chatStore';
import { ApprovalCard } from './ApprovalCard';

/**
 * The approval card answers from the keyboard only while it has the focus:
 * `y` / Cmd+Enter approve, `n` declines, a held key does nothing, and a key
 * typed elsewhere never reaches it.
 */

const answerInteraction = vi.fn();
const denyInteraction = vi.fn();

function renderCard() {
  render(
    <>
      <ApprovalCard />
      <textarea aria-label="composer" id="nerve-chat-input" />
    </>,
  );
  return userEvent.setup();
}

beforeEach(() => {
  answerInteraction.mockClear();
  denyInteraction.mockClear();
  useChatStore.setState({
    pendingInteraction: {
      interactionId: 'i1', interactionType: 'command_approval', toolName: 'exec',
      toolInput: { item: { command: ['rm', '-rf', 'build'], cwd: '/w' } },
    },
    answerInteraction, denyInteraction,
  });
});

describe('ApprovalCard keys', () => {
  it('ignores y and n typed outside the card', async () => {
    const user = renderCard();
    await user.click(screen.getByLabelText('composer'));
    await user.keyboard('yn');
    expect(answerInteraction).not.toHaveBeenCalled();
    expect(denyInteraction).not.toHaveBeenCalled();
  });

  it('is reached with Shift+Tab from the message box', async () => {
    const user = renderCard();
    await user.click(screen.getByLabelText('composer'));
    await user.tab({ shift: true });
    expect(screen.getByRole('button', { name: /Decline/ })).toHaveFocus();
  });

  it('approves on y while it has the focus', async () => {
    const user = renderCard();
    screen.getByRole('button', { name: /Decline/ }).focus();
    await user.keyboard('y');
    expect(answerInteraction).toHaveBeenCalledWith(null);
    expect(denyInteraction).not.toHaveBeenCalled();
  });

  it('declines on n while it has the focus', async () => {
    const user = renderCard();
    screen.getByRole('button', { name: /Approve/ }).focus();
    await user.keyboard('n');
    expect(denyInteraction).toHaveBeenCalledWith('Declined by user.');
    expect(answerInteraction).not.toHaveBeenCalled();
  });

  it('takes a held key once: its repeats do nothing', async () => {
    const user = renderCard();
    screen.getByRole('button', { name: /Decline/ }).focus();
    // One press and two auto-repeat keydowns.
    await user.keyboard('{y>3/}');
    expect(answerInteraction).toHaveBeenCalledTimes(1);
  });

  it('hands Escape back to the message box instead of the app', async () => {
    const onDocKey = vi.fn();
    document.addEventListener('keydown', onDocKey);
    const user = renderCard();
    screen.getByRole('button', { name: /Approve/ }).focus();
    await user.keyboard('{Escape}');
    expect(screen.getByLabelText('composer')).toHaveFocus();
    expect(onDocKey).not.toHaveBeenCalled();
    expect(answerInteraction).not.toHaveBeenCalled();
    expect(denyInteraction).not.toHaveBeenCalled();
    document.removeEventListener('keydown', onDocKey);
  });
});
