import { Button } from "antd";
import { ReloadOutlined } from "@ant-design/icons";

interface Props {
  appName: string;
  sourceName: string;
  busy: boolean;
  onConfirm: () => void;
  onDismiss: () => void;
}

// 聊天消息流内「更新已分享看板」提示卡（T6 迁 antd Button）。
// 文案、aria 与回调契约零改动；clarifyCard 家族布局类沿用聊天域样式。
export function AppUpdateClarifyCard({
  appName,
  sourceName,
  busy,
  onConfirm,
  onDismiss
}: Props) {
  return <section className="clarifyCard appUpdateClarifyCard" aria-label="是否更新看板区" aria-busy={busy}>
    <div className="clarifyCardHeading">
      <strong>需要你确认</strong>
      <span>最新渲染已生成</span>
    </div>
    <p>是否将最新渲染更新到看板区「{appName}」？更新后，看板区点击打开将显示最新 HTML。</p>
    <small className="appUpdateClarifySource">产物：{sourceName}</small>
    <div className="clarifyActions">
      <Button size="small" autoInsertSpace={false} disabled={busy} onClick={onDismiss}>暂不更新</Button>
      <Button
        type="primary"
        size="small"
        autoInsertSpace={false}
        disabled={busy}
        loading={busy}
        icon={<ReloadOutlined aria-hidden />}
        onClick={onConfirm}
      >{busy ? "正在更新…" : "更新到看板区"}</Button>
    </div>
  </section>;
}
