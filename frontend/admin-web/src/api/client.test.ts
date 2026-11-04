import { afterEach, describe, expect, it, vi } from "vitest";
import { adminApi } from "./client";
import { clearSession, setSession } from "../auth";

describe("adminApi expert directory", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
  });

  it("uses admin-authenticated directory endpoints for expert approval", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      }));

    await expect(adminApi.searchDirectoryUsers("张 义", 20)).resolves.toEqual([]);
    await expect(adminApi.directoryDepartmentTree()).resolves.toEqual([]);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/v1/admin/experts/directory/users/search?keyword=%E5%BC%A0%20%E4%B9%89&limit=20",
      expect.objectContaining({ credentials: "include" })
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/admin/experts/directory/departments/tree",
      expect.objectContaining({ credentials: "include" })
    );
  });
});

describe("adminApi expert review errors", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
  });

  it("shows readable Chinese errors for expert share scope failures", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({
        detail: "invalid_expert_share_department"
      }), { status: 409, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        detail: "invalid_expert_share_user"
      }), { status: 409, headers: { "Content-Type": "application/json" } }));

    await expect(adminApi.approveExpertReview(39, { reviewComment: null }))
      .rejects.toThrow(
        "审核失败：专家分享范围包含无效、已停用或不可勾选的部门，请联系创建者调整后重新提交审核。"
      );
    await expect(adminApi.approveExpertReview(39, { reviewComment: null }))
      .rejects.toThrow(
        "审核失败：专家分享范围包含无效或已停用的人员，请联系创建者调整后重新提交审核。"
      );

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
describe("adminApi password login", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
  });

  it("uses the dedicated admin password endpoint and stores the returned session", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      access_token: "admin-token",
      token_type: "bearer",
      principal: { org_id: 1, user_id: 2, name: "Administrator", user_code: "admin" }
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    await expect(adminApi.login("admin", "managed-password")).resolves.toMatchObject({
      accessToken: "admin-token",
      principal: { userCode: "admin" }
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/auth/admin/login",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ username: "admin", password: "managed-password" })
      })
    );
    expect(window.sessionStorage.getItem("hermes_platform_access_token")).toBe("admin-token");
  });
});

describe("adminApi model configuration", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
  });

  it("serializes runtime capability enum values using Backend wire literals", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({
        release_id: 7,
        catalog_version: 3,
        previous_catalog_version: 2,
        config_hash: "a".repeat(64),
        status: "published",
        published_at: "2026-09-03T00:00:00Z"
      }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    ));

    await adminApi.publishModelConfig([{
      providerName: "provider-a",
      providerDisplayName: "Provider A",
      apiEndpoint: "https://provider.example/v1",
      modelKey: "model-a",
      modelDisplayName: "Model A",
      runtimeCapabilities: {
        thinkingMode: "provider_default",
        reasoningReplay: "not_supported"
      }
    }]);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/admin/model-config",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({
          entries: [{
            provider_name: "provider-a",
            provider_display_name: "Provider A",
            api_endpoint: "https://provider.example/v1",
            model_key: "model-a",
            model_display_name: "Model A",
            runtime_capabilities: {
              thinking_mode: "provider_default",
              reasoning_replay: "not_supported"
            }
          }]
        })
      })
    );
  });
});

