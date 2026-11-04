export type ChatComposerAction = "send" | "stop";

export interface ChatComposerActionInput {
  draft: string;
  hasActiveRun: boolean;
  uploadPending: boolean;
  recovering: boolean;
  cancellationPending: boolean;
  canSendWithoutText?: boolean;
}

export interface ChatComposerActionState {
  action: ChatComposerAction;
  disabled: boolean;
}

export function chatComposerAction({
  draft,
  hasActiveRun,
  uploadPending,
  recovering,
  cancellationPending,
  canSendWithoutText = false
}: ChatComposerActionInput): ChatComposerActionState {
  if (draft.trim() || canSendWithoutText) {
    return {
      action: "send",
      disabled: uploadPending || recovering
    };
  }
  if (hasActiveRun) {
    return {
      action: "stop",
      disabled: cancellationPending || recovering
    };
  }
  return { action: "send", disabled: true };
}
