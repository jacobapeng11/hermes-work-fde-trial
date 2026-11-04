export interface RuntimeWorkspaceSummary {
  runtimeWorkspaceId: number;
  cloudProfileId: number;
  orgId: number;
  userId: number;
  name: string;
  status: string;
  defaultModelId?: number | null;
  runtimeStatus?: string | null;
  pendingReload: boolean;
  updatedAt?: string | null;
}

export interface RuntimeStartupContextSummary {
  runtimeWorkspaceId?: number | null;
  context: "first_allocation" | "environment_recovery" | "none";
}

export interface SkillCategorySummary {
  categoryKey: string;
  displayName: string;
  isBuiltin: boolean;
  isMarketVisible?: boolean;
  isPersonalVisible?: boolean;
  scope?: "system" | "organization" | "personal" | "uncategorized";
  /** @deprecated legacy admin UI compatibility during cutover */
  isVisible?: boolean;
}

export interface ProfileSkillCatalogItem {
  assetId: number;
  name: string;
  description?: string | null;
  categoryKey: string;
  currentVersionLabel: string;
  state: "installable" | "current" | "update_available";
  configuredAssetId?: number | null;
  installedVersionLabel?: string | null;
  operating: boolean;
  executionStatus?: "enabled" | "force_disabled";
}

export interface ProfileSkillConfiguredItem {
  assetId: number;
  source: "market" | "skillhub" | "personal" | "hermes" | "application" | "system";
  applicationAgentSkillId?: number | null;
  name: string;
  description?: string | null;
  categoryKey: string;
  versionLabel: string;
  updateStatus: "current" | "update_available" | "source_removed" | "not_applicable";
  catalogAssetId?: number | null;
  skillhubNamespace?: string | null;
  skillhubPublicSlug?: string | null;
  availableVersionLabel?: string | null;
  lastCheckError?: string | null;
  operating: boolean;
}

export interface SkillHubMarketItem {
  namespace: string;
  publicSlug: string;
  name: string;
  description?: string | null;
  categoryKey: string;
  upstreamCategory?: string | null;
  versionLabel: string;
  downloads: number;
  installs: number;
  state: "installable" | "current" | "update_available";
  installedSkillId?: number | null;
  installedVersionLabel?: string | null;
  operating: boolean;
}

export interface SkillHubMarketPage {
  enabled: boolean;
  items: SkillHubMarketItem[];
  nextCursor?: string | null;
}

export interface PendingSkillOperation {
  operationId: string;
  action: "install" | "sync" | "uninstall" | "delete" | "personal_upload";
  targetKey: string;
  status: "queued" | "running" | "failed";
  submittedAt: string;
  /** Present only for the short-lived browser operation created before 202. */
  submissionPhase?: "submitting" | "accepted";
  errorCode?: string | null;
  errorMessage?: string | null;
}

export interface PlazaSkillView {
  marketItems: ProfileSkillCatalogItem[];
  skillhubEnabled?: boolean;
  installedItems: ProfileSkillConfiguredItem[];
  categories: SkillCategorySummary[];
  pendingOperations: PendingSkillOperation[];
}

export interface SkillOperationAccepted {
  operationId: string;
  status: "queued" | "running";
  targetKey?: string;
}

export interface DefaultProfileAssetItem {
  assetType: "skill" | "mcp";
  assetId: number;
  currentVersionId?: number | null;
  assetScope?: "public" | "personal" | null;
  version?: number | null;
  configured?: boolean;
  desired: boolean;
  applied: boolean;
  name: string;
  description?: string | null;
  status: string;
  categoryKey?: string | null;
  transport?: string | null;
}

export interface DefaultProfileAssetView {
  runtimeWorkspaceId: number;
  runtimeAgentProfileId: number;
  desiredRevision: number;
  appliedRevision: number;
  convergenceStatus: string;
  lastError?: string | null;
  skills: DefaultProfileAssetItem[];
  mcps: DefaultProfileAssetItem[];
}

export interface UserAssetCatalogView {
  skills: DefaultProfileAssetItem[];
  mcps: DefaultProfileAssetItem[];
}

export interface DefaultProfileAssetOperationAccepted {
  operationId: string;
  runtimeWorkspaceId: number;
  runtimeAgentProfileId: number;
  assetType: "skill" | "mcp";
  assetId: number;
  action: "install" | "uninstall";
  desiredRevision: number;
  appliedRevision: number;
  convergenceStatus: string;
}

/** MCP presentation model shared by default-profile and application-Agent
 * configuration. `desired` is the requested configuration; `applied` is the
 * capability currently visible to a newly created Agent. */
export interface ProfileMcpAssetItem {
  assetId: number;
  currentVersionId?: number | null;
  slug: string;
  name: string;
  description?: string | null;
  transport: string;
  status: string;
  desired: boolean;
  applied: boolean;
  convergenceStatus: string;
  errorMessage?: string | null;
}

export interface ProfileMcpAssetDetail {
  asset: ProfileMcpAssetItem;
  detail: Record<string, unknown>;
}

export interface EnterpriseKnowledgeSettings {
  runtimeWorkspaceId: number;
  enabled: boolean;
  mode: "full";
  updatedAt?: string | null;
}

export interface EnterpriseKnowledgeSettingsUpdate {
  enabled: boolean;
}

export type PersonalKnowledgeRole = "owner" | "download" | "preview" | "retrieve";
export type PersonalKnowledgeFileStatus = "pending" | "parsing" | "ready" | "failed";

export interface PersonalKnowledgeCreate {
  name: string;
  description?: string | null;
}

export type PersonalKnowledgeUpdate = PersonalKnowledgeCreate;

export interface PersonalKnowledgeSummary {
  knowledgeBaseId: number;
  name: string;
  description?: string | null;
  ownerUserId: number;
  ownerName: string;
  effectiveRole: PersonalKnowledgeRole;
  runtimeEnabled: boolean;
  fileCount: number;
  readyFileCount: number;
  visibility: "private" | "shared";
  status: string;
  createdAt: string;
  updatedAt: string;
}

export interface PersonalKnowledgeListResponse {
  items: PersonalKnowledgeSummary[];
  total: number;
}

export interface PersonalKnowledgeDetail extends PersonalKnowledgeSummary {
  canEnter: boolean;
  canPreview: boolean;
  canDownload: boolean;
  canManage: boolean;
}

export interface PersonalKnowledgePermissionTarget {
  userIds: number[];
  departmentIds: string[];
  all: boolean;
}

export interface PersonalKnowledgePermissionReplace {
  download: PersonalKnowledgePermissionTarget;
  preview: PersonalKnowledgePermissionTarget;
  retrieve: PersonalKnowledgePermissionTarget;
}

export interface PersonalKnowledgeSelectedUser {
  id: number;
  name?: string | null;
  userCode?: string | null;
  status: "active" | "inactive" | "missing";
}

export type PersonalKnowledgePermissionGrant =
  | {
      targetType: "department";
      targetId: string;
      name: string;
      departmentPath: string[];
      role: Exclude<PersonalKnowledgeRole, "owner">;
    }
  | {
      targetType: "user";
      targetId: number;
      name?: string | null;
      userCode?: string | null;
      departmentId?: string | null;
      departmentPath: string[];
      status: "active" | "inactive" | "missing";
      role: Exclude<PersonalKnowledgeRole, "owner">;
    };

export interface PersonalKnowledgePermissionResponse extends PersonalKnowledgePermissionReplace {
  normalizedTargets: string[];
  selectedUsers?: PersonalKnowledgeSelectedUser[];
  grants: PersonalKnowledgePermissionGrant[];
}

export interface PersonalKnowledgeFileSummary {
  fileId: number;
  filename: string;
  sizeBytes: number;
  mimeType?: string | null;
  status: PersonalKnowledgeFileStatus;
  uploaderId: number;
  uploaderName?: string | null;
  updatedAt: string;
}

export interface PersonalKnowledgeFileListResponse {
  items: PersonalKnowledgeFileSummary[];
  total: number;
}

export interface DirectoryUserSearchItem {
  id: number;
  name: string;
  userCode?: string | null;
}

export interface DepartmentTreeNode {
  departmentId: string;
  name: string;
  parentId?: string | null;
  parentName?: string | null;
  virtual?: boolean;
  children: DepartmentTreeNode[];
}

export type PersonalKnowledgePermissionDirectoryNode =
  | {
      nodeType: "department";
      departmentId: string;
      name: string;
      parentId?: string | null;
      parentName?: string | null;
      virtual?: boolean;
      children: PersonalKnowledgePermissionDirectoryNode[];
    }
  | {
      nodeType: "user";
      /** 平台 User 表 id；通讯录全量展示时未开户人员为 null，前端须置灰不可选。 */
      userId: number | null;
      /** 是否完成首次登录；预开户账号登录前仍显示“未激活”。 */
      loggedIn?: boolean;
      name: string;
      userCode?: string | null;
      departmentId?: string | null;
      status: "active";
    };

export type OrganizationDirectoryNode =
  | {
      nodeType: "organization";
      organizationId: number;
      name: string;
      children: OrganizationDirectoryNode[];
    }
  | {
      nodeType: "department";
      departmentId: string;
      name: string;
      parentId?: string | null;
      parentName?: string | null;
      virtual?: boolean;
      children: OrganizationDirectoryNode[];
    }
  | {
      nodeType: "user";
      userId: number | null;
      /** 是否完成首次登录；预开户账号登录前仍显示“未激活”。 */
      loggedIn?: boolean;
      name: string;
      userCode?: string | null;
      departmentId?: string | null;
      status: "active";
    };

export interface PersonalKnowledgeBindingResponse {
  runtimeWorkspaceId: number;
  runtimeAgentProfileId?: number | null;
  knowledgeBaseId: number;
  enabled: boolean;
  effectiveFrom: "next_turn";
  warning?: "no_ready_files" | null;
}

export interface RuntimePersonalKnowledgeCapabilities {
  rag: boolean;
  wiki: boolean;
}

export interface RuntimePersonalKnowledgeItem {
  id: number;
  name: string;
  description?: string | null;
  readyFileCount: number;
  capabilities?: RuntimePersonalKnowledgeCapabilities | null;
}

export interface RuntimePersonalKnowledgeState {
  enabled: boolean;
  knowledgeBases: RuntimePersonalKnowledgeItem[];
  revision: string;
}

export interface RuntimePersonalKnowledgeSearchRequest {
  query: string;
  knowledgeBaseIds?: number[] | null;
}