describe("adminApi presence", () => {
  afterEach(() => {
    clearSession();
    vi.restoreAllMocks();
    window.sessionStorage.clear();
  });

  it("silently refreshes one 401 and retries the request with the new token", async () => {
    setSession({
      accessToken: "expired-admin-jwt",
      tokenType: "bearer",
      expiresIn: 1,
      principal: { orgId: 1, userId: 2, name: "Administrator", userCode: "admin" }
    });
    const summary = {
      web_online: { count: 0, users: [] },
      desktop_online: { count: 0, users: [] },
      daily_logins: { count: 0, users: [] },
      window_seconds: 120,
      daily_login_since: "2026-10-29T00:00:00+08:00",
      observed_at: "2026-10-29T08:30:00Z",
      source: "platform-session-user-directory"
    };
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        access_token: "renewed-admin-jwt",
        expires_in: 900,
        principal: { org_id: 1, user_id: 2, name: "Administrator", user_code: "admin" }
      }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(summary), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      }));

    await expect(adminApi.presenceSummary()).resolves.toMatchObject({ windowSeconds: 120 });

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[1][0]).toBe("/api/v1/auth/refresh");
    expect(fetchMock.mock.calls[2][0]).toBe("/api/v1/admin/usage/presence");
    expect(fetchMock.mock.calls[2][1]).toEqual(expect.objectContaining({
      headers: expect.objectContaining({ Authorization: "Bearer renewed-admin-jwt" })
    }));
  });

  it("reads the dedicated aggregate presence endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({
        web_online: { count: 12, users: ["张三（10001）"] },
        desktop_online: { count: 5, users: ["李四（10002）"] },
        daily_logins: { count: 38, users: ["王五（10003）"] },
        window_seconds: 120,
        daily_login_since: "2026-10-13T00:00:00+08:00",
        observed_at: "2026-10-13T08:30:00Z",
        source: "platform-session-user-directory"
      }), { status: 200, headers: { "Content-Type": "application/json" } })
    );

    await expect(adminApi.presenceSummary()).resolves.toEqual({
      webOnline: { count: 12, users: ["张三（10001）"] },
      desktopOnline: { count: 5, users: ["李四（10002）"] },
      dailyLogins: { count: 38, users: ["王五（10003）"] },
      windowSeconds: 120,
      dailyLoginSince: "2026-10-13T00:00:00+08:00",
      observedAt: "2026-10-13T08:30:00Z",
      source: "platform-session-user-directory"
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/admin/usage/presence",
      expect.objectContaining({ credentials: "include" })
    );
  });
});

describe("adminApi model visibility directory", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
  });

  it("uses the dedicated admin organization tree endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      })
    );

    await expect(adminApi.modelVisibilityDirectoryTree()).resolves.toEqual([]);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/admin/model-config/directory/tree",
      expect.objectContaining({ credentials: "include" })
    );
  });

  it("shows readable Chinese errors for invalid model visibility departments", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response(JSON.stringify({
      detail: "invalid model visibility department"
    }), { status: 400, headers: { "Content-Type": "application/json" } }));

    await expect(adminApi.publishModelConfig([{
      providerName: "provider-a",
      providerDisplayName: "Provider A",
      apiEndpoint: "https://provider.example/v1",
      modelKey: "model-a",
      modelDisplayName: "Model A",
      isRestricted: true,
      visibilityScope: {
        organization: false,
        userIds: [],
        departmentIds: ["virtual:unassigned"]
      }
    }])).rejects.toThrow(
      "模型可见范围包含无效、已停用或不可勾选的部门，请调整后重新发布。"
    );
  });
});

describe("adminApi application agent administrators", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
  });

  it("replaces the full administrator list with one PUT request", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      })
    );

    await expect(
      adminApi.replaceApplicationAgentAdmins(["zhangyi", "lisi"])
    ).resolves.toEqual([]);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/admin/application-agent-admins",
      expect.objectContaining({
        method: "PUT",
        credentials: "include",
        body: JSON.stringify({ user_codes: ["zhangyi", "lisi"] })
      })
    );
  });
});

