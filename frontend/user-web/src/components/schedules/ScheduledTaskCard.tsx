import { useState } from "react";
import { Button, Switch, Tag, Tooltip } from "antd";
import {
  CaretRightOutlined,
  ClockCircleOutlined,
  DeleteOutlined,
  DownOutlined,
  EditOutlined,
  MessageOutlined,
  ThunderboltOutlined,
  UpOutlined
} from "@ant-design/icons";
import type { ScheduledTask } from "@hermes-cloud/schemas";
import { formatScheduledTime, scheduledErrorMessage, scheduleStatusLabel } from "./schedulePresentation";

interface ScheduledTaskCardProps {
  task: ScheduledTask;
  onEdit: (task: ScheduledTask) => void;
  onPause: (task: ScheduledTask) => void;
  onResume: (task: ScheduledTask) => void;
  onTrigger: (task: ScheduledTask) => void;
  onDelete?: (task: ScheduledTask) => void;
  onOpenSession?: (runtimeSessionId: string) => void;
  busy?: boolean;
}

function runStatusLabel(status: ScheduledTask["lastRunStatus"]): string {
  if (status === "success") return "成功";
  if (status === "failed") return "失败";
  return "—";
}

// 定时任务卡（按用户参考定版）：顶行 = 名称 + 状态 Tag + hover 管理图标组 + Switch 启停开关；
// 中部 = 单行截断指令（点击卡片主体展开详情）；footer = 频率/目标对话/上次执行 + 立即执行。
// Switch 的 aria-label 保留「暂停任务/恢复任务」语义（原按钮契约迁移）。
export function ScheduledTaskCard({ task, onEdit, onPause, onResume, onTrigger, onDelete, onOpenSession, busy = false }: ScheduledTaskCardProps) {
  const sessionAvailable = !task.targetSessionStatus || task.targetSessionStatus === "active";
  const errorMessage = scheduledErrorMessage(task.lastErrorCode || null, task.lastError ? "最近一次执行失败，请稍后重试" : null);
  const targetTitle = task.targetSessionTitle || "未命名对话";
  const [expanded, setExpanded] = useState(() => !sessionAvailable || Boolean(errorMessage));
  const toggleExpanded = () => setExpanded((current) => !current);
  const running = task.status === "active";

  return <article className={`task-card-item${expanded ? " is-expanded" : ""}`} aria-label={`定时任务 ${task.taskName}`}>
    <div className="task-card-top">
      <span className="task-card-title-row">
        <h3 className="task-card-name" title={task.taskName}>{task.taskName}</h3>
        <Tag className={`task-card-status scheduledTaskStatus-${task.status}`} bordered={false} color={running ? "success" : "default"}>
          {scheduleStatusLabel(task.status, task.isRunning)}
        </Tag>
      </span>
      <span className="task-card-top-actions">
        <span className="task-card-hover-actions">
          <Tooltip title="编辑任务">
            <Button type="text" size="small" aria-label="编辑任务" icon={<EditOutlined aria-hidden/>} disabled={busy} onClick={() => onEdit(task)}/>
          </Tooltip>
          {onDelete && (
            <Tooltip title="删除任务">
              <Button type="text" size="small" className="task-card-danger" aria-label="删除任务" icon={<DeleteOutlined aria-hidden/>} disabled={busy} onClick={() => onDelete(task)}/>
            </Tooltip>
          )}
        </span>
        <Tooltip title={running ? "暂停任务" : "恢复任务"}>
          <Switch
            size="small"
            checked={running}
            loading={busy && task.isRunning}
            disabled={busy}
            aria-label={running ? "暂停任务" : "恢复任务"}
            onChange={(checked) => (checked ? onResume(task) : onPause(task))}
          />
        </Tooltip>
      </span>
    </div>
    <button type="button" className="task-card-main" onClick={toggleExpanded} aria-expanded={expanded} title={task.prompt}>
      {task.prompt}
    </button>
    <footer className="task-card-footer">
      <span className="task-card-meta">
        <span title={task.scheduleSummary}><ClockCircleOutlined aria-hidden/>{task.scheduleSummary}</span>
        {onOpenSession && sessionAvailable
          ? <Button className="task-card-session" type="link" size="small" aria-label={`打开对话 ${targetTitle}`} onClick={(event) => { event.stopPropagation(); onOpenSession(task.targetRuntimeSessionId); }}><MessageOutlined aria-hidden/>{targetTitle}</Button>
          : <span title={targetTitle}><MessageOutlined aria-hidden/>{targetTitle}</span>}
      </span>
      <span className="task-card-footer-side">
        <span className="task-card-last-run">上次执行 {formatScheduledTime(task.lastRunAt || null)}</span>
        <Button
          className="task-card-trigger"
          size="small"
          autoInsertSpace={false}
          aria-label="立即执行"
          icon={<ThunderboltOutlined aria-hidden/>}
          disabled={busy || !sessionAvailable}
          onClick={() => onTrigger(task)}
        >{task.isRunning ? "执行中" : "立即执行"}</Button>
        <Button
          type="text"
          size="small"
          className="task-card-expand"
          aria-label={expanded ? "收起详情" : "展开详情"}
          aria-expanded={expanded}
          icon={expanded ? <UpOutlined aria-hidden/> : <DownOutlined aria-hidden/>}
          onClick={toggleExpanded}
        >{expanded ? "收起详情" : "展开详情"}</Button>
      </span>
    </footer>
    {expanded && <div className="task-card-details">
      <p className="task-card-detail-prompt">{task.prompt}</p>
      <dl className="task-card-detail-meta">
        <div><dt>上次结果</dt><dd>{runStatusLabel(task.lastRunStatus)}</dd></div>
        <div><dt>下次执行</dt><dd>{formatScheduledTime(task.nextRunAt || null)}</dd></div>
        <div><dt>执行次数</dt><dd>{task.runCount}</dd></div>
        {task.lastRuntimeRunId && <div><dt>最近运行</dt><dd title={task.lastRuntimeRunId}>{task.lastRuntimeRunId}</dd></div>}
      </dl>
      {!sessionAvailable && <p className="scheduledTaskRecovery" role="status">输出对话不可用，请编辑并重新选择对话</p>}
      {errorMessage && <p className="scheduledTaskError" role="alert">{errorMessage}</p>}
    </div>}
  </article>;
}
