import { useMemo, useRef, useState, useEffect } from "react";
import type { FormEvent } from "react";
import {
  AlertTriangle,
  BookOpen,
  ChevronLeft,
  ChevronRight,
  Database,
  GitBranch,
  ListChecks,
  LoaderCircle,
  RefreshCw,
  Search,
  ShieldCheck
} from "lucide-react";
import {
  adminApi,
  type KnowledgeGovernanceAssetDetail,
  type KnowledgeGovernanceAssetList,
  type KnowledgeGovernanceImpact,
  type KnowledgeGovernanceMetricContracts,
  type KnowledgeGovernanceNode,
  type KnowledgeGovernanceSummary,
  type KnowledgeGovernanceValidation
} from "../api/client";

type GovernanceView =
  | "overview"
  | "validation"
  | "assets"
  | "relationships"
  | "impact"
  | "contracts";
type FindingLevel = "all" | "ERROR" | "WARN" | "INFO";
type ContractSeverity = "all" | "error" | "warning";
type RelationshipDepth = "one" | "two";

const governanceViews: Array<{ key: GovernanceView; label: string }> = [
  { key: "overview", label: "治理总览" },
  { key: "validation", label: "校验中心" },
  { key: "assets", label: "资产目录" },
  { key: "relationships", label: "关系浏览" },
  { key: "impact", label: "影响分析" },
  { key: "contracts", label: "指标契约" }
];

const nodeTypeLabels: Record<string, string> = {
  semantic_model: "语义模型",
  physical_table: "物理表",
  field: "字段",
  department: "部门",
  permission_policy: "权限策略",
  relationship: "关系定义",
  metric_contract: "指标契约",
  document: "文档",
  instruction_section: "指令章节",
  skill: "技能",
  glossary: "术语表",
  methodology: "方法论",
  policy: "制度",
  role: "角色",
  training: "培训",
  faq: "FAQ",
  sop: "SOP",
  wiki_query: "Wiki 查询",
  wiki_tag: "Wiki 标签"
};

function typeLabel(type: string): string {
  return nodeTypeLabels[type] ?? type;
}

function formatDate(value?: string | null): string {
  if (!value) return "尚无记录";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}

function displayValue(value: unknown): string {
  if (Array.isArray(value)) return value.map((item) => String(item)).join("、");
  if (typeof value === "boolean") return value ? "是" : "否";
  if (value && typeof value === "object") return JSON.stringify(value);
  return String(value ?? "—");
}

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}

function CacheNotice({
  summary
}: {
  summary: KnowledgeGovernanceSummary | null;
}) {
  if (!summary || (summary.cache.state !== "stale" && summary.latestAttempt.status !== "failed")) {
    return null;
  }
  return (
    <div className="knowledgeGovernanceStale" role="status">
      <AlertTriangle size={17} aria-hidden />
      <div>
        <strong>当前显示上一次成功的治理结果</strong>
        <span>
          {summary.latestAttempt.error?.message ?? "最新一次扫描失败，请检查知识源后重新扫描。"}
        </span>
      </div>
    </div>
  );
}

function EmptySelection({ onOpenAssets }: { onOpenAssets: () => void }) {
  return (
    <div className="knowledgeGovernanceEmpty">
      <GitBranch size={28} aria-hidden />
      <strong>尚未选择知识资产</strong>
      <p>先从资产目录选择一个节点，再查看它的关系和变更影响。</p>
      <button type="button" className="secondaryButton" onClick={onOpenAssets}>
        打开资产目录
      </button>
    </div>
  );
}

