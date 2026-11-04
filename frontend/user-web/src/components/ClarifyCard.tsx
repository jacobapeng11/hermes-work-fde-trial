import { useEffect, useState } from "react";

import type { RuntimeClarifyAnswer, RuntimeClarifyRequest } from "../api/client";

type ClarifyCardProps = {
  request: RuntimeClarifyRequest;
  busy?: boolean;
  onAnswer: (answer: RuntimeClarifyAnswer) => void;
};

function countdownText(autoDefaultAt?: string | null): string | null {
  if (!autoDefaultAt) return null;
  const deadline = Date.parse(autoDefaultAt);
  if (Number.isNaN(deadline)) return null;
  const remaining = Math.max(0, Math.ceil((deadline - Date.now()) / 1_000));
  if (remaining <= 0) return "即将按默认值继续";
  return `${remaining} 秒后按默认值继续`;
}

function answerText(answer?: RuntimeClarifyAnswer | null): string {
  if (!answer) return "未提供回答";
  if (answer.kind === "multi") return answer.values?.join("、") ?? "";
  if (answer.kind === "skip") return "按默认值继续";
  return answer.text ?? "";
}

export function ClarifyCard({ request, busy = false, onAnswer }: ClarifyCardProps) {
  const [text, setText] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [countdown, setCountdown] = useState(() => countdownText(request.autoDefaultAt));
  const isWaiting = request.status === "waiting";

  useEffect(() => {
    if (!isWaiting || !request.autoDefaultAt) return;
    setCountdown(countdownText(request.autoDefaultAt));
    const timer = window.setInterval(() => {
      setCountdown(countdownText(request.autoDefaultAt));
    }, 1_000);
    return () => window.clearInterval(timer);
  }, [isWaiting, request.autoDefaultAt]);

  if (!isWaiting) {
    const sourceText = request.answerSource === "auto" ? "自动默认" : "用户回答";
    const statusText = request.status === "claimed"
      ? request.answerSource === "auto" ? "正在按默认值继续…" : "正在继续…"
      : request.status === "resume_failed"
        ? "继续失败，可重试"
        : request.status === "answered"
          ? request.resumeRuntimeRunId ? "已继续执行" : "继续结果缺失"
          : request.status === "timed_out"
            ? "该确认已超时，原任务未能自动继续。"
            : request.status === "canceled"
              ? "该确认已取消。"
              : "该确认已结束。";
    const adopted = ["claimed", "resume_failed", "answered"].includes(request.status);
    return <section className="clarifyCard clarifyCardResolved" aria-label="需要确认的信息" data-status={request.status}>
      <div className="clarifyCardHeading">
        <strong>{request.requestedBy ? `${request.requestedBy} 需要确认` : "需要确认"}</strong>
        <span>{statusText}</span>
      </div>
      <p>{request.question}</p>
      {adopted && <p className="clarifyAdoptedAnswer">{`已采纳${sourceText}：${answerText(request.answer)}`}</p>}
      {request.status === "resume_failed" && <div className="clarifyActions">
        <button type="button" disabled={busy} onClick={() => onAnswer(request.answer ?? { kind: "skip" })}>
          {busy ? "正在重试…" : "重试继续"}
        </button>
      </div>}
    </section>;
  }

  const choices = request.choices || [];
  const hasChoices = choices.length > 0;
  const canSubmit = hasChoices ? selected.length > 0 : text.trim().length > 0;
  const toggle = (value: string) => setSelected((current) => (
    current.includes(value) ? current.filter((item) => item !== value) : [...current, value]
  ));
  const submit = () => {
    if (!canSubmit || busy) return;
    if (!hasChoices) onAnswer({ kind: "text", text: text.trim() });
    else if (request.multiSelect) onAnswer({ kind: "multi", values: selected });
    else onAnswer({ kind: "single", text: selected[0] });
  };

  return <section className="clarifyCard" aria-label="需要你确认的信息" aria-busy={busy}>
    <div className="clarifyCardHeading">
      <strong>{request.requestedBy ? `${request.requestedBy} 需要你确认` : "需要你确认"}</strong>
      <span>{countdown ?? "回答后将继续执行任务"}</span>
    </div>
    <p>{request.question}</p>
    {hasChoices ? <div className="clarifyChoices" role={request.multiSelect ? "group" : "radiogroup"}>
      {choices.map((choice) => <label key={choice} className="clarifyChoice">
        <input
          type={request.multiSelect ? "checkbox" : "radio"}
          name={`clarify-${request.requestId}`}
          checked={selected.includes(choice)}
          disabled={busy}
          onChange={() => request.multiSelect ? toggle(choice) : setSelected([choice])}
        />
        <span>{choice}</span>
      </label>)}
    </div> : <textarea value={text} disabled={busy} onChange={(event) => setText(event.target.value)} placeholder="请输入你的回答" />}
    <div className="clarifyActions">
      <button type="button" className="secondary" disabled={busy} onClick={() => onAnswer({ kind: "skip" })}>按默认值继续</button>
      <button type="button" disabled={!canSubmit || busy} onClick={submit}>{busy ? "正在继续…" : "确认并继续"}</button>
    </div>
  </section>;
}
