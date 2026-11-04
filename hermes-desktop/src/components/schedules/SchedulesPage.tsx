import { useCallback, useEffect, useRef, useState } from "react";
import type { RuntimeSessionSummary, ScheduledTask, ScheduledTaskCommand } from "@hermes-cloud/schemas";
import { Alert, Button, Empty } from "antd";
import { ClockCircleOutlined, PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import { userApi } from "../../api/client";
import { ScheduledTaskCard } from "./ScheduledTaskCard";
import { ScheduledTaskDialog } from "./ScheduledTaskDialog";

const IDLE_REFRESH_MS = 10_000;
const RUNNING_REFRESH_MS = 3_000;

interface SchedulesPageProps {
  runtimeWorkspaceId: number | null;
  sessions: RuntimeSessionSummary[];
  onOpenSession: (runtimeSessionId: string, taskId?: number, acceptedAt?: string, taskName?: string, prompt?: string) => void;
  onRetryWorkspace?: () => void;
}

export function SchedulesPage({ runtimeWorkspaceId, sessions, onOpenSession, onRetryWorkspace }: SchedulesPageProps) {
  const [tasks, setTasks] = useState<ScheduledTask[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [dialogTask, setDialogTask] = useState<ScheduledTask | null | undefined>(undefined);
  const [busyTaskId, setBusyTaskId] = useState<number | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const requestRef = useRef(0);

  const refresh = useCallback(async (preserveScroll = false) => {
    if (runtimeWorkspaceId === null) return;
    const requestId = ++requestRef.current;
    const scrollTop = preserveScroll ? listRef.current?.scrollTop ?? 0 : 0;
    setLoading(true);
    setError(null);
    try {
      const next = await userApi.listScheduledTasks(runtimeWorkspaceId);
      if (requestRef.current !== requestId) return;
      setTasks(next);
      if (preserveScroll && listRef.current) listRef.current.scrollTop = scrollTop;
    } catch (reason) {
      if (requestRef.current === requestId) setError(reason instanceof Error ? reason.message : "读取定时任务失败");
    } finally {
      if (requestRef.current === requestId) setLoading(false);
    }
  }, [runtimeWorkspaceId]);

  useEffect(() => {
    setTasks([]);
    setNotice(null);
    if (runtimeWorkspaceId !== null) void refresh();
  }, [runtimeWorkspaceId, refresh]);

  useEffect(() => {
    if (runtimeWorkspaceId === null) return undefined;
    const delay = tasks.some((task) => task.isRunning) ? RUNNING_REFRESH_MS : IDLE_REFRESH_MS;
    const timer = window.setInterval(() => void refresh(true), delay);
    return () => window.clearInterval(timer);
  }, [runtimeWorkspaceId, tasks, refresh]);

  const mutate = async (taskId: number, action: () => Promise<unknown>, successMessage?: string) => {
    if (runtimeWorkspaceId === null) return;
    setBusyTaskId(taskId);
    setError(null);
    setNotice(null);
    try {
      await action();
      if (successMessage) setNotice(successMessage);
      await refresh(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "更新定时任务失败");
    } finally {
      setBusyTaskId(null);
    }
  };

  const submit = async (payload: ScheduledTaskCommand) => {
    if (runtimeWorkspaceId === null) return;
    const editing = dialogTask && typeof dialogTask === "object";
    const taskId = editing ? dialogTask.taskId : -1;
    setBusyTaskId(taskId);
    setError(null);
    try {
      if (editing) await userApi.updateScheduledTask(runtimeWorkspaceId, taskId, payload);
      else await userApi.createScheduledTask(runtimeWorkspaceId, payload);
      setDialogTask(undefined);
      setNotice(editing ? "任务已更新" : "任务已创建");
      await refresh(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存定时任务失败");
    } finally {
      setBusyTaskId(null);
    }
  };

  const trigger = async (task: ScheduledTask) => {
    if (runtimeWorkspaceId === null) return;
    setBusyTaskId(task.taskId);
    setError(null);
    setNotice(null);
    try {
      const accepted = await userApi.triggerScheduledTask(runtimeWorkspaceId, task.taskId);
      onOpenSession(accepted.targetRuntimeSessionId, accepted.taskId, accepted.acceptedAt, task.taskName, task.prompt);
      setNotice("任务已提交");
      await refresh(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "更新定时任务失败");
    } finally {
      setBusyTaskId(null);
    }
  };

  if (runtimeWorkspaceId === null) return <section className="schedulesPage schedulesEmptyView" aria-label="定时任务页面">
    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前运行环境尚未连接">
      <p className="schedulesEmptyHint">连接后即可管理定时任务。</p>
      {onRetryWorkspace && <Button onClick={onRetryWorkspace}>重试连接</Button>}
    </Empty>
  </section>;

  return <section className="schedulesPage" aria-label="定时任务页面">
    <div className="schedulesPanel">
      <header className="schedulesPanelHeader">
        <div className="schedulesPanelHeaderMain">
          <span className="schedulesPanelHeaderIcon"><ClockCircleOutlined aria-hidden/></span>
          <div className="schedulesPanelHeaderText">
            <h2 className="schedulesPanelHeaderTitle">定时任务</h2>
            <p className="schedulesPanelHeaderSubtitle">让 Hermes 按计划在指定对话中自动完成任务</p>
          </div>
        </div>
        <div className="schedulesPanelHeaderActions">
          <Button title="刷新任务" aria-label="刷新任务" icon={<ReloadOutlined aria-hidden/>} onClick={() => void refresh()} autoInsertSpace={false}>刷新</Button>
          <Button type="primary" autoInsertSpace={false} icon={<PlusOutlined aria-hidden/>} onClick={() => setDialogTask(null)}>新建任务</Button>
        </div>
      </header>
      {notice && <Alert role="status" className="schedulesAlert" type="success" showIcon message={notice}/>}
      {error && <Alert role="alert" className="schedulesAlert" type="error" showIcon message={error}/>}
      <div className="scheduledTaskList" ref={listRef} aria-busy={loading}>
      {loading && tasks.length === 0 ? <p className="schedulesLoading">正在读取定时任务…</p> : tasks.length === 0 ? <div className="schedulesEmpty"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有定时任务"><p className="schedulesEmptyHint">创建任务后，Hermes 会按计划在指定对话中执行。</p><Button type="primary" autoInsertSpace={false} icon={<PlusOutlined aria-hidden/>} onClick={() => setDialogTask(null)}>创建第一个任务</Button></Empty></div> : tasks.map((task) => <ScheduledTaskCard key={task.taskId} task={task} busy={busyTaskId === task.taskId}
        onEdit={(next) => setDialogTask(next)} onOpenSession={(id) => onOpenSession(id, task.taskId)}
        onPause={(next) => void mutate(next.taskId, () => userApi.pauseScheduledTask(runtimeWorkspaceId, next.taskId), "任务已暂停")}
        onResume={(next) => void mutate(next.taskId, () => userApi.resumeScheduledTask(runtimeWorkspaceId, next.taskId), "任务已恢复")}
        onTrigger={(next) => void trigger(next)}
        onDelete={(next) => void mutate(next.taskId, () => userApi.deleteScheduledTask(runtimeWorkspaceId, next.taskId), "任务已删除")}/>) }
      </div>
    </div>
    {dialogTask !== undefined && <ScheduledTaskDialog task={dialogTask} sessions={sessions} busy={busyTaskId !== null} onClose={() => setDialogTask(undefined)} onSubmit={submit}/>}
  </section>;
}
