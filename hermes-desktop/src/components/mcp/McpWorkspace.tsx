import { useEffect, useState } from "react";
import { Alert, Button, Empty, Modal, Tag } from "antd";
import { LoaderCircle, PlugZap } from "lucide-react";
import type {
  ProfileMcpAssetDetail,
  ProfileMcpAssetItem
} from "@hermes-cloud/schemas";
import { usePlatformTheme } from "../../theme/platform-theme";
import { usePlazaMcpStore } from "../../state/plaza-mcp-store";

export type McpWorkspaceAction = "install" | "remove";

interface McpWorkspaceProps {
  items: ProfileMcpAssetItem[];
  busyKey?: string | null;
  activeOperations?: Readonly<Record<number, McpWorkspaceAction>>;
  statusMessage?: string | null;
  error?: string | null;
  onAction: (
    asset: ProfileMcpAssetItem,
    action: McpWorkspaceAction
  ) => Promise<unknown>;
  loadDetail: (
    asset: ProfileMcpAssetItem
  ) => Promise<ProfileMcpAssetDetail>;
}

function configured(asset: ProfileMcpAssetItem): boolean {
  return asset.desired;
}

function badge(
  asset: ProfileMcpAssetItem,
  activeOperation?: McpWorkspaceAction | null,
): string {
  if (asset.status !== "published" && asset.status !== "active") {
    return "MCP已下架，无法使用，请删除";
  }
  if (activeOperation) return activeOperation === "install" ? "安装中" : "删除中";
  if (asset.desired !== asset.applied) {
    return asset.desired ? "安装中" : "删除中";
  }
  return asset.desired ? "已安装" : "未安装";
}

// 状态徽标语义色（对齐 expert-card-tag 色板）：已安装=green，进行中=blue，
// 下架/失败=red，未安装=muted。
function badgeTone(
  asset: ProfileMcpAssetItem,
  activeOperation?: McpWorkspaceAction | null,
): string {
  if (asset.status !== "published" && asset.status !== "active") return "red";
  if (activeOperation) return "blue";
  if (asset.desired !== asset.applied) return "blue";
  return asset.desired ? "green" : "muted";
}

function actionText(asset: ProfileMcpAssetItem): string {
  return configured(asset) ? "删除" : "安装";
}

function operationText(asset: ProfileMcpAssetItem): string | null {
  if (asset.convergenceStatus === "failed") return "配置更新失败";
  if (asset.desired !== asset.applied) return "正在更新 Runtime 配置…";
  return null;
}

function convergingAction(asset: ProfileMcpAssetItem): McpWorkspaceAction | null {
  if (asset.desired === asset.applied || asset.convergenceStatus === "failed") return null;
  return asset.desired ? "install" : "remove";
}

function progressText(action: McpWorkspaceAction): string {
  return action === "install" ? "安装中" : "删除中";
}

