import { memo, useEffect, useRef, useState } from "react";
import { Check, CircleAlert, Clock3, Copy, LoaderCircle, Zap } from "lucide-react";

import type {
  ChatMessage,
  ChatMessageAttachment,
  ScheduledMessageSource
} from "../pages/chatRunProjection";
import { groupExecutionMessages } from "../toolGrouping";
import { AgentMarkdown } from "./AgentMarkdown";
import { StreamingAgentMarkdown } from "./StreamingAgentMarkdown";
import { RunActivityTimeline } from "./RunActivityTimeline";
import { ChatAttachmentList } from "./ChatAttachmentList";
import { extractHtmlArtifacts } from "../utils/htmlArtifact";

interface ChatMessageListProps {
  messages: ChatMessage[];
  streaming: boolean;
  agentNamesByProfileId?: Record<number, string>;
  onOpenScheduledTask?: (taskId: number) => void;
  resolveImageUrl?: (attachment: ChatMessageAttachment) => Promise<string>;
  onOpenImage?: (
    attachment: ChatMessageAttachment,
    objectUrl: string
  ) => void;
  onOpenHtmlPreview?: (message: ChatMessage) => void;
  htmlPreviewOpenedMessageIds?: string[];
}

const COPY_FEEDBACK_MS = 2000;
const COPY_BUTTON_HIDE_DELAY_MS = 650;
const REASONING_STATUS_ICON_SIZE = 13;

type CopyState = "idle" | "copied" | "failed";

function MessageCopyButton({ content, onInteraction }: { content: string; onInteraction: (duration?: number) => void }) {
  const [state, setState] = useState<CopyState>("idle");
  const resetTimer = useRef<number>();

  useEffect(() => () => {
    if (resetTimer.current) window.clearTimeout(resetTimer.current);
  }, []);

  const copy = async () => {
    onInteraction(COPY_FEEDBACK_MS);
    if (resetTimer.current) window.clearTimeout(resetTimer.current);
    try {
      if (!navigator.clipboard?.writeText) {
        setState("failed");
        return;
      }
      await navigator.clipboard.writeText(content);
      setState("copied");
      resetTimer.current = window.setTimeout(() => setState("idle"), COPY_FEEDBACK_MS);
    } catch {
      setState("failed");
    }
  };

  const label = state === "copied" ? "已复制" : state === "failed" ? "复制失败" : "复制消息";
  const Icon = state === "copied" ? Check : state === "failed" ? CircleAlert : Copy;
  return <button
    type="button"
    className="messageCopyButton"
    data-state={state}
    aria-label={label}
    title={label}
    onMouseEnter={() => onInteraction()}
    onFocus={() => onInteraction()}
    onClick={() => void copy()}
  >
    <Icon size={15} aria-hidden="true"/>
  </button>;
}

function TtftBadge({ milliseconds }: { milliseconds?: number }) {
  if (milliseconds === undefined) return null;
  return <small className="ttftBadge" title="从发送请求到首次可见 Agent 内容">TTFT {(milliseconds / 1000).toFixed(1)}s</small>;
}

