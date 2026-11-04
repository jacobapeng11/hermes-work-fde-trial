import type { ChatFileSummary } from "@hermes-cloud/schemas";

import type {
  ChatBubbleMessage,
  ChatMessage,
  ChatMessageAttachment
} from "../chatRunProjection";

export function chatAttachmentFromFile(file: ChatFileSummary): ChatMessageAttachment {
  return {
    fileId: file.fileId,
    displayName: file.displayName,
    ...(file.mimeType ? { mimeType: file.mimeType } : {}),
    sourceType: file.sourceType
  };
}

export function isRuntimeArtifactAttachment(
  attachment: Pick<ChatMessageAttachment, "sourceType">
): boolean {
  return attachment.sourceType === "runtime";
}

function isAssistantBubble(message: ChatMessage): message is ChatBubbleMessage {
  return message.role === "assistant"
    && (message.kind === undefined || message.kind === "message");
}

function sameAttachments(
  current: ChatMessageAttachment[] | undefined,
  next: ChatMessageAttachment[]
): boolean {
  if ((current ?? []).length !== next.length) return false;
  return next.every((attachment, index) => {
    const existing = current?.[index];
    return existing?.fileId === attachment.fileId
      && existing.displayName === attachment.displayName
      && existing.mimeType === attachment.mimeType
      && existing.sourceType === attachment.sourceType;
  });
}

export function attachGeneratedImageArtifacts(
  messages: ChatMessage[],
  files: ChatFileSummary[]
): ChatMessage[] {
  const imagesByRunId = new Map<string, ChatMessageAttachment[]>();
  for (const file of [...files].sort((left, right) => left.fileId - right.fileId)) {
    if (
      file.sourceType !== "runtime"
      || !file.runtimeRunId
      || !file.mimeType?.startsWith("image/")
    ) {
      continue;
    }
    const attachments = imagesByRunId.get(file.runtimeRunId) ?? [];
    attachments.push(chatAttachmentFromFile(file));
    imagesByRunId.set(file.runtimeRunId, attachments);
  }

  let changed = false;
  const nextMessages = messages.map((message): ChatMessage => {
    if (!isAssistantBubble(message) || !message.runtimeRunId) return message;
    const attachments = imagesByRunId.get(message.runtimeRunId) ?? [];
    if (sameAttachments(message.attachments, attachments)) return message;
    changed = true;
    const { attachments: _previousAttachments, ...rest } = message;
    return attachments.length > 0 ? { ...rest, attachments } : rest;
  });
  return changed ? nextMessages : messages;
}
