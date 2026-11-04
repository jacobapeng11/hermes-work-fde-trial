import { useEffect, useId, useRef, useState } from "react";
import { Brain, CheckCircle2, ChevronRight, CircleAlert, LoaderCircle } from "lucide-react";

import type { ToolMessage } from "../pages/chatRunProjection";
import type { ExecutionTraceMessage } from "../toolGrouping";
import { traceLabelForTool } from "../toolPresentation";
import { ToolActivityItem } from "./ToolActivityGroup";

const STATUS_ICON_SIZE = 14;

function ToolStatusIcon({ status }: { status: ToolMessage["status"] }) {
  if (status === "running") return <LoaderCircle className="toolRunningIcon" size={STATUS_ICON_SIZE} aria-hidden="true"/>;
  if (status === "failed") return <CircleAlert size={STATUS_ICON_SIZE} aria-hidden="true"/>;
  return <CheckCircle2 size={STATUS_ICON_SIZE} aria-hidden="true"/>;
}

function ToolTraceStep({ message }: { message: ToolMessage }) {
  const [open, setOpen] = useState(false);
  const regionId = useId();
  const label = traceLabelForTool(message);
  return <article className="toolTraceStep" data-status={message.status}>
    <button type="button" className="toolTraceStepHeader" aria-expanded={open} aria-controls={regionId} onClick={() => setOpen((current) => !current)}>
      <span className="toolTraceStepStatus"><ToolStatusIcon status={message.status}/></span>
      <span className="toolTraceStepLabel" title={label}>{label}</span>
      <ChevronRight className="toolTraceStepChevron" size={14} aria-hidden="true"/>
    </button>
    {open && <div className="toolTraceStepDetail" id={regionId}><ToolActivityItem message={message}/></div>}
  </article>;
}

function TraceTextStep({ message }: { message: Exclude<ExecutionTraceMessage, ToolMessage> }) {
  return <pre className="executionTraceText" data-kind={message.kind}>{message.text}</pre>;
}

interface ExecutionTraceProps {
  id: string;
  messages: ExecutionTraceMessage[];
  active: boolean;
  preserveCollapsedContent?: boolean;
}

export function ExecutionTrace({ id, messages, active, preserveCollapsedContent = false }: ExecutionTraceProps) {
  const regionId = useId();
  const toolMessages = messages.filter((message): message is ToolMessage => message.kind === "tool");
  const failed = toolMessages.filter((message) => message.status === "failed").length;
  const running = toolMessages.filter((message) => message.status === "running").length;
  const completed = toolMessages.filter((message) => message.status === "completed").length;
  const defaultOpen = active || failed > 0;
  const [open, setOpen] = useState(defaultOpen);
  const manuallyToggled = useRef(false);
  const previousId = useRef(id);
  const ttftMs = messages.find((message) => message.ttftMs !== undefined)?.ttftMs;

  useEffect(() => {
    if (previousId.current !== id) {
      previousId.current = id;
      manuallyToggled.current = false;
      setOpen(defaultOpen);
    } else if (!manuallyToggled.current) {
      setOpen(defaultOpen);
    }
  }, [defaultOpen, id]);

  const toggle = () => {
    manuallyToggled.current = true;
    setOpen((current) => !current);
  };
  const title = active ? "思考中…" : failed > 0 ? `${failed} 个操作未完成` : null;
  const secondary = active
    ? running > 0 ? `正在执行 ${running} 个操作` : "深度思考"
    : failed > 0 ? "深度思考" : undefined;
  const count = active ? toolMessages.length : completed + failed;

  return <section className="executionTrace" data-kind="execution-trace" data-message-id={id} data-active={active} data-has-failure={failed > 0 ? "true" : "false"} data-expanded={open ? "true" : "false"}>
    <button type="button" className="executionTraceHeader" aria-expanded={open} aria-controls={regionId} onClick={toggle}>
      {active ? <LoaderCircle className="reasoningSpinner executionTraceIcon" size={STATUS_ICON_SIZE} aria-hidden="true"/> : failed > 0 ? <CircleAlert className="executionTraceIcon" size={STATUS_ICON_SIZE} aria-hidden="true"/> : <Brain className="executionTraceIcon" size={STATUS_ICON_SIZE} aria-hidden="true"/>}
      <span className="executionTraceTitle" aria-live="polite">{title ?? "思考过程"}</span>
      {secondary && <span className="executionTraceSecondary">{secondary}</span>}
      {count > 0 && <span className="executionTraceCount">{active ? `${count} 次操作` : `已完成 ${count} 个操作`}</span>}
      {ttftMs !== undefined && <small className="ttftBadge" title="从发送请求到首次可见 Agent 内容">TTFT {(ttftMs / 1000).toFixed(1)}s</small>}
      <ChevronRight className="executionTraceChevron" size={15} aria-hidden="true"/>
    </button>
    {(open || preserveCollapsedContent) && <div className="executionTraceBody" id={regionId} hidden={!open}>
      {messages.map((message) => message.kind === "tool"
        ? <ToolTraceStep key={message.id} message={message}/>
        : <TraceTextStep key={message.id} message={message}/>
      )}
    </div>}
  </section>;
}