export function AdminKnowledgeGovernance() {
  const [view, setView] = useState<GovernanceView>("overview");
  const [summary, setSummary] = useState<KnowledgeGovernanceSummary | null>(null);
  const [validation, setValidation] = useState<KnowledgeGovernanceValidation | null>(null);
  const [contracts, setContracts] = useState<KnowledgeGovernanceMetricContracts | null>(null);
  const [assets, setAssets] = useState<KnowledgeGovernanceAssetList | null>(null);
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
  const [assetDetail, setAssetDetail] = useState<KnowledgeGovernanceAssetDetail | null>(null);
  const [impact, setImpact] = useState<KnowledgeGovernanceImpact | null>(null);
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [assetType, setAssetType] = useState("");
  const [findingLevel, setFindingLevel] = useState<FindingLevel>("all");
  const [findingQuery, setFindingQuery] = useState("");
  const [contractSeverity, setContractSeverity] = useState<ContractSeverity>("all");
  const [relationshipDepth, setRelationshipDepth] = useState<RelationshipDepth>("one");
  const [loading, setLoading] = useState(true);
  const [assetsLoading, setAssetsLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const detailRequest = useRef(0);

  async function loadAssets(
    page = 1,
    nextQuery = appliedQuery,
    nextType = assetType
  ) {
    setAssetsLoading(true);
    try {
      const result = await adminApi.knowledgeGovernanceAssets({
        query: nextQuery || undefined,
        assetType: nextType || undefined,
        page,
        pageSize: 50
      });
      setAssets(result);
    } catch (reason) {
      setError(errorMessage(reason, "知识资产目录加载失败"));
    } finally {
      setAssetsLoading(false);
    }
  }

  async function loadInitial() {
    setLoading(true);
    setError(null);
    const results = await Promise.allSettled([
      adminApi.knowledgeGovernanceSummary(),
      adminApi.knowledgeGovernanceValidation(),
      adminApi.knowledgeGovernanceMetricContracts(),
      adminApi.knowledgeGovernanceAssets({ page: 1, pageSize: 50 })
    ]);
    if (results[0].status === "fulfilled") setSummary(results[0].value);
    if (results[1].status === "fulfilled") setValidation(results[1].value);
    if (results[2].status === "fulfilled") setContracts(results[2].value);
    if (results[3].status === "fulfilled") setAssets(results[3].value);
    const failed = results.find(
      (result): result is PromiseRejectedResult => result.status === "rejected"
    );
    if (failed) setError(errorMessage(failed.reason, "知识治理数据加载失败"));
    setLoading(false);
  }

  useEffect(() => {
    void loadInitial();
  }, []);

  async function refresh() {
    setRefreshing(true);
    setError(null);
    try {
      const nextSummary = await adminApi.refreshKnowledgeGovernance();
      setSummary(nextSummary);
      const [nextValidation, nextContracts, nextAssets] = await Promise.all([
        adminApi.knowledgeGovernanceValidation(),
        adminApi.knowledgeGovernanceMetricContracts(),
        adminApi.knowledgeGovernanceAssets({
          query: appliedQuery || undefined,
          assetType: assetType || undefined,
          page: assets?.page ?? 1,
          pageSize: 50
        })
      ]);
      setValidation(nextValidation);
      setContracts(nextContracts);
      setAssets(nextAssets);
      if (selectedAssetId) await selectAsset(selectedAssetId);
    } catch (reason) {
      setError(errorMessage(reason, "知识治理重新扫描失败"));
    } finally {
      setRefreshing(false);
    }
  }

  async function selectAsset(assetId: string) {
    const requestId = ++detailRequest.current;
    setSelectedAssetId(assetId);
    setDetailLoading(true);
    setError(null);
    try {
      const [detail, nextImpact] = await Promise.all([
        adminApi.knowledgeGovernanceAsset(assetId),
        adminApi.knowledgeGovernanceImpact(assetId)
      ]);
      if (requestId !== detailRequest.current) return;
      setAssetDetail(detail);
      setImpact(nextImpact);
    } catch (reason) {
      if (requestId !== detailRequest.current) return;
      setAssetDetail(null);
      setImpact(null);
      setError(errorMessage(reason, "知识资产详情加载失败"));
    } finally {
      if (requestId === detailRequest.current) setDetailLoading(false);
    }
  }

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const nextQuery = query.trim();
    setAppliedQuery(nextQuery);
    void loadAssets(1, nextQuery, assetType);
  }

  const filteredFindings = useMemo(() => {
    const needle = findingQuery.trim().toLocaleLowerCase();
    return (validation?.validation.findings ?? []).filter((finding) => {
      if (findingLevel !== "all" && finding.level !== findingLevel) return false;
      if (!needle) return true;
      return `${finding.code} ${finding.message} ${finding.source ?? ""} ${finding.assetId ?? ""}`
        .toLocaleLowerCase()
        .includes(needle);
    });
  }, [findingLevel, findingQuery, validation]);

  const filteredContracts = useMemo(
    () => (contracts?.metricContracts.rules ?? []).filter(
      (rule) => contractSeverity === "all" || rule.severity === contractSeverity
    ),
    [contractSeverity, contracts]
  );

  const affectedByType = useMemo(() => {
    const grouped = new Map<string, KnowledgeGovernanceNode[]>();
    for (const node of impact?.affected ?? []) {
      grouped.set(node.type, [...(grouped.get(node.type) ?? []), node]);
    }
    return [...grouped.entries()].sort(([left], [right]) => left.localeCompare(right));
  }, [impact]);

  const openSelectedView = (nextView: "relationships" | "impact") => {
    setView(nextView);
    document.querySelector<HTMLElement>(".adminWorkspace")?.scrollTo?.({
      top: 0,
      behavior: "smooth"
    });
  };

  return (
    <section className="adminPanel knowledgeGovernance" aria-label="知识治理">
      <div className="panelHeader knowledgeGovernanceHeader">
        <div>
          <h2>企业知识治理</h2>
          <p>检查语义模型、权限、关系和指标契约，并在变更前评估影响范围。</p>
        </div>
        <button
          type="button"
          className="secondaryButton"
          onClick={() => void refresh()}
          disabled={refreshing || loading}
        >
          {refreshing
            ? <LoaderCircle className="knowledgeGovernanceSpinner" size={15} aria-hidden />
            : <RefreshCw size={15} aria-hidden />}
          {refreshing ? "扫描中" : "重新扫描"}
        </button>
      </div>

      <div className="knowledgeGovernanceTabs" role="tablist" aria-label="知识治理视图">
        {governanceViews.map((item) => (
          <button
            key={item.key}
            type="button"
            role="tab"
            aria-selected={view === item.key}
            onClick={() => setView(item.key)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {error && (
        <div className="adminInlineError knowledgeGovernanceError" role="alert">
          <strong>治理数据暂不可用</strong>
          <span>{error}</span>
          {!summary && (
            <button type="button" onClick={() => void loadInitial()}>
              重试
            </button>
          )}
        </div>
      )}
      <CacheNotice summary={summary} />

      {loading && !summary ? (
        <div className="knowledgeGovernanceLoading" role="status">
          <LoaderCircle className="knowledgeGovernanceSpinner" size={18} aria-hidden />
          正在编译知识治理快照…
        </div>
      ) : (
        <>
          {view === "overview" && (
            <div className="knowledgeGovernanceOverview">
              <div className="knowledgeGovernanceMetrics">
                <article>
                  <Database size={18} aria-hidden />
                  <span>治理节点</span>
                  <strong>{summary?.summary.nodeCount ?? 0}</strong>
                  <small>{summary?.summary.edgeCount ?? 0} 条关系</small>
                </article>
                <article>
                  <BookOpen size={18} aria-hidden />
                  <span>语义模型</span>
                  <strong>{summary?.summary.modelCount ?? 0}</strong>
                  <small>{summary?.summary.fieldCount ?? 0} 个字段</small>
                </article>
                <article>
                  <GitBranch size={18} aria-hidden />
                  <span>关系定义</span>
                  <strong>{summary?.summary.relationshipCount ?? 0}</strong>
                  <small>{summary?.summary.permissionCount ?? 0} 条权限策略</small>
                </article>
                <article data-status={summary?.validationSummary.status ?? "unknown"}>
                  <ShieldCheck size={18} aria-hidden />
                  <span>一致性校验</span>
                  <strong>{summary?.validationSummary.errors ?? 0}</strong>
                  <small>
                    错误 · {summary?.validationSummary.warnings ?? 0} 警告
                  </small>
                </article>
                <article>
                  <ListChecks size={18} aria-hidden />
                  <span>指标契约</span>
                  <strong>{summary?.summary.metricContractCount ?? 0}</strong>
                  <small>
                    {contracts?.metricContracts.summary.errors ?? 0} 阻断 ·{" "}
                    {contracts?.metricContracts.summary.warnings ?? 0} 提醒
                  </small>
                </article>
              </div>

              <div className="knowledgeGovernanceOverviewGrid">
                <section>
                  <h3>节点构成</h3>
                  <div className="knowledgeGovernanceTypeList">
                    {Object.entries(summary?.summary.nodeTypes ?? {})
                      .sort(([, left], [, right]) => right - left)
                      .map(([type, count]) => (
                        <div key={type}>
                          <span>{typeLabel(type)}</span>
                          <strong>{count}</strong>
                        </div>
                      ))}
                  </div>
                </section>
                <section>
                  <h3>快照状态</h3>
                  <dl className="knowledgeGovernanceFacts">
                    <dt>当前状态</dt>
                    <dd>
                      <span className="knowledgeGovernanceBadge" data-status={summary?.cache.state}>
                        {summary?.cache.state === "stale" ? "使用旧快照" : "已同步"}
                      </span>
                    </dd>
                    <dt>生成时间</dt>
                    <dd>{formatDate(summary?.generatedAt)}</dd>
                    <dt>最近扫描</dt>
                    <dd>{formatDate(summary?.cache.lastAttemptAt)}</dd>
                    <dt>源版本</dt>
                    <dd className="knowledgeGovernanceHash">{summary?.sourceHash.slice(0, 12) ?? "—"}</dd>
                  </dl>
                </section>
              </div>
            </div>
          )}

          {view === "validation" && (
            <div className="knowledgeGovernanceValidation">
              <div className="knowledgeGovernanceToolbar">
                <div className="knowledgeGovernanceSegmented" role="group" aria-label="校验级别">
                  {([
                    ["all", "全部"],
                    ["ERROR", `错误 ${validation?.validation.summary.errors ?? 0}`],
                    ["WARN", `警告 ${validation?.validation.summary.warnings ?? 0}`],
                    ["INFO", `提示 ${validation?.validation.summary.info ?? 0}`]
                  ] as Array<[FindingLevel, string]>).map(([key, label]) => (
                    <button
                      type="button"
                      key={key}
                      aria-pressed={findingLevel === key}
                      onClick={() => setFindingLevel(key)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <label className="knowledgeGovernanceSearch">
                  <Search size={15} aria-hidden />
                  <span className="srOnly">搜索校验结果</span>
                  <input
                    value={findingQuery}
                    onChange={(event) => setFindingQuery(event.target.value)}
                    placeholder="搜索规则、文件或资产"
                  />
                </label>
              </div>
              <div className="knowledgeGovernanceFindingList">
                {filteredFindings.map((finding, index) => (
                  <article
                    key={`${finding.code}:${finding.source ?? ""}:${index}`}
                    data-level={finding.level}
                  >
                    <div>
                      <span>{finding.level}</span>
                      <strong>{finding.code}</strong>
                    </div>
                    <p>{finding.message}</p>
                    {(finding.source || finding.assetId) && (
                      <small>
                        {[finding.source, finding.assetId].filter(Boolean).join(" · ")}
                      </small>
                    )}
                  </article>
                ))}
                {filteredFindings.length === 0 && (
                  <div className="knowledgeGovernanceEmpty compact">
                    <ShieldCheck size={26} aria-hidden />
                    <strong>
                      {validation?.validation.summary.status === "passed"
                        ? "当前校验已通过"
                        : "当前筛选下没有结果"}
                    </strong>
                    <p>
                      {validation?.validation.summary.status === "passed"
                        ? "知识源未发现一致性错误或警告。"
                        : "请调整级别或搜索条件。"}
                    </p>
                  </div>
                )}
              </div>
            </div>
          )}

          {view === "assets" && (
            <div className="knowledgeGovernanceAssets">
              <form className="knowledgeGovernanceAssetFilters" onSubmit={submitSearch}>
                <label className="knowledgeGovernanceSearch">
                  <Search size={15} aria-hidden />
                  <span className="srOnly">搜索知识资产</span>
                  <input
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="按名称、ID 或来源搜索"
                  />
                </label>
                <label>
                  <span className="srOnly">资产类型</span>
                  <select
                    value={assetType}
                    onChange={(event) => {
                      const nextType = event.target.value;
                      setAssetType(nextType);
                      void loadAssets(1, appliedQuery, nextType);
                    }}
                  >
                    <option value="">全部类型</option>
                    {Object.keys(summary?.summary.nodeTypes ?? {}).map((type) => (
                      <option key={type} value={type}>{typeLabel(type)}</option>
                    ))}
                  </select>
                </label>
                <button type="submit" className="primaryButton">查询</button>
                <span>{assets?.total ?? 0} 项资产</span>
              </form>

              <div className="knowledgeGovernanceAssetLayout">
                <div className="knowledgeGovernanceAssetList" aria-busy={assetsLoading}>
                  {assetsLoading && (
                    <div className="knowledgeGovernanceLoading">
                      <LoaderCircle className="knowledgeGovernanceSpinner" size={16} aria-hidden />
                      正在加载资产…
                    </div>
                  )}
                  {!assetsLoading && assets?.items.map((asset) => (
                    <button
                      type="button"
                      key={asset.id}
                      data-selected={selectedAssetId === asset.id}
                      onClick={() => void selectAsset(asset.id)}
                    >
                      <span className="knowledgeGovernanceBadge">{typeLabel(asset.type)}</span>
                      <strong>{asset.name}</strong>
                      <small>{asset.id}</small>
                      <em>{asset.sources[0] ?? "无来源记录"}</em>
                    </button>
                  ))}
                  {!assetsLoading && (assets?.items.length ?? 0) === 0 && (
                    <div className="knowledgeGovernanceEmpty compact">
                      <Database size={25} aria-hidden />
                      <strong>没有匹配的知识资产</strong>
                      <p>请调整关键词或资产类型。</p>
                    </div>
                  )}
                  {(assets?.totalPages ?? 0) > 1 && (
                    <div className="knowledgeGovernancePagination">
                      <button
                        type="button"
                        aria-label="上一页"
                        disabled={(assets?.page ?? 1) <= 1 || assetsLoading}
                        onClick={() => void loadAssets((assets?.page ?? 1) - 1)}
                      >
                        <ChevronLeft size={15} aria-hidden />
                      </button>
                      <span>{assets?.page ?? 1} / {assets?.totalPages ?? 1}</span>
                      <button
                        type="button"
                        aria-label="下一页"
                        disabled={(assets?.page ?? 1) >= (assets?.totalPages ?? 1) || assetsLoading}
                        onClick={() => void loadAssets((assets?.page ?? 1) + 1)}
                      >
                        <ChevronRight size={15} aria-hidden />
                      </button>
                    </div>
                  )}
                </div>

                <aside className="knowledgeGovernanceAssetDetail" aria-label="知识资产详情">
                  {detailLoading ? (
                    <div className="knowledgeGovernanceLoading">
                      <LoaderCircle className="knowledgeGovernanceSpinner" size={16} aria-hidden />
                      正在加载详情…
                    </div>
                  ) : assetDetail ? (
                    <>
                      <header>
                        <span className="knowledgeGovernanceBadge">
                          {typeLabel(assetDetail.asset.type)}
                        </span>
                        <h3>{assetDetail.asset.name}</h3>
                        <small>{assetDetail.asset.id}</small>
                      </header>
                      <dl className="knowledgeGovernanceFacts">
                        <dt>来源</dt>
                        <dd>{assetDetail.asset.sources.join("、") || "—"}</dd>
                        {Object.entries(assetDetail.asset.properties).slice(0, 8).map(([key, value]) => (
                          <div key={key}>
                            <dt>{key}</dt>
                            <dd>{displayValue(value)}</dd>
                          </div>
                        ))}
                      </dl>
                      <div className="knowledgeGovernanceDetailActions">
                        <button
                          type="button"
                          className="secondaryButton"
                          onClick={() => openSelectedView("relationships")}
                        >
                          查看关系
                        </button>
                        <button
                          type="button"
                          className="primaryButton"
                          onClick={() => openSelectedView("impact")}
                        >
                          分析影响
                        </button>
                      </div>
                    </>
                  ) : (
                    <div className="knowledgeGovernanceEmpty compact">
                      <BookOpen size={25} aria-hidden />
                      <strong>选择一项资产查看详情</strong>
                      <p>详情仅展示治理快照中的安全元数据。</p>
                    </div>
                  )}
                </aside>
              </div>
            </div>
          )}

          {view === "relationships" && (
            <div className="knowledgeGovernanceRelationships">
              {!assetDetail ? (
                <EmptySelection onOpenAssets={() => setView("assets")} />
              ) : (
                <>
                  <div className="knowledgeGovernanceSectionHeader">
                    <div>
                      <span className="knowledgeGovernanceBadge">
                        {typeLabel(assetDetail.asset.type)}
                      </span>
                      <h3>{assetDetail.asset.name}</h3>
                      <small>{assetDetail.asset.id}</small>
                    </div>
                    <div className="knowledgeGovernanceSegmented" role="group" aria-label="关系深度">
                      <button
                        type="button"
                        aria-pressed={relationshipDepth === "one"}
                        onClick={() => setRelationshipDepth("one")}
                      >
                        一跳关系
                      </button>
                      <button
                        type="button"
                        aria-pressed={relationshipDepth === "two"}
                        onClick={() => setRelationshipDepth("two")}
                      >
                        两跳影响
                      </button>
                    </div>
                  </div>

                  {relationshipDepth === "one" ? (
                    <div className="knowledgeGovernanceRelationList">
                      {assetDetail.relationships.map((edge) => (
                        <article key={edge.id}>
                          <span>{edge.from === assetDetail.asset.id ? "下游" : "上游"}</span>
                          <div>
                            <strong>{edge.type}</strong>
                            <small>
                              {edge.from === assetDetail.asset.id ? edge.to : edge.from}
                            </small>
                          </div>
                          <em>{edge.sources[0] ?? "—"}</em>
                        </article>
                      ))}
                      {assetDetail.relationships.length === 0 && (
                        <div className="knowledgeGovernanceEmpty compact">
                          <GitBranch size={25} aria-hidden />
                          <strong>该资产没有一跳关系</strong>
                        </div>
                      )}
                    </div>
                  ) : (
                    <div className="knowledgeGovernanceRelationList">
                      {(impact?.affected ?? []).map((node) => (
                        <button type="button" key={node.id} onClick={() => void selectAsset(node.id)}>
                          <span>
                            {assetDetail.dependency?.downstream.includes(node.id) ? "一跳" : "两跳"}
                          </span>
                          <div>
                            <strong>{node.name}</strong>
                            <small>{node.id}</small>
                          </div>
                          <em>{typeLabel(node.type)}</em>
                        </button>
                      ))}
                      {(impact?.affected.length ?? 0) === 0 && (
                        <div className="knowledgeGovernanceEmpty compact">
                          <GitBranch size={25} aria-hidden />
                          <strong>该资产没有两跳内的下游节点</strong>
                        </div>
                      )}
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          {view === "impact" && (
            <div className="knowledgeGovernanceImpact">
              {!assetDetail || !impact ? (
                <EmptySelection onOpenAssets={() => setView("assets")} />
              ) : (
                <>
                  <div className="knowledgeGovernanceSectionHeader">
                    <div>
                      <span className="knowledgeGovernanceBadge">
                        {typeLabel(assetDetail.asset.type)}
                      </span>
                      <h3>{assetDetail.asset.name}</h3>
                      <small>变更将影响 {impact.affected.length} 项下游资产</small>
                    </div>
                    <button type="button" className="secondaryButton" onClick={() => setView("assets")}>
                      更换资产
                    </button>
                  </div>

                  <div className="knowledgeGovernanceImpactGrid">
                    <section>
                      <h3>受影响资产</h3>
                      {affectedByType.map(([type, nodes]) => (
                        <div className="knowledgeGovernanceImpactGroup" key={type}>
                          <header>
                            <span>{typeLabel(type)}</span>
                            <strong>{nodes.length}</strong>
                          </header>
                          {nodes.map((node) => (
                            <button type="button" key={node.id} onClick={() => void selectAsset(node.id)}>
                              <strong>{node.name}</strong>
                              <small>{node.id}</small>
                            </button>
                          ))}
                        </div>
                      ))}
                      {affectedByType.length === 0 && (
                        <p className="muted">两跳范围内没有下游依赖。</p>
                      )}
                    </section>
                    <section>
                      <h3>建议验证动作</h3>
                      <ol className="knowledgeGovernanceActions">
                        {impact.recommendedActions.map((action) => (
                          <li key={action}>{action}</li>
                        ))}
                      </ol>
                      <p className="knowledgeGovernanceReadOnly">
                        此处只展示建议，不会从管理页执行脚本或修改知识源。
                      </p>
                    </section>
                  </div>
                </>
              )}
            </div>
          )}

          {view === "contracts" && (
            <div className="knowledgeGovernanceContracts">
              <div className="knowledgeGovernanceContractSummary">
                <article>
                  <span>契约总数</span>
                  <strong>{contracts?.metricContracts.summary.total ?? 0}</strong>
                </article>
                <article data-severity="error">
                  <span>执行前阻断</span>
                  <strong>{contracts?.metricContracts.summary.errors ?? 0}</strong>
                </article>
                <article data-severity="warning">
                  <span>结果提醒</span>
                  <strong>{contracts?.metricContracts.summary.warnings ?? 0}</strong>
                </article>
                <article>
                  <span>模型覆盖</span>
                  <strong>
                    {contracts?.metricContracts.summary.coveredModels ?? 0}/
                    {contracts?.metricContracts.summary.totalModels ?? 0}
                  </strong>
                </article>
              </div>
              <div className="knowledgeGovernanceSegmented" role="group" aria-label="契约级别">
                {([
                  ["all", "全部"],
                  ["error", "阻断规则"],
                  ["warning", "提醒规则"]
                ] as Array<[ContractSeverity, string]>).map(([key, label]) => (
                  <button
                    type="button"
                    key={key}
                    aria-pressed={contractSeverity === key}
                    onClick={() => setContractSeverity(key)}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <div className="knowledgeGovernanceContractList">
                {filteredContracts.map((rule) => (
                  <article key={rule.id} data-severity={rule.severity}>
                    <header>
                      <span>{rule.severity === "error" ? "执行前阻断" : "结果提醒"}</span>
                      <strong>{rule.id}</strong>
                    </header>
                    {rule.messages.map((message) => <p key={message}>{message}</p>)}
                    <dl>
                      <dt>适用表</dt>
                      <dd>{rule.appliesToTables.join("、") || "按来源模型派生"}</dd>
                      <dt>规则来源</dt>
                      <dd>{rule.source}</dd>
                    </dl>
                  </article>
                ))}
              </div>

              {(contracts?.metricContracts.reviewedExemptions.length ?? 0) > 0 && (
                <details className="knowledgeGovernanceExemptions">
                  <summary>
                    已审查豁免（{contracts?.metricContracts.reviewedExemptions.length}）
                  </summary>
                  {contracts?.metricContracts.reviewedExemptions.map((exemption) => (
                    <div key={exemption.source}>
                      <strong>{exemption.source}</strong>
                      <span>{exemption.rationale}</span>
                    </div>
                  ))}
                </details>
              )}
            </div>
          )}
        </>
      )}
    </section>
  );
}
