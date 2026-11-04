import { useMemo } from "react";
import { DatePicker, Segmented, Select, TimePicker } from "antd";
import { GlobalOutlined } from "@ant-design/icons";
import dayjs, { type Dayjs } from "dayjs";
import type { ScheduledTaskScheduleConfig } from "@hermes-cloud/schemas";

export type ScheduleKind = "once" | "interval" | "daily" | "weekly";

const INTERVAL_VALUES = {
  minutes: [5, 10, 15, 30, 45],
  hours: [1, 2, 3, 4, 6, 8, 12]
} as const;
const WEEKDAY_OPTIONS = [
  { value: 1, label: "周一" }, { value: 2, label: "周二" }, { value: 3, label: "周三" },
  { value: 4, label: "周四" }, { value: 5, label: "周五" }, { value: 6, label: "周六" },
  { value: 7, label: "周日" }
];

interface ScheduleFieldsProps {
  scheduleConfig: ScheduledTaskScheduleConfig;
  onChange: (scheduleConfig: ScheduledTaskScheduleConfig) => void;
  disabled?: boolean;
}

function scheduleKind(config: ScheduledTaskScheduleConfig): ScheduleKind {
  if (config.type === "once" || config.type === "interval") return config.type;
  return config.preset;
}

// 北京时间 ⇄ 本地 Dayjs 的换算（服务端契约固定 Asia/Shanghai）。
function asBeijingDateTimeInput(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23"
  }).formatToParts(date).reduce<Record<string, string>>((result, part) => ({ ...result, [part.type]: part.value }), {});
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

export function beijingDateTimeToIso(value: string): string {
  if (!value) return "";
  const normalized = value.includes("T") ? value : value.replace(" ", "T");
  const date = new Date(`${normalized}:00+08:00`);
  return Number.isNaN(date.getTime()) ? "" : date.toISOString();
}