describe("adminApi public assets", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
  });

  it("uploads a new public Skill zip through the catalog endpoint", async () => {
    const file = new File(["skill zip"], "skill.zip", { type: "application/zip" });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({
        asset_id: 42,
        asset_type: "skill",
        current_version_id: 51,
        version: 1,
        slug: "sales-skill",
        name: "Sales Skill",
        description: "Revenue helper",
        category_key: "productivity",
        status: "published",
        visibility: "organization",
        manifest: {},
        connection_config: {}
      }),
      { status: 201, headers: { "Content-Type": "application/json" } }
    ));

    await expect(adminApi.uploadPublicSkill({
      file,
      categoryKey: "productivity"
    })).resolves.toMatchObject({
      assetId: 42,
      versionId: 51,
      assetType: "skill",
      version: "1",
      categoryKey: "productivity",
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/admin/assets/skills?category_key=productivity",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        body: expect.any(FormData)
      })
    );
  });

  it("uploads a version to the selected public Skill without category parameters", async () => {
    const file = new File(["skill v2"], "skill-v2.zip", { type: "application/zip" });
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify([{
        asset_id: 42, asset_type: "skill", current_version_id: 51,
        version: 1, slug: "sales-skill", name: "Sales Skill",
        category_key: "productivity", status: "published",
        visibility: "organization", manifest: {}, connection_config: {}
      }]), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        asset_id: 42, asset_type: "skill", current_version_id: 52,
        version: 2, slug: "sales-skill", name: "Sales Skill",
        description: "Revenue helper", category_key: "productivity",
        status: "published", visibility: "organization", manifest: {},
        connection_config: {}
      }), { status: 201, headers: { "Content-Type": "application/json" } }));

    await expect(adminApi.uploadPublicSkillVersion(42, file)).resolves.toMatchObject({
      assetId: 42,
      versionId: 52,
      version: "2",
      categoryKey: "productivity"
    });

    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/v1/admin/assets/skills/42/versions?category_key=productivity",
      expect.objectContaining({ method: "POST", credentials: "include", body: expect.any(FormData) })
    );
  });

  it("manages a public Skill category without an irreversible unlist command", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({
        asset_id: 42, asset_type: "skill", current_version_id: 51,
        version: 1, slug: "sales-skill", name: "Sales Skill",
        category_key: "coding", status: "published",
        visibility: "organization", manifest: {}, connection_config: {}
      }), { status: 200, headers: { "Content-Type": "application/json" } }));

    await expect(adminApi.updatePublicSkillCategory(42, "coding")).resolves.toMatchObject({ categoryKey: "coding" });

    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/v1/admin/assets/skills/42/category?category_key=coding", expect.objectContaining({ method: "PATCH" }));
  });

  it("creates an organization Skill category from a display name", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({
        category_key: "category-writing",
        display_name: "写作",
        is_builtin: false,
        is_market_visible: true,
        is_personal_visible: true,
      }),
      { status: 201, headers: { "Content-Type": "application/json" } },
    ));

    await expect(adminApi.createSkillCategory({ displayName: "写作" }))
      .resolves.toMatchObject({ categoryKey: "category-writing", displayName: "写作" });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/admin/assets/skill-categories",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ display_name: "写作" }),
      }),
    );
  });

  it("creates a Streamable HTTP public MCP using snake_case payload keys", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({
        asset_id: 43,
        asset_type: "mcp",
        current_version_id: 53,
        version: 1,
        slug: "governed-mcp",
        name: "Governed MCP",
        description: null,
        transport: "streamable_http",
        status: "published",
        visibility: "organization",
        manifest: {},
        connection_config: { url: "https://mcp.example.com" },
      }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    ));

    await expect(adminApi.createPublicMcp({
      slug: "governed-mcp",
      name: "Governed MCP",
      description: null,
      transport: "streamable-http",
      endpoint: "https://mcp.example.com",
      config: {}
    })).resolves.toMatchObject({
      assetId: 43,
      manifest: { transport: "streamable_http" }
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/admin/assets/mcps",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          slug: "governed-mcp",
          name: "Governed MCP",
          description: null,
          visibility: "organization",
          material: {
            transport: "streamable_http",
            connection_config: { url: "https://mcp.example.com" },
            secret_refs: {},
            tools_schema: null
          }
        })
      })
    );
  });

  it("updates a public MCP version and returns rollout impact", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({
        asset: {
          asset_id: 43,
          asset_type: "mcp",
          current_version_id: 54,
          version: 2,
          slug: "governed-mcp",
          name: "Governed MCP v2",
          status: "published",
          visibility: "organization",
          transport: "streamable_http",
          manifest: {},
          connection_config: { url: "https://mcp-v2.example.com" },
        },
        rollout: {
          rollout_id: "mcp-43-rollout",
          affected_user_count: 2,
          affected_profile_count: 3,
          status: "waiting",
          created: true,
        },
      }),
      { status: 201, headers: { "Content-Type": "application/json" } }
    ));

    await expect(adminApi.updatePublicMcp({
      assetId: 43,
      expectedCurrentVersionId: 53,
      name: "Governed MCP v2",
      description: "new endpoint",
      transport: "streamable-http",
      endpoint: "https://mcp-v2.example.com",
      changeSummary: "admin update",
    })).resolves.toMatchObject({
      asset: { assetId: 43, currentVersionId: 54 },
      rollout: { affectedUserCount: 2, affectedProfileCount: 3 },
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/admin/assets/mcps/43/versions",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          expected_current_version_id: 53,
          name: "Governed MCP v2",
          description: "new endpoint",
          material: {
            transport: "streamable_http",
            connection_config: { url: "https://mcp-v2.example.com" },
            secret_refs: {},
            tools_schema: null,
          },
          change_summary: "admin update",
        }),
      })
    );
  });

  it("accepts the asynchronous MCP force-disable rollout response", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({
        rollout_id: "mcp-43-revoke",
        affected_user_count: 4,
        affected_profile_count: 6,
        status: "waiting",
        created: true,
      }),
      { status: 202, headers: { "Content-Type": "application/json" } }
    ));

    await expect(adminApi.forceDisablePublicMcp(43, "security issue", "mcp-disable-43")).resolves.toMatchObject({
      affectedUserCount: 4,
      affectedProfileCount: 6,
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/admin/assets/mcps/43/force-disable",
      expect.objectContaining({ method: "POST" })
    );
  });

  it("permanently deletes public Skill and MCP assets through distinct DELETE endpoints", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({
        rollout_id: "skill-delete-42",
        affected_user_count: 2,
        affected_profile_count: 3,
        status: "waiting",
        created: true,
      }), { status: 202, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        rollout_id: "mcp-delete-43",
        affected_user_count: 1,
        affected_profile_count: 1,
        status: "waiting",
        created: true,
      }), { status: 202, headers: { "Content-Type": "application/json" } }));

    await expect(adminApi.deletePublicSkill(42)).resolves.toMatchObject({
      rolloutId: "skill-delete-42",
      affectedProfileCount: 3,
    });
    await expect(adminApi.deletePublicMcp(43)).resolves.toMatchObject({
      rolloutId: "mcp-delete-43",
      affectedProfileCount: 1,
    });

    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/v1/admin/assets/skills/42", expect.objectContaining({ method: "DELETE" }));
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/v1/admin/assets/mcps/43", expect.objectContaining({ method: "DELETE" }));
  });

  it("maps MCP slug and version conflicts to actionable Chinese messages", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(
        JSON.stringify({ detail: "MCP_SLUG_EXISTS" }),
        { status: 409, headers: { "Content-Type": "application/json" } }
      ))
      .mockResolvedValueOnce(new Response(
        JSON.stringify({ detail: "MCP_VERSION_CONFLICT" }),
        { status: 409, headers: { "Content-Type": "application/json" } }
      ));

    await expect(adminApi.createPublicMcp({
      slug: "governed-mcp",
      name: "Governed MCP",
      transport: "streamable-http",
      endpoint: "https://mcp.example.com",
    })).rejects.toThrow("请使用“更新配置”");
    await expect(adminApi.updatePublicMcp({
      assetId: 43,
      expectedCurrentVersionId: 53,
      name: "Governed MCP",
      transport: "streamable-http",
      endpoint: "https://mcp-v2.example.com",
    })).rejects.toThrow("请刷新列表后重试");
  });

  it("surfaces readable backend validation errors", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(
      JSON.stringify({ detail: "secret fields are not allowed" }),
      { status: 400, headers: { "Content-Type": "application/json" } }
    ));

    await expect(adminApi.publicSkillCatalog()).rejects.toThrow("secret fields are not allowed");
  });
});