export function McpWorkspace({
  items,
  busyKey = null,
  activeOperations = {},
  statusMessage = null,
  error = null,
  onAction,
  loadDetail
}: McpWorkspaceProps) {
  const [detail, setDetail] =
    useState<ProfileMcpAssetDetail | null>(null);
  const [detailSource, setDetailSource] =
    useState<ProfileMcpAssetItem | null>(null);
  const [detailLoading, setDetailLoading] = useState<number | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const { theme } = usePlatformTheme();

  const closeDetail = () => {
    setDetail(null);
    setDetailSource(null);
  };

  const openDetail = async (asset: ProfileMcpAssetItem) => {
    if (detailLoading !== null) return;
    setDetailLoading(asset.assetId);
    setDetailError(null);
    try {
      setDetailSource(asset);
      const loaded = await loadDetail(asset);
      setDetail({
        ...loaded,
        asset: {
          ...loaded.asset,
          desired: asset.desired,
          applied: asset.applied,
          convergenceStatus: asset.convergenceStatus,
          errorMessage: asset.errorMessage
        }
      });
    } catch (reason) {
      setDetailError(
        reason instanceof Error ? reason.message : "读取 MCP 详情失败"
      );
    } finally {
      setDetailLoading(null);
    }
  };

  const submitAction = async (
    asset: ProfileMcpAssetItem,
    action: McpWorkspaceAction
  ) => {
    setDetailError(null);
    try {
      await onAction(asset, action);
    } catch (reason) {
      setDetailError(
        reason instanceof Error ? reason.message : "保存 MCP 配置失败"
      );
    }
  };

  return <>
    <section className="mcpPage" aria-label="MCP 广场">
      <div className="mcpWorkspace">
        <header className="mcpModuleHeader">
          <div className="mcpModuleHeaderMain">
            <span className="mcpModuleHeaderIcon" aria-hidden="true"><PlugZap size={20}/></span>
            <div className="mcpModuleHeaderText">
              <h2 className="mcpModuleHeaderTitle">MCP</h2>
              <p className="mcpModuleHeaderSubtitle">选择平台治理后的 MCP 服务。</p>
            </div>
          </div>
        </header>
        <p className="mcpPanelHint">能力变更完成后，从下一轮对话开始使用最新配置。</p>
        {statusMessage && <p className="mcpPanelStatus" role="status">{statusMessage}</p>}
        {error && <Alert className="mcpPanelAlert" type="error" showIcon message={error}/>}
        {detailError && <Alert className="mcpPanelAlert" type="error" showIcon message={detailError}/>}
        <div className="mcpCatalogGrid">
          {items.map((asset) => {
            const localOperation = activeOperations[asset.assetId];
            const legacyBusy = busyKey === `mcp:${asset.assetId}`;
            const isConfigured = configured(asset);
            const activeOperation = localOperation
              || convergingAction(asset)
              || (legacyBusy ? (isConfigured ? "remove" : "install") : null);
            const currentOperationText = localOperation
              ? "正在提交并等待 Runtime 配置…"
              : operationText(asset);
            return (
              <article
                className="mcp-card"
                key={`mcp-${asset.assetId}`}
                onClick={() => void openDetail(asset)}
              >
                <div className="mcp-card-header">
                  <span className="mcp-card-tags">
                    <Tag className="mcp-card-tag owned">MCP · 不可变配置</Tag>
                    <Tag
                      className={`mcp-card-tag ${badgeTone(asset, activeOperation)}`}
                      data-state={
                        asset.convergenceStatus
                        || (asset.applied ? "applied" : "idle")
                      }
                    >
                      {badge(asset, activeOperation)}
                    </Tag>
                  </span>
                </div>
                <div className="mcp-card-body">
                  <div className="mcp-card-text">
                    <button
                      type="button"
                      className="mcp-card-title-btn"
                      onClick={(event) => { event.stopPropagation(); void openDetail(asset); }}
                    >
                      <h3 className="mcp-card-title" title={asset.name}>{asset.name}</h3>
                    </button>
                    <p className="mcp-card-desc" title={asset.description || asset.slug}>{asset.description || asset.slug}</p>
                    {currentOperationText && (
                      <small className="mcp-card-operation">
                        {asset.errorMessage
                          ? `${currentOperationText}：${asset.errorMessage}`
                          : currentOperationText}
                      </small>
                    )}
                    {detailLoading === asset.assetId && (
                      <small className="mcp-card-operation">正在读取详情…</small>
                    )}
                  </div>
                </div>
                <div className="mcp-card-footer">
                  <span className="mcp-card-meta" title={asset.slug}>{asset.slug}</span>
                  <Button
                    size="small"
                    autoInsertSpace={false}
                    className={isConfigured ? "mcp-card-action-btn" : "mcp-card-action-btn ghost-primary-btn"}
                    disabled={Boolean(activeOperation)}
                    onClick={(event) => {
                      event.stopPropagation();
                      void submitAction(asset, isConfigured ? "remove" : "install");
                    }}
                  >
                    {activeOperation
                      ? <><LoaderCircle className="uploadSpinner" size={15}/>{progressText(activeOperation)}</>
                      : actionText(asset)}
                  </Button>
                </div>
              </article>
            );
          })}
          {items.length === 0 && (
            <div className="mcpEmpty">
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无可用公共 MCP。"/>
            </div>
          )}
        </div>
      </div>
    </section>
    {detail && (
      <Modal
        open
        title={`${detail.asset.name} 详情`}
        centered
        width={560}
        rootClassName={theme === "dark" ? "mcpAntDialog mcpAntDialogDark" : "mcpAntDialog"}
        className="mcpDetailModal"
        onCancel={closeDetail}
        footer={[
          <Button key="close" autoInsertSpace={false} onClick={closeDetail}>关闭</Button>,
          detailSource && (
            <Button
              key="action"
              type={configured(detailSource) ? "default" : "primary"}
              autoInsertSpace={false}
              disabled={Boolean(
                activeOperations[detailSource.assetId]
                || convergingAction(detailSource)
                || busyKey === `mcp:${detailSource.assetId}`
              )}
              onClick={() => void submitAction(
                detailSource,
                configured(detailSource) ? "remove" : "install"
              )}
            >
              {(() => {
                const activeOperation = activeOperations[detailSource.assetId]
                  || convergingAction(detailSource)
                  || (busyKey === `mcp:${detailSource.assetId}`
                    ? (configured(detailSource) ? "remove" : "install")
                    : null);
                return activeOperation
                  ? <><LoaderCircle className="uploadSpinner" size={15}/>{progressText(activeOperation)}</>
                  : actionText(detailSource);
              })()}
            </Button>
          )
        ].filter(Boolean)}
      >
        <div className="mcpDetailBody">
          <p className="mcpDetailDescription">{detail.asset.description || detail.asset.slug}</p>
          <section className="mcpDetailSection">
            <h3>状态</h3>
            <Tag className={`mcp-card-tag ${badgeTone(detail.asset, activeOperations[detail.asset.assetId])}`}>
              {badge(detail.asset, activeOperations[detail.asset.assetId])}
            </Tag>
          </section>
          <section className="mcpDetailSection">
            <h3>配置摘要</h3>
            <dl className="mcpDetailMeta">
              <div><dt>Transport</dt><dd>{String(detail.detail.transport || "")}</dd></div>
              <div><dt>Endpoint</dt><dd>{String(detail.detail.endpoint || "")}</dd></div>
              <div><dt>权限</dt><dd>{String(
                detail.detail.permissionHint
                || detail.detail.permission_hint
                || ""
              )}</dd></div>
            </dl>
          </section>
        </div>
      </Modal>
    )}
  </>;
}

