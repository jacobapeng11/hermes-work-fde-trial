import { useEffect, useState } from "react";
import type { RuntimeSessionSummary, ScheduledTask, ScheduledTaskCommand, ScheduledTaskScheduleConfig } from "@hermes-cloud/schemas";
import { Button, Flex, Form, Input, Modal, Select, Tag } from "antd";
import { usePlatformTheme } from "../../theme/platform-theme";
import { ScheduleFields, onceRunAtHasAllowedYear } from "./ScheduleFields";
import { isScheduledTaskSessionEligible, isScheduledTaskSessionVisible, sessionTitle, agentLabel } from "./SessionCombobox";

interface ScheduledTaskDialogProps {
  task?: ScheduledTask | null;
  sessions: RuntimeSessionSummary[];
  onClose: () => void;
  onSubmit: (payload: ScheduledTaskCommand) => void | Promise<void>;
  busy?: boolean;
}

interface FormState extends ScheduledTaskCommand {}

const NEW_SCHEDULE: ScheduledTaskScheduleConfig = { type: "once", runAt: "" };

function initialState(task: ScheduledTask | null | undefined, sessions: RuntimeSessionSummary[]): FormState {
  return task ? {
    taskName: task.taskName,
    prompt: task.prompt,
    targetRuntimeSessionId: task.targetRuntimeSessionId,
    scheduleConfig: task.scheduleConfig
  } : {
    taskName: "",
    prompt: "",
    targetRuntimeSessionId: sessions.find(isScheduledTaskSessionEligible)?.runtimeSessionId || "",
    scheduleConfig: NEW_SCHEDULE
  };
}

// 输出对话 Select 选项：会话种类 Tag（专家紫 / 企微绿 / 个人蓝）+ 标题；
// 不可用会话禁用（对齐原 SessionCombobox 的可见性/可选用规则）。
function sessionOption(session: RuntimeSessionSummary) {
  const label = agentLabel(session);
  const shortLabel = session.sourceType === "wecom" ? "企微" : session.agentProfileKind === "expert" ? "专家" : "个人";
  return {
    value: session.runtimeSessionId,
    disabled: !isScheduledTaskSessionEligible(session),
    label: <span className="task-session-option">
      <Tag className="task-session-tag" data-kind={session.sourceType === "wecom" ? "wecom" : session.agentProfileKind === "expert" ? "expert" : "default"} bordered={false}>{label}</Tag>
      <span className="task-session-option-title">{sessionTitle(session)}</span>
      {session.status !== "active" && <span className="task-session-option-state">（不可用）</span>}
    </span>,
    title: `${shortLabel} ${sessionTitle(session)}${session.status === "active" ? "" : "（不可用）"}`
  };
}

