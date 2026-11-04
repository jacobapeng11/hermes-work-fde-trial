import type { ChatFileSummary } from "@hermes-cloud/schemas";
import type { ChatMessage, ChatMessageAttachment } from "./chatRunProjection";

export const MAX_IMAGE_ATTACHMENT_BYTES = 20 * 1024 * 1024;
export const SUPPORTED_IMAGE_MIME_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/webp"
]);

export const isImageFile = (file: File) => file.type.startsWith("image/");

export function isSupportedImageFile(file: File): boolean {
  return SUPPORTED_IMAGE_MIME_TYPES.has(file.type);
}

export function normalizePastedImage(file: File, ordinal: number): File {
  if (file.name && file.name !== "image.png") return file;
  const extension = file.type === "image/jpeg"
    ? "jpg"
    : file.type === "image/webp" ? "webp" : "png";
  const timestamp = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14);
  return new File([file], `pasted-image-${timestamp}-${ordinal}.${extension}`, {
    type: file.type
  });
}

export function chatAttachmentFromFile(
  file: ChatFileSummary
): ChatMessageAttachment {
  return {
    fileId: file.fileId,
    displayName: file.displayName,
    ...(file.mimeType ? { mimeType: file.mimeType } : {})
  };
}

export function hydrateAttachmentMetadata(
  messages: ChatMessage[],
  files: ChatFileSummary[]
): ChatMessage[] {
  const filesById = new Map(files.map((file) => [file.fileId, file]));
  return messages.map((message) => {
    if (message.role !== "user" || !message.attachments) return message;
    const attachments = message.attachments.map((attachment) => {
      const file = filesById.get(attachment.fileId);
      if (!file) return attachment;
      return chatAttachmentFromFile(file);
    });
    return { ...message, attachments };
  });
}

export function acceptAttachmentFiles(
  selected: File[],
  selectedModelSupportsVision: boolean
): { accepted: File[]; errors: string[] } {
  const accepted: File[] = [];
  const errors: string[] = [];
  selected.forEach((file, index) => {
    if (!isImageFile(file)) {
      accepted.push(file);
      return;
    }
    if (!selectedModelSupportsVision) {
      errors.push("当前模型不支持图片识别，请切换模型后重试");
      return;
    }
    if (!isSupportedImageFile(file)) {
      errors.push("仅支持粘贴 PNG、JPEG 或 WebP 图片");
      return;
    }
    if (file.size > MAX_IMAGE_ATTACHMENT_BYTES) {
      errors.push("单张图片不能超过 20 MB");
      return;
    }
    accepted.push(normalizePastedImage(file, index + 1));
  });
  return { accepted, errors };
}
