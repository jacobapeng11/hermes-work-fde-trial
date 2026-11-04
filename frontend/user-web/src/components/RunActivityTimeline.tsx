import { useEffect, useId, useRef, useState } from "react";
import { CheckCircle2, ChevronRight, CircleAlert, LoaderCircle, Users } from "lucide-react";

import type { BackgroundTaskMessage, CapabilityErrorMessage } from "../pages/chatRunProjection";
import type { ExecutionTimelineMessage, ExecutionTraceMessage } from "../toolGrouping";
import { ExecutionTrace } from "./ExecutionTrace";

type RunActivityTimelineProps = {
  id: string;
  messages: ExecutionTimelineMessage[];
  active: boolean;
};

function TtftBadge({ milliseconds }: { milliseconds?: number }) {
  if (milliseconds === undefined) return null;
  return <small className="ttftBadge" title="从发送请求到首次可见 Agent 内容">TTFT {(milliseconds / 1000).toFixed(1)}s</small>;
}

function taskLabel(message: BackgroundTaskMessage): string {
  if (message.taskId === "transport") return "连接恢复";
  if (message.taskId === "self-evolution") return "能力自演进";
  if (message.taskId === "clarify") return "等待确认";
  return "协作任务";
}

function taskDetail(message: BackgroundTaskMessage): string {
  return message.activity || message.summary || message.goal || (
    message.status === "running" || message.status === "queued"
      ? "完成后将自动继续回答"
      : ""
  );
}

function BackgroundActivityGroup({ messages }: { messages: BackgroundTaskMessage[] }) {
  const regionId = useId();
  const running = messages.filter((message) => message.status === "running" || message.status === "queued").length;
  const failed = messages.filter((message) => message.status === "failed" || message.status === "interrupted").length;
  const waitingForConfirmation = messages.some((message) => message.taskId === "clarify" && (message.status === "running" || message.status === "queued"));
  const defaultOpen = running > 0 || failed > 0;
  const [open, setOpen] = useState(defaultOpen);
  const manuallyToggled = useRef(false);

  useEffect(() => {
    if (!manuallyToggled.current) setOpen(defaultOpen);
  }, [defaultOpen]);

  const title = waitingForConfirmation
    ? "任务已暂停，等待确认"
    : running > 0
    ? `正在协作 ${running} 个任务`
    : failed > 0
      ? `${failed} 个协作任务未完成`
      : "协作任务已完成";
  return <section className="backgroundActivityGroup" data-running={running > 0 || undefined} data-has-failure={failed > 0 || undefined}>
    <button type="button" className="backgroundActivityHeader" aria-expanded={open} aria-controls={regionId} onClick={() => {
      manuallyToggled.current = true;
      setOpen((current) => !current);
    }}>
      {running > 0 ? <LoaderCircle className="reasoningSpinner" size={14} aria-hidden="true"/> : failed > 0 ? <CircleAlert size={14} aria-hidden="true"/> : <Users size={14} aria-hidden="true"/>}
      <span>{title}</span>
      <span className="executionTraceCount">{messages.length} 项</span>
      <TtftBadge milliseconds={messages.find((message) => message.ttftMs !== undefined)?.ttftMs}/>
      <ChevronRight className="backgroundActivityChevron" size={15} aria-hidden="true"/>
    </button>
    {open && <div className="backgroundActivityBody" id={regionId}>
      {messages.map((message) => {
        const pending = message.status === "running" || message.status === "queued";
        const failedTask = message.status === "failed" || message.status === "interrupted";
        return <div className="backgroundActivityItem" data-status={message.status} key={message.id}>
          {pending ? <LoaderCircle className="reasoningSpinner" size={13} aria-hidden="true"/> : failedTask ? <CircleAlert size={13} aria-hidden="true"/> : <CheckCircle2 size={13} aria-hidden="true"/>}
          <span className="backgroundActivityName">{taskLabel(message)}</span>
          {taskDetail(message) && <span className="backgroundActivityDetail">{taskDetail(message)}</span>}
        </div>;
      })}
    </div>}
  </section>;
}

function CapabilityErrorCard({ message }: { message: CapabilityErrorMessage }) {
  const typeLabel = message.capabilityType === "knowledge_base"
    ? "知识库"
    : message.capabilityType === "mcp"
      ? "MCP"
      : "技能";
  return <article className="capabilityErrorCard" data-message-id={message.id} data-error-code={message.errorCode} role="status">
    <CircleAlert size={17} aria-hidden="true"/>
    <div>
      <strong>{message.displayMessage}</strong>
      <span>{typeLabel}调用未完成；{message.recoverable ? "专家将继续使用其他能力回答。" : "请联系管理员处理后重试。"}</span>
      <small>错误编号：{message.errorId}</small>
    </div>
  </article>;
}

export function RunActivityTimeline({ id, messages, active }: RunActivityTimelineProps) {
  const primary = messages.filter((message): message is ExecutionTraceMessage => (
    message.kind === "reasoning" || message.kind === "activity" || message.kind === "tool"
  ));
  const background = messages.filter((message): message is BackgroundTaskMessage => message.kind === "background");
  const capabilityErrors = messages.filter((message): message is CapabilityErrorMessage => message.kind === "capability-error");
  return <section className="runActivityTimeline" data-message-id={id}>
    {primary.length > 0 && <ExecutionTrace id={`${id}-primary`} messages={primary} active={active} preserveCollapsedContent={primary.some((message) => Boolean(message.runtimeRunId))}/>}
    {background.length > 0 && <BackgroundActivityGroup messages={background}/>}
    {capabilityErrors.map((message) => <CapabilityErrorCard key={message.id} message={message}/>)}
  </section>;
}