const ChatBubble = memo(function ChatBubble({
  message,
  assistantActive,
  agentNamesByProfileId = {},
  onOpenScheduledTask,
  resolveImageUrl,
  onOpenImage,
  onOpenHtmlPreview,
  htmlPreviewOpenedMessageIds = []
}: {
  message: ChatMessage;
  assistantActive: boolean;
  agentNamesByProfileId?: Record<number, string>;
  onOpenScheduledTask?: (taskId: number) => void;
  resolveImageUrl?: (attachment: ChatMessageAttachment) => Promise<string>;
  onOpenImage?: (
    attachment: ChatMessageAttachment,
    objectUrl: string
  ) => void;
  onOpenHtmlPreview?: (message: ChatMessage) => void;
  htmlPreviewOpenedMessageIds?: string[];
}) {
  const [copyVisible, setCopyVisible] = useState(false);
  const copyHideTimer = useRef<number>();
  const clearCopyHideTimer = () => {
    if (copyHideTimer.current) window.clearTimeout(copyHideTimer.current);
    copyHideTimer.current = undefined;
  };
  const showCopy = (duration?: number) => {
    clearCopyHideTimer();
    setCopyVisible(true);
    if (duration) {
      copyHideTimer.current = window.setTimeout(() => {
        setCopyVisible(false);
        copyHideTimer.current = undefined;
      }, duration);
    }
  };
  const hideCopyLater = () => {
    clearCopyHideTimer();
    copyHideTimer.current = window.setTimeout(() => {
      setCopyVisible(false);
      copyHideTimer.current = undefined;
    }, COPY_BUTTON_HIDE_DELAY_MS);
  };
  useEffect(() => () => clearCopyHideTimer(), []);
  const thinking = message.role === "assistant"
    && Boolean(message.thinkingPhase)
    && !message.content?.trim();
  const hasTtft = message.role === "assistant"
    && !thinking
    && message.ttftMs !== undefined;
  const hasHtmlArtifact = message.role === "assistant"
    && message.kind === undefined
    && extractHtmlArtifacts(message.content || "").length > 0;
  const contextAgentName = message.role === "assistant"
    && message.kind === undefined
    && message.contextAgentProfileId !== undefined
    ? agentNamesByProfileId[message.contextAgentProfileId]
    : undefined;
  return <article
    className={thinking ? "message thinkingPlaceholder" : "message"}
    data-message-id={message.id}
    data-role={message.role}
    data-status={message.status}
    data-thinking-phase={thinking ? message.thinkingPhase : undefined}
    data-has-ttft={hasTtft ? "true" : undefined}
    data-streaming={assistantActive && !thinking ? "true" : undefined}
    data-copy-visible={copyVisible || undefined}
    role={thinking ? "status" : undefined}
    aria-live={thinking ? "polite" : undefined}
    onMouseEnter={() => showCopy()}
    onMouseLeave={hideCopyLater}
    onFocusCapture={() => showCopy()}
    onBlurCapture={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) hideCopyLater();
    }}
  >
    {message.role === "assistant" && <span className="assistantAvatar" aria-hidden="true"><Zap size={16}/></span>}
    {contextAgentName && <span className="contextAgentBadge">{contextAgentName}</span>}
    {message.role === "user" && message.scheduledSource && <ScheduledSourceBadge source={message.scheduledSource} onOpen={onOpenScheduledTask}/>}
    {message.role === "user"
      && message.attachments
      && message.attachments.length > 0
      && resolveImageUrl
      && onOpenImage
      && <ChatAttachmentList
        attachments={message.attachments}
        resolveImageUrl={resolveImageUrl}
        onOpenImage={onOpenImage}
      />}
    {thinking && <span className="thinkingStatus">
      <LoaderCircle className="reasoningSpinner reasoningIcon" size={REASONING_STATUS_ICON_SIZE}/>
      <span>正在思考...</span>
    </span>}
    {message.role === "assistant"
      && message.kind === undefined
      && message.attachments
      && message.attachments.length > 0
      && resolveImageUrl
      && onOpenImage
      && <ChatAttachmentList
        attachments={message.attachments}
        resolveImageUrl={resolveImageUrl}
        onOpenImage={onOpenImage}
      />}
    {message.content && (message.role === "assistant"
      ? assistantActive
        ? <StreamingAgentMarkdown content={message.content}/>
        : <AgentMarkdown content={message.content}/>
      : <p className="messagePlainText">{message.content}</p>)}
    {hasHtmlArtifact && onOpenHtmlPreview && (
      <button
        type="button"
        className="htmlArtifactHint"
        onClick={() => onOpenHtmlPreview(message)}
      >
        检测到完整 HTML 页面
        <span>{htmlPreviewOpenedMessageIds.includes(message.id) ? "重新打开预览" : "打开预览"}</span>
      </button>
    )}
    {hasTtft && <TtftBadge milliseconds={message.ttftMs}/>}
    {message.role === "user" && message.submissionHint
      && <small className="messageSubmissionHint" role="status">{message.submissionHint}</small>}
    {message.error && <p className="messageError">{message.error}</p>}
    {message.content && (message.role === "user" || message.role === "assistant") && <MessageCopyButton content={message.content} onInteraction={showCopy}/>}
  </article>;
});