export interface RuntimePersonalKnowledgeMatch {
  knowledgeBaseId: number;
  knowledgeBaseName: string;
  fileId: number;
  filename: string;
  chunkId?: string | null;
  content: string;
  score?: number | null;
  page?: number | null;
  sourceUrl: string;
  imageSourceUrl?: string | null;
}

export interface RuntimePersonalKnowledgeSearchResponse {
  query: string;
  matches: RuntimePersonalKnowledgeMatch[];
  lowConfidence: boolean;
  searchedKnowledgeBaseIds: number[];
}

export interface RuntimePersonalKnowledgeSourceFile {
  fileId: number;
  filename: string;
  sourceUrl: string;
}

export interface RuntimePersonalKnowledgeWikiMatch {
  sourceType: "wiki_page";
  knowledgeBaseId: number;
  knowledgeBaseName: string;
  slug: string;
  title: string;
  summary?: string | null;
  matchSnippet?: string | null;
  sourceUrl: string;
  sourceFiles: RuntimePersonalKnowledgeSourceFile[];
}

export type RuntimePersonalKnowledgeWikiSearchRequest =
  RuntimePersonalKnowledgeSearchRequest;

export interface RuntimePersonalKnowledgeWikiSearchResponse {
  query: string;
  matches: RuntimePersonalKnowledgeWikiMatch[];
  lowConfidence: boolean;
  searchedKnowledgeBaseIds: number[];
}

export interface RuntimePersonalKnowledgeHybridSearchResponse {
  query: string;
  ragResults: RuntimePersonalKnowledgeMatch[];
  wikiResults: RuntimePersonalKnowledgeWikiMatch[];
  ragSearchedKnowledgeBaseIds: number[];
  wikiSearchedKnowledgeBaseIds: number[];
  lowConfidence: boolean;
}

export interface RuntimePersonalKnowledgeWikiReadPageRequest {
  knowledgeBaseId: number;
  slug: string;
}

export interface RuntimePersonalKnowledgeWikiPage {
  sourceType: "wiki_page";
  knowledgeBaseId: number;
  knowledgeBaseName: string;
  slug: string;
  title: string;
  content: string;
  sourceUrl: string;
  sourceFiles: RuntimePersonalKnowledgeSourceFile[];
}

export interface LoginRequest {
  identifier: string;
  password: string;
  orgCode?: string;
}

export interface AdminLoginRequest {
  username: string;
  password: string;
}

export interface AuthPrincipalSummary {
  orgId: number;
  userId: number;
  name: string;
  email?: string | null;
  userCode?: string | null;
  canManageApplicationAgents?: boolean;
  isAdmin?: boolean;
}

export interface AuthTokenResponse {
  accessToken: string;
  tokenType: string;
  expiresIn?: number | null;
  principal: AuthPrincipalSummary;
}

export interface TaskPromptTask {
  summary: string;
  detail: string;
}

export interface TaskPromptTopic {
  title: string;
  tasks: TaskPromptTask[];
}

export interface TaskPromptTab {
  label: string;
  description: string;
  isDefault: boolean;
  showBoardGallery: boolean;
  topics: TaskPromptTopic[];
}

export interface TaskPromptConfigDocument {
  tabs: TaskPromptTab[];
}

export interface TaskPromptConfigState {
  revision: number;
  config?: TaskPromptConfigDocument | null;
}

export interface TaskPromptConfigSaveRequest {
  revision: number;
  config: TaskPromptConfigDocument;
}

export interface TaskPromptConfigSaveResponse {
  revision: number;
  config: TaskPromptConfigDocument;
}

export interface RuntimeRunSummary {
  runId: number;
  runtimeWorkspaceId: number;
  runtimeRunId: string;
  sessionId?: number | null;
  modelEntryId?: number | null;
  status: string;
  startedAt?: string | null;
  finishedAt?: string | null;
}

export interface RuntimeModelSummary {
  modelEntryId: number;
  modelKey: string;
  displayName: string;
  providerId: number;
  providerDisplayName: string;
  contextLength?: number | null;
  tags?: string[] | null;
  isDefault: boolean;
  supportsVision?: boolean;
}

export interface DefaultModelUpdateResponse {
  runtimeWorkspaceId: number;
  modelEntryId: number;
  projectionScope: string;
  pendingReload: boolean;
}

export interface RuntimeCapabilitySummary {
  assetType: string;
  assetId: number;
  displayName: string;
  status: string;
  sourceType?: string | null;
}

export interface RuntimeCapabilitiesResponse {
  runtimeWorkspaceId: number;
  experts: RuntimeCapabilitySummary[];
  skills: RuntimeCapabilitySummary[];
  mcps: RuntimeCapabilitySummary[];
  knowledgeBases: RuntimeCapabilitySummary[];
}

export interface RuntimeSessionLatestRunSummary {
  runtimeRunId: string;
  status: string;
  startedAt?: string | null;
  finishedAt?: string | null;
}

export interface RuntimeSessionSummary {
  sessionId: number;
  runtimeWorkspaceId: number;
  runtimeAgentProfileId?: number | null;
  runtimeSessionId: string;
  title?: string | null;
  sourceType: string;
  messageCount: number;
  sessionModelEntryId?: number | null;
  status: string;
  lastActiveAt?: string | null;
  agentDisplayName?: string | null;
  agentAvatarUrl?: string | null;
  agentProfileKind?: "default" | "expert" | null;
  isReadOnly?: boolean;
  isImmutable?: boolean;
  originLabel?: string | null;
  unavailableReason?: "expert_deleted" | null;
  scheduledTaskEligible?: boolean;
  latestRun?: RuntimeSessionLatestRunSummary | null;
  contextSourceRuntimeSessionId?: string | null;
  contextHeadRuntimeSessionId?: string | null;
}

export type ImProvider = "feishu" | "wecom" | "wechat" | "dingtalk";
export type ImChannelStatus =
  | "unconfigured"
  | "connecting"
  | "connected"
  | "reconnecting"
  | "disabled"
  | "error"
  | "revoked";

export interface ImChannelCard {
  provider: ImProvider;
  displayName: string;
  available: boolean;
  enabled: boolean;
  status: ImChannelStatus;
  connectionMode?: string | null;
  botId?: string | null;
  secretConfigured: boolean;
  runtimeSessionId?: string | null;
  configGeneration?: number | null;
  lastConnectedAt?: string | null;
  lastMessageAt?: string | null;
  lastErrorCode?: string | null;
  lastErrorMessage?: string | null;
}

export interface ImChannelListResponse {
  channels: ImChannelCard[];
}

export interface ImQrOnboardingCreated {
  onboardingId: string;
  qrContent: string;
  expiresAt: string;
  pollAfterMs: number;
}

export interface ImQrOnboardingStatus {
  onboardingId: string;
  status:
    | "pending"
    | "scanned"
    | "credentials_ready"
    | "consumed"
    | "expired"
    | "failed";
  expiresAt: string;
  botId?: string | null;
  secretReady: boolean;
  errorCode?: string | null;
  errorMessage?: string | null;
}

export interface RegisterWeComChannelRequest {
  connectionMode: "websocket" | "url_callback";
  botId?: string;
  secret?: string;
  onboardingId?: string;
}

export type ApplicationWeComChannelStatus =
  | ImChannelStatus
  | "credentials_invalid"
  | "legacy_migration_required";

export interface ApplicationWeComChannel {
  channelId?: number | null;
  applicationAgentId: number;
  provider: "wecom";
  targetType: "application_agent";
  activationAllowed: boolean;
  enabled: boolean;
  status: ApplicationWeComChannelStatus;
  connectionMode: "websocket";
  botId?: string | null;
  secretConfigured: boolean;
  directEnabled: boolean;
  groupEnabled: boolean;
  groupTrigger: "mention_only";
  configGeneration?: number | null;
  lastConnectedAt?: string | null;
  lastMessageAt?: string | null;
  lastErrorCode?: string | null;
  lastErrorMessage?: string | null;
  legacyMigrationRequired: boolean;
}

export interface ApplicationWeComUser {
  identityId: number | null;
  platformUserId: number;
  userName: string;
  userCode?: string | null;
  memberIdMasked: string;
  status: "active" | "revoked";
  firstSeenAt: string;
  lastSeenAt: string;
  lastChatType: "direct" | "group";
}

export interface ApplicationWeComUserList {
  users: ApplicationWeComUser[];
  total: number;
}

export interface ApplicationWeComMessage {
  inboundMessageId: number;
  chatType: "direct" | "group";
  status: string;
  identityStatus: string;
  senderPlatformUserId?: number | null;
  receivedAt: string;
  firstProviderDispatchAt?: string | null;
  completedAt?: string | null;
}

export interface ApplicationWeComMessageList {
  messages: ApplicationWeComMessage[];
}

export interface SessionModelSelectionRequest {
  modelEntryId: number;
}

export interface SessionModelSelectionResponse {
  runtimeWorkspaceId: number;
  runtimeSessionId: string;
  modelEntryId: number;
  appliesTo: string;
  activeRunCount: number;
}

export interface CreateRuntimeSessionRequest {
  title?: string | null;
  modelEntryId?: number | null;
  sourceType: string;
  runtimeAgentProfileId?: number | null;
  contextSourceRuntimeSessionId?: string | null;
}

export interface ExpertCapabilityReference {
  source: "public" | "personal" | "default_installed";
  assetId: number;
  versionId?: number | null;
}

export interface ExpertCapabilitySelection {
  skillIds: number[];
  mcpIds: number[];
  knowledgeBaseIds: number[];
  enterpriseKnowledgeEnabled: boolean;
  pluginIds: string[];
  skills?: ExpertCapabilityReference[];
  mcps?: ExpertCapabilityReference[];
  knowledgeBases?: ExpertCapabilityReference[];
}

export interface AvatarAssetSummary {
  avatarValue: string;
  label: string;
  sourceKind: "builtin" | "organization" | "user_upload" | string;
  imageUrl?: string | null;
  sortOrder: number;
}

export interface ExpertShareScope {
  organization: boolean;
  userIds: number[];
  departmentIds: string[];
}