/** Default-profile user MCP page.  Expert configuration keeps passing its own
 * controller to McpWorkspace, while this wrapper is the sole polling owner
 * for the user plaza. */
export function UserPlazaMcpWorkspace({
  runtimeWorkspaceId,
}: {
  runtimeWorkspaceId?: number | null;
}) {
  const store = usePlazaMcpStore();
  useEffect(() => {
    if (runtimeWorkspaceId) void store.load(runtimeWorkspaceId).catch(() => undefined);
    // Store methods are singleton-bound; load only once per mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runtimeWorkspaceId]);
  const items = store.items;
  const active = new Set([
    ...items.filter((item) => item.desired !== item.applied).map((item) => item.assetId),
    ...Object.keys(store.operations).map(Number),
  ]).size;
  return <McpWorkspace
    items={items}
    activeOperations={store.operations}
    statusMessage={active > 0 ? `${active} 个 MCP 操作正在排队或执行，完成后会自动刷新。` : null}
    error={store.error}
    loadDetail={async (asset) => ({
      asset,
      detail: { transport: "平台治理", endpoint: "安装时写入 Runtime 配置" }
    })}
    onAction={async (asset, action) => {
      if (action === "install") await store.submitInstall(asset.assetId);
      else await store.submitUninstall(asset.assetId);
    }}
  />;
}
