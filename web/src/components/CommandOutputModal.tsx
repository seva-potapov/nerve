import { useUIStore } from '../stores/uiStore';
import { Modal } from './ui/Modal';

/**
 * The text answer of a composer command (`/status`, `/cost`, `/diag`,
 * `/btw`): the same text the Telegram bot sends, in a dialog.
 */
export function CommandOutputModal() {
  const output = useUIStore(s => s.commandOutput);
  const close = useUIStore(s => s.closeCommandOutput);
  return (
    <Modal open={output !== null} onClose={close} title={output?.title ?? ''} size="md">
      <pre className="whitespace-pre-wrap break-words font-mono text-sm leading-relaxed text-text-secondary">
        {output?.text ?? ''}
      </pre>
    </Modal>
  );
}
