import { afterEach, describe, expect, it, vi } from "vitest";
import { clearSession, setSession } from "../auth";
import { userApi } from "./client";

const scheduledTask = {
  task_id: 123,
  task_name: "每日工作总结",
  prompt: "总结今天的工作内容",
  status: "active",
  schedule_config: { type: "cron", preset: "daily", time: "18:00" },
  schedule_summary: "每天 18:00",
  target_runtime_session_id: "rs-7-1",
  target_session_title: "销售日报",
  target_session_status: "active",
  next_run_at: "2026-07-18T10:00:00+00:00",
  last_run_at: null,
  last_run_status: null,
  last_error_code: null,
  last_error: null,
  last_runtime_run_id: null,
  run_count: 0,
  is_running: false,
  created_at: "2026-07-17T10:00:00+00:00",
  updated_at: "2026-07-17T10:00:00+00:00"
};

describe("userApi", () => {
  it("preserves the HTTP status on API errors", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({ detail: "session title is already in use" }),
      { status: 409 }
    ));

    await expect(userApi.renameRuntimeSession(7, "rs-7", { title: "你好" })).rejects.toEqual(
      expect.objectContaining({
        message: "已存在同名会话，请换一个名称",
        status: 409
      })
    );
  });

  it("preserves a FastAPI string detail as the API error code", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({ detail: "application_agent_runtime_busy" }),
      { status: 409 }
    ));

    await expect(
      userApi.warmupApplicationAgentStudio(31)
    ).rejects.toEqual(expect.objectContaining({
      message: "application_agent_runtime_busy",
      status: 409,
      code: "application_agent_runtime_busy"
    }));
  });

  it("translates WeKnora file failures without exposing provider details", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({ detail: { code: "personal_knowledge_delete_failed" } }),
      { status: 502 }
    ));

    await expect(userApi.deletePersonalKnowledgeFile(7, 21)).rejects.toEqual(
      expect.objectContaining({
        message: "知识底座尚未确认删除，文件已保留，请稍后重试",
        status: 502,
        code: "personal_knowledge_delete_failed"
      })
    );
  });

  it.each([
    ["personal_knowledge_capability_unavailable", 409, "当前知识库不支持该检索方式，请调整知识库范围"],
    ["personal_knowledge_wiki_page_not_found", 404, "Wiki 页面不存在或已移除，请重新检索"]
  ])("translates %s without exposing provider details", async (code, status, message) => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({ detail: { code } }),
      { status }
    ));

    await expect(userApi.deletePersonalKnowledgeFile(7, 21)).rejects.toEqual(
      expect.objectContaining({ message, status, code })
    );
  });

  afterEach(() => {
    clearSession();
    vi.restoreAllMocks();
  });

  it("loads one runtime session through the encoded platform endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      session_id: 7,
      runtime_workspace_id: 7,
      runtime_session_id: "rs 7/old",
      title: "Old app task",
      source_type: "web",
      message_count: 3,
      status: "active"
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    await expect(userApi.runtimeSession(7, "rs 7/old")).resolves.toMatchObject({
      runtimeSessionId: "rs 7/old",
      title: "Old app task"
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/user/runtime-workspace/7/sessions/rs%207%2Fold",
      expect.anything()
    );
  });

  it("accepts a no-content response when deleting a runtime session", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 204 }));

    await expect(userApi.deleteRuntimeSession(7, "rs-7-1")).resolves.toBeUndefined();

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/user/runtime-workspace/7/sessions/rs-7-1",
      expect.objectContaining({ method: "DELETE" })
    );
  });

  it("loads the mixed organization directory from its unified endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify([{
      node_type: "organization",
      organization_id: 1,
      name: "示例业务技术",
      children: [{
        node_type: "user",
        user_id: 990095,
        name: "示例用户甲",
        user_code: "10001",
        status: "active"
      }]
    }]), { status: 200, headers: { "Content-Type": "application/json" } }));

    await expect(userApi.organizationDirectoryTree()).resolves.toEqual([{
      nodeType: "organization",
      organizationId: 1,
      name: "示例业务技术",
      children: [{
        nodeType: "user",
        userId: 990095,
        name: "示例用户甲",
        userCode: "10001",
        status: "active"
      }]
    }]);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/user/directory/tree",
      expect.any(Object)
    );
  });

  it("permanently deletes one application Agent through its management endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(null, { status: 204 })
    );

    await expect(userApi.deleteApplicationAgent(31)).resolves.toBeUndefined();

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/user/application-agents/31",
      expect.objectContaining({ method: "DELETE" })
    );
  });

  it("accepts the asynchronous 202 empty response when deleting an application Agent", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(null, { status: 202 })
    );

    await expect(userApi.deleteApplicationAgent(31)).resolves.toBeUndefined();

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/user/application-agents/31",
      expect.objectContaining({ method: "DELETE" })
    );
  });

  it("uploads a Skill to the selected application Agent as multipart data", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({
        application_agent_skill_id: 41,
        application_agent_id: 31,
        slug: "private-ticket",
        name: "私有工单",
        category_key: "personal",
        package_sha256: "a".repeat(64)
      }), {
        status: 201,
        headers: { "Content-Type": "application/json" }
      })
    );
    const file = new File(["zip"], "private-ticket.zip", {
      type: "application/zip"
    });

    await expect(
      userApi.uploadApplicationAgentSkill(31, file, "personal")
    ).resolves.toMatchObject({
      applicationAgentSkillId: 41,
      applicationAgentId: 31
    });

    const [, init] = fetchMock.mock.calls[0];
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/user/application-agents/31/skills/upload"
    );
    expect(init).toEqual(expect.objectContaining({ method: "POST" }));
    expect(init?.body).toBeInstanceOf(FormData);
    expect((init?.body as FormData).get("file")).toBe(file);
    expect((init?.body as FormData).get("category_key")).toBe("personal");
    expect((init?.headers as Record<string, string>)["Content-Type"]).toBeUndefined();
  });

  it("explains a gateway 413 for application Skill uploads", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("<html><body>413 Request Entity Too Large</body></html>", {
        status: 413,
        headers: { "Content-Type": "text/html" }
      })
    );
    const file = new File(["zip"], "large.zip", { type: "application/zip" });

    await expect(
      userApi.uploadApplicationAgentSkill(31, file, "uncategorized")
    ).rejects.toEqual(expect.objectContaining({
      message: "应用 Agent 技能包超过 25 MiB，请压缩后重试",
      status: 413
    }));
  });

  it("renames a runtime session through the encoded platform endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      session_id: 7,
      runtime_workspace_id: 7,
      runtime_session_id: "rs 7/1",
      title: "Release plan",
      source_type: "web",
      message_count: 2,
      status: "active"
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    await expect(userApi.renameRuntimeSession(7, "rs 7/1", { title: "Release plan" })).resolves.toMatchObject({
      runtimeSessionId: "rs 7/1",
      title: "Release plan"
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/user/runtime-workspace/7/sessions/rs%207%2F1",
      expect.objectContaining({ method: "PATCH", body: JSON.stringify({ title: "Release plan" }) })
    );
  });

  it("maps the latest durable run projection on session summaries", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify([{
      session_id: 7,
      runtime_workspace_id: 7,
      runtime_session_id: "rs-7-1",
      title: "Release plan",
      source_type: "web",
      message_count: 2,
      status: "active",
      latest_run: {
        runtime_run_id: "run-7-latest",
        status: "completed",
        started_at: "2026-08-21T08:00:00Z",
        finished_at: "2026-08-21T08:01:00Z"
      }
    }]), { status: 200, headers: { "Content-Type": "application/json" } }));

    await expect(userApi.runtimeSessions(7)).resolves.toEqual([
      expect.objectContaining({
        runtimeSessionId: "rs-7-1",
        latestRun: {
          runtimeRunId: "run-7-latest",
          status: "completed",
          startedAt: "2026-08-21T08:00:00Z",
          finishedAt: "2026-08-21T08:01:00Z"
        }
      })
    ]);
  });

  it("sends runtime keepalive through the authenticated platform endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 204 }));

    await expect(userApi.keepaliveRuntime()).resolves.toBeUndefined();

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/user/runtime-workspace/keepalive",
      expect.objectContaining({ method: "POST" })
    );
  });

  it("silently refreshes one 401 and retries the ordinary request once", async () => {
    setSession({
      accessToken: "expired-jwt",
      tokenType: "bearer",
      expiresIn: 1,
      principal: { orgId: 1, userId: 7, name: "employee-7" }
    });
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        access_token: "renewed-jwt",
        expires_in: 900,
        principal: { org_id: 1, user_id: 7, name: "employee-7" }
      }), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));

    await expect(userApi.keepaliveRuntime()).resolves.toBeUndefined();

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[1][0]).toBe("/api/v1/auth/refresh");
    expect(fetchMock.mock.calls[2][0]).toBe(
      "/api/v1/user/runtime-workspace/keepalive"
    );
    expect(fetchMock.mock.calls[2][1]).toEqual(expect.objectContaining({
      headers: expect.objectContaining({ Authorization: "Bearer renewed-jwt" })
    }));
  });

  it("opens the global browser presence stream independently of chat", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        "event: presence.heartbeat\ndata: {\"runtime\":\"ready\"}\n\n",
        { status: 200, headers: { "Content-Type": "text/event-stream" } }
      )
    );
    const events: Array<{ event: string; data: Record<string, unknown> }> = [];

    await userApi.streamBrowserPresence((event) => events.push(event));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/user/browser-presence/stream",
      expect.objectContaining({
        method: "GET",
        headers: expect.objectContaining({ "X-Hermes-Client-Type": "web" })
      })
    );
    expect(events).toEqual([{
      event: "presence.heartbeat",
      data: { runtime: "ready" }
    }]);
  });

  it("preserves structured prewarm conflict metadata", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      detail: {
        code: "DASHBOARD_CHANNEL_DRAINING",
        message: "上一会话仍在执行，结束后将自动发送",
        runCreated: false
      }
    }), {
      status: 409,
      headers: { "Content-Type": "application/json" }
    }));

    await expect(
      userApi.prewarmRuntimeSession(7, "rs-7-new")
    ).rejects.toMatchObject({
      status: 409,
      code: "DASHBOARD_CHANNEL_DRAINING",
      runCreated: false,
      message: "上一会话仍在执行，结束后将自动发送"
    });
  });

  it("translates invalid expert share departments for saving", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({ detail: "invalid_expert_share_department" }),
      { status: 400 }
    ));

    await expect(userApi.createExpert({
      runtimeWorkspaceId: 7,
      name: "售前专家",
      soulContent: "你是一名售前专家",
      shareScope: {
        organization: false,
        userIds: [],
        departmentIds: ["virtual:unassigned"]
      }
    })).rejects.toEqual(expect.objectContaining({
      message: "分享范围包含无效、已停用或不可勾选的部门，请重新选择后保存",
      status: 400,
      code: "invalid_expert_share_department"
    }));
  });

  it("translates invalid personal knowledge permission departments", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({ detail: { code: "invalid_permission_department" } }),
      { status: 400 }
    ));
    const targets = { userIds: [], departmentIds: [] };

    await expect(userApi.replacePersonalKnowledgePermissions(7, {
      download: targets,
      preview: targets,
      retrieve: targets
    })).rejects.toEqual(expect.objectContaining({
      message: "所选部门已失效或不可授权，请刷新目录后重新选择",
      status: 400,
      code: "invalid_permission_department"
    }));
  });

  it("extracts a readable detail from platform JSON errors", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({ detail: "runtime is not ready" }),
      { status: 503, headers: { "Content-Type": "application/json" } }
    ));

    await expect(userApi.keepaliveRuntime()).rejects.toThrow("云端运行环境尚未就绪，请稍后重试");
  });

  it("translates an unavailable expert capability into a readable message", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({ detail: "expert_capability_not_visible" }),
      { status: 403, headers: { "Content-Type": "application/json" } }
    ));

    await expect(userApi.createExpert({
      runtimeWorkspaceId: 7,
      name: "写作专家",
      description: null,
      avatarUrl: null,
      soulContent: "",
      inheritedFromProfileId: 1,
      shareScope: { organization: false, userIds: [], departmentIds: [] }
    })).rejects.toThrow("部分专家能力已失效或你无权使用，请刷新能力列表后重新选择");
  });

  it("explains that expert creation is blocked by Runtime startup", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({ detail: "expert_runtime_unavailable" }),
      { status: 503, headers: { "Content-Type": "application/json" } }
    ));

    await expect(userApi.createExpert({
      runtimeWorkspaceId: 7,
      name: "写作专家",
      description: null,
      avatarUrl: null,
      soulContent: "",
      inheritedFromProfileId: null,
      shareScope: { organization: false, userIds: [], departmentIds: [] }
    })).rejects.toThrow("Runtime 尚未完成启动，请稍后重试");
  });

  it("translates an expert name conflict into a readable message", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({ detail: "expert_name_conflict" }),
      { status: 409, headers: { "Content-Type": "application/json" } }
    ));

    await expect(userApi.createExpert({
      runtimeWorkspaceId: 7,
      name: "写作专家",
      description: null,
      avatarUrl: null,
      soulContent: "",
      inheritedFromProfileId: null,
      shareScope: { organization: false, userIds: [], departmentIds: [] }
    })).rejects.toThrow("当前工作区已有同名专家。请更换名称，或先删除同名专家后重试");
  });

  it("distinguishes a disabled expert from an operation in progress", async () => {
    const fetch = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(
        JSON.stringify({ detail: "expert_disabled" }),
        { status: 409, headers: { "Content-Type": "application/json" } }
      ))
      .mockResolvedValueOnce(new Response(
        JSON.stringify({ detail: "expert_operation_in_progress" }),
        { status: 409, headers: { "Content-Type": "application/json" } }
      ));
    const payload = {
      name: "写作专家",
      description: null,
      avatarUrl: null,
      soulContent: "",
      capabilities: { skillIds: [], mcpIds: [], knowledgeBaseIds: [], enterpriseKnowledgeEnabled: false, pluginIds: [] },
      shareScope: { organization: false, userIds: [], departmentIds: [] },
      changeSummary: null
    };

    await expect(userApi.saveExpertConfiguration(7, 8, payload))
      .rejects.toThrow("该专家已停用，无法继续配置或召唤");
    await expect(userApi.saveExpertConfiguration(7, 8, payload))
      .rejects.toThrow("专家正在处理上一个操作，请稍后刷新再试");
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("translates an unchanged expert configuration into a readable message", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({ detail: "expert_configuration_unchanged" }),
      { status: 422, headers: { "Content-Type": "application/json" } }
    ));

    await expect(userApi.saveExpertConfiguration(7, 8, {
      name: "写作专家",
      description: null,
      avatarUrl: null,
      soulContent: "",
      capabilities: { skillIds: [], mcpIds: [], knowledgeBaseIds: [], enterpriseKnowledgeEnabled: false, pluginIds: [] },
      shareScope: { organization: false, userIds: [], departmentIds: [] },
      changeSummary: null
    })).rejects.toThrow("未检测到专家配置变更，无需发布新版本");
  });

  it("translates a missing expert change summary into a readable message", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({ detail: "expert_change_summary_required" }),
      { status: 422, headers: { "Content-Type": "application/json" } }
    ));

    await expect(userApi.saveExpertConfiguration(7, 8, {
      name: "写作专家",
      description: null,
      avatarUrl: null,
      soulContent: "",
      capabilities: { skillIds: [], mcpIds: [], knowledgeBaseIds: [], enterpriseKnowledgeEnabled: true, pluginIds: [] },
      shareScope: { organization: true, userIds: [], departmentIds: [] },
      changeSummary: null
    })).rejects.toThrow("发布新版本前，请填写本次版本改动简介");
  });

  it("translates structured request validation errors into an actionable message", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({
        detail: [{
          type: "extra_forbidden",
          loc: ["body", "capabilities", "enterprise_knowledge_enabled"],
          msg: "Extra inputs are not permitted",
          input: true
        }]
      }),
      { status: 422, headers: { "Content-Type": "application/json" } }
    ));

    await expect(userApi.saveExpertConfiguration(7, 8, {
      name: "写作专家",
      description: null,
      avatarUrl: null,
      soulContent: "",
      capabilities: { skillIds: [], mcpIds: [], knowledgeBaseIds: [], enterpriseKnowledgeEnabled: true, pluginIds: [] },
      shareScope: { organization: true, userIds: [], departmentIds: [] },
      changeSummary: "启用企业知识库"
    })).rejects.toThrow("请求参数与服务版本不一致，请刷新页面或稍后重试");

    const requestBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(requestBody.capabilities.enterprise_knowledge_enabled).toBe(true);
  });

  it("omits the disabled enterprise capability for legacy strict backends", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({}), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      })
    );

    await userApi.saveExpertConfiguration(7, 8, {
      name: "写作专家",
      description: null,
      avatarUrl: null,
      soulContent: "",
      capabilities: {
        skillIds: [],
        mcpIds: [],
        knowledgeBaseIds: [],
        enterpriseKnowledgeEnabled: false,
        pluginIds: []
      },
      shareScope: { organization: false, userIds: [], departmentIds: [] },
      changeSummary: null
    });

    const requestBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(requestBody.capabilities).not.toHaveProperty("enterprise_knowledge_enabled");
  });

  it("loads market expert details through the on-demand endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      expert_package_id: 17,
      expert_package_version_id: 3,
      name: "写作专家",
      description: "写作",
      avatar_url: null,
      version: 3,
      skills: [],
      mcps: [],
      knowledge_bases: []
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    await expect(userApi.marketExpertDetail(7, 17)).resolves.toMatchObject({
      expertPackageId: 17,
      expertPackageVersionId: 3,
      knowledgeBases: []
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/user/experts/market/17?runtime_workspace_id=7",
      expect.anything()
    );
  });

  it("normalizes an older market detail response during a rolling deployment", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      expert_package_id: 17,
      expert_package_version_id: 3,
      name: "写作专家",
      description: "写作",
      avatar_url: null,
      provider_name: "专家市场",
      version: 3,
      installed: false,
      catalog_status: "published",
      execution_status: "enabled",
      allowed_actions: { view_detail: true }
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    await expect(userApi.marketExpertDetail(7, 17)).resolves.toMatchObject({
      soulContent: "",
      systemPrompt: null,
      skills: [],
      mcps: [],
      knowledgeBases: []
    });
  });

  it("uploads normalized avatars as multipart data", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      avatar_value: "https://assets.example/avatar.jpg",
      label: "avatar",
      source_kind: "user_upload",
      image_url: "https://assets.example/avatar.jpg",
      sort_order: 1000
    }), { status: 201, headers: { "Content-Type": "application/json" } }));
    const file = new File(["jpeg"], "avatar.jpg", { type: "image/jpeg" });

    await expect(userApi.uploadAvatarAsset(file)).resolves.toMatchObject({
      avatarValue: "https://assets.example/avatar.jpg",
      sourceKind: "user_upload"
    });
    const request = fetchMock.mock.calls[0][1];
    expect(request?.body).toBeInstanceOf(FormData);
    expect((request?.headers as Record<string, string>)["Content-Type"]).toBeUndefined();
  });

  it("queries the active run for a session", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({ run: {
        runtime_workspace_id: 7,
        runtime_session_id: "rs-7-1",
        runtime_run_id: "rr-7-1",
        status: "running",
        last_seq: 4,
        source: {
          type: "scheduled",
          scheduled_task_id: 123,
          scheduled_task_name: "每日工作总结",
          trigger_kind: "scheduled",
          task_available: true
        }
      } }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    ));

    await expect(userApi.activeChatRun(7, "rs-7-1")).resolves.toEqual({
      run: {
        runtimeWorkspaceId: 7,
        runtimeSessionId: "rs-7-1",
        runtimeRunId: "rr-7-1",
        status: "running",
        lastSeq: 4,
        source: {
          type: "scheduled",
          scheduledTaskId: 123,
          scheduledTaskName: "每日工作总结",
          triggerKind: "scheduled",
          taskAvailable: true
        }
      }
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/user/chat/runs/active?runtime_workspace_id=7&runtime_session_id=rs-7-1",
      expect.any(Object)
    );
  });

  it("streams background expert collaboration events through the authenticated GET endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      "event: chat.background.run.created\ndata: {\"final_runtime_run_id\":\"rr-final\"}\n\n",
      { status: 200, headers: { "Content-Type": "text/event-stream" } }
    ));
    const onEvent = vi.fn();

    await expect(
      userApi.streamBackgroundCollaborations(7, "rs 7/1", onEvent)
    ).resolves.toBeUndefined();

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/user/chat/background-collaborations/stream?runtime_workspace_id=7&runtime_session_id=rs%207%2F1",
      expect.objectContaining({ method: "GET" })
    );
    expect(onEvent).toHaveBeenCalledWith({
      event: "chat.background.run.created",
      data: { finalRuntimeRunId: "rr-final" }
    });
  });

  it("reports a bounded client timing sample without blocking chat", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 204 }));

    await expect(userApi.reportChatClientTiming("rr 7/1", {
      sampleKind: "lifecycle",
      requestToFirstEventMs: 10,
      requestToFirstVisibleReceivedMs: 20,
      requestToFirstVisibleCommitMs: 25,
      requestToFirstVisiblePaintMs: 40,
      requiredPrewarmWaitMs: 15,
      documentVisibility: "visible",
      reconnectBeforeFirstVisible: false,
      sendOverlappedPrewarm: false
    })).resolves.toBeUndefined();

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/user/chat/runs/rr%207%2F1/client-timing",
      expect.objectContaining({
        method: "POST",
        keepalive: true,
        body: JSON.stringify({
          sample_kind: "lifecycle",
          request_to_first_event_ms: 10,
          request_to_first_visible_received_ms: 20,
          request_to_first_visible_commit_ms: 25,
          request_to_first_visible_paint_ms: 40,
          required_prewarm_wait_ms: 15,
          document_visibility: "visible",
          reconnect_before_first_visible: false,
          send_overlapped_prewarm: false
        })
      })
    );
  });

  it("swallows client timing transport and server failures", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(new Response(null, { status: 503 }));
    const payload = {
      requestToFirstVisibleReceivedMs: 20,
      requestToFirstVisibleCommitMs: 25,
      requestToFirstVisiblePaintMs: 40,
      requiredPrewarmWaitMs: 0,
      documentVisibility: "visible" as const,
      reconnectBeforeFirstVisible: false,
      sendOverlappedPrewarm: false
    };

    await expect(userApi.reportChatClientTiming("rr-1", payload)).resolves.toBeUndefined();
    await expect(userApi.reportChatClientTiming("rr-1", payload)).resolves.toBeUndefined();
  });

  it("retries client timing only while the accepted Run projection is pending", async () => {
    vi.useFakeTimers();
    try {
      const fetchMock = vi.spyOn(globalThis, "fetch")
        .mockResolvedValueOnce(new Response(null, { status: 404 }))
        .mockResolvedValueOnce(new Response(null, { status: 404 }))
        .mockResolvedValueOnce(new Response(null, { status: 204 }));
      const pending = userApi.reportChatClientTiming("rr-fast", {
        requestToFirstVisibleReceivedMs: 20,
        requestToFirstVisibleCommitMs: 25,
        requestToFirstVisiblePaintMs: 40,
        requiredPrewarmWaitMs: 0,
        documentVisibility: "visible",
        reconnectBeforeFirstVisible: false,
        sendOverlappedPrewarm: false
      });

      await vi.runAllTimersAsync();
      await expect(pending).resolves.toBeUndefined();
      expect(fetchMock).toHaveBeenCalledTimes(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it("uses the scheduled-task collection path and sends only the command fields", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({ tasks: [scheduledTask] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(scheduledTask), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...scheduledTask, task_name: "晚间总结" }), { status: 200 }));
    const payload = {
      taskName: "每日工作总结",
      prompt: "总结今天的工作内容",
      targetRuntimeSessionId: "rs-7-1",
      scheduleConfig: { type: "cron" as const, preset: "daily" as const, time: "18:00" },
      profileId: 77,
      runtimeId: "not-allowed",
      cronExpr: "0 18 * * *",
      targetSessionId: 9
    };

    await expect(userApi.listScheduledTasks(7)).resolves.toEqual([
      expect.objectContaining({ taskId: 123, scheduleConfig: { type: "cron", preset: "daily", time: "18:00" } })
    ]);
    await expect(userApi.createScheduledTask(7, payload)).resolves.toMatchObject({ taskId: 123 });
    await expect(userApi.updateScheduledTask(7, 123, { ...payload, taskName: "晚间总结" })).resolves.toMatchObject({ taskName: "晚间总结" });

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/v1/user/runtime-workspace/7/scheduled-tasks",
      expect.any(Object)
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/user/runtime-workspace/7/scheduled-tasks",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          task_name: "每日工作总结",
          prompt: "总结今天的工作内容",
          target_runtime_session_id: "rs-7-1",
          schedule_config: { type: "cron", preset: "daily", time: "18:00" }
        })
      })
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/api/v1/user/runtime-workspace/7/scheduled-tasks/123",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({
          task_name: "晚间总结",
          prompt: "总结今天的工作内容",
          target_runtime_session_id: "rs-7-1",
          schedule_config: { type: "cron", preset: "daily", time: "18:00" }
        })
      })
    );
  });

  it("uses scheduled-task state and trigger endpoints, accepting trigger 202", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...scheduledTask, status: "paused" }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(scheduledTask), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        task_id: 123,
        target_runtime_session_id: "rs-7-1",
        accepted_at: "2026-07-17T10:00:00+00:00"
      }), { status: 202 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));

    await expect(userApi.pauseScheduledTask(7, 123)).resolves.toMatchObject({ status: "paused" });
    await expect(userApi.resumeScheduledTask(7, 123)).resolves.toMatchObject({ status: "active" });
    await expect(userApi.triggerScheduledTask(7, 123)).resolves.toEqual({
      taskId: 123,
      targetRuntimeSessionId: "rs-7-1",
      acceptedAt: "2026-07-17T10:00:00+00:00"
    });
    await expect(userApi.deleteScheduledTask(7, 123)).resolves.toBeUndefined();

    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/v1/user/runtime-workspace/7/scheduled-tasks/123/pause", expect.objectContaining({ method: "POST" }));
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/v1/user/runtime-workspace/7/scheduled-tasks/123/resume", expect.objectContaining({ method: "POST" }));
    expect(fetchMock).toHaveBeenNthCalledWith(3, "/api/v1/user/runtime-workspace/7/scheduled-tasks/123/trigger", expect.objectContaining({ method: "POST" }));
    expect(fetchMock).toHaveBeenNthCalledWith(4, "/api/v1/user/runtime-workspace/7/scheduled-tasks/123", expect.objectContaining({ method: "DELETE" }));
  });

  it("uploads personal Skill zip assets and camelizes the response", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      skill_id: 21,
      skill_version_id: 31,
      slug: "my-skill",
      version: null,
      name: "My Skill",
      status: "private",
      source: "personal_asset",
      applied: false
    }), { status: 200 }));
    const file = new File(["zip"], "skill.zip", { type: "application/zip" });

    await expect(userApi.uploadPersonalSkill({
      file,
      categoryKey: "personal-reporting",
    })).resolves.toMatchObject({ skillId: 21, skillVersionId: 31, slug: "my-skill", version: null });

    const [, init] = fetchMock.mock.calls[0];
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/user/personal-skills?category_key=personal-reporting",
      expect.objectContaining({
        method: "POST",
        body: expect.any(FormData)
      })
    );
    expect((init?.body as FormData).get("file")).toBe(file);
    expect((init?.body as FormData).get("description")).toBeNull();
    expect((init?.headers as Record<string, string> | undefined)?.["X-Skill-Description"]).toBeUndefined();
  });

  it("sends a Unicode personal Skill description in multipart data, never a header", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      skill_id: 21,
      skill_version_id: 31,
      slug: "my-skill",
      version: 1,
      name: "My Skill",
      status: "private",
      source: "personal_asset",
      applied: false
    }), { status: 201 }));
    const file = new File(["zip"], "skill.zip", { type: "application/zip" });

    await expect(userApi.uploadPersonalSkill({
      file,
      categoryKey: "uncategorized",
      description: "中文面试评价",
    })).resolves.toMatchObject({ skillId: 21 });

    const [, init] = fetchMock.mock.calls[0];
    expect(init?.body).toBeInstanceOf(FormData);
    expect((init?.body as FormData).get("description")).toBe("中文面试评价");
    expect((init?.headers as Record<string, string> | undefined)?.["X-Skill-Description"]).toBeUndefined();
  });

  it("explains personal Skill names reserved by the platform", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "SKILL_SYSTEM_RESERVED" }), { status: 409 })
    );
    const file = new File(["zip"], "minimax-docx.zip", { type: "application/zip" });

    await expect(userApi.uploadPersonalSkill({
      file,
      categoryKey: "uncategorized"
    })).rejects.toEqual(expect.objectContaining({
      message: "该技能名称已被系统内置技能占用，请修改 SKILL.md 中的 name 后重新上传",
      status: 409,
      code: "SKILL_SYSTEM_RESERVED"
    }));
  });

  it("explains a gateway 413 for personal Skill uploads", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("<html><body>413 Request Entity Too Large</body></html>", {
        status: 413,
        headers: { "Content-Type": "text/html" }
      })
    );
    const file = new File(["zip"], "large.zip", { type: "application/zip" });

    await expect(userApi.uploadPersonalSkill({
      file,
      categoryKey: "uncategorized"
    })).rejects.toEqual(expect.objectContaining({
      message: "个人 Skill 包超过 25 MiB，请压缩后重试",
      status: 413
    }));
  });

  it("lists and creates private personal Skill categories", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify([{
        category_key: "uncategorized",
        display_name: "未分类",
        scope: "uncategorized",
        is_builtin: true,
      }]), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        category_key: "personal-writing",
        display_name: "写作",
        scope: "personal",
        is_builtin: false,
      }), { status: 201 }));

    await expect(userApi.personalSkillCategories()).resolves.toEqual([
      expect.objectContaining({ categoryKey: "uncategorized", scope: "uncategorized" }),
    ]);
    await expect(userApi.createPersonalSkillCategory("写作")).resolves.toMatchObject({
      categoryKey: "personal-writing",
      scope: "personal",
    });
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/user/personal-skills/categories",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ display_name: "写作" }) }),
    );
  });

  it("uploads and installs a personal Skill through one default Profile command", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      asset: {
        skill_id: 21,
        skill_version_id: 31,
        slug: "my-skill",
        version: 1,
        name: "My Skill",
        description: null,
        status: "private",
        source: "personal_asset",
        applied: false
      },
      operation: {
        operation_id: "personal-skill-install-1",
        runtime_workspace_id: 7,
        runtime_agent_profile_id: 9,
        asset_type: "skill",
        asset_id: 21,
        action: "install",
        desired_revision: 2,
        applied_revision: 1,
        convergence_status: "updating"
      }
    }), { status: 202 }));
    const file = new File(["zip"], "skill.zip", { type: "application/zip" });

    await expect(userApi.uploadAndInstallPersonalSkill(7, {
      file,
      categoryKey: "uncategorized",
    }))
      .resolves.toMatchObject({
        asset: { skillId: 21, skillVersionId: 31 },
        operation: { operationId: "personal-skill-install-1", assetId: 21 }
      });
    const [, init] = fetchMock.mock.calls[0];
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/user/runtime-workspaces/7/default-profile/personal-skills?category_key=uncategorized",
      expect.objectContaining({
        method: "POST",
        body: expect.any(FormData)
      })
    );
    expect((init?.body as FormData).get("file")).toBe(file);
    expect((init?.body as FormData).get("description")).toBeNull();
    expect((init?.headers as Record<string, string> | undefined)?.["X-Skill-Description"]).toBeUndefined();
  });

  it("sends a Unicode description for default Profile installation in multipart data", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      asset: {
        skill_id: 21, skill_version_id: 31, slug: "my-skill", version: 1,
        name: "My Skill", description: "中文面试评价", status: "private",
        source: "personal_asset", applied: false
      },
      operation: {
        operation_id: "personal-skill-install-1", runtime_workspace_id: 7,
        runtime_agent_profile_id: 9, asset_type: "skill", asset_id: 21,
        action: "install", desired_revision: 2, applied_revision: 1,
        convergence_status: "updating"
      }
    }), { status: 202 }));
    const file = new File(["zip"], "skill.zip", { type: "application/zip" });

    await expect(userApi.uploadAndInstallPersonalSkill(7, {
      file,
      categoryKey: "uncategorized",
      description: "中文面试评价",
    })).resolves.toMatchObject({ asset: { skillId: 21 } });

    const request = vi.mocked(globalThis.fetch).mock.calls[0]?.[1] as RequestInit;
    expect((request.body as FormData).get("description")).toBe("中文面试评价");
    expect((request.headers as Record<string, string> | undefined)?.["X-Skill-Description"]).toBeUndefined();
  });

  it("archives a personal Skill without removing it from existing experts", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      skill_id: 21, skill_version_id: 31, slug: "my-skill", version: 1,
      name: "My Skill", status: "archived", source: "personal_asset", applied: false
    }), { status: 200 }));

    await expect(userApi.archivePersonalSkill(21)).resolves.toMatchObject({ skillId: 21, status: "archived" });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/user/personal-skills/21/archive",
      expect.objectContaining({ method: "POST" })
    );
  });

  it("deletes a personal Skill through the permanent stable-asset command", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      skill_id: 21,
      rollout_id: "personal-skill-delete-21",
      affected_profile_ids: [7, 9],
      desired_revisions: {},
    }), { status: 200 }));

    await expect(userApi.deletePersonalSkill(21)).resolves.toMatchObject({
      skillId: 21,
      rolloutId: "personal-skill-delete-21",
      affectedProfileIds: [7, 9],
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/user/personal-skills/21",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("binds a WeCom qw_token via the self-hosted endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      status: "bound",
      qw_token: "wecom-zhangsan",
    }), { status: 200 }));

    await expect(userApi.bindQwToken("wecom-zhangsan")).resolves.toEqual({
      status: "bound",
      qwToken: "wecom-zhangsan",
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/user/wecom-bind",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ qw_token: "wecom-zhangsan" }),
      }),
    );
  });
});
