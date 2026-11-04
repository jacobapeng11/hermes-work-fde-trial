import { useEffect, useId, useRef, useState } from "react";
import {
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  Database,
  File,
  FileSearch,
  FileSpreadsheet,
  Globe2,
  LoaderCircle,
  TerminalSquare,
  Users,
  Wrench
} from "lucide-react";

import type { ToolMessage } from "../pages/chatRunProjection";
import { detailForTool, headerNameForTool, presentationForTool, statusText, type ToolCategory } from "../toolPresentation";

interface ToolActivityGroupProps {
  id: string;
  messages: ToolMessage[];
}

function CategoryIcon({ category }: { category: ToolCategory }) {
  const Icon = category === "search" ? FileSearch
    : category === "file" ? File
      : category === "sheet" ? FileSpreadsheet
        : category === "web" ? Globe2
          : category === "knowledge" ? Database
            : category === "shell" ? TerminalSquare
              : category === "delegate" ? Users
                : Wrench;
  return <Icon className="toolItemIcon" size={15} aria-hidden="true"/>;
}

function StatusIcon({ status }: { status: ToolMessage["status"] }) {
  if (status === "running") return <LoaderCircle className="toolRunningIcon" size={15} aria-hidden="true"/>;
  if (status === "failed") return <CircleAlert size={15} aria-hidden="true"/>;
  return <CheckCircle2 size={15} aria-hidden="true"/>;
}

export function ToolActivityItem({ message }: { message: ToolMessage }) {
  const tool = presentationForTool(message.name);
  const detail = detailForTool(message);
  return <article className="toolActivityItem" data-status={message.status}>
    <div className="toolActivityItemMain">
      <CategoryIcon category={tool.category}/>
      <div className="toolActivityItemContent">
        <div className="toolActivityItemTitle">
          <span className="toolStatusIcon"><StatusIcon status={message.status}/></span>
          <strong>{statusText(message, tool)}</strong>
        </div>
        <span className="toolActivityDescription">{tool.description}</span>
        {detail.input && <div className="toolActivityReadableDetail"><span>{tool.inputLabel}</span><pre>{detail.input}</pre></div>}
        {detail.context && <div className="toolActivityReadableDetail"><span>查看内容</span><pre>{detail.context}</pre></div>}
        {detail.result && <div className="toolActivityReadableDetail"><span>执行结果</span><pre>{detail.result}</pre></div>}
        {detail.summary && !detail.result && <span className="toolActivitySummary">{detail.summary}</span>}
        {message.status === "failed" && <span className="toolFailureHint">该操作未完成，数字人仍可能通过其他方式继续任务</span>}
      </div>
    </div>
    {detail.formatted && <details className="toolTechnicalDetails">
        <summary>技术详情</summary>
        <pre className="toolDetailBody">{detail.formatted}</pre>
        <dl>
          <dt>工具标识</dt><dd>{message.name}</dd>
          <dt>调用标识</dt><dd>{message.providerCallId ?? message.callId}</dd>
          <dt>关联方式</dt><dd>{message.hasStableCallId ? "Runtime 稳定标识" : "平台兼容关联"}</dd>
          {message.ttftMs !== undefined && <><dt>首次可见 Agent 内容</dt><dd>TTFT {(message.ttftMs / 1000).toFixed(1)}s</dd></>}
        </dl>
      </details>
    }
  </article>;
}

function summaryFor(messages: ToolMessage[]) {
  const running = messages.filter((message) => message.status === "running").length;
  const completed = messages.filter((message) => message.status === "completed").length;
  const failed = messages.filter((message) => message.status === "failed").length;
  const names = operationNames(messages);
  const named = (text: string) => names ? `${text} · ${names}` : text;
  if (running > 0) {
    return {
      status: "running" as const,
      text: named(`正在执行 ${messages.length} 个操作${failed ? ` · ${failed} 个未完成` : ""}`),
      defaultOpen: true
    };
  }
  if (failed > 0) {
    return {
      status: "failed" as const,
      text: named(completed > 0 ? `${completed} 个操作已完成，${failed} 个未完成` : `${failed} 个操作未完成`),
      defaultOpen: true
    };
  }
  return { status: "completed" as const, text: named(`已完成 ${completed} 个操作`), defaultOpen: false };
}

function operationNames(messages: ToolMessage[]): string {
  const names = [...new Set(messages.map(headerNameForTool))];
  const shown = names.slice(0, 3);
  if (shown.length === 0) return "";
  return `${shown.join("、")}${names.length > shown.length ? ` +${names.length - shown.length}` : ""}`;
}

export function ToolActivityGroup({ id, messages }: ToolActivityGroupProps) {
  const regionId = useId();
  const summary = summaryFor(messages);
  const [open, setOpen] = useState(summary.defaultOpen);
  const manuallyToggled = useRef(false);
  const previousId = useRef(id);

  useEffect(() => {
    if (previousId.current !== id) {
      previousId.current = id;
      manuallyToggled.current = false;
      setOpen(summary.defaultOpen);
    } else if (!manuallyToggled.current) {
      setOpen(summary.defaultOpen);
    }
  }, [id, summary.defaultOpen]);

  const toggle = () => {
    manuallyToggled.current = true;
    setOpen((current) => !current);
  };

  return <section className="toolActivityGroup" data-message-id={id} data-kind="tool-group" data-status={summary.status} data-expanded={open ? "true" : "false"}>
    <button
      type="button"
      className="toolActivityHeader"
      aria-expanded={open}
      aria-controls={regionId}
      onClick={toggle}
    >
      <span className="toolGroupStatusIcon"><StatusIcon status={summary.status}/></span>
      <span className="toolActivityTitle" aria-live="polite">{summary.text}</span>
      <ChevronRight className="toolActivityChevron" size={16} aria-hidden="true"/>
    </button>
    {open && <div className="toolActivityList" id={regionId}>
      {messages.map((message) => <ToolActivityItem key={message.id} message={message}/>)}
    </div>}
  </section>;
}