export interface RuntimeAgentProfileSummary {
  runtimeAgentProfileId: number;
  runtimeWorkspaceId: number;
  hermesProfileName: string;
  profileKind: "default" | "expert";
  displayName: string;
  description?: string | null;
  avatarUrl?: string | null;
  soulContent: string;
  ownershipType: "default" | "self_created" | "market_installed" | string;
  inheritedFromProfileId?: number | null;
  expertPackageId?: number | null;
  localVersion?: number | null;
  installedVersionId?: number | null;
  installedVersion?: number | null;
  availableVersionId?: number | null;
  updateAvailable: boolean;
  desiredRevision?: number;
  appliedRevision?: number;
  configuredExpertVersionId?: number | null;
  appliedExpertVersionId?: number | null;
  convergenceStatus?: "unconfigured" | "updating" | "applied" | "error" | "disabled" | "revoked" | string;
  effectiveStatus?: "unconfigured" | "updating" | "awaiting_activation" | "error" | "disabled" | "revoked" | string;
  marketRemoved?: boolean;
  capabilities: ExpertCapabilitySelection;
  shareScope: ExpertShareScope;
  shareScopeUsers: PersonalKnowledgeSelectedUser[];
  reviewId?: number | null;
  reviewStatus?: "pending" | "approved" | "rejected" | null;
  reviewComment?: string | null;
  lastOperationId?: string | null;
  lastOperationAction?: "create" | "install" | "update" | "configure" | "delete" | string | null;
  lastOperationStatus?: "running" | "succeeded" | "failed" | string | null;
  lastOperationStep?: string | null;
  lastOperationError?: string | null;
  allowedActions: ExpertAllowedActions;
  status: string;
  lastError?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
}

export interface ExpertAllowedActions {
  viewDetail: boolean;
  install: boolean;
  uninstall: boolean;
  summon: boolean;
  edit: boolean;
  copy: boolean;
  publish: boolean;
  unlist: boolean;
  delete: boolean;
}

export type ExpertErrorCode =
  | "EXPERT_NOT_VISIBLE"
  | "EXPERT_NAME_CONFLICT"
  | "EXPERT_REVIEW_STATE_CONFLICT"
  | "EXPERT_INSTALLATION_DISABLED"
  | "PROFILE_CORE_UNAVAILABLE"
  | "SKILL_VERSION_UNAVAILABLE"
  | "MCP_REGISTRY_UNAVAILABLE"
  | "KNOWLEDGE_BASE_NO_PERMISSION"
  | "CAPABILITY_FORCE_DISABLED";

export interface ExpertOperationSummary {
  operationId: string;
  action: string;
  status: "pending" | "running" | "succeeded" | "failed";
  stage: string;
  targetId?: number | null;
  profileId?: number | null;
  completedUnits?: number | null;
  totalUnits?: number | null;
  errorCode?: string | null;
  errorMessage?: string | null;
}

export interface ExpertMarketItem {
  expertPackageId: number;
  expertPackageVersionId: number;
  name: string;
  description?: string | null;
  avatarUrl?: string | null;
  providerName: string;
  version: number;
  installed: boolean;
  installedRuntimeAgentProfileId?: number | null;
  catalogStatus: "published" | "unlisted" | "deleted";
  executionStatus: "enabled" | "force_disabled";
  allowedActions: ExpertAllowedActions;
  operation?: ExpertOperationSummary | null;
  updatedAt?: string | null;
}

export interface ExpertMarketCapabilitySummary {
  name: string;
  description?: string | null;
}

export interface ExpertMarketDetail {
  expertPackageId: number;
  expertPackageVersionId: number;
  name: string;
  description?: string | null;
  avatarUrl?: string | null;
  providerName: string;
  version: number;
  installed: boolean;
  installedRuntimeAgentProfileId?: number | null;
  catalogStatus: "published" | "unlisted" | "deleted";
  executionStatus: "enabled" | "force_disabled";
  allowedActions: ExpertAllowedActions;
  soulContent: string;
  systemPrompt?: string | null;
  skills: ExpertMarketCapabilitySummary[];
  mcps: ExpertMarketCapabilitySummary[];
  knowledgeBases: ExpertMarketCapabilitySummary[];
}

export interface ExpertCapabilityCatalogItem {
  assetId: number;
  name: string;
  description?: string | null;
  kind?: string | null;
  source?: "public" | "personal" | "default_installed";
  versionId?: number | null;
  categoryKey?: string | null;
  runtimeSkillName?: string | null;
}

export interface ExpertSkillCategory {
  categoryKey: string;
  displayName: string;
}

export interface ExpertCapabilityCatalog {
  marketSkills: ExpertCapabilityCatalogItem[];
  installedSkills: ExpertCapabilityCatalogItem[];
  skillCategories: ExpertSkillCategory[];
  marketMcps: ExpertCapabilityCatalogItem[];
  installedMcps: ExpertCapabilityCatalogItem[];
  knowledgeBases: ExpertCapabilityCatalogItem[];
  personalSkills: ExpertCapabilityCatalogItem[];
  personalKnowledgeBases: ExpertCapabilityCatalogItem[];
  pluginIds: string[];
}

export interface CreateExpertRequest {
  runtimeWorkspaceId: number;
  name: string;
  description?: string | null;
  avatarUrl?: string | null;
  soulContent: string;
  inheritedFromProfileId?: number | null;
  capabilities?: ExpertCapabilitySelection;
  shareScope: ExpertShareScope;
  changeSummary?: string | null;
}

export interface UpdateExpertRequest {
  name?: string;
  description?: string | null;
  avatarUrl?: string | null;
  soulContent?: string;
  changeSummary?: string | null;
}

export interface UpdateExpertConfigurationRequest {
  name: string;
  description?: string | null;
  avatarUrl?: string | null;
  soulContent: string;
  capabilities: ExpertCapabilitySelection;
  shareScope: ExpertShareScope;
  changeSummary?: string | null;
}

export interface ExpertReviewSummary {
  reviewId: number;
  expertPackageId: number;
  expertPackageVersionId: number;
  submitterUserId: number;
  name: string;
  description?: string | null;
  avatarUrl?: string | null;
  soulContent: string;
  systemPrompt?: string | null;
  capabilities: ExpertCapabilitySelection;
  shareScope: ExpertShareScope;
  shareScopeUsers: PersonalKnowledgeSelectedUser[];
  changeSummary?: string | null;
  status: "pending" | "approved" | "rejected";
  reviewComment?: string | null;
  executionStatus: "enabled" | "force_disabled";
  executionStatusReason?: string | null;
  activeInstallationCount: number;
  createdAt?: string | null;
  reviewedAt?: string | null;
  updatedAt?: string | null;
}

export interface RenameRuntimeSessionRequest {
  title: string;
}

export interface RuntimeChatRequest {
  runtimeWorkspaceId: number;
  runtimeSessionId?: string | null;
  modelEntryId?: number | null;
  message: string;
  sourceType: string;
  attachmentFileIds?: number[];
  readyReceiptId?: string | null;
  runtimeRunId?: string | null;
  parentRuntimeRunId?: string | null;
}

export interface RuntimeChatRunSummary {
  runtimeWorkspaceId: number;
  runtimeSessionId: string;
  runtimeRunId: string;
  modelEntryId?: number | null;
  status: string;
}

export interface RuntimeRunActionRequest {
  runtimeRunId: string;
  afterSeq?: number;
  runtimeWorkspaceId?: number;
  runtimeSessionId?: string;
  modelEntryId?: number | null;
}

export type ScheduledTaskScheduleConfig =
  | { type: "once"; runAt: string }
  | { type: "interval"; unit: "minutes" | "hours"; value: number }
  | { type: "cron"; preset: "daily"; time: string }
  | { type: "cron"; preset: "weekly"; weekday: number; time: string };

export interface ScheduledTaskCommand {
  taskName: string;
  prompt: string;
  targetRuntimeSessionId: string;
  scheduleConfig: ScheduledTaskScheduleConfig;
}

export interface ScheduledTask extends ScheduledTaskCommand {
  taskId: number;
  status: "active" | "paused" | "completed";
  scheduleSummary: string;
  targetSessionTitle?: string | null;
  targetSessionStatus?: "active" | "deleting" | "deleted" | string | null;
  nextRunAt?: string | null;
  lastRunAt?: string | null;
  lastRunStatus?: "success" | "failed" | string | null;
  lastErrorCode?: string | null;
  lastError?: string | null;
  lastRuntimeRunId?: string | null;
  runCount: number;
  isRunning: boolean;
  createdAt?: string | null;
  updatedAt?: string | null;
}

export type CreateScheduledTaskRequest = ScheduledTaskCommand;
export type UpdateScheduledTaskRequest = ScheduledTaskCommand;

export interface ScheduledTaskTriggerResponse {
  taskId: number;
  targetRuntimeSessionId: string;
  acceptedAt: string;
}

export interface RuntimeChatHistoryMessage extends Record<string, unknown> {
  role: "user" | "assistant" | "system" | "tool";
  kind?: string;
  content?: string;
  runtimeRunId?: string | null;
  messageSeq?: number | null;
  eventSeq?: number | null;
  clientMessageId?: string | null;
  contextAgentProfileId?: number | null;
}

export interface RuntimeChatHistoryResponse {
  runtimeWorkspaceId: number;
  runtimeSessionId: string;
  source: "hermes" | "mysql";
  consistency: "authoritative" | "projection_preview";
  generatedAt: string;
  hasActiveRun: boolean;
  messages: RuntimeChatHistoryMessage[];
}

export interface RuntimeRunActivityEvent {
  event: string;
  data: Record<string, unknown>;
}

export interface RuntimeRunActivityResponse {
  runtimeWorkspaceId: number;
  runtimeSessionId: string;
  runtimeRunId: string;
  status: string;
  events: RuntimeRunActivityEvent[];
}

export interface ChatFileSummary {
  fileId: number;
  runtimeWorkspaceId: number;
  runtimeSessionId?: string | null;
  runtimeRunId?: string | null;
  relativePath: string;
  displayName: string;
  mimeType?: string | null;
  sizeBytes: number;
  sourceType: string;
  createdAt?: string | null;
}

/** Safe platform index entry. It intentionally contains no runtime endpoint or local path. */
export interface ChatUploadResponse {
  file: ChatFileSummary;
}

export interface ChatFileTransferIntent {
  file: ChatFileSummary;
  runtimePath: string;
  fileTransferTicket: string;
  expiresAt: string;
}

export interface ChatDownloadIntent {
  runtimePath: string;
  fileTransferTicket: string;
  expiresAt: string;
  displayName: string;
  mimeType?: string | null;
}

export interface OfficePreviewSession {
  documentServerUrl: string;
  config: Record<string, unknown>;
}

export interface VoiceTranscriptionResponse {
  transcript: string;
  attachmentFile?: ChatFileSummary | null;
}

export interface RegisterChatFileRequest {
  runtimeWorkspaceId: number;
  runtimeSessionId?: string | null;
  runtimeRunId?: string | null;
  relativePath: string;
  mimeType?: string | null;
  sizeBytes: number;
  sourceType: "upload" | "tool" | "export";
}

export interface RenameChatFileRequest {
  relativePath: string;
}

