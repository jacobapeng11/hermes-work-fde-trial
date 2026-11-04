import { useEffect, useRef, useState } from "react";
import { FileText, ImageOff, LoaderCircle } from "lucide-react";
import type { ChatMessageAttachment } from "../pages/chatRunProjection";

interface ChatAttachmentListProps {
  attachments: ChatMessageAttachment[];
  resolveImageUrl: (attachment: ChatMessageAttachment) => Promise<string>;
  onOpenImage: (
    attachment: ChatMessageAttachment,
    objectUrl: string
  ) => void;
}

export function ChatAttachmentList({
  attachments,
  resolveImageUrl,
  onOpenImage
}: ChatAttachmentListProps) {
  if (attachments.length === 0) return null;
  return <div
    className="chatAttachmentList"
    aria-label="消息附件"
    style={{ display: "flex", flexWrap: "wrap", gap: 8, margin: "0 0 8px" }}
  >
    {attachments.map((attachment) => (
      isImageAttachment(attachment)
        ? <ChatImageAttachment
          key={`${attachment.fileId}:${attachment.displayName}`}
          attachment={attachment}
          resolveImageUrl={resolveImageUrl}
          onOpenImage={onOpenImage}
        />
        : <span
          className="chatAttachmentFile"
          key={`${attachment.fileId}:${attachment.displayName}`}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            maxWidth: "100%",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap"
          }}
        >
          <FileText size={14}/>
          <span title={attachment.displayName}>{attachment.displayName}</span>
        </span>
    ))}
  </div>;
}

function isImageAttachment(attachment: ChatMessageAttachment): boolean {
  return Boolean(attachment.mimeType?.startsWith("image/"));
}

function ChatImageAttachment({
  attachment,
  resolveImageUrl,
  onOpenImage
}: {
  attachment: ChatMessageAttachment;
  resolveImageUrl: (attachment: ChatMessageAttachment) => Promise<string>;
  onOpenImage: (attachment: ChatMessageAttachment, objectUrl: string) => void;
}) {
  const buttonRef = useRef<HTMLButtonElement>(null);
  const [visible, setVisible] = useState(false);
  const [url, setUrl] = useState<string>();
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") {
      setVisible(true);
      return;
    }
    const element = buttonRef.current;
    if (!element) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) setVisible(true);
    }, { rootMargin: "160px" });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!visible || url || failed) return;
    let revoked = false;
    void resolveImageUrl(attachment)
      .then((nextUrl) => {
        if (!revoked) setUrl(nextUrl);
        else URL.revokeObjectURL(nextUrl);
      })
      .catch(() => setFailed(true));
    return () => {
      revoked = true;
    };
  }, [attachment, failed, resolveImageUrl, url, visible]);

  useEffect(() => () => {
    if (url) URL.revokeObjectURL(url);
  }, [url]);

  return <button
    ref={buttonRef}
    type="button"
    className="chatImageAttachment"
    aria-label={`查看图片 ${attachment.displayName}`}
    style={{
      display: "grid",
      placeItems: "center",
      width: 144,
      height: 104,
      overflow: "hidden",
      padding: 0,
      border: "1px solid var(--border)",
      borderRadius: "var(--radius-md)",
      background: "var(--surface-secondary)",
      color: "var(--text-muted)",
      fontSize: 12
    }}
    onClick={() => url && onOpenImage(attachment, url)}
  >
    {url
      ? <img
        src={url}
        alt={attachment.displayName}
        loading="lazy"
        decoding="async"
        style={{ width: "100%", height: "100%", objectFit: "cover" }}
      />
      : failed
        ? <><ImageOff size={16}/><span>图片不可用</span></>
        : <><LoaderCircle className="uploadSpinner" size={16}/><span>加载图片...</span></>}
  </button>;
}
