import type { ChatQueuedRun } from "../pages/chatQueueProjection";

interface ChatQueuedRunListProps {
  runs: ChatQueuedRun[];
}

export function ChatQueuedRunList({ runs }: ChatQueuedRunListProps) {
  if (runs.length === 0) return null;
  return <section className="chatQueuedRunList" aria-label="等待执行的消息" role="status">
    <header><strong>等待执行</strong><span>{runs.length} 条</span></header>
    <ol>
      {runs.map((run, index) => <li key={run.runId}>
        <span className="chatQueuedRunNumber">{index + 1}</span>
        <div>
          <p>{run.prompt || "（空消息）"}</p>
          <small>{run.attachmentFileIds.length > 0 ? `含 ${run.attachmentFileIds.length} 个附件` : "将在当前任务完成后执行"}</small>
        </div>
      </li>)}
    </ol>
  </section>;
}
