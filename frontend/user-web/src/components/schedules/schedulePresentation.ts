const BEIJING_TIME_ZONE = "Asia/Shanghai";

const scheduledTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: BEIJING_TIME_ZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false
});

const STATUS_LABELS = {
  active: "已启用",
  paused: "已暂停",
  completed: "已完成"
} as const;

const ERROR_MESSAGES: Record<string, string> = {
  SESSION_DELETED: "输出对话已删除，请编辑并重新选择对话"
};

export function formatScheduledTime(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return scheduledTimeFormatter.format(date);
}

export function scheduleStatusLabel(status: "active" | "paused" | "completed", isRunning: boolean): string {
  const label = STATUS_LABELS[status];
  return isRunning ? `${label} · 执行中` : label;
}

export function scheduledErrorMessage(code: string | null, fallback: string | null): string | null {
  if (code && ERROR_MESSAGES[code]) return ERROR_MESSAGES[code];
  return fallback || null;
}
