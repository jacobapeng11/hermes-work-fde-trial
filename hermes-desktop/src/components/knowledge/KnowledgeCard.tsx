import { Button, Card, Switch, Tag } from "antd";
import {
  ArrowRightOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  EditOutlined,
  FileTextOutlined,
  LockOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import type { PersonalKnowledgeSummary } from "@hermes-cloud/schemas";

export interface KnowledgeToggleFeedback {
  kind: "notice" | "error";
  message: string;
}

export interface KnowledgeCardProps {
  item: PersonalKnowledgeSummary;
  /** assetApi 模式：角色 Tag 显示「应用专属」，隐藏检索勾选与权限入口。 */
  applicationOwned: boolean;
  roleText: string;
  roleColor: "blue" | "green" | "gold" | "default";
  canEnter: boolean;
  canConfigureRetrieval: boolean;
  canManageAssets: boolean;
  canOpenAssets: boolean;
  busy: boolean;
  feedback?: KnowledgeToggleFeedback;
  onToggle: (enabled: boolean) => void;
  onOpen: () => void;
  onEdit: () => void;
  onShare: () => void;
  onDelete: () => void;
}

const cardBodyStyle = {
  padding: 0,
  height: "100%",
  display: "flex",
  flexDirection: "column",
} as const;

// 个人知识库卡片（布局参考用户提供的知识库布局参考.html）：
// 顶部行（角色 Tag + hover 显现的操作组）→ 中部（图标 + 标题/描述）→
// footer（文件统计 · 所有者 + 进入）。卡片 hover 只加深阴影。
export function KnowledgeCard({
  item,
  applicationOwned,
  roleText,
  roleColor,
  canEnter,
  canConfigureRetrieval,
  canManageAssets,
  canOpenAssets,
  busy,
  feedback,
  onToggle,
  onOpen,
  onEdit,
  onShare,
  onDelete,
}: KnowledgeCardProps) {
  const manageAllowed = canManageAssets && item.effectiveRole === "owner";
  const retrieveStatus = busy ? "保存中…" : item.runtimeEnabled ? "已启用" : "未启用";
  const retrieveStatusId = `pkb-retrieve-status-${item.knowledgeBaseId}`;

  return (
    <Card
      className={`pkbCard ${canEnter ? "enterable" : "restricted"}`}
      styles={{ body: cardBodyStyle }}
      classNames={{ body: "pkbCardBody" }}
    >
      <div className="pkbCardTop">
        <div className="pkbCardTags">
          <Tag className="pkbRole" color={roleColor}>{roleText}</Tag>
          {!applicationOwned && item.runtimeEnabled && (
            <Tag className="pkbRetrieveTag">用于对话检索</Tag>
          )}
        </div>
        <div className="pkbCardActions">
          {!applicationOwned && (
            <span className="pkbRetrieveToggle" title={`用于对话检索：${retrieveStatus}`}>
              <Switch
                size="small"
                checked={item.runtimeEnabled}
                loading={busy}
                disabled={!canConfigureRetrieval}
                onChange={(checked) => onToggle(checked)}
                aria-label={`用于对话检索 ${item.name}`}
                aria-describedby={retrieveStatusId}
              />
              <span id={retrieveStatusId} className="srOnly">检索状态：{retrieveStatus}</span>
            </span>
          )}
          {manageAllowed && (
            <div className="pkbCardManageActions">
              <Button
                type="text"
                size="small"
                aria-label={`编辑 ${item.name}`}
                title="编辑知识库"
                icon={<EditOutlined aria-hidden />}
                onClick={(event) => { event.stopPropagation(); onEdit(); }}
              />
              {!applicationOwned && (
                <Button
                  type="text"
                  size="small"
                  aria-label={`权限设置 ${item.name}`}
                  title="权限设置"
                  icon={<SafetyCertificateOutlined aria-hidden />}
                  onClick={(event) => { event.stopPropagation(); onShare(); }}
                />
              )}
              <Button
                type="text"
                size="small"
                className="pkbDangerAction"
                aria-label={`删除 ${item.name}`}
                title="永久删除知识库"
                disabled={busy}
                icon={<DeleteOutlined aria-hidden />}
                onClick={(event) => { event.stopPropagation(); onDelete(); }}
              />
            </div>
          )}
        </div>
      </div>
      <button type="button" className="pkbCardMain" disabled={!canEnter} onClick={() => canEnter && onOpen()}>
        <span className="pkbCardIcon"><DatabaseOutlined aria-hidden /></span>
        <span className="pkbCardText">
          <h3>{item.name}</h3>
          <p>{item.description || "暂未填写知识库描述"}</p>
        </span>
      </button>
      <footer className="pkbCardFooter">
        <span className="pkbStats"><FileTextOutlined aria-hidden />{item.fileCount} 个文件 · {item.readyFileCount} 个可检索</span>
        <span className="pkbCardFooterSide">
          <span className="pkbOwner" title={`所有者：${item.ownerName}`}>{item.ownerName}</span>
          {canEnter ? (
            <Button
              className="pkbEnterButton"
              size="small"
              autoInsertSpace={false}
              aria-label={`进入 ${item.name}`}
              onClick={(event) => { event.stopPropagation(); onOpen(); }}
            >进入<ArrowRightOutlined aria-hidden /></Button>
          ) : (
            <span className="pkbAccessNotice"><LockOutlined aria-hidden />{canOpenAssets ? "无浏览权限" : "请在个人 Agent 管理内容"}</span>
          )}
        </span>
      </footer>
      {feedback && <p className={`pkbCardFeedback ${feedback.kind}`} role={feedback.kind === "error" ? "alert" : "status"}>{feedback.message}</p>}
    </Card>
  );
}
