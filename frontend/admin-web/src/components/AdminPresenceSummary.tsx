import type {
  AdminPresenceSummaryResponse,
  AdminPresenceUserGroup
} from "@hermes-cloud/schemas";

interface AdminPresenceSummaryProps {
  summary: AdminPresenceSummaryResponse | null;
  error?: string | null;
}

function observedAtLabel(observedAt: string): string {
  const parsed = Date.parse(observedAt);
  if (!Number.isFinite(parsed)) return "更新时间不可用";
  return `更新于 ${new Date(parsed).toLocaleTimeString()}`;
}

function windowLabel(windowSeconds: number): string {
  if (windowSeconds % 60 === 0) return `近 ${windowSeconds / 60} 分钟活跃`;
  return `近 ${windowSeconds} 秒活跃`;
}

function onlineWindowLabel(windowSeconds: number): string {
  return `${windowLabel(windowSeconds)} · 个人 Runtime 运行中`;
}

function UserList({ group }: { group?: AdminPresenceUserGroup }) {
  return <ul className="adminPresenceUserList" aria-label="用户名单">
    {group?.users.map((user) => (
      <li className="adminPresenceUser" key={user}>{user}</li>
    ))}
    {(!group || group.users.length === 0) && (
      <li className="adminPresenceUserEmpty">暂无用户</li>
    )}
  </ul>;
}

export function AdminPresenceSummary({
  summary,
  error = null
}: AdminPresenceSummaryProps) {
  return <section
    className="adminPresenceSummary"
    role="status"
    aria-label="平台在线状态"
    data-state={error ? "error" : summary ? "ready" : "loading"}
  >
    <div className="adminPresenceCard">
      <span>网页端在线用户数</span>
      <strong>{summary ? summary.webOnline.count : "—"}</strong>
      <small>{summary ? onlineWindowLabel(summary.windowSeconds) : "正在读取网页端会话状态"}</small>
      <UserList group={summary?.webOnline} />
    </div>
    <div className="adminPresenceCard">
      <span>桌面端在线用户数</span>
      <strong>{summary ? summary.desktopOnline.count : "—"}</strong>
      <small>{summary ? onlineWindowLabel(summary.windowSeconds) : "正在读取桌面端会话状态"}</small>
      <UserList group={summary?.desktopOnline} />
    </div>
    <div className="adminPresenceCard">
      <span>当天登录用户数</span>
      <strong>{summary ? summary.dailyLogins.count : "—"}</strong>
      <small>
        {error ?? (summary
          ? `今天 00:00 起去重 · 跨天在线计入 · ${observedAtLabel(summary.observedAt)}`
          : "正在读取当天登录用户")}
      </small>
      <UserList group={summary?.dailyLogins} />
    </div>
  </section>;
}
