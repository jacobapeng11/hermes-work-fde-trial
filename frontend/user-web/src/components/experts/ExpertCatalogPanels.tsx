import { Alert, Button, Empty, Modal, Tag, Tooltip } from "antd";
import {
  AppstoreAddOutlined,
  ArrowRightOutlined,
  CheckOutlined,
  CopyOutlined,
  DeleteOutlined,
  MessageOutlined,
  MinusCircleOutlined,
  SettingOutlined
} from "@ant-design/icons";
import type {
  ExpertMarketCapabilitySummary,
  ExpertMarketDetail,
  ExpertMarketItem,
  RuntimeAgentProfileSummary,
} from "@hermes-cloud/schemas";
import { usePlatformTheme } from "../../theme/platform-theme";
import { ExpertAvatar } from "./ExpertAvatar";

function ExpertMarketCapabilitySection({ title, items }: {
  title: string;
  items: ExpertMarketCapabilitySummary[];
}) {
  return <section className="expertMarketCapabilitySection">
    <h3>{title}</h3>
    {items.length > 0 ? <ul>{items.map((item, index) => <li key={`${item.name}:${index}`}>
      <strong>{item.name}</strong>
      <span>{item.description || "未填写能力简介"}</span>
    </li>)}</ul> : <p className="expertMarketConfigurationEmpty">未配置{title}</p>}
  </section>;
}

export function ExpertMarketDetailDialog({ target, detail, loading, error, busy, profileStatus, pendingProfileIds, onClose, onRetry, onInstall, onSummon, onUninstall }: {
  target: ExpertMarketItem;
  detail: ExpertMarketDetail | null;
  loading: boolean;
  error: string | null;
  busy: string | null;
  profileStatus: RuntimeAgentProfileSummary | null;
  pendingProfileIds: ReadonlySet<number>;
  onClose: () => void;
  onRetry: () => void;
  onInstall: () => void;
  onSummon: () => void;
  onUninstall: () => void;
}) {
  const { theme } = usePlatformTheme();
  // 市场专家投影状态（main 侧 a740913b 功能，cherry-pick 时移植进 antd Modal 结构）
  const profileId = target.installedRuntimeAgentProfileId;
  const projectionStatus = profileStatus
    ? availabilityLabel(profileStatus)
    : profileId != null && pendingProfileIds.has(profileId)
      ? "准备中"
      : null;
  return <Modal
    open
    title={detail?.name || target.name}
    centered
    width={620}
    rootClassName={theme === "dark" ? "expertAntDialog expertAntDialogDark" : "expertAntDialog"}
    className="expertMarketDetailModal"
    closable={!busy}
    maskClosable={!busy}
    keyboard={!busy}
    onCancel={onClose}
    footer={[
      <Button key="close" autoInsertSpace={false} disabled={busy !== null} onClick={onClose}>关闭</Button>,
      target.allowedActions.install && (
        <Button
          key="install"
          type="primary"
          autoInsertSpace={false}
          icon={<AppstoreAddOutlined aria-hidden/>}
          loading={busy === `install:${target.expertPackageId}`}
          disabled={busy !== null && busy !== `install:${target.expertPackageId}`}
          onClick={onInstall}
        >安装</Button>
      ),
      target.allowedActions.summon && (
        <Button
          key="summon"
          type={target.allowedActions.install ? "default" : "primary"}
          autoInsertSpace={false}
          icon={<MessageOutlined aria-hidden/>}
          disabled={busy !== null}
          onClick={onSummon}
        >召唤</Button>
      ),
      target.allowedActions.uninstall && (
        <Button
          key="uninstall"
          danger
          autoInsertSpace={false}
          icon={<MinusCircleOutlined aria-hidden/>}
          disabled={busy !== null}
          onClick={onUninstall}
        >卸载</Button>
      ),
      !target.allowedActions.install && !target.allowedActions.summon && !target.allowedActions.uninstall && (
        <Button key="mine" icon={<CheckOutlined aria-hidden/>} disabled>我创建的</Button>
      )
    ]}
  >
    <div className="expertMarketDetailBody">
      <p className="expertVersionBadge">市场专家 · v{detail?.version || target.version}</p>
      {loading && <p className="expertLoading">正在加载专家详情…</p>}
      {error && <Alert
        className="expertDetailError"
        type="error"
        showIcon
        message={`专家详情加载失败：${error}`}
        action={<Button size="small" autoInsertSpace={false} onClick={onRetry}>重试</Button>}
      />}
      {detail && <>
        <p className="expertMarketDetailDescription">{detail.description || "这个专家还没有填写介绍。"}</p>
        {projectionStatus && <p className="expertMarketProjectionStatus" data-state={profileStatus?.convergenceStatus || "waiting"}>{projectionStatus}</p>}
        <p className="expertPrivateHint">提供方：{detail.providerName}。以下为创建者授权公开的当前市场版本配置；执行凭据与底层文件仍受保护。</p>
        <section className="expertMarketConfigurationSection"><h3>人格设定（SOUL）</h3><pre>{detail.soulContent || "未配置人格设定"}</pre></section>
        <section className="expertMarketConfigurationSection"><h3>系统提示词</h3><pre>{detail.systemPrompt || "未配置系统提示词"}</pre></section>
        <div className="expertMarketCapabilityGrid">
          <ExpertMarketCapabilitySection title="技能" items={detail.skills}/>
          <ExpertMarketCapabilitySection title="MCP" items={detail.mcps}/>
          <ExpertMarketCapabilitySection title="知识库" items={detail.knowledgeBases}/>
        </div>
      </>}
    </div>
  </Modal>;
}

