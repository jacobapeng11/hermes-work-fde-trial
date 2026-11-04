import { useEffect, useMemo, useState } from "react";
import { Bot, Check, LoaderCircle, ShieldCheck, Store, X } from "lucide-react";
import type { ExpertReviewSummary } from "@hermes-cloud/schemas";
import { adminApi } from "../api/client";
import { builtinAvatarId, selectedDefaultAvatar } from "../../../user-web/src/avatar";

type ReviewFilter = "all" | "pending" | "approved" | "rejected";

const statusLabel = (status: string) => ({
  pending: "待审核",
  approved: "已通过",
  rejected: "已拒绝"
}[status] || status);

function AdminExpertAvatar({ avatarUrl, name }: { avatarUrl?: string | null; name: string }) {
  const builtinId = builtinAvatarId(avatarUrl);
  const avatar = builtinId ? selectedDefaultAvatar(builtinId) : null;
  const label = `${name || "专家"}的头像`;
  return <span className="adminExpertAvatar" role="img" aria-label={label}>
    {avatar
      ? <img src={avatar.source} alt=""/>
      : avatarUrl && <img src={avatarUrl} alt=""/>
    }
    <span aria-hidden="true">{name.slice(0, 1) || "专"}</span>
  </span>;
}

export function AdminExpertManagement() {
  const [filter, setFilter] = useState<ReviewFilter>("all");
  const [reviews, setReviews] = useState<ExpertReviewSummary[]>([]);
  const [selected, setSelected] = useState<ExpertReviewSummary | null>(null);
  const [comment, setComment] = useState("");
  const [securityReason, setSecurityReason] = useState("");
  const [operationNotice, setOperationNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      setReviews(await adminApi.expertReviews(filter === "all" ? undefined : filter));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "专家审核列表加载失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [filter]);

  const counts = useMemo(() => ({
    skill: selected?.capabilities.skills?.length || selected?.capabilities.skillIds.length || 0,
    mcp: selected?.capabilities.mcps?.length || selected?.capabilities.mcpIds.length || 0,
    knowledge: selected?.capabilities.knowledgeBases?.length || selected?.capabilities.knowledgeBaseIds.length || 0,
    plugin: selected?.capabilities.pluginIds.length || 0
  }), [selected]);

  const openReview = (review: ExpertReviewSummary) => {
    setSelected(review);
    setComment(review.reviewComment || "");
    setSecurityReason(review.executionStatusReason || "");
  };

  const approve = async () => {
    if (!selected) return;
    setBusy("approve");
    setError(null);
    try {
      await adminApi.approveExpertReview(selected.reviewId, {
        reviewComment: comment.trim() || null
      });
      setSelected(null);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "专家审批失败");
    } finally {
      setBusy(null);
    }
  };

  const reject = async () => {
    if (!selected) return;
    if (!comment.trim()) {
      setError("拒绝审核必须填写原因");
      return;
    }
    setBusy("reject");
    setError(null);
    try {
      await adminApi.rejectExpertReview(selected.reviewId, comment.trim());
      setSelected(null);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "专家拒绝审核失败");
    } finally {
      setBusy(null);
    }
  };

  const unpublish = async () => {
    if (!selected) return;
    setBusy("unpublish");
    setError(null);
    try {
      await adminApi.unpublishExpertPackage(selected.expertPackageId);
      setSelected(null);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "专家下架失败");
    } finally {
      setBusy(null);
    }
  };

  const setExecutionEnabled = async (enabled: boolean) => {
    if (!selected) return;
    if (!securityReason.trim()) {
      setError(enabled ? "重新启用必须填写处置说明" : "强制停用必须填写安全原因");
      return;
    }
    setBusy(enabled ? "enable" : "force-disable");
    setError(null);
    setOperationNotice(null);
    try {
      const idempotencyKey = crypto.randomUUID();
      const rollout = enabled
        ? await adminApi.enableExpertPackage(
          selected.expertPackageId,
          securityReason.trim(),
          idempotencyKey
        )
        : await adminApi.forceDisableExpertPackage(
          selected.expertPackageId,
          securityReason.trim(),
          idempotencyKey
        );
      setOperationNotice(
        `${enabled ? "重新启用" : "强制停用"}已接纳：${rollout.affectedProfileCount} 个 Profile，Rollout ${rollout.rolloutId}`
      );
      setSelected(null);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "专家安全状态操作失败");
    } finally {
      setBusy(null);
    }
  };

  return <section className="adminPanel adminExpertManagement" aria-label="专家管理">
    <div className="panelHeader">
      <div><h2>专家管理</h2><p>审核企业内部专家；创建者提交的市场可见范围在审核中保持只读。</p></div>
      <span>{reviews.length} reviews</span>
    </div>
    <div className="adminExpertFilters" role="tablist">
      {([
        ["all", "全部"],
        ["pending", "待审核"],
        ["approved", "已通过"],
        ["rejected", "已拒绝"]
      ] as Array<[ReviewFilter, string]>).map(([key, label]) => <button key={key} type="button" role="tab" aria-selected={filter === key} onClick={() => setFilter(key)}>{label}</button>)}
    </div>
    {error && <div className="adminExpertError" role="alert">{error}</div>}
    {operationNotice && <div className="adminExpertNotice" role="status">{operationNotice}</div>}
    {loading ? <p className="adminExpertLoading"><LoaderCircle size={16}/>正在加载专家审核…</p> : <div className="adminExpertReviewList">
      {reviews.map((review) => <button className="adminExpertReviewCard" type="button" key={review.reviewId} onClick={() => openReview(review)}>
        <AdminExpertAvatar avatarUrl={review.avatarUrl} name={review.name}/>
        <span className="adminExpertReviewIdentity"><strong>{review.name}</strong><small>提交人 #{review.submitterUserId} · 版本 #{review.expertPackageVersionId}</small></span>
        <span className="adminExpertScope">{review.shareScope.organization ? "全组织" : `${review.shareScope.userIds.length} 人 / ${review.shareScope.departmentIds.length} 部门`}</span>
        <span className="adminExpertReviewStatus" data-status={review.status}>{statusLabel(review.status)}</span>
      </button>)}
      {reviews.length === 0 && <div className="emptyState"><div><Bot size={28}/><p>当前筛选下没有专家审核记录。</p></div></div>}
    </div>}

    {selected && <div className="adminExpertBackdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) setSelected(null);
    }}>
      <section className="adminExpertDialog" role="dialog" aria-modal="true" aria-labelledby="admin-expert-dialog-title" onMouseDown={(event) => event.stopPropagation()}>
        <header><div><AdminExpertAvatar avatarUrl={selected.avatarUrl} name={selected.name}/><div><small>专家审核 #{selected.reviewId}</small><h2 id="admin-expert-dialog-title">{selected.name}</h2></div></div><button type="button" aria-label="关闭" onClick={() => setSelected(null)} disabled={Boolean(busy)}><X size={18}/></button></header>
        <div className="adminExpertDialogBody">
          <section className="adminExpertSection"><h3>基本信息</h3><p>{selected.description || "未填写专家介绍"}</p><dl><dt>提交人</dt><dd>#{selected.submitterUserId}</dd><dt>专家包</dt><dd>#{selected.expertPackageId}</dd><dt>版本</dt><dd>#{selected.expertPackageVersionId}</dd></dl></section>
          <section className="adminExpertSection"><h3>本次版本改动简介</h3><p>{selected.changeSummary || "初始发布（未填写额外说明）"}</p></section>
          <section className="adminExpertSection"><h3>SOUL.md</h3><pre>{selected.soulContent || "未填写个性内容"}</pre></section>
          <section className="adminExpertSection"><h3>系统提示词</h3><pre>{selected.systemPrompt || "未配置系统提示词"}</pre></section>
          <section className="adminExpertSection"><h3>能力清单</h3><div className="adminExpertCapabilityGrid"><span>技能 <strong>{counts.skill}</strong></span><span>MCP <strong>{counts.mcp}</strong></span><span>知识库 <strong>{counts.knowledge}</strong></span><span>Plugin <strong>{counts.plugin}</strong></span></div>{selected.capabilities.pluginIds.length > 0 && <p>Plugin：{selected.capabilities.pluginIds.join("、")}</p>}</section>
          <section className="adminExpertSection">
            <h3>专家市场可见范围</h3>
            <p>该范围由创建者提交，管理员不能在审批时修改。</p>
            <dl>
              <dt>全组织</dt><dd>{selected.shareScope.organization ? "是" : "否"}</dd>
              <dt>人员</dt><dd>{selected.shareScopeUsers.map((user) => user.name ? (user.userCode ? `${user.name}（${user.userCode}）` : user.name) : `用户 #${user.id}`).join("、") || "无"}</dd>
              <dt>部门</dt><dd>{selected.shareScope.departmentIds.join("、") || "无"}</dd>
            </dl>
          </section>
          <section className="adminExpertSection"><h3>{selected.status === "pending" ? "审批意见" : "审核记录"}</h3><textarea rows={3} value={comment} onChange={(event) => setComment(event.target.value)} placeholder={selected.status === "pending" ? "通过时可选，拒绝时必填" : "审核意见"} disabled={selected.status !== "pending"}/></section>
          {selected.status === "approved" && <section className="adminExpertSection">
            <h3>执行安全状态</h3>
            <p>{selected.executionStatus === "force_disabled" ? "已强制停用；所有安装禁止发起新 Run。" : "正常；安全停用不会改变市场目录或安装关系。"}</p>
            <p>当前活动安装：{selected.activeInstallationCount} 个。命令接纳后请在 Rollout 监控中观察 Runtime 收敛。</p>
            <textarea rows={3} value={securityReason} onChange={(event) => setSecurityReason(event.target.value)} placeholder={selected.executionStatus === "force_disabled" ? "填写重新启用的处置说明" : "填写强制停用的安全原因"}/>
          </section>}
        </div>
        <footer>
          {selected.status === "pending" ? <>
            <button type="button" className="adminExpertReject" onClick={() => void reject()} disabled={Boolean(busy)}>{busy === "reject" ? <LoaderCircle size={15}/> : <X size={15}/>}拒绝</button>
            <button type="button" className="adminExpertApprove" onClick={() => void approve()} disabled={Boolean(busy)}>{busy === "approve" ? <LoaderCircle size={15}/> : <ShieldCheck size={15}/>}通过审核</button>
          </> : selected.status === "approved" ? <>
            <button type="button" className="adminExpertReject" onClick={() => void unpublish()} disabled={Boolean(busy)}>{busy === "unpublish" ? <LoaderCircle size={15}/> : <Store size={15}/>}正常下架</button>
            <button type="button" className={selected.executionStatus === "force_disabled" ? "adminExpertApprove" : "adminExpertReject"} onClick={() => void setExecutionEnabled(selected.executionStatus === "force_disabled")} disabled={Boolean(busy)}>{busy === "force-disable" || busy === "enable" ? <LoaderCircle size={15}/> : <ShieldCheck size={15}/>} {selected.executionStatus === "force_disabled" ? "重新启用" : "强制停用"}</button>
          </> : <button type="button" onClick={() => setSelected(null)}><Check size={15}/>关闭</button>}
        </footer>
      </section>
    </div>}
  </section>;
}