export interface AssetSummary {
  assetType: string;
  assetId: number;
  versionId?: number | null;
  slug: string;
  name: string;
  description?: string | null;
  status: string;
  visibility?: string | null;
  sourceType?: string | null;
}

export interface ExpertPackageSummary {
  expertPackageId: number;
  slug: string;
  name: string;
  description?: string | null;
  avatarUrl?: string | null;
  status: string;
  visibility: string;
  currentVersionId?: number | null;
  updatedAt?: string | null;
}

export interface ExpertPackageVersionSummary {
  expertPackageVersionId: number;
  expertPackageId: number;
  version: number;
  status: string;
  manifest: Record<string, unknown>;
  skillRefs?: Record<string, unknown>[] | null;
  mcpRefs?: Record<string, unknown>[] | null;
  knowledgeRefs?: Record<string, unknown>[] | null;
  modelPreferences?: Record<string, unknown> | null;
  createdAt?: string | null;
}

export interface PlazaTemplateSummary {
  templateId: number;
  slug: string;
  name: string;
  description?: string | null;
  iconUrl?: string | null;
  category?: string | null;
  tags: string[];
  visibility: string;
  status: string;
  currentVersionId?: number | null;
  sortOrder: number;
  updatedAt?: string | null;
}

export interface PersonalSkillAssetSummary {
  skillId: number;
  skillVersionId?: number | null;
  slug: string;
  version?: number | null;
  name: string;
  description?: string | null;
  categoryKey?: string;
  categoryScope?: "organization" | "personal" | "uncategorized";
  status: string;
  source: "personal_asset" | "hermes_managed";
  applied: boolean;
}

export interface SystemSkillSummary {
  key: string;
  name: string;
  description?: string | null;
  categoryKey: "system";
  source: "system";
}

export interface CreatePersonalSkillCategoryRequest {
  displayName: string;
}

export interface PersonalSkillAssetDetailSummary {
  asset: PersonalSkillAssetSummary;
  detail: Record<string, unknown>;
}

export interface PersonalSkillInstallAccepted {
  asset: PersonalSkillAssetSummary;
  operation: DefaultProfileAssetOperationAccepted;
}

export interface AdminAssetSummary {
  assetId: number;
  versionId?: number | null;
  assetType: "skill" | "mcp";
  slug: string;
  version: string;
  name: string;
  description?: string | null;
  status: string;
  marketVisible?: boolean;
  executionStatus?: "enabled" | "force_disabled";
  categoryKey?: string | null;
  currentVersionId?: number | null;
  storageUri: string;
  manifest: Record<string, unknown>;
  summary: Record<string, unknown>;
  createdAt?: string | null;
}

export interface SkillCategorySummary {
  categoryKey: string;
  displayName: string;
  isBuiltin: boolean;
  isMarketVisible?: boolean;
  isPersonalVisible?: boolean;
  scope?: "system" | "organization" | "personal" | "uncategorized";
  isVisible?: boolean;
}

export interface CreateSkillCategoryRequest {
  categoryKey: string;
  displayName: string;
}

export interface InstallExpertResponse {
  runtimeWorkspaceId: number;
  expertPackageId: number;
  expertPackageVersionId: number;
  installedExpertBindingId: number;
  status: string;
  projectionScope: string;
  pendingReload: boolean;
  warnings: string[];
}

export interface AdminRuntimeWorkspaceResourceSummary {
  runtimeWorkspaceId: number;
  cloudProfileId?: number | null;
  resourceKey: string;
  runtimeKind: "personal" | "application";
  applicationAgentId?: number | null;
  orgId: number;
  userId: number;
  employeeNo?: string | null;
  userName: string;
  runtimeName: string;
  profileStatus: string;
  bindingStatus?: string | null;
  runtimeStatus?: string | null;
  defaultModelEntryId?: number | null;
  defaultModelDisplayName?: string | null;
  sessionCount: number;
  runCount: number;
  activeRunCount: number;
  installedExpertCount: number;
  installedSkillCount: number;
  installedMcpCount: number;
  installedKnowledgeBaseCount: number;
  pendingReload: boolean;
  hasRuntimeBinding: boolean;
  kserviceName?: string | null;
  latestError?: string | null;
}

export interface AdminPrincipalSummary {
  orgId: number;
  userId: number;
  roles: string[];
}

export interface ApplicationAgentAdminCandidateSummary {
  userId?: number | null;
  userCode: string;
  name: string;
  departmentName?: string | null;
  status: string;
  isApplicationAgentAdmin: boolean;
}

export interface ApplicationAgentAdminSummary {
  userId: number;
  userCode?: string | null;
  name: string;
  departmentName?: string | null;
  grantedByUserId?: number | null;
  grantedAt?: string | null;
}

export interface ApplicationAgentConfig {
  systemPrompt: string;
  modelEntryId?: number | null;
  skillAssetIds: number[];
  applicationSkillIds?: number[];
  mcpAssetIds: number[];
  enterpriseKnowledgeEnabled: boolean;
  memoryEnabled: boolean;
}

export interface ApplicationAgentSkillSummary {
  applicationAgentSkillId: number;
  applicationAgentId: number;
  slug: string;
  name: string;
  description?: string | null;
  categoryKey: string;
  packageSha256: string;
  skillVersionId?: number | null;
  version?: number | null;
  createdAt?: string | null;
}

export interface ApplicationAgentAccessSummary {
  canManageApplicationAgents: boolean;
}

export interface ApplicationAgentSummary {
  applicationAgentId: number;
  slug: string;
  name: string;
  avatarUrl?: string | null;
  status: "unpublished" | "published" | "disabled";
  runtimeStatus: string;
  configGeneration: number;
  appliedGeneration?: number | null;
  applyStatus: string;
  lifecycleStatus: "active" | "deleting";
  deleteRequestedAt?: string | null;
  deleteLastErrorCode?: string | null;
  clientCount: number;
  activeRunCount: number;
  queuedRunCount: number;
  publishedAt?: string | null;
  updatedAt?: string | null;
  accessMode: "owner" | "viewer";
  creatorName?: string | null;
  creatorUserCode?: string | null;
  creatorStatus?: string | null;
  personalKnowledgeEnabledCount: number;
  personalKnowledgeRevokedCount: number;
}

export interface ApplicationAgentPersonalKnowledgeCandidate {
  knowledgeBaseId: number;
  name: string;
  description?: string | null;
  ownerUserId: number;
  ownerName: string;
  effectiveRole: PersonalKnowledgeRole;
  fileCount: number;
  readyFileCount: number;
  visibility: "private" | "shared";
  status: string;
  createdAt: string;
  updatedAt: string;
  selected: boolean;
}

export interface ApplicationAgentPersonalKnowledgeCatalog {
  items: ApplicationAgentPersonalKnowledgeCandidate[];
  enabledCount: number;
  revokedCount: number;
  configGeneration: number;
}

export interface ApplicationAgentDetail extends ApplicationAgentSummary {
  config: ApplicationAgentConfig;
  lastApplyErrorCode?: string | null;
}

export interface ApplicationAgentConfigRevisionSummary {
  revisionId: number;
  generation: number;
  configHash: string;
  applyStatus: string;
  appliedAt?: string | null;
  errorCode?: string | null;
  createdByUserId: number;
  createdAt?: string | null;
}

export interface ApplicationClientSummary {
  applicationClientId: number;
  clientId: string;
  name: string;
  status: string;
  allowUserDelegation: boolean;
  allowServiceInvocation: boolean;
  authMode: "delegated_user" | "service" | "legacy_dual" | "invalid";
  scopes: string[];
  allowedRedirectUris: string[];
  createdAt?: string | null;
}

export interface ApplicationClientCreated extends ApplicationClientSummary {
  clientSecret: string;
}

export interface ApplicationAgentWeComChannelSummary {
  channelId: number;
  applicationAgentId: number;
  name: string;
  corpId: string;
  wecomAgentId: number;
  status: "active" | "disabled";
  callbackUrl: string;
  entryUrl: string;
  secretsConfigured: boolean;
  updatedAt?: string | null;
}

export interface ApplicationAgentWeComBindingSummary {
  bindingId: number;
  wecomMemberIdMasked: string;
  platformUserId: number;
  status: "active" | "revoked";
  boundAt: string;
  lastUsedAt?: string | null;
}

export interface ApplicationAgentWeComBindingResponse {
  applicationAgentName: string;
  channelName: string;
  wecomMemberIdMasked: string;
  boundAt: string;
}

export interface AdminModelSummary {
  modelEntryId: number;
  modelKey: string;
  displayName: string;
  providerId: number;
  providerDisplayName: string;
  status: string;
  isDefault: boolean;
  isRestricted: boolean;
  contextLength?: number | null;
  tags: string[];
}

export interface AdminModelStatusRequest {
  status: string;
}

export interface ModelVisibilityScope {
  organization: boolean;
  userIds: number[];
  departmentIds: string[];
}

export interface AdminModelVisibilityUserSummary {
  id: number;
  name: string;
  userCode?: string | null;
  status: string;
}

export interface AdminModelConfigEntryRequest {
  providerName: string;
  providerDisplayName: string;
  providerType?: string;
  apiEndpoint: string;
  providerCredential?: string | null;
  modelKey: string;
  modelDisplayName: string;
  contextLength?: number | null;
  tags?: string[];
  isRestricted?: boolean;
  visibilityScope?: ModelVisibilityScope | null;
  modelStatus?: string;
  isDefault?: boolean;
  runtimeCapabilities?: RuntimeModelCapabilities;
}

export interface RuntimeModelCapabilities {
  // Admin API values are wire values; decamelize only changes object keys.
  reasoningReplay: "not_supported" | "optional" | "required";
  thinkingMode: "provider_default" | "enabled" | "disabled";
  vision?: "not_supported" | "native" | null;
}

export interface AdminModelCatalogPublishResponse {
  releaseId: number;
  catalogVersion: number;
  previousCatalogVersion?: number | null;
  configHash: string;
  status: string;
  publishedAt: string;
}

export interface AdminModelConfigEntrySummary {
  providerId: number;
  providerName: string;
  providerDisplayName: string;
  providerType: string;
  apiEndpoint?: string | null;
  credentialConfigured: boolean;
  providerStatus: string;
  providerCatalogVersion: number;
  modelEntryId: number;
  modelKey: string;
  modelDisplayName: string;
  contextLength?: number | null;
  tags: string[];
  isRestricted: boolean;
  visibilityScope: ModelVisibilityScope;
  visibilityUsers: AdminModelVisibilityUserSummary[];
  isDefault: boolean;
  modelStatus: string;
  modelCatalogVersion: number;
  runtimeCapabilities: RuntimeModelCapabilities;
}

