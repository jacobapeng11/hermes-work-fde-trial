import { Check, ChevronDown } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import type { RuntimeSessionSummary } from "@hermes-cloud/schemas";

interface SessionComboboxProps {
  sessions: RuntimeSessionSummary[];
  value: string;
  onChange: (runtimeSessionId: string) => void;
  disabled?: boolean;
}

export function isScheduledTaskSessionEligible(session: RuntimeSessionSummary): boolean {
  return session.status === "active"
    && (session.sourceType !== "wecom" || session.scheduledTaskEligible === true);
}

export function isScheduledTaskSessionVisible(session: RuntimeSessionSummary): boolean {
  return session.sourceType !== "wecom" || session.scheduledTaskEligible === true;
}

export function sessionTitle(session: RuntimeSessionSummary): string {
  return session.title?.trim() || "未命名对话";
}

export function agentLabel(session: RuntimeSessionSummary): string {
  if (session.sourceType === "wecom") return "企微";
  return session.agentProfileKind === "expert"
    ? `专家 · ${session.agentDisplayName?.trim() || "专家"}`
    : "个人";
}

function AgentBadge({ session }: { session: RuntimeSessionSummary }) {
  const kind = session.sourceType === "wecom"
    ? "wecom"
    : session.agentProfileKind === "expert" ? "expert" : "default";
  return <span className="sessionAgentBadge" data-agent-kind={kind}>{agentLabel(session)}</span>;
}

function SessionLabel({ session }: { session: RuntimeSessionSummary }) {
  return <span className="sessionComboboxLabel">
    <AgentBadge session={session}/>
    <span className="sessionComboboxTitle" title={sessionTitle(session)}>{sessionTitle(session)}</span>
    {session.status !== "active" && <span className="sessionComboboxUnavailable">（不可用）</span>}
  </span>;
}

export function SessionCombobox({ sessions, value, onChange, disabled = false }: SessionComboboxProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const visibleSessions = sessions.filter(isScheduledTaskSessionVisible);
  const selected = visibleSessions.find((session) => session.runtimeSessionId === value) ?? null;
  const unavailable = disabled || visibleSessions.length === 0;

  const close = (restoreFocus = false) => {
    setOpen(false);
    if (restoreFocus) triggerRef.current?.focus();
  };

  useEffect(() => {
    if (!open) return;
    const handleOutsidePointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) close();
    };
    document.addEventListener("pointerdown", handleOutsidePointer);
    return () => document.removeEventListener("pointerdown", handleOutsidePointer);
  }, [open]);

  useEffect(() => {
    if (unavailable && open) close();
  }, [open, unavailable]);

  const selectableOptions = () => Array.from(
    rootRef.current?.querySelectorAll<HTMLButtonElement>('[role="option"]:not(:disabled)') ?? []
  );

  const focusOption = (direction: "first" | "last") => {
    const options = selectableOptions();
    if (direction === "first") options[0]?.focus();
    else options[options.length - 1]?.focus();
  };

  const handleListKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      close(true);
      return;
    }
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const options = selectableOptions();
    if (options.length === 0) return;
    event.preventDefault();
    const currentIndex = options.findIndex((option) => option === document.activeElement);
    const nextIndex = event.key === "ArrowDown"
      ? Math.min(currentIndex + 1, options.length - 1)
      : currentIndex < 0 ? options.length - 1 : Math.max(currentIndex - 1, 0);
    options[nextIndex]?.focus();
  };

  return <div className="sessionCombobox" ref={rootRef}>
    <button
      ref={triggerRef}
      className="sessionComboboxTrigger"
      type="button"
      id="scheduled-task-session"
      aria-label="输出对话"
      aria-haspopup="listbox"
      aria-controls="scheduled-task-session-listbox"
      aria-expanded={open}
      disabled={unavailable}
      onClick={() => setOpen((current) => !current)}
      onKeyDown={(event) => {
        if (open || (event.key !== "ArrowDown" && event.key !== "ArrowUp")) return;
        event.preventDefault();
        setOpen(true);
        window.requestAnimationFrame(() => focusOption(event.key === "ArrowDown" ? "first" : "last"));
      }}
    >
      {selected ? <SessionLabel session={selected}/> : <span className="sessionComboboxPlaceholder">请选择输出对话</span>}
      <ChevronDown size={16} aria-hidden="true"/>
    </button>
    {open && <div className="sessionComboboxDropdown" role="listbox" id="scheduled-task-session-listbox" aria-label="输出对话列表" onKeyDown={handleListKeyDown}>
      {visibleSessions.map((session) => <button
        className="sessionComboboxOption"
        type="button"
        role="option"
        aria-selected={session.runtimeSessionId === value}
        aria-label={`${agentLabel(session)} ${sessionTitle(session)}${session.status === "active" ? "" : "（不可用）"}`}
        key={session.runtimeSessionId}
        disabled={!isScheduledTaskSessionEligible(session)}
        onClick={() => {
          onChange(session.runtimeSessionId);
          close(true);
        }}
      >
        <SessionLabel session={session}/>
        {session.runtimeSessionId === value && <Check size={15} aria-label="已选择"/>}
      </button>)}
    </div>}
  </div>;
}