export function onceRunAtHasAllowedYear(value: string, now = new Date()): boolean {
  const localValue = asBeijingDateTimeInput(value);
  if (!localValue) return false;
  const year = Number(localValue.slice(0, 4));
  const current = Number(new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric" }).format(now));
  return [current, current + 1].includes(year);
}

const DATE_TIME_FORMAT = "YYYY-MM-DD HH:mm";

export function ScheduleFields({ scheduleConfig, onChange, disabled = false }: ScheduleFieldsProps) {
  const kind = scheduleKind(scheduleConfig);
  const updateKind = (next: ScheduleKind) => {
    if (next === "once") onChange({ type: "once", runAt: "" });
    if (next === "interval") onChange({ type: "interval", unit: "hours", value: 1 });
    if (next === "daily") onChange({ type: "cron", preset: "daily", time: "09:00" });
    if (next === "weekly") onChange({ type: "cron", preset: "weekly", weekday: 1, time: "09:00" });
  };

  // 一次性执行：仅允许本年与明年（北京时间口径），其余年份在面板中禁选。
  const allowedYears = useMemo(() => {
    const current = Number(new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric" }).format(new Date()));
    return [current, current + 1];
  }, []);

  const onceValue = scheduleConfig.type === "once" && scheduleConfig.runAt
    ? dayjs(asBeijingDateTimeInput(scheduleConfig.runAt), DATE_TIME_FORMAT)
    : null;
  const dailyValue = scheduleConfig.type === "cron" && scheduleConfig.preset === "daily"
    ? dayjs(scheduleConfig.time, "HH:mm")
    : null;
  const weeklyValue = scheduleConfig.type === "cron" && scheduleConfig.preset === "weekly"
    ? dayjs(scheduleConfig.time, "HH:mm")
    : null;

  return <div className="scheduleFields">
    <div className="scheduleFieldsHeading">
      <label className="scheduleFieldsTitle">什么时候执行？</label>
      <span className="scheduleTimezoneBadge"><GlobalOutlined aria-hidden/> Asia/Shanghai</span>
    </div>
    <Segmented
      block
      className="scheduleKindSegmented"
      aria-label="计划类型"
      value={kind}
      disabled={disabled}
      onChange={(value) => updateKind(value as ScheduleKind)}
      options={[
        { value: "once", label: "一次" },
        { value: "interval", label: "间隔" },
        { value: "daily", label: "每天" },
        { value: "weekly", label: "每周" }
      ]}
    />
    <div className="scheduleDynamicArea">
      {scheduleConfig.type === "once" && <div className="scheduleDynamicField">
        <label className="scheduleDynamicLabel" htmlFor="scheduled-task-run-at">指定执行时间</label>
        <DatePicker
          id="scheduled-task-run-at"
          showTime={{ format: "HH:mm" }}
          format={DATE_TIME_FORMAT}
          style={{ width: "100%" }}
          placeholder="请选择日期和时间"
          disabled={disabled}
          value={onceValue && onceValue.isValid() ? onceValue : null}
          disabledDate={(current: Dayjs) => !allowedYears.includes(current.year())}
          onChange={(value) => onChange({ type: "once", runAt: value ? beijingDateTimeToIso(value.format("YYYY-MM-DDTHH:mm")) : "" })}
        />
      </div>}
      {scheduleConfig.type === "interval" && <div className="scheduleDynamicRow">
        <div className="scheduleDynamicField">
          <label className="scheduleDynamicLabel" htmlFor="scheduled-task-interval-value">间隔数值</label>
          <Select
            id="scheduled-task-interval-value"
            aria-label="间隔数值"
            virtual={false}
            disabled={disabled}
            value={scheduleConfig.value}
            options={INTERVAL_VALUES[scheduleConfig.unit].map((value) => ({ value, label: String(value) }))}
            onChange={(value) => onChange({ ...scheduleConfig, value })}
          />
        </div>
        <div className="scheduleDynamicField">
          <label className="scheduleDynamicLabel" htmlFor="scheduled-task-interval-unit">间隔单位</label>
          <Select
            id="scheduled-task-interval-unit"
            aria-label="间隔单位"
            virtual={false}
            disabled={disabled}
            value={scheduleConfig.unit}
            options={[{ value: "minutes", label: "分钟" }, { value: "hours", label: "小时" }]}
            onChange={(unit) => onChange({ type: "interval", unit, value: INTERVAL_VALUES[unit][0] })}
          />
        </div>
      </div>}
      {scheduleConfig.type === "cron" && scheduleConfig.preset === "daily" && <div className="scheduleDynamicField">
        <label className="scheduleDynamicLabel" htmlFor="scheduled-task-daily-time">每日执行时间</label>
        <TimePicker
          id="scheduled-task-daily-time"
          aria-label="每日执行时间"
          format="HH:mm"
          style={{ width: "100%" }}
          minuteStep={5}
          disabled={disabled}
          value={dailyValue && dailyValue.isValid() ? dailyValue : null}
          onChange={(value) => onChange({ ...scheduleConfig, time: value ? value.format("HH:mm") : "09:00" })}
        />
      </div>}
      {scheduleConfig.type === "cron" && scheduleConfig.preset === "weekly" && <div className="scheduleDynamicRow">
        <div className="scheduleDynamicField">
          <label className="scheduleDynamicLabel" htmlFor="scheduled-task-weekday">星期</label>
          <Select
            id="scheduled-task-weekday"
            aria-label="星期"
            virtual={false}
            disabled={disabled}
            value={scheduleConfig.weekday}
            options={WEEKDAY_OPTIONS}
            onChange={(weekday) => onChange({ ...scheduleConfig, weekday })}
          />
        </div>
        <div className="scheduleDynamicField">
          <label className="scheduleDynamicLabel" htmlFor="scheduled-task-weekly-time">每周执行时间</label>
          <TimePicker
            id="scheduled-task-weekly-time"
            aria-label="每周执行时间"
            format="HH:mm"
            style={{ width: "100%" }}
            minuteStep={5}
            disabled={disabled}
            value={weeklyValue && weeklyValue.isValid() ? weeklyValue : null}
            onChange={(value) => onChange({ ...scheduleConfig, time: value ? value.format("HH:mm") : "09:00" })}
          />
        </div>
      </div>}
    </div>
  </div>;
}