export interface AdminModelCatalogReleaseSummary {
  releaseId: number;
  catalogVersion?: number | null;
  previousCatalogVersion?: number | null;
  configHash?: string | null;
  status: string;
  errorMessage?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
}

export interface AdminModelConfigResponse {
  entries: AdminModelConfigEntrySummary[];
  latestRelease?: AdminModelCatalogReleaseSummary | null;
}

export interface AdminWeKnoraRerankModelSummary {
  id: string;
  name: string;
  displayName: string;
  source?: string;
  status: string;
}

export interface AdminWeKnoraRerankConfigResponse {
  provider: "weknora";
  mode: "model" | "disabled";
  configuredModelId: string;
  effectiveModelId: string;
  configSource: "tenant" | "auto" | "none" | "disabled";
  models: AdminWeKnoraRerankModelSummary[];
}

export interface AdminWeKnoraRerankConfigUpdateRequest {
  mode: "model" | "disabled";
  rerankModelId?: string;
}

export interface AdminModelUpdateResponse {
  modelEntryId: number;
  status: string;
  isDefault: boolean;
  upgradeLogId?: number | null;
  affectedRuntimeWorkspaceCount: number;
}

export type RuntimeKubernetesStatus =
  | "scaled_to_zero"
  | "starting"
  | "ready"
  | "stopping"
  | "degraded"
  | "failed"
  | "unknown";

export interface AdminClusterSummary {
  nodeCount: number;
  readyNodeCount: number;
  namespaceCount: number;
  runningPodCount: number;
  readyRuntimeCount: number;
  scaledToZeroRuntimeCount: number;
  startingRuntimeCount: number;
  warningEventCount: number;
  knativeControlPlaneReady: boolean;
  collectedAt: string;
  capacity?: AdminClusterCapacity | null;
  stale: boolean;
  error?: string | null;
}

export interface AdminResourceAmount {
  cpuMillicores: number;
  memoryBytes: number;
}

export interface AdminResourceUsage {
  cpuMillicores?: number | null;
  memoryBytes?: number | null;
}

export interface AdminResourceRatios {
  cpu?: number | null;
  memory?: number | null;
}

export interface AdminClusterGroupCapacity {
  key: string;
  podCount: number;
  runningPodCount: number;
  pendingPodCount: number;
  requests: AdminResourceAmount;
  limits: AdminResourceAmount;
  actualUsage: AdminResourceUsage;
  missingRequestPodCount: number;
  missingMetricsPodCount: number;
}

export interface AdminNodeCapacity {
  name: string;
  capacity: AdminResourceAmount;
  allocatable: AdminResourceAmount;
  requests: AdminResourceAmount;
  limits: AdminResourceAmount;
  actualUsage: AdminResourceUsage;
  requestRatio: AdminResourceRatios;
  usageRatio: AdminResourceRatios;
  headroom: AdminResourceAmount;
  ready: boolean;
  pressureConditions: string[];
}

export interface AdminClusterCapacity {
  capacity: AdminResourceAmount;
  allocatable: AdminResourceAmount;
  requests: AdminResourceAmount;
  limits: AdminResourceAmount;
  actualUsage: AdminResourceUsage;
  requestRatio: AdminResourceRatios;
  usageRatio: AdminResourceRatios;
  headroom: AdminResourceAmount;
  pendingPodCount: number;
  pendingReasons: string[];
  pressureNodeCount: number;
  nodes: AdminNodeCapacity[];
  groups: AdminClusterGroupCapacity[];
  observedAt: string;
  metricsCoverage: number;
  missingMetricsPodCount: number;
  metricsStale: boolean;
}

export interface AdminKubernetesEventSummary {
  eventType: string;
  reason: string;
  message: string;
  involvedKind: string;
  involvedName: string;
  occurredAt?: string | null;
  count: number;
}

export interface AdminPodSummary {
  name: string;
  phase: string;
  ready: boolean;
  containers: string[];
  restartCount: number;
  nodeName?: string | null;
  createdAt?: string | null;
  deletionTimestamp?: string | null;
  waitingReason?: string | null;
  terminatedReason?: string | null;
  imageNames: string[];
  revisionName?: string | null;
  cpuUsageMillicores?: number | null;
  memoryUsageBytes?: number | null;
  resourceMetricsCollectedAt?: string | null;
  containerMetrics?: AdminContainerResourceSummary[];
}

export interface AdminContainerResourceSummary {
  name: string;
  cpuUsageMillicores?: number | null;
  memoryUsageBytes?: number | null;
}

export interface AdminRuntimeServiceSnapshot {
  role: "runtime";
  kserviceName: string;
  namespace: string;
  status: RuntimeKubernetesStatus;
  ready: boolean;
  latestCreatedRevision?: string | null;
  latestReadyRevision?: string | null;
  trafficRevision?: string | null;
  desiredScale?: number | null;
  actualScale?: number | null;
  active?: boolean | null;
  scaleDownDelaySeconds?: number | null;
  pods: AdminPodSummary[];
}

export interface AdminRuntimeKubernetesSnapshot {
  runtimeWorkspaceId: number;
  resourceKey: string;
  runtimeKind: "personal" | "application";
  applicationAgentId?: number | null;
  collectedAt: string;
  aggregateStatus: RuntimeKubernetesStatus;
  wakeBehavior: "allowed" | "blocked" | "unknown";
  runtimeService: AdminRuntimeServiceSnapshot;
  pods: AdminPodSummary[];
  recentEvents: AdminKubernetesEventSummary[];
  stateMismatch: boolean;
  error?: string | null;
}

export interface AdminRuntimeHealthSnapshot {
  runtimeWorkspaceId: number;
  runtimeKind: "personal" | "application";
  runtimeId: string;
  observedAt: string;
  receivedAt: string;
  stale: boolean;
  summary: {
    state?: string;
    errorCode?: string | null;
    configRevision?: string;
    configDigest?: string;
    storage?: {
      level?: string;
      usedPercent?: number;
      freeBytes?: number;
      reservePresent?: boolean;
    } | null;
    runs?: { active?: number; limit?: number; rejectedTotal?: number };
    websockets?: {
      active?: number;
      limit?: number;
      rejectedTotal?: number;
    };
    sessions?: {
      resident?: number;
      residentLimit?: number;
      warmIdle?: number;
      preparing?: number;
    };
    outbox?: {
      pendingCount?: number;
      oldestAgeSeconds?: number;
      writerQueueDepth?: number;
    };
    modelRelay?: {
      active?: number;
      capacity?: number;
      capacityRejections?: number;
      transportWarmup?: string;
    };
    sqlite?: {
      commitP95Ms?: number;
      writerQueueDepth?: number;
    };
    realtime?: {
      bufferBytes?: number;
      slowClientDisconnects?: number;
    };
  };
}

export interface AdminSafeKubernetesSummary {
  runtimeWorkspaceId: number;
  resourceKey: string;
  runtimeKind: "personal" | "application";
  applicationAgentId?: number | null;
  collectedAt: string;
  objects: Array<Record<string, unknown>>;
}

export interface AdminAuditLogSummary {
  auditLogId: number;
  orgId: number;
  actorUserId?: number | null;
  runtimeWorkspaceId?: number | null;
  action: string;
  targetType: string;
  targetId: string;
  detail?: Record<string, unknown> | null;
  createdAt?: string | null;
}

export interface AdminUsageOverviewSummary {
  runCount: number;
  activeRunCount: number;
  modelCallCount: number;
  auditLogCount: number;
  errorCount: number;
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  averageLatencyMs: number;
}

export interface AdminPresenceUserGroup {
  count: number;
  users: string[];
}

export interface AdminPresenceSummaryResponse {
  webOnline: AdminPresenceUserGroup;
  desktopOnline: AdminPresenceUserGroup;
  dailyLogins: AdminPresenceUserGroup;
  windowSeconds: number;
  dailyLoginSince: string;
  observedAt: string;
  source: string;
}

export interface AdminRuntimeWorkspaceUsageSummary {
  runtimeWorkspaceId: number;
  userId?: number | null;
  runCount: number;
  activeRunCount: number;
  modelCallCount: number;
  errorCount: number;
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  averageLatencyMs: number;
  lastError?: string | null;
}

export interface AdminModelUsageSummary {
  modelEntryId?: number | null;
  modelDisplayName?: string | null;
  providerId?: number | null;
  modelCallCount: number;
  errorCount: number;
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  averageLatencyMs: number;
}

export interface AdminUsageRecentErrorSummary {
  source: string;
  runtimeWorkspaceId?: number | null;
  runtimeRunId?: string | null;
  modelEntryId?: number | null;
  action?: string | null;
  errorCode?: string | null;
  message?: string | null;
  createdAt?: string | null;
}

export interface AdminUsageSummaryResponse {
  overview: AdminUsageOverviewSummary;
  runtimeWorkspaces: AdminRuntimeWorkspaceUsageSummary[];
  models: AdminModelUsageSummary[];
  recentErrors: AdminUsageRecentErrorSummary[];
}

// Runtime execution-plane protocol v1. Wire fields intentionally remain
// snake_case so Browser, Backend, and Supervisor use the same JSON fixtures.
export const RUNTIME_EDGE_SCHEMA_VERSION = 1 as const;
export const RUNTIME_INTERNAL_SCHEMA_VERSION = 1 as const;
export const RUNTIME_EDGE_WEBSOCKET_PROTOCOL = "hermes-runtime-v1" as const;
export const CHANNEL_FAST_LANE_V1_SCHEMA_VERSION = 1 as const;
export const CHANNEL_FAST_LANE_V2_SCHEMA_VERSION = 2 as const;

export interface RuntimeModelCapabilitiesWire {
  reasoning_replay: "not_supported" | "optional" | "required";
  thinking_mode: "provider_default" | "enabled" | "disabled";
  vision?: "not_supported" | "native" | null;
}

