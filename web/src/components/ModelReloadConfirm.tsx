import { ConfirmDialog } from "@/components/ConfirmDialog";

/**
 * Offer a dashboard reload after persisting a model change. Reloading
 * reattaches the server-owned PTY and does not imply a fresh chat.
 *
 * Shared by the chat sidebar picker and the Models page so both behave
 * identically. `model` is the short model name awaiting confirmation, or null
 * when the dialog is closed.
 */
export function ModelReloadConfirm({
  model,
  description,
  onCancel,
}: {
  model: string | null;
  /** Override the default body copy (e.g. the Models-page phrasing). */
  description?: string;
  onCancel: () => void;
}) {
  return (
    <ConfirmDialog
      open={model !== null}
      title="Switch model?"
      description={
        description ??
        `${model ?? "The model"} is saved. Eligible unpinned chats can use the saved provider and model on a future turn. Pinned overrides and reasoning settings may behave differently. Reload the dashboard now?`
      }
      confirmLabel="Reload dashboard"
      onConfirm={() => window.location.reload()}
      onCancel={onCancel}
    />
  );
}