export function ExpertUninstallDialog({ target, busy, error, onCancel, onConfirm }: { target: RuntimeAgentProfileSummary; busy: boolean; error: string | null; onCancel: () => void; onConfirm: () => void }) {
  const marketInstalled = target.ownershipType === "market_installed";
  const { theme } = usePlatformTheme();
  return <Modal
    open
    title={marketInstalled ? "卸载专家" : "删除专家"}
    centered
    width={420}
    rootClassName={theme === "dark" ? "expertAntDialog expertAntDialogDark" : "expertAntDialog"}
    className="expertUninstallModal"
    closable={!busy}
    maskClosable={!busy}
    keyboard={!busy}
    onCancel={onCancel}
    footer={[
      <Button key="cancel" autoInsertSpace={false} disabled={busy} onClick={onCancel}>取消</Button>,
      <Button key="confirm" danger type="primary" autoInsertSpace={false} loading={busy} onClick={onConfirm}>{marketInstalled ? "确认卸载" : "确认删除"}</Button>
    ]}
  >
    <div className="expertUninstallBody">
      <div className="expertUninstallTarget">
        <ExpertAvatar name={target.displayName} avatarUrl={target.avatarUrl} size="small"/>
        <div><small>即将{marketInstalled ? "卸载" : "删除"}</small><strong>{target.displayName}</strong></div>
      </div>
      <p className="expertUninstallWarning">{marketInstalled ? "卸载后，该专家仍保留在专家市场，历史会话保留只读；以后可重新安装最新版本。" : "删除后，该专家将从“我的”移除，历史会话保留只读；如已发布到市场，也会同步下架，但不影响他人已安装的副本。"}</p>
      {error && <Alert type="error" showIcon message={error}/>}
    </div>
  </Modal>;
}

const installedVersionLabel = (profile: RuntimeAgentProfileSummary): string => {
  if (profile.ownershipType !== "market_installed") {
    return `我创建的${profile.localVersion ? ` · v${profile.localVersion}` : ""}`;
  }
  return profile.installedVersion != null
    ? `市场安装 · v${profile.installedVersion}`
    : "市场安装 · 自动跟随最新版";
};

const availabilityLabel = (profile: RuntimeAgentProfileSummary): string | null => {
  if (["failed", "error"].includes(profile.convergenceStatus || "")) {
    return "配置失败";
  }
  if (
    ["provisioning", "updating"].includes(profile.status)
      || ["waiting", "updating", "unconfigured"].includes(profile.convergenceStatus || "")
      || (profile.desiredRevision ?? 0) > (profile.appliedRevision ?? 0)
  ) return "准备中";
  if (
    !profile.allowedActions.summon
      || ["disabled", "revoked"].includes(profile.convergenceStatus || "")
  ) return "暂不可用";
  return null;
};

const reviewLabel = (profile: RuntimeAgentProfileSummary) => {
  if (profile.reviewStatus === "pending") return "等待管理员审核";
  if (profile.reviewStatus === "approved") return "已发布到专家市场";
  if (profile.reviewStatus === "rejected") return "审核未通过";
  return "仅自己可见";
};