export function canonicalRuntimePayloadDigestInput(value: unknown): string {
  if (value === null || typeof value !== "object") {
    if (typeof value === "number" && !Number.isFinite(value)) {
      throw new TypeError("Runtime payload cannot contain non-finite numbers");
    }
    const serialized = JSON.stringify(value);
    if (serialized === undefined) {
      throw new TypeError("Runtime payload cannot contain undefined values");
    }
    return serialized;
  }
  if (Array.isArray(value)) {
    return `[${value.map(canonicalRuntimePayloadDigestInput).join(",")}]`;
  }
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record)
    .sort()
    .map(
      (key) =>
        `${JSON.stringify(key)}:${canonicalRuntimePayloadDigestInput(record[key])}`,
    )
    .join(",")}}`;
}

export interface RuntimeAccessClaims {
  token_type: "runtime_access";
  schema_version: 1;
  sub: string;
  user_id: number;
  org_id: number;
  runtime_id: string;
  admission_generation: number;
  allowed_origin: string;
  connection_nonce: string;
  issued_at: number;
  expires_at: number;
}

export interface ProviderRouteGrantV1 {
  version: 1;
  capsule: string;
  digest: `sha256:${string}`;
}

export interface SessionGrantClaims {
  token_type: "session_grant";
  schema_version: 1;
  user_id: number;
  org_id: number;
  runtime_id: string;
  admission_generation: number;
  session_id: string;
  profile_id: string;
  runtime_agent_profile_id: number;
  application_agent_id?: number | null;
  model_id: string;
  upstream_model_id: string;
  context_length: number;
  runtime_capabilities?: RuntimeModelCapabilitiesWire | null;
  provider_route?: ProviderRouteGrantV1 | null;
  config_revision: string;
  profile_desired_revision: number;
  profile_applied_revision: number;
  profile_digest: `sha256:${string}`;
  skill_digest: `sha256:${string}`;
  mcp_digest: `sha256:${string}`;
  knowledge_digest: `sha256:${string}`;
  max_prompt_bytes: number;
  max_output_tokens: number;
  attachments: Array<{
    file_id: string;
    display_name: string;
    relative_path: string;
    mime_type?: string | null;
  }>;
  issued_at: number;
  expires_at: number;
}

export interface RuntimeEdgeGrantRequest {
  runtime_workspace_id: number;
  runtime_session_id: string;
  model_entry_id?: number | null;
  attachment_file_ids: number[];
}

export interface RuntimeEdgeSessionGrantResponse {
  schema_version: 1;
  runtime_id: string;
  runtime_session_id: string;
  model_entry_id: number;
  profile_id: string;
  runtime_agent_profile_id: number;
  application_agent_id?: number | null;
  config_revision: string;
  profile_desired_revision: number;
  profile_applied_revision: number;
  profile_digest: `sha256:${string}`;
  skill_digest: `sha256:${string}`;
  mcp_digest: `sha256:${string}`;
  knowledge_digest: `sha256:${string}`;
  session_grant: string;
  session_grant_expires_at: string;
}

export interface ProfileComponentDigests {
  profile: `sha256:${string}`;
  skill: `sha256:${string}`;
  mcp: `sha256:${string}`;
  knowledge: `sha256:${string}`;
}

export interface ProfileArtifactManifestEntry {
  asset_type: "skill" | "personal_skill_artifact";
  asset_id: number;
  version_id: number;
  storage_uri: string;
  sha256: string;
  target_relative_path: string;
}

export interface ProfileCapabilityTarget {
  capability_type: "skill" | "mcp" | "knowledge_base";
  capability_id: number;
  target_version_id?: number | null;
  desired_digest?: `sha256:${string}` | null;
  required: boolean;
}

export interface ProfileCapabilityEvidence {
  capability_type: "skill" | "mcp" | "knowledge_base";
  capability_id: number;
  target_version_id?: number | null;
  status: "applied" | "unavailable" | "revoked";
  applied_digest?: `sha256:${string}` | null;
  error_code?: string | null;
  error_message?: string | null;
  retry_classification: "none" | "transient" | "deterministic" | "authorization";
}

export interface ProfileProjectionApplyRequest {
  schema_version: 1;
  operation_id: string;
  runtime_id: string;
  binding_id: number;
  runtime_workspace_id: number;
  runtime_agent_profile_id: number;
  hermes_profile_name: string;
  desired_revision: number;
  expected_applied_revision: number;
  expected_effective_generation: number;
  digests: ProfileComponentDigests;
  profile_config: Record<string, unknown>;
  artifacts: ProfileArtifactManifestEntry[];
  capabilities: ProfileCapabilityTarget[];
}

export interface ProfileProjectionApplyResponse {
  schema_version: 1;
  operation_id: string;
  runtime_agent_profile_id: number;
  desired_revision: number;
  applied_revision: number;
  effective_generation: number;
  status: "pending" | "applied" | "applied_degraded" | "failed" | "superseded" | "revoked";
  digests?: ProfileComponentDigests | null;
  capabilities: ProfileCapabilityEvidence[];
  error_code?: string | null;
  error_message?: string | null;
}

export interface ProfileExecutionControlRequest {
  schema_version: 1;
  operation_id: string;
  action: "revoke" | "enable";
  runtime_id: string;
  binding_id: number;
  runtime_workspace_id: number;
  runtime_agent_profile_id: number;
  admission_generation: number;
  reason_code: string;
  cancel_active_runs: boolean;
}

export interface ProfileExecutionControlResponse {
  schema_version: 1;
  operation_id: string;
  runtime_agent_profile_id: number;
  action: "revoke" | "enable";
  admission_generation: number;
  status: "applied" | "already_applied" | "failed";
  affected_active_run_ids: string[];
  error_code?: string | null;
  error_message?: string | null;
}

export interface CapabilityErrorEvent {
  type: "capability.error";
  data: {
    run_id: string;
    capability_type: "skill" | "mcp" | "knowledge_base";
    error_code:
      | "KNOWLEDGE_BASE_NO_PERMISSION"
      | "MCP_USER_AUTH_REQUIRED"
      | "CAPABILITY_FORCE_DISABLED"
      | "CAPABILITY_PROJECTION_UNAVAILABLE";
    display_message: string;
    recoverable: boolean;
    error_id: string;
  };
}

export interface RuntimeMcpRegistryServer {
  mcp_id: number;
  mcp_version_id: number;
  /** Stable internal alias (`asset-<mcp_id>`); not the catalog business slug. */
  server_slug: string;
  transport: "streamable_http" | "sse" | "stdio";
  connection_config: Record<string, unknown>;
  credential_refs: Record<string, string>;
  config_digest: `sha256:${string}`;
}

export interface RuntimeMcpRegistryApplyRequest {
  schema_version: 1;
  operation_id: string;
  runtime_id: string;
  binding_id: number;
  runtime_workspace_id: number;
  desired_revision: number;
  expected_applied_revision: number;
  registry_digest: `sha256:${string}`;
  servers: RuntimeMcpRegistryServer[];
}

export interface RuntimeMcpRegistryServerEvidence {
  server_slug: string;
  status: "ready" | "failed";
  tools_schema_digest?: `sha256:${string}` | null;
  tool_names: string[];
  error_code?: string | null;
}

export interface RuntimeMcpRegistryApplyResponse {
  schema_version: 1;
  operation_id: string;
  desired_revision: number;
  applied_revision: number;
  status: "reloading" | "ready" | "failed";
  registry_digest?: `sha256:${string}` | null;
  servers: RuntimeMcpRegistryServerEvidence[];
  affected_active_run_ids: string[];
  error_code?: string | null;
  error_message?: string | null;
}

export interface RuntimeEdgeBootstrapResponse
  extends RuntimeEdgeSessionGrantResponse {
  runtime_handle: string;
  websocket_path: string;
  websocket_protocol: "hermes-runtime-v1";
  runtime_access: string;
  runtime_access_expires_at: string;
}

export interface RuntimeEdgePrepareRequest {
  schema_version: 1;
  type: "session.prepare";
  request_id: string;
  session_grant: string;
  local_capabilities?: boolean;
}

/** Optional post-prepare capability advertisement, ignored by older Runtimes. */
export interface RuntimeEdgeDesktopCapabilitiesRequest {
  schema_version: 1;
  type: "desktop.capabilities";
  request_id: string;
  local_capabilities: boolean;
}

export interface RuntimeEdgeSessionHistoryRequest {
  schema_version: 1;
  type: "session.history";
  request_id: string;
  session_grant: string;
}

export interface RuntimeEdgeSessionQueueRequest {
  schema_version: 1;
  type: "session.queue";
  request_id: string;
  session_grant: string;
}

export interface RuntimeEdgeRunSubmitRequest {
  schema_version: 1;
  type: "run.submit";
  client_message_id: string;
  session_grant: string;
  prompt: string;
  attachment_file_ids: string[];
}

export interface RuntimeEdgeFollowupSubmitRequest {
  schema_version: 1;
  type: "followup.submit";
  client_message_id: string;
  parent_run_id: string;
  session_grant: string;
  prompt: string;
  attachment_file_ids: string[];
}

export interface RuntimeEdgeRunResumeRequest {
  schema_version: 1;
  type: "run.resume";
  run_id: string;
  after_seq: number;
}

export interface RuntimeEdgeRunCancelRequest {
  schema_version: 1;
  type: "run.cancel";
  run_id: string;
}

/** A request for a connected desktop to perform a bounded local operation. */
export interface RuntimeEdgeLocalCapabilityRequest {
  schema_version: 1;
  type: "local.capability.request";
  request_id: string;
  run_id: string;
  session_id: string;
  capability:
    | "workspace.list"
    | "workspace.read"
    | "workspace.write"
    | "workspace.mkdir"
    | "workspace.trash"
    | "command.run"
    | "browser.open"
    | "browser.session_start"
    | "browser.navigate"
    | "browser.snapshot"
    | "browser.click"
    | "browser.type"
    | "browser.press"
    | "browser.scroll"
    | "browser.screenshot"
    | "browser.session_close"
    | "browser.read"
    | "browser.wait"
    | "browser.hover"
    | "browser.select"
    | "browser.upload"
    | "browser.downloads"
    | "browser.back"
    | "browser.reload"
    | "browser.tab_list"
    | "browser.tab_new"
    | "browser.tab_select"
    | "browser.tab_close";
  args: Record<string, unknown>;
  reason?: string;
}

/** The desktop result for a local capability request. */
export interface RuntimeEdgeLocalCapabilityResult {
  schema_version: 1;
  type: "local.capability.result";
  request_id: string;
  run_id: string;
  capability: RuntimeEdgeLocalCapabilityRequest["capability"];
  ok: boolean;
  value?: unknown;
  error?: string;
  canceled?: boolean;
}

export interface RuntimeEdgeRunCancelAck {
  schema_version: 1;
  type: "run.cancel_ack";
  run_id: string;
  status: "accepted" | "already_terminal" | "failed";
}

export interface RuntimeEdgePrepareReady {
  schema_version: 1;
  type: "session.ready";
  request_id: string;
  session_id: string;
  config_revision: string;
}

export interface RuntimeEdgeSessionHistory {
  schema_version: 1;
  type: "session.history";
  request_id: string;
  session_id: string;
  messages: Record<string, unknown>[];
}

export interface RuntimeEdgeSessionQueue {
  schema_version: 1;
  type: "session.queue";
  request_id: string;
  session_id: string;
  queued_count: number;
  scheduled_queued_count: number;
  queued_runs: RuntimeEdgeQueuedRun[];
  active_run: RuntimeEdgeActiveRun | null;
}

export interface RuntimeEdgeQueuedRun {
  run_id: string;
  client_message_id: string;
  enqueue_seq: number;
  prompt: string;
  attachment_file_ids: string[];
}

export interface RuntimeEdgeActiveRun {
  run_id: string;
  state: string;
  cancellation_requested_at: string | null;
}

export interface RuntimeEdgeSessionQueueUpdated {
  schema_version: 1;
  type: "session.queue.updated";
  session_id: string;
  queued_count: number;
  scheduled_queued_count: number;
}

export interface RuntimeEdgeRunQueued {
  schema_version: 1;
  type: "run.queued";
  run_id: string;
  client_message_id: string;
  enqueued_at: string;
  enqueue_seq: number;
}

export interface RuntimeEdgeRunAccepted {
  schema_version: 1;
  type: "run.accepted";
  run_id: string;
  client_message_id: string;
  accepted_at: string;
}

/**
 * Durable per-member identity shared by every Runtime Edge redirect
 * disposition. `event_seq` belongs to the parent Run's public event stream.
 * Group fields remain null until a member is claimed for one redirect RPC;
 * `start_after_seq` remains null until Hermes has accepted that RPC.
 */
export interface RuntimeRedirectMemberIdentity {
  redirect_id: string;
  client_message_id: string;
  parent_run_id: string;
  input_admission_seq: number;
  dispatch_group_id: string | null;
  dispatch_group_position: number | null;
  event_seq: number;
  start_after_seq: number | null;
}

export interface RuntimeRedirectPendingIdentity
  extends RuntimeRedirectMemberIdentity {
  dispatch_group_id: null;
  dispatch_group_position: null;
  start_after_seq: null;
}

export interface RuntimeRedirectAcceptedIdentity
  extends RuntimeRedirectMemberIdentity {
  dispatch_group_id: string;
  dispatch_group_position: number;
  start_after_seq: number;
}

export interface RuntimeRedirectConsumedIdentity
  extends RuntimeRedirectAcceptedIdentity {}

export interface RuntimeRedirectPromotedIdentity
  extends RuntimeRedirectMemberIdentity {
  start_after_seq: null;
}

export interface RuntimeRedirectCanceledIdentity
  extends RuntimeRedirectMemberIdentity {
  start_after_seq: null;
}

export interface RuntimeRedirectUncertainIdentity
  extends RuntimeRedirectMemberIdentity {
  dispatch_group_id: string;
  dispatch_group_position: number;
}

export interface RuntimeEdgeRunRedirectPending
  extends RuntimeRedirectPendingIdentity {
  schema_version: 1;
  type: "run.redirect_pending";
}

export interface RuntimeEdgeRunRedirectAccepted
  extends RuntimeRedirectAcceptedIdentity {
  schema_version: 1;
  type: "run.redirect_accepted";
}

export interface RuntimeEdgeRunRedirectConsumed
  extends RuntimeRedirectConsumedIdentity {
  schema_version: 1;
  type: "run.redirect_consumed";
}

export interface RuntimeEdgeRunRedirectPromoted
  extends RuntimeRedirectPromotedIdentity {
  schema_version: 1;
  type: "run.redirect_promoted";
  promoted_run_id: string;
  promoted_enqueue_seq: number;
}

// A public canceled disposition is only emitted for a correction that never
// crossed the Hermes RPC boundary. More detailed internal diagnostics stay
// out of the Browser contract.
export type RuntimeRedirectCancelReason = "PARENT_RUN_CANCELED";

export interface RuntimeEdgeRunRedirectCanceled
  extends RuntimeRedirectCanceledIdentity {
  schema_version: 1;
  type: "run.redirect_canceled";
  cancel_reason: RuntimeRedirectCancelReason;
}

// The Browser must not infer which uncertain recovery branch ran or retry
// automatically. Internal telemetry retains the detailed failure cause.
export type RuntimeRedirectUncertainReasonCode =
  "REDIRECT_DELIVERY_UNPROVEN";

export interface RuntimeEdgeRunSteerSubmissionUncertain
  extends RuntimeRedirectUncertainIdentity {
  schema_version: 1;
  type: "run.steer_submission_uncertain";
  reason_code: RuntimeRedirectUncertainReasonCode;
}

export type RuntimeEdgeRedirectDisposition =
  | RuntimeEdgeRunRedirectPending
  | RuntimeEdgeRunRedirectAccepted
  | RuntimeEdgeRunRedirectConsumed
  | RuntimeEdgeRunRedirectPromoted
  | RuntimeEdgeRunRedirectCanceled
  | RuntimeEdgeRunSteerSubmissionUncertain;

export interface RuntimeFollowupMemberIdentity {
  followup_id: string;
  client_message_id: string;
  parent_run_id: string;
  input_admission_seq: number;
  dispatch_group_id: string | null;
  dispatch_group_position: number | null;
  event_seq: number;
}

export interface RuntimeEdgeFollowupReceived
  extends RuntimeFollowupMemberIdentity {
  schema_version: 1;
  type: "followup.received";
  dispatch_group_id: null;
  dispatch_group_position: null;
}

export interface RuntimeEdgeFollowupApplying
  extends RuntimeFollowupMemberIdentity {
  schema_version: 1;
  type: "followup.applying";
}

export interface RuntimeEdgeFollowupApplied
  extends RuntimeFollowupMemberIdentity {
  schema_version: 1;
  type: "followup.applied";
  dispatch_group_id: string;
  dispatch_group_position: number;
  proof_kind:
    | "correction_user_row"
    | "steer_oob_marker"
    | "canonical_history";
  proof_digest: string | null;
}

export interface RuntimeEdgeFollowupContinued
  extends RuntimeFollowupMemberIdentity {
  schema_version: 1;
  type: "followup.continued";
  continuation_run_id: string;
  member_client_message_ids: string[];
}

export interface RuntimeEdgeFollowupFailed
  extends RuntimeFollowupMemberIdentity {
  schema_version: 1;
  type: "followup.failed";
  error_code:
    | "FOLLOWUP_DELIVERY_UNRESOLVED"
    | "FOLLOWUP_CONTINUATION_FAILED"
    | "FOLLOWUP_PARENT_CANCELED";
}

export type RuntimeEdgeFollowupDisposition =
  | RuntimeEdgeFollowupReceived
  | RuntimeEdgeFollowupApplying
  | RuntimeEdgeFollowupApplied
  | RuntimeEdgeFollowupContinued
  | RuntimeEdgeFollowupFailed;

/** Data payload carried by the parent's chat.steer.accepted event. */
export type RuntimeSteerAcceptedEventData =
  RuntimeRedirectAcceptedIdentity & Record<string, unknown>;

/** Data payload carried by the parent's chat.steer.consumed event. */
export type RuntimeSteerConsumedEventData =
  RuntimeRedirectConsumedIdentity & Record<string, unknown>;

export interface RuntimeEdgeSteerAcceptedEvent {
  schema_version: 1;
  type: "run.event";
  run_id: string;
  seq: number;
  event_type: "chat.steer.accepted";
  emitted_at: string;
  data: RuntimeSteerAcceptedEventData;
}

export interface RuntimeEdgeSteerConsumedEvent {
  schema_version: 1;
  type: "run.event";
  run_id: string;
  seq: number;
  event_type: "chat.steer.consumed";
  emitted_at: string;
  data: RuntimeSteerConsumedEventData;
}

export interface RuntimeEdgeRunEvent {
  schema_version: 1;
  type: "run.event";
  run_id: string;
  seq: number;
  event_type: string;
  emitted_at: string;
  data: Record<string, unknown>;
}

export interface RuntimeEdgeRunEventBatch {
  schema_version: 1;
  type: "run.event_batch";
  events: RuntimeEdgeRunEvent[];
}

export interface RuntimeEdgeRunSnapshot {
  schema_version: 1;
  type: "run.snapshot";
  run_id: string;
  through_seq: number;
  status: string;
  answer: string;
  events: RuntimeEdgeRunEvent[];
}

export interface RuntimeEdgeError {
  schema_version: 1;
  type: "error";
  code: string;
  message: string;
  retryable: boolean;
  request_id?: string | null;
  client_message_id?: string | null;
  run_id?: string | null;
  phase?:
    | "live_execution_release"
    | "model_route"
    | "session_import"
    | "dashboard_prewarm"
    | "profile_prompt_refresh"
    | "unknown"
    | null;
  audit_event_id?: string | null;
}

export type RuntimeEdgeClientMessage =
  | RuntimeEdgePrepareRequest
  | RuntimeEdgeDesktopCapabilitiesRequest
  | RuntimeEdgeSessionHistoryRequest
  | RuntimeEdgeSessionQueueRequest
  | RuntimeEdgeRunSubmitRequest
  | RuntimeEdgeFollowupSubmitRequest
  | RuntimeEdgeRunResumeRequest
  | RuntimeEdgeRunCancelRequest
  | RuntimeEdgeLocalCapabilityResult;

export type RuntimeEdgeServerMessage =
  | RuntimeEdgePrepareReady
  | RuntimeEdgeSessionHistory
  | RuntimeEdgeSessionQueue
  | RuntimeEdgeSessionQueueUpdated
  | RuntimeEdgeRunQueued
  | RuntimeEdgeRunAccepted
  | RuntimeEdgeFollowupDisposition
  | RuntimeEdgeRedirectDisposition
  | RuntimeEdgeRunCancelAck
  | RuntimeEdgeSteerAcceptedEvent
  | RuntimeEdgeSteerConsumedEvent
  | RuntimeEdgeRunEvent
  | RuntimeEdgeRunEventBatch
  | RuntimeEdgeRunSnapshot
  | RuntimeEdgeLocalCapabilityRequest
  | RuntimeEdgeError;

export interface RuntimeTurnIdentity {
  user_id: number;
  org_id: number;
  session_id: string;
  profile_id: string;
  runtime_agent_profile_id: number;
  application_agent_id?: number | null;
  profile_desired_revision: number;
  profile_applied_revision: number;
  profile_digest: `sha256:${string}`;
  skill_digest: `sha256:${string}`;
  mcp_digest: `sha256:${string}`;
  knowledge_digest: `sha256:${string}`;
  model_id: string;
  upstream_model_id?: string | null;
  context_length: number;
  runtime_capabilities?: RuntimeModelCapabilitiesWire | null;
  provider_route?: ProviderRouteGrantV1 | null;
  config_revision: string;
  admission_generation: number;
  max_prompt_bytes: number;
  max_output_tokens: number;
}

export interface PrepareCommandPayload {
  type: "prepare";
  identity: RuntimeTurnIdentity;
  priority: "interactive" | "business" | "background";
}

export interface ChannelExecutionLease {
  version: 1;
  lease_id: string;
  org_id: number;
  channel_id: number;
  target_type: "personal_agent" | "application_agent";
  target_id: number;
  subject_user_id: number;
  runtime_id: string;
  binding_id: string;
  runtime_session_id: string;
  model_entry_id: string;
  profile_id: string;
  application_agent_id?: number | null;
  gateway_instance_id: string;
  channel_lease_epoch: number;
  channel_generation: number;
  config_generation: number;
  admission_generation: number;
  authorization_epoch: number;
  issued_at: string;
  expires_at: string;
  signature: string;
}

export interface InternalTurnCommandPayload {
  type: "internal_turn";
  source:
    | "personal_wecom"
    | "application_wecom"
    | "openapi"
    | "scheduled_task"
    | "expert_collaboration";
  run_id: string;
  client_message_id: string;
  identity: RuntimeTurnIdentity;
  prompt: string;
  attachment_file_ids: string[];
  attachments: Array<{
    file_id: string;
    display_name: string;
    relative_path: string;
    mime_type?: string | null;
  }>;
  channel_execution_lease?: ChannelExecutionLease | null;
  source_detail?: Record<string, unknown> | null;
  scheduled_task_id?: number | null;
  scheduled_occurrence_key?: string | null;
}

export interface ChannelFastLaneSubmitFrame {
  schema_version: 1;
  type: "channel.turn.submit";
  request_id: string;
  gateway_instance_id: string;
  channel_lease_epoch: number;
  envelope: RuntimeCommandEnvelope;
  channel_fast_lane_schema_version?: never;
  delivery_id?: never;
}

export interface ChannelFastLaneDeliveryAck {
  schema_version: 1;
  type: "channel.delivery.ack";
  request_id: string;
  gateway_instance_id: string;
  channel_lease_epoch: number;
  run_id: string;
  highest_contiguous_seq: number;
  provider_request_id: string;
  provider_message_id?: string | null;
  projection_version: number;
  confirmed_at: string;
  channel_fast_lane_schema_version?: never;
  delivery_id?: never;
}

/** The deployed run-keyed Fast Lane contract retained only for v1 drain. */
export type ChannelFastLaneV1SubmitFrame = ChannelFastLaneSubmitFrame;
export type ChannelFastLaneV1DeliveryAck = ChannelFastLaneDeliveryAck;

export interface ChannelFastLaneV2SubmitFrame {
  schema_version: 1;
  channel_fast_lane_schema_version: 2;
  type: "channel.turn.submit";
  request_id: string;
  gateway_instance_id: string;
  channel_lease_epoch: number;
  delivery_id: string;
  client_message_id: string;
  envelope: RuntimeCommandEnvelope;
}

export interface ChannelFastLaneV2ResumeFrame {
  schema_version: 1;
  channel_fast_lane_schema_version: 2;
  type: "channel.turn.resume";
  request_id: string;
  gateway_instance_id: string;
  channel_lease_epoch: number;
  delivery_id: string;
  last_disposition_seq: number;
  last_event_seq: number;
}

export interface ChannelFastLaneV2DispositionAck {
  schema_version: 1;
  channel_fast_lane_schema_version: 2;
  type: "channel.turn.disposition.ack";
  request_id: string;
  gateway_instance_id: string;
  channel_lease_epoch: number;
  delivery_id: string;
  disposition_seq: number;
}

export interface ChannelFastLaneV2DispositionBase {
  schema_version: 1;
  channel_fast_lane_schema_version: 2;
  type: "channel.turn.disposition";
  delivery_id: string;
  client_message_id: string;
  disposition_seq: number;
}

export interface ChannelFastLaneV2PendingDisposition
  extends ChannelFastLaneV2DispositionBase {
  state: "pending";
  canonical_run_id: string;
  parent_run_id: string;
  start_after_seq: null;
  promoted_run_id: null;
}

export interface ChannelFastLaneV2OrdinaryRunAcceptedDisposition
  extends ChannelFastLaneV2DispositionBase {
  state: "ordinary_run_accepted";
  canonical_run_id: string;
  parent_run_id: null;
  start_after_seq: 0;
  promoted_run_id: null;
}

export interface ChannelFastLaneV2RedirectAcceptedDisposition
  extends ChannelFastLaneV2DispositionBase {
  state: "redirect_accepted";
  canonical_run_id: string;
  parent_run_id: string;
  start_after_seq: number;
  promoted_run_id: null;
}

export interface ChannelFastLaneV2RedirectConsumedDisposition
  extends ChannelFastLaneV2DispositionBase {
  state: "redirect_consumed";
  canonical_run_id: string;
  parent_run_id: string;
  start_after_seq: number;
  promoted_run_id: null;
}

export interface ChannelFastLaneV2PromotedDisposition
  extends ChannelFastLaneV2DispositionBase {
  state: "promoted";
  canonical_run_id: string;
  parent_run_id: string;
  start_after_seq: 0;
  promoted_run_id: string;
}

export interface ChannelFastLaneV2UncertainDisposition
  extends ChannelFastLaneV2DispositionBase {
  state: "uncertain";
  canonical_run_id: string;
  parent_run_id: string;
  start_after_seq: number | null;
  promoted_run_id: null;
}

export interface ChannelFastLaneV2CanceledDisposition
  extends ChannelFastLaneV2DispositionBase {
  state: "canceled";
  canonical_run_id: string;
  parent_run_id: string;
  start_after_seq: null;
  promoted_run_id: null;
}

export type ChannelFastLaneV2Disposition =
  | ChannelFastLaneV2PendingDisposition
  | ChannelFastLaneV2OrdinaryRunAcceptedDisposition
  | ChannelFastLaneV2RedirectAcceptedDisposition
  | ChannelFastLaneV2RedirectConsumedDisposition
  | ChannelFastLaneV2PromotedDisposition
  | ChannelFastLaneV2UncertainDisposition
  | ChannelFastLaneV2CanceledDisposition;

export type ChannelFastLaneV2DispositionFrame =
  ChannelFastLaneV2Disposition;

export interface ChannelFastLaneV2DeliveryAck {
  schema_version: 1;
  channel_fast_lane_schema_version: 2;
  type: "channel.delivery.ack";
  request_id: string;
  gateway_instance_id: string;
  channel_lease_epoch: number;
  owner_delivery_id: string;
  canonical_run_id: string;
  highest_contiguous_seq: number;
  provider_request_id: string;
  provider_message_id?: string | null;
  projection_version: number;
  confirmed_at: string;
}

export type ChannelFastLaneV2ClientFrame =
  | ChannelFastLaneV2SubmitFrame
  | ChannelFastLaneV2ResumeFrame
  | ChannelFastLaneV2DispositionAck
  | ChannelFastLaneV2DeliveryAck;

export interface CancelCommandPayload {
  type: "cancel";
  run_id: string;
  clarify_request_id?: string;
}

/** Maintenance-time user-interruption simulation before Runtime restart. */
export interface CancelActiveRunsCommandPayload {
  type: "cancel_active_runs";
}

export interface CancelScheduledCandidateCommandPayload {
  type: "cancel_scheduled_candidate";
  session_id: string;
  scheduled_task_id: number;
}

export interface DrainCommandPayload {
  type: "drain";
  deadline_seconds: 900;
}

export interface ConfigBundleReference {
  runtime_id: string;
  binding_id: string;
  config_revision: string;
  config_digest: string;
  download_url: string;
  minimum_protocol_version: number;
  expires_at: string;
  signature: string;
}

export interface ConfigBundlePayload {
  schema_version: 1;
  runtime_id: string;
  binding_id: string;
  config_revision: string;
  config_digest: string;
  files: Record<string, string>;
}

export interface ApplyConfigCommandPayload {
  type: "apply_config";
  bundle: ConfigBundleReference;
}

export interface RevokeCommandPayload {
  type: "revoke";
  scope: "user" | "model" | "application";
  subject_id: string;
  revoked_at: string;
  cancel_active_runs: boolean;
}

export interface SessionMutationCommandPayload {
  type: "session_mutation";
  operation: "rename" | "delete" | "invalidate";
  session_id: string;
  profile_id: string;
  title?: string | null;
}

export type RuntimeCommandPayload =
  | PrepareCommandPayload
  | InternalTurnCommandPayload
  | CancelCommandPayload
  | CancelActiveRunsCommandPayload
  | CancelScheduledCandidateCommandPayload
  | DrainCommandPayload
  | ApplyConfigCommandPayload
  | RevokeCommandPayload
  | SessionMutationCommandPayload;

export interface RuntimeCommandEnvelope {
  schema_version: 1;
  command_id: string;
  runtime_id: string;
  binding_id: string;
  admission_generation: number;
  payload_digest: string;
  issued_at: string;
  deadline_at?: string | null;
  payload: RuntimeCommandPayload;
}

export interface RuntimeCommandAck {
  schema_version: 1;
  command_id: string;
  state: "accepted" | "in_progress" | "completed" | "rejected";
  run_id?: string | null;
  result?: Record<string, unknown> | null;
  error_code?: string | null;
}

export interface RuntimeProjectionItem {
  projection_id: string;
  runtime_id: string;
  projection_type: string;
  occurred_at: string;
  payload_digest: string;
  run_id?: string | null;
  seq?: number | null;
  config_revision?: string | null;
  payload: Record<string, unknown>;
}

export interface RuntimeProjectionBatch {
  schema_version: 1;
  runtime_id: string;
  binding_id: string;
  items: RuntimeProjectionItem[];
}

export interface RuntimeProjectionRejection {
  projection_id: string;
  error_code: string;
}

export interface RuntimeProjectionAck {
  schema_version: 1;
  runtime_id: string;
  accepted_projection_ids: string[];
  rejected: RuntimeProjectionRejection[];
}

export interface PostTurnModelGrant {
  run_id: string;
  session_id: string;
  profile_id: string;
  max_output_tokens: number;
  expires_at: string;
}

export interface ModelRouteBundle {
  schema_version: 1;
  runtime_id: string;
  session_id: string;
  run_id: string;
  model_id: string;
  config_revision: string;
  api_mode: string;
  relay_route: string;
  max_output_tokens: number;
  expires_at: string;
  post_turn_grant?: PostTurnModelGrant | null;
}
