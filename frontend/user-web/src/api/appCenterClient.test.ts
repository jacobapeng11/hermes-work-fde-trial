import { afterEach, describe, expect, it, vi } from "vitest";

import { clearSession } from "../auth";
import { userApi } from "./client";

const response = {
  id: 11,
  name: "看板",
  description: "看板数据概览",
  access_url: "https://demo.example/11",
  source_type: "agent_html",
  share_mode: "scope",
  creator_name: "创建者",
  mine: true,
  updated_at: "2026-10-16T12:00:00",
  scope_summary: "全员",
  publish_version_id: "3567"
};

describe("userApi app center", () => {
  afterEach(() => {
    clearSession();
    vi.restoreAllMocks();
  });

  it("creates an app from html with a camelized request body", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(response), { status: 201 })
    );

    await expect(userApi.createApp({
      name: "看板",
      description: "看板数据概览",
      htmlContent: "<!doctype html><html></html>",
      shareMode: "scope",
      idempotencyKey: "request-1",
      scope: { organization: true, departmentIds: [], userIds: [] },
      sourceSessionId: "session-1",
      sourceRunId: "run-1"
    })).resolves.toMatchObject({ id: 11, accessUrl: "https://demo.example/11" });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/user/apps",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          name: "看板",
          description: "看板数据概览",
          html_content: "<!doctype html><html></html>",
          share_mode: "scope",
          idempotency_key: "request-1",
          scope: { organization: true, department_ids: [], user_ids: [] },
          source_session_id: "session-1",
          source_run_id: "run-1"
        })
      })
    );
  });

  it("uploads a package as metadata plus zip multipart fields", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(response), { status: 201 })
    );
    const file = new File(["zip"], "app.zip", { type: "application/zip" });

    await expect(userApi.uploadApp({
      name: "看板",
      description: "看板数据概览",
      shareMode: "link",
      idempotencyKey: "request-2",
      scope: null,
      file
    })).resolves.toMatchObject({ id: 11 });

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    const body = init.body as FormData;
    expect(init.method).toBe("POST");
    expect(body.get("file")).toBe(file);
    expect(JSON.parse(String(body.get("metadata")))).toEqual({
      name: "看板",
      description: "看板数据概览",
      share_mode: "link",
      idempotency_key: "request-2",
      scope: null,
      source_session_id: null,
      source_run_id: null
    });
  });

  it("maps controlled app center errors to user-facing messages", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      detail: {
        code: "APP_CENTER_PACKAGE_INVALID",
        error_code: -14001,
        message: "分享范围包含不可用部门",
        request_id: "request-1"
      }
    }), { status: 400 }));

    await expect(userApi.listApps()).rejects.toEqual(expect.objectContaining({
      message: "分享范围包含无效或已停用的部门，请重新选择后保存",
      status: 400
    }));
  });
});