function ScheduledSourceBadge({ source, onOpen }: { source: ScheduledMessageSource; onOpen?: (taskId: number) => void }) {
  const label = `定时任务 · ${source.scheduledTaskName}`;
  const content = <>
    <Clock3 size={12} aria-hidden="true" />
    <span className="scheduledSourceBadgeKind">定时执行</span>
    <span className="scheduledSourceBadgeDivider" aria-hidden="true" />
    <span className="scheduledSourceBadgeName" title={source.scheduledTaskName}>{source.scheduledTaskName}</span>
  </>;
  if (source.taskAvailable) {
    return <button
      type="button"
      className="scheduledSourceBadge"
      data-scheduled-task-id={source.scheduledTaskId}
      aria-label={label}
      title={label}
      onClick={() => onOpen?.(source.scheduledTaskId)}
    >{content}</button>;
  }
  return <span
    className="scheduledSourceBadge scheduledSourceBadgeDisabled"
    data-scheduled-task-id={source.scheduledTaskId}
    aria-label={label}
    title="定时任务已删除"
  >{content}</span>;
}

function latestUserMessageIndex(messages: ChatMessage[]) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].role === "user") return index;
  }
  return -1;
}

function isExecutionMessage(message: ChatMessage): boolean {
  return message.kind === "activity"
    || message.kind === "reasoning"
    || message.kind === "tool"
    || message.kind === "background"
    || message.kind === "capability-error";
}

export function ChatMessageList({
  messages,
  streaming,
  agentNamesByProfileId,
  onOpenScheduledTask,
  resolveImageUrl,
  onOpenImage,
  onOpenHtmlPreview,
  htmlPreviewOpenedMessageIds
}: ChatMessageListProps) {
  const visible = messages.filter((message) =>
    isExecutionMessage(message)
    || Boolean(message.content?.trim())
    || Boolean(
      (message.role === "user" || (message.role === "assistant" && message.kind === undefined))
      && message.attachments?.length
    )
    || Boolean(message.error)
    || Boolean(message.thinkingPhase)
  );
  const latestUserIndex = latestUserMessageIndex(messages);
  const currentTurn = messages.slice(latestUserIndex + 1);
  const hasRunningBackground = currentTurn.some((message) => (
    message.kind === "background" && (message.status === "running" || message.status === "queued")
  ));
  const foregroundStreaming = streaming && !hasRunningBackground;
  const renderNodes = groupExecutionMessages(visible);
  const lastVisibleMessage = visible[visible.length - 1];
  const activeMessageId = foregroundStreaming ? lastVisibleMessage?.id : undefined;
  return <>
    {renderNodes.map((node) => {
      if (node.kind === "execution-timeline") {
        const hasRuntimeIdentity = node.messages.some((message) => Boolean(message.runtimeRunId));
        const answerAlreadyVisible = currentTurn.some((message) => (
          message.role === "assistant"
          && message.kind === undefined
          && Boolean(message.content?.trim())
        ));
        const active = foregroundStreaming
          && node.messages.some((message) => currentTurn.some((item) => item.id === message.id))
          && (!hasRuntimeIdentity || !answerAlreadyVisible);
        return <div key={node.id} className="executionTimelineNode" data-message-id={node.id}>
          <RunActivityTimeline id={node.id} messages={node.messages} active={active}/>
        </div>;
      }
      const message = node.message;
      const assistantActive = message.role === "assistant"
        && message.status === "streaming"
        && message.id === activeMessageId;
      return <ChatBubble
        key={message.id}
        message={message}
        assistantActive={assistantActive}
        agentNamesByProfileId={agentNamesByProfileId}
        onOpenScheduledTask={onOpenScheduledTask}
        resolveImageUrl={resolveImageUrl}
        onOpenImage={onOpenImage}
        onOpenHtmlPreview={onOpenHtmlPreview}
        htmlPreviewOpenedMessageIds={htmlPreviewOpenedMessageIds}
      />;
    })}
  </>;
}