export function ExpertMarketPanel({
  items,
  loading,
  error,
  busy,
  cardErrors,
  profileStatuses,
  pendingProfileIds,
  onRetry,
  onOpenDetail,
  onInstall,
  onSummon,
  onUninstall,
}: {
  items: ExpertMarketItem[];
  loading: boolean;
  error: string | null;
  busy: string | null;
  cardErrors: Record<string, string>;
  profileStatuses: ReadonlyMap<number, RuntimeAgentProfileSummary>;
  pendingProfileIds: ReadonlySet<number>;
  onRetry: () => void;
  onOpenDetail: (item: ExpertMarketItem) => void;
  onInstall: (item: ExpertMarketItem) => void;
  onSummon: (item: ExpertMarketItem) => void;
  onUninstall: (item: ExpertMarketItem) => void;
}) {
  if (loading) return <div className="expertCatalogGrid expertCatalogSkeleton" aria-label="正在加载专家市场"><span/><span/><span/></div>;
  if (error) return <Alert
    className="expertPanelError"
    type="error"
    showIcon
    message={`专家市场加载失败：${error}`}
    action={<Button size="small" autoInsertSpace={false} onClick={onRetry}>重试</Button>}
  />;
  return <div className="expertCatalogGrid" role="tabpanel">
    {items.map((item) => {
      const itemBusy = busy?.endsWith(`:${item.expertPackageId}`) ?? false;
      const profileId = item.installedRuntimeAgentProfileId;
      const profileStatus = profileId != null ? profileStatuses.get(profileId) : undefined;
      const projectionStatus = profileStatus
        ? availabilityLabel(profileStatus)
        : profileId != null && pendingProfileIds.has(profileId)
          ? "准备中"
          : null;
      return <article className="expert-card expert-market-card" key={item.expertPackageId} onClick={() => onOpenDetail(item)}>
        <div className="expert-card-header">
          <Tag className="expert-card-tag owned">市场专家</Tag>
          <Tag className="expert-card-tag muted">v{item.version}</Tag>
        </div>
        <div className="expert-card-body">
          <span className="expert-card-avatar"><ExpertAvatar name={item.name} avatarUrl={item.avatarUrl}/></span>
          <div className="expert-card-text">
            <h3 className="expert-card-title" title={item.name}>{item.name}</h3>
            <p className="expert-card-desc" title={item.description || undefined}>{item.description || "这个专家还没有填写介绍。"}</p>
          </div>
        </div>
        {item.executionStatus === "force_disabled" && <p className="expert-card-notice">该专家已被平台强制停用，仅可卸载</p>}
        {projectionStatus && (
          <p
            className="expertMarketProjectionStatus"
            data-state={profileStatus?.convergenceStatus || "waiting"}
          >{projectionStatus}</p>
        )}
        <div className="expert-card-footer">
          <span className="expert-card-provider">由 {item.providerName} 提供 · v{item.version}</span>
          <span className="expert-card-footer-actions">
            <Button type="text" size="small" className="expert-card-detail-btn" onClick={(event) => { event.stopPropagation(); onOpenDetail(item); }}>查看详情</Button>
            {item.allowedActions.install && <Button type="primary" size="small" className="ghost-primary-btn" autoInsertSpace={false} loading={itemBusy} onClick={(event) => { event.stopPropagation(); onInstall(item); }}>安装</Button>}
            {item.allowedActions.summon && <Button type="primary" size="small" className="ghost-primary-btn" autoInsertSpace={false} disabled={itemBusy} onClick={(event) => { event.stopPropagation(); onSummon(item); }}>召唤</Button>}
            {item.allowedActions.uninstall && (
              <Tooltip title="卸载专家">
                <Button type="text" size="small" className="action-icon-btn danger" aria-label="卸载" icon={<MinusCircleOutlined aria-hidden/>} disabled={itemBusy} onClick={(event) => { event.stopPropagation(); onUninstall(item); }}/>
              </Tooltip>
            )}
            {!item.allowedActions.install && !item.allowedActions.summon && !item.allowedActions.uninstall && <Button size="small" className="expert-card-owned-btn" icon={<CheckOutlined aria-hidden/>} disabled>我创建的</Button>}
          </span>
        </div>
        {["install", "summon", "uninstall"].map((action) => cardErrors[`${action}:${item.expertPackageId}`]
          ? <p key={action} className="expert-card-error" role="alert">{cardErrors[`${action}:${item.expertPackageId}`]}</p>
          : null)}
      </article>;
    })}
    {items.length === 0 && <div className="expertEmpty">
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="专家市场暂时没有可见专家">
        <p className="expertEmptyHint">你仍然可以创建自己的专家；分享通过审核后会出现在这里。</p>
      </Empty>
    </div>}
  </div>;
}