describe("adminApi kubernetes resources", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
  });

  it("requests a bounded previous log snapshot", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("line one\n", { status: 200 })
    );

    await expect(adminApi.podLogs(3, "runtime-pod", {
      container: "user-container",
      tailLines: 500,
      previous: true
    })).resolves.toBe("line one\n");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/admin/resources/runtime-workspaces/3/pods/runtime-pod/logs?runtime_kind=personal&container=user-container&tail_lines=500&previous=true",
      expect.objectContaining({ credentials: "include" })
    );
  });

  it("uses fetch with an abort signal for bearer-authenticated log follow", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("event: log.done\ndata: {}\n\n", {
        status: 200,
        headers: { "Content-Type": "text/event-stream" }
      })
    );
    const controller = new AbortController();

    await adminApi.followPodLogs(
      3,
      "runtime-pod",
      { container: "user-container", tailLines: 100 },
      controller.signal
    );

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/admin/resources/runtime-workspaces/3/pods/runtime-pod/logs/stream?runtime_kind=personal&container=user-container&tail_lines=100",
      expect.objectContaining({
        credentials: "include",
        signal: controller.signal,
        headers: expect.objectContaining({ Accept: "text/event-stream" })
      })
    );
  });
});

describe("adminApi knowledge governance", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
  });

  it("preserves compiler node type identifiers used by asset filters", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({
        schema_version: "1.0",
        generated_at: "2026-07-30T00:00:00Z",
        source_hash: "a".repeat(64),
        summary: {
          node_count: 1,
          edge_count: 0,
          model_count: 1,
          field_count: 0,
          department_count: 0,
          permission_count: 0,
          relationship_count: 0,
          metric_contract_count: 0,
          node_types: { semantic_model: 1, permission_policy: 2 },
          edge_types: {}
        },
        validation_summary: { errors: 0, warnings: 0, info: 0, status: "passed" },
        cache: { state: "hit" },
        latest_attempt: { status: "succeeded" }
      }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        items: [],
        total: 0,
        page: 1,
        page_size: 50,
        total_pages: 0,
        cache: { state: "hit" },
        latest_attempt: { status: "succeeded" }
      }), { status: 200, headers: { "Content-Type": "application/json" } }));

    const result = await adminApi.knowledgeGovernanceSummary();
    expect(result.summary.nodeTypes).toEqual({
      semantic_model: 1,
      permission_policy: 2
    });

    await adminApi.knowledgeGovernanceAssets({ assetType: "semantic_model" });
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/admin/knowledge-governance/assets?asset_type=semantic_model&page=1&page_size=50",
      expect.objectContaining({ credentials: "include" })
    );
  });

  it("encodes opaque asset IDs and reads structured governance failures", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({
        asset: {
          id: "document:wiki/sales.md",
          type: "document",
          name: "sales",
          properties: {},
          sources: []
        },
        relationships: [],
        dependency: null,
        cache: { state: "hit" },
        latest_attempt: { status: "succeeded" }
      }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        error: {
          code: "knowledge_governance_source_not_configured",
          message: "知识治理源目录尚未配置。",
          retryable: false
        }
      }), { status: 503, headers: { "Content-Type": "application/json" } }));

    await adminApi.knowledgeGovernanceAsset("document:wiki/sales.md");
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/v1/admin/knowledge-governance/assets/document%3Awiki%2Fsales.md",
      expect.objectContaining({ credentials: "include" })
    );

    await expect(adminApi.knowledgeGovernanceSummary())
      .rejects.toThrow("知识治理源目录尚未配置。");
  });
});
