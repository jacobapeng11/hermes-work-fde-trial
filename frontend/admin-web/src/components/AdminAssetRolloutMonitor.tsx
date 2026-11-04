import { useEffect, useMemo, useState } from "react";
import { LoaderCircle, RefreshCw, RotateCcw } from "lucide-react";

import {
  adminApi,
  type AssetRolloutBatchSummary,
  type AssetRolloutDetail
} from "../api/client";


const statusLabel = (status: string): string => ({
  waiting: "等待中",
  updating: "更新中",
  completed: "已完成",
  partial_failed: "部分失败",
  failed: "失败",
  applied: "已投影",
  superseded: "已被新版本取代",
  revoked: "已撤回",
  success: "成功"
}[status] ?? status);

const assetLabel = (batch: AssetRolloutBatchSummary): string =>
  `${batch.assetType.toUpperCase()} #${batch.assetId}`;

export function AdminAssetRolloutMonitor() {
  const [batches, setBatches] = useState<AssetRolloutBatchSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<AssetRolloutDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const selectedBatch = useMemo(
    () => batches.find((item) => item.rolloutId === selectedId) ?? null,
    [batches, selectedId]
  );

  const loadBatches = async () => {
    const next = await adminApi.assetRollouts();
    setBatches(next);
    setSelectedId((current) => current && next.some((item) => item.rolloutId === current)
      ? current
      : next[0]?.rolloutId ?? null);
  };

  const loadDetail = async (rolloutId: string) => {
    setDetail(await adminApi.assetRollout(rolloutId));
  };

  const refresh = async () => {
    setError(null);
    try {
      await loadBatches();
      if (selectedId) await loadDetail(selectedId);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "资产推送进度加载失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadBatches()
      .catch((reason) => setError(reason instanceof Error ? reason.message : "资产推送进度加载失败"))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    void loadDetail(selectedId).catch((reason) => {
      setError(reason instanceof Error ? reason.message : "资产推送明细加载失败");
    });
  }, [selectedId]);

  useEffect(() => {
    if (!selectedBatch || !["waiting", "updating"].includes(selectedBatch.status)) return;
    const timer = window.setInterval(() => void refresh(), 2_000);
    return () => window.clearInterval(timer);
  }, [selectedBatch?.rolloutId, selectedBatch?.status, selectedId]);

  const retry = async (targetId?: number) => {
    if (!selectedId || busy) return;
    setBusy(targetId ? `target:${targetId}` : "batch");
    setError(null);
    try {
      if (targetId) await adminApi.retryFailedAssetRolloutTarget(selectedId, targetId);
      else await adminApi.retryFailedAssetRollout(selectedId);
      await Promise.all([loadBatches(), loadDetail(selectedId)]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "失败任务重试提交失败");
    } finally {
      setBusy(null);
    }
  };

  const finishedCount = selectedBatch
    ? selectedBatch.appliedCount + selectedBatch.revokedCount + selectedBatch.supersededCount + selectedBatch.failedCount
    : 0;
  const progress = selectedBatch?.affectedProfileCount
    ? Math.round(finishedCount / selectedBatch.affectedProfileCount * 100)
    : 100;

  return <section className="adminPanel rolloutMonitor" aria-label="资产推送监控">
    <div className="panelHeader">
      <div><h2>资产推送监控</h2><small>查看每次发布影响的用户、Profile 和失败原因。</small></div>
      <button type="button" className="iconButton" title="刷新推送进度" onClick={() => void refresh()}>
        <RefreshCw size={16}/>
      </button>
    </div>
    {error && <div className="adminNotice" role="alert"><strong>操作失败</strong><span>{error}</span></div>}
    {loading && <p className="muted"><LoaderCircle className="uploadSpinner" size={15}/> 正在加载推送记录</p>}
    {!loading && batches.length === 0 && <p className="muted">暂无资产推送记录。</p>}
    {batches.length > 0 && <div className="rolloutLayout">
      <div className="rolloutBatchList" aria-label="推送批次">
        {batches.map((batch) => <button
          type="button"
          key={batch.rolloutId}
          data-active={batch.rolloutId === selectedId}
          onClick={() => setSelectedId(batch.rolloutId)}
        >
          <strong>{assetLabel(batch)} · {batch.action}</strong>
          <span data-state={batch.status}>{statusLabel(batch.status)}</span>
          <small>{batch.affectedUserCount} 用户 / {batch.affectedProfileCount} Profile</small>
        </button>)}
      </div>
      {selectedBatch && <div className="rolloutDetail">
        <div className="rolloutProgressHeader">
          <div><h3>{assetLabel(selectedBatch)}</h3><small>{selectedBatch.rolloutId}</small></div>
          {selectedBatch.failedCount > 0 && <button type="button" disabled={busy !== null} onClick={() => void retry()}>
            {busy === "batch" ? <LoaderCircle className="uploadSpinner" size={14}/> : <RotateCcw size={14}/>}重试全部失败
          </button>}
        </div>
        <div className="rolloutProgress" aria-label={`推送进度 ${progress}%`}>
          <span style={{ width: `${progress}%` }}/>
        </div>
        <div className="rolloutMetrics">
          <span>等待 <strong>{selectedBatch.waitingCount}</strong></span>
          <span>更新中 <strong>{selectedBatch.updatingCount}</strong></span>
          <span>成功 <strong>{selectedBatch.appliedCount + selectedBatch.revokedCount}</strong></span>
          <span>失败 <strong>{selectedBatch.failedCount}</strong></span>
        </div>
        <div className="rolloutUserList">
          {detail?.users.map((user) => <article key={user.userId}>
            <header><strong>{user.userName || user.userCode || `用户 #${user.userId}`}</strong><span data-state={user.status}>{statusLabel(user.status)}</span></header>
            {user.profiles.map((profile) => <div className="rolloutProfileRow" key={profile.targetId}>
              <div>
                <strong>{profile.profileName}</strong>
                <small>{profile.profileKind} · Profile #{profile.runtimeAgentProfileId} · 目标/当前 revision {profile.targetRevision}/{profile.appliedRevision}</small>
                {profile.errorMessage && <em>{profile.errorCode}: {profile.errorMessage}</em>}
              </div>
              <span data-state={profile.status}>{statusLabel(profile.status)}</span>
              {profile.status === "failed" && <button type="button" disabled={busy !== null} onClick={() => void retry(profile.targetId)}>重试</button>}
            </div>)}
          </article>)}
        </div>
      </div>}
    </div>}
  </section>;
}