export function ExpertMinePanel({
  items,
  loading,
  error,
  busy,
  uncertainProfileIds,
  onRetry,
  onStartChat,
  onEdit,
  onCopy,
  onDelete,
}: {
  items: RuntimeAgentProfileSummary[];
  loading: boolean;
  error: string | null;
  busy: string | null;
  uncertainProfileIds: ReadonlySet<number>;
  onRetry: () => void;
  onStartChat: (profile: RuntimeAgentProfileSummary) => void;
  onEdit: (profile: RuntimeAgentProfileSummary) => void;
  onCopy: (profile: RuntimeAgentProfileSummary) => void;
  onDelete: (profile: RuntimeAgentProfileSummary) => void;
}) {
  if (loading) return <div className="expertCatalogGrid expertCatalogSkeleton" aria-label="正在加载我的专家"><span/><span/><span/></div>;
  if (error) return <Alert
    className="expertPanelError"
    type="error"
    showIcon
    message={`我的专家加载失败：${error}`}
    action={<Button size="small" autoInsertSpace={false} onClick={onRetry}>重试</Button>}
  />;
  return <div className="expertCatalogGrid" role="tabpanel">
    {items.map((profile) => {
      const availability = availabilityLabel(profile);
      const selfCreated = profile.ownershipType !== "market_installed";
      const copyBusy = busy === `copy:${profile.runtimeAgentProfileId}`;
      const summonBlocked = !profile.allowedActions.summon || uncertainProfileIds.has(profile.runtimeAgentProfileId);
      return <article className={`expert-card expert-mine-card${profile.convergenceStatus === "updating" ? " expert-card-updating" : ""}`} key={profile.runtimeAgentProfileId}>
      <div className="expert-card-inner expertInstalledActions">
        <div className="expert-card-header">
          <span className="expert-card-tags">
            <Tag className="expert-card-tag owned">{selfCreated ? "我创建的" : "市场安装"}</Tag>
            {profile.reviewStatus === "pending" && <Tag className="expert-card-tag gold">待审核</Tag>}
            {profile.reviewStatus === "approved" && <Tag className="expert-card-tag green">已发布</Tag>}
            {profile.reviewStatus === "rejected" && <Tag className="expert-card-tag red">审核未过</Tag>}
            {availability && <Tag className={`expert-card-tag${availability === "配置失败" ? " red" : " blue"}`}>{availability}</Tag>}
          </span>
          <span className="expert-card-header-actions">
            {profile.allowedActions.copy && (
              <Tooltip title="复制专家">
                <Button type="text" size="small" className="action-icon-btn" aria-label={`复制 ${profile.displayName}`} icon={<CopyOutlined aria-hidden/>} loading={copyBusy} onClick={() => onCopy(profile)}/>
              </Tooltip>
            )}
            {profile.allowedActions.edit && (
              <Tooltip title="配置专家">
                <Button type="text" size="small" className="action-icon-btn" aria-label={`配置 ${profile.displayName}`} icon={<SettingOutlined aria-hidden/>} onClick={() => onEdit(profile)}/>
              </Tooltip>
            )}
            {(profile.allowedActions.delete || profile.allowedActions.uninstall) && (
              <Tooltip title={selfCreated ? "删除专家" : "卸载专家"}>
                <Button type="text" size="small" className="action-icon-btn danger" aria-label={`${selfCreated ? "删除" : "卸载"} ${profile.displayName}`} icon={<DeleteOutlined aria-hidden/>} onClick={() => onDelete(profile)}/>
              </Tooltip>
            )}
          </span>
        </div>
        <div className="expert-card-body">
          <span className="expert-card-avatar"><ExpertAvatar name={profile.displayName} avatarUrl={profile.avatarUrl}/></span>
          <div className="expert-card-text">
            <span className="expert-card-title-row">
              <h3 className="expert-card-title" title={profile.displayName}>{profile.displayName}</h3>
              {selfCreated && profile.localVersion && <Tag className="expert-card-tag muted">v{profile.localVersion}</Tag>}
              {!selfCreated && profile.installedVersion != null && <Tag className="expert-card-tag muted">v{profile.installedVersion}</Tag>}
            </span>
            <p className="expert-card-desc" title={profile.description || undefined}>{profile.description || "这个专家还没有填写介绍。"}</p>
          </div>
        </div>
        {profile.marketRemoved && <p className="expert-card-notice">该专家已从广场移除</p>}
        <div className="expert-card-footer">
          <span className="expert-card-meta" title={reviewLabel(profile)}>{reviewLabel(profile)}</span>
          <Button type="primary" size="small" className="ghost-primary-btn" autoInsertSpace={false} icon={<ArrowRightOutlined aria-hidden/>} iconPosition="end" disabled={summonBlocked} onClick={() => onStartChat(profile)}>召唤</Button>
        </div>
      </div>
      </article>;
    })}
    {items.length === 0 && <div className="expertEmpty">
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有自建专家">
        <p className="expertEmptyHint">创建一个只属于你的专家；市场专家在“专家市场”中管理。</p>
      </Empty>
    </div>}
  </div>;
}
