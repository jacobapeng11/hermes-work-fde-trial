import { useCallback, useEffect, useMemo, useState } from "react";
import { RefreshCw, Save } from "lucide-react";
import type { AdminWeKnoraRerankConfigResponse } from "@hermes-cloud/schemas";
import { adminApi } from "../api/client";

type LoadState = "loading" | "ready" | "failed";
const DISABLED_VALUE = "disabled";

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

function modelLabel(id: string): string {
  return id.length > 12 ? `${id.slice(0, 12)}…` : id;
}

export function AdminWeKnoraRerankConfig() {
  const [snapshot, setSnapshot] = useState<AdminWeKnoraRerankConfigResponse | null>(null);
  const [state, setState] = useState<LoadState>("loading");
  const [selectedModelId, setSelectedModelId] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState("loading");
    setError(null);
    setNotice(null);
    try {
      const nextSnapshot = await adminApi.weKnoraRerankConfig();
      setSnapshot(nextSnapshot);
      setSelectedModelId(
        nextSnapshot.mode === "disabled"
          ? DISABLED_VALUE
          : nextSnapshot.configuredModelId || nextSnapshot.effectiveModelId
      );
      setState("ready");
    } catch (reason) {
      setError(errorMessage(reason, "WeKnora Rerank 配置加载失败"));
      setState("failed");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const models = snapshot?.models ?? [];
  const isDisabled = snapshot?.mode === "disabled";
  const configuredModel = useMemo(
    () => models.find((model) => model.id === snapshot?.configuredModelId) ?? null,
    [models, snapshot?.configuredModelId]
  );
  const effectiveModel = useMemo(
    () => models.find((model) => model.id === snapshot?.effectiveModelId) ?? null,
    [models, snapshot?.effectiveModelId]
  );

  async function save() {
    if (!selectedModelId.trim()) {
      setError("请选择一个 WeKnora Rerank 模型，或选择不使用重排模型。");
      return;
    }
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const disabled = selectedModelId === DISABLED_VALUE;
      const nextSnapshot = await adminApi.updateWeKnoraRerankConfig(
        disabled
          ? { mode: "disabled" }
          : { mode: "model", rerankModelId: selectedModelId.trim() }
      );
      setSnapshot(nextSnapshot);
      setSelectedModelId(
        nextSnapshot.mode === "disabled"
          ? DISABLED_VALUE
          : nextSnapshot.configuredModelId
      );
      setNotice(
        nextSnapshot.mode === "disabled"
          ? "已关闭 WeKnora 重排，后续个人知识检索按召回顺序返回。"
          : "Rerank 默认模型已保存，后续检索将使用该模型。"
      );
    } catch (reason) {
      setError(errorMessage(reason, "WeKnora Rerank 配置保存失败"));
    } finally {
      setSaving(false);
    }
  }

  const canSave = state === "ready" && (isDisabled || models.length > 0 || selectedModelId === DISABLED_VALUE);

  return (
    <section className="modelConfigEditor weknoraRerankConfig" aria-label="WeKnora Rerank 配置">
      <div className="modelConfigEditorHeader">
        <div>
          <h3>WeKnora Rerank 配置</h3>
          <p>选择空间级默认重排模型，或为 Hermes 个人知识检索关闭重排。</p>
        </div>
        <div className="modelConfigActions">
          <button
            type="button"
            className="secondaryButton"
            onClick={() => void load()}
            disabled={saving || state === "loading"}
          >
            <RefreshCw size={15} aria-hidden />
            刷新
          </button>
          <button
            type="button"
            className="primaryButton"
            onClick={() => void save()}
            disabled={saving || !canSave}
          >
            <Save size={15} aria-hidden />
            {saving ? "保存中" : "保存配置"}
          </button>
        </div>
      </div>

      {state === "loading" && <p className="muted">正在加载 WeKnora Rerank 配置…</p>}
      {state === "failed" && <p className="adminError">{error}</p>}
      {state === "ready" && (
        <>
          <div className="weknoraRerankStatus" data-source={snapshot?.configSource ?? "none"}>
            <div>
              <span>当前配置</span>
              <strong>
                {isDisabled
                  ? "不使用重排模型"
                  : snapshot?.configSource === "tenant"
                    ? configuredModel?.displayName ?? modelLabel(snapshot?.configuredModelId ?? "")
                    : "未显式设置"}
              </strong>
              <small>
                {isDisabled
                  ? "Hermes 个人知识检索将显式传 rerank.enabled=false"
                  : snapshot?.configSource === "tenant"
                    ? `模型 ID：${modelLabel(snapshot?.configuredModelId ?? "")}`
                    : "WeKnora 将自动选择第一个可用 Rerank 模型"}
              </small>
            </div>
            <div>
              <span>实际生效</span>
              <strong>{isDisabled ? "已关闭" : effectiveModel?.displayName ?? "无可用模型"}</strong>
              <small>
                {isDisabled
                  ? "检索结果按召回顺序返回"
                  : effectiveModel
                    ? `来源：${snapshot?.configSource === "tenant" ? "空间配置" : "自动选择"} · ID：${modelLabel(effectiveModel.id)}`
                    : "请在 WeKnora 中先配置并激活一个 Rerank 模型"}
              </small>
            </div>
          </div>

          {!isDisabled && snapshot?.configSource === "tenant" && !configuredModel && (
            <p className="weknoraRerankWarning" role="alert">
              当前空间配置引用的模型不可用，请重新选择并保存。
            </p>
          )}
          {!isDisabled && snapshot?.configSource === "auto" && (
            <p className="weknoraRerankWarning">
              当前依赖自动选择。空间内新增多个 Rerank 模型后选择可能变化，建议显式保存一个默认模型。
            </p>
          )}
          {isDisabled && (
            <p className="weknoraRerankWarning">
              关闭后检索速度通常更快，但结果排序精度可能下降；企业知识中间平台不自动跟随此开关。
            </p>
          )}

          <label className="weknoraRerankSelect">
            <span>Rerank 模型</span>
            <select
              value={selectedModelId}
              onChange={(event) => {
                setSelectedModelId(event.target.value);
                setError(null);
                setNotice(null);
              }}
              disabled={saving}
            >
              <option value={DISABLED_VALUE}>不使用重排模型</option>
              {models.length === 0 && <option value="">暂无可用模型</option>}
              {models.map((model) => (
                <option key={model.id} value={model.id}>
                  {model.displayName || model.name}
                </option>
              ))}
            </select>
          </label>

          {error && <p className="adminError" role="alert">{error}</p>}
          {notice && <p className="weknoraRerankNotice" role="status">{notice}</p>}
        </>
      )}
    </section>
  );
}