export function ScheduledTaskDialog({ task = null, sessions, busy = false, onClose, onSubmit }: ScheduledTaskDialogProps) {
  const [form, setForm] = useState<FormState>(() => initialState(task, sessions));
  const [errors, setErrors] = useState<string[]>([]);
  const [scheduleTouched, setScheduleTouched] = useState(false);
  const { theme } = usePlatformTheme();
  const editing = Boolean(task);

  useEffect(() => {
    setForm(initialState(task, sessions));
    setErrors([]);
    setScheduleTouched(false);
  // The task identity is the reset boundary.  Session lists can refresh while
  // the dialog is open and must not discard text the user is entering.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [task]);

  const submit = async () => {
    const nextErrors: string[] = [];
    const target = sessions.find((session) => session.runtimeSessionId === form.targetRuntimeSessionId);
    if (!form.taskName.trim()) nextErrors.push("请输入任务名称");
    if (!form.prompt.trim()) nextErrors.push("请输入任务指令");
    if (!target || !isScheduledTaskSessionEligible(target)) nextErrors.push("请选择仍可用的输出对话");
    if (form.scheduleConfig.type === "once" && (!form.scheduleConfig.runAt || new Date(form.scheduleConfig.runAt).getTime() <= Date.now())) nextErrors.push("一次性任务必须安排在未来时间");
    if (form.scheduleConfig.type === "once" && form.scheduleConfig.runAt && !onceRunAtHasAllowedYear(form.scheduleConfig.runAt)) nextErrors.push("一次性任务只能安排在本年或明年");
    if (form.scheduleConfig.type === "interval") {
      const supported = form.scheduleConfig.unit === "minutes" ? [5, 10, 15, 30, 45] : [1, 2, 3, 4, 6, 8, 12];
      if (!supported.includes(form.scheduleConfig.value)) nextErrors.push("请选择支持的执行间隔");
    }
    if (task?.status === "completed" && !scheduleTouched) nextErrors.push("已完成任务需要重新安排未来执行时间或周期");
    setErrors(nextErrors);
    if (nextErrors.length) return;
    await onSubmit({ ...form, taskName: form.taskName.trim(), prompt: form.prompt.trim() });
  };

  return <Modal
    open
    title={<span className="create-task-modal-title">{editing ? "编辑定时任务" : "新建定时任务"}</span>}
    centered
    width={540}
    rootClassName={theme === "dark" ? "scheduleAntDialog scheduleAntDialogDark" : "scheduleAntDialog"}
    className="create-task-modal"
    styles={{ header: { padding: 0 }, body: { padding: 0 } }}
    closable={!busy}
    maskClosable={!busy}
    keyboard={!busy}
    onCancel={onClose}
    footer={null}
  >
    <div className="create-task-body">
      <p className="create-task-subtitle">Hermes 会按照计划，在指定输出对话中执行你的任务。</p>
      <Form layout="vertical" className="create-task-form">
        <Form.Item label="任务名称" className="create-task-field">
          <Input
            aria-label="任务名称"
            placeholder="例如：每日工作总结"
            maxLength={100}
            value={form.taskName}
            disabled={busy}
            onChange={(event) => setForm({ ...form, taskName: event.target.value })}
          />
        </Form.Item>
        <Form.Item label="输出对话" className="create-task-field">
          <Select
            aria-label="输出对话"
            virtual={false}
            placeholder="请选择执行对话"
            value={form.targetRuntimeSessionId || undefined}
            disabled={busy}
            options={sessions.filter(isScheduledTaskSessionVisible).map(sessionOption)}
            onChange={(targetRuntimeSessionId) => setForm({ ...form, targetRuntimeSessionId })}
          />
        </Form.Item>
        <Form.Item className="create-task-field">
          <ScheduleFields scheduleConfig={form.scheduleConfig} disabled={busy} onChange={(scheduleConfig) => { setScheduleTouched(true); setForm({ ...form, scheduleConfig }); }}/>
        </Form.Item>
        <Form.Item label="任务指令" className="create-task-field create-task-last-field">
          <Input.TextArea
            aria-label="任务指令"
            rows={4}
            maxLength={20000}
            placeholder="告诉 Hermes 需要按时完成什么…"
            value={form.prompt}
            disabled={busy}
            onChange={(event) => setForm({ ...form, prompt: event.target.value })}
          />
          <span className="create-task-counter-hint">支持多行内容，最多 20,000 字符。</span>
        </Form.Item>
        {errors.length > 0 && <div role="alert" className="create-task-errors">{errors.map((error) => <p key={error}>{error}</p>)}</div>}
      </Form>
    </div>
    <div className="create-task-footer">
      <span className="create-task-footer-tz">时间均按北京时间（Asia/Shanghai）执行</span>
      <Flex gap={12} align="center">
        <Button autoInsertSpace={false} onClick={onClose} disabled={busy}>取消</Button>
        <Button type="primary" autoInsertSpace={false} disabled={busy} onClick={() => void submit()}>{editing ? "保存修改" : "创建任务"}</Button>
      </Flex>
    </div>
  </Modal>;
}
