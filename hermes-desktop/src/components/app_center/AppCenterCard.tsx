import { Button, Card, Space, Tooltip, Typography } from "antd";
import {
  ArrowRightOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  LinkOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import type { AppCenterListItem } from "../../api/client";

const { Text, Paragraph } = Typography;

interface Props {
  app: AppCenterListItem;
  busy: boolean;
  onOpen: () => void;
  onEdit: () => void;
  onPermission: () => void;
  onClone: () => void;
  onDownload: () => void;
  onCopyLink: () => void;
  onDelete: () => void;
}

const cardBodyStyle = {
  padding: 0,
  height: "100%",
  display: "flex",
  flexDirection: "column",
} as const;

interface CardAction {
  type: "edit" | "permission" | "download" | "copyLink";
  icon: React.ReactNode;
  tip: string;
  label: string;
}

// 看板卡片（参照用户提供的 AppCenterCard demo 调优）：
// 封面 = iframe 实况缩略图 + 悬浮遮罩主操作（进入看板/克隆看板）；
// 归属角标已按用户需求移除（014 §14）：归属信息由 Tab（我的看板/看板广场）与元信息行表达。
// body = 名称 / 两行描述 / 元信息（范围 · 创建者）/ 底部主操作链接 + 副操作图标组。
// 缩略图 iframe 的 src/sandbox/loading/tabIndex 是安全契约，不得改动。
// mine 主操作为打开看板；plaza 主操作按 demo 定为克隆（点击卡片即触发）。
export function AppCenterCard({
  app,
  busy,
  onOpen,
  onEdit,
  onPermission,
  onClone,
  onDownload,
  onCopyLink,
  onDelete
}: Props) {
  const primaryText = app.mine ? "进入看板" : "克隆看板";
  const primaryHandler = app.mine ? onOpen : onClone;
  const secondaryActions: CardAction[] = app.mine
    ? [
        { type: "edit", icon: <EditOutlined aria-hidden />, tip: "编辑", label: "编辑" },
        { type: "permission", icon: <SafetyCertificateOutlined aria-hidden />, tip: "权限设置", label: "权限" },
        { type: "download", icon: <DownloadOutlined aria-hidden />, tip: "下载", label: "下载" },
        { type: "copyLink", icon: <LinkOutlined aria-hidden />, tip: "复制链接", label: "复制链接" }
      ]
    : [{ type: "download", icon: <DownloadOutlined aria-hidden />, tip: "下载", label: "下载" }];
  const handleSecondary = (type: CardAction["type"]) => ({
    edit: onEdit,
    permission: onPermission,
    download: onDownload,
    copyLink: onCopyLink
  })[type];

  return (
    <Card
      className="appCenterCard"
      styles={{ body: cardBodyStyle }}
      classNames={{ body: "appCenterCardBody" }}
      onClick={() => primaryHandler()}
    >
      <div className="appCenterCover">
        <div className="appCenterCoverZoom" aria-hidden="true">
          <iframe
            src={`/api/v1/app-center/${app.id}/index.html?embed=thumbnail`}
            title={`${app.name}首页缩略图`}
            sandbox=""
            loading="lazy"
            tabIndex={-1}
          />
        </div>
        <div className="appCenterCoverOverlay">
          <Button
            type="primary"
            autoInsertSpace={false}
            onClick={(event) => {
              event.stopPropagation();
              primaryHandler();
            }}
          >{primaryText}</Button>
        </div>
      </div>
      <div className="appCenterCardBodyInner">
        <div className="appCenterCardTitleRow">
          <Text strong className="appCenterCardTitle" title={app.name}>{app.name}</Text>
        </div>
        <Paragraph
          type="secondary"
          className="appCenterCardDesc"
          title={app.description}
          ellipsis={{ rows: 2 }}
        >{app.description}</Paragraph>
        <div className="appCenterCardMeta">
          <span title={app.scopeSummary}>{app.scopeSummary}</span>
          <i className="appCenterCardMetaDot" aria-hidden="true" />
          <span title={app.creatorName}>{app.creatorName}</span>
        </div>
        <footer className="appCenterCardFooter">
          <Button
            size="small"
            className="appCenterCardPrimary"
            autoInsertSpace={false}
            aria-label={`${primaryText} ${app.name}`}
            icon={<ArrowRightOutlined aria-hidden />}
            iconPosition="end"
            onClick={(event) => {
              event.stopPropagation();
              primaryHandler();
            }}
          >{primaryText}</Button>
          <Space size={0} className="appCenterCardActions">
            {secondaryActions.map((action) => (
              <Tooltip title={action.tip} key={action.type}>
                <Button
                  type="text"
                  size="small"
                  loading={busy}
                  aria-label={`${action.label} ${app.name}`}
                  icon={action.icon}
                  onClick={(event) => {
                    event.stopPropagation();
                    handleSecondary(action.type)();
                  }}
                />
              </Tooltip>
            ))}
            {app.mine && (
              <Tooltip title="删除">
                <Button
                  type="text"
                  size="small"
                  className="appCardDelete"
                  aria-label={`删除 ${app.name}`}
                  disabled={busy}
                  icon={<DeleteOutlined aria-hidden />}
                  onClick={(event) => {
                    event.stopPropagation();
                    onDelete();
                  }}
                />
              </Tooltip>
            )}
          </Space>
        </footer>
      </div>
    </Card>
  );
}
