import { describe, expect, it } from "vitest";

import type { ToolMessage } from "./pages/chatRunProjection";
import { detailForTool, headerNameForTool, presentationForTool, sanitizeToolDetail, statusText, traceLabelForTool } from "./toolPresentation";

function tool(name: string, result?: string, status: ToolMessage["status"] = "completed"): ToolMessage {
  return { id: `tool-${name}`, kind: "tool", role: "tool", callId: `call-${name}`, name, status, ...(result ? { result } : {}) };
}

describe("tool presentation", () => {
  it("maps known tools to user-facing Chinese and hides unknown identifiers", () => {
    expect(presentationForTool("search_files")).toMatchObject({ displayName: "查找相关文件", category: "search" });
    expect(presentationForTool("read_excel").completedText).toBe("已读取表格");
    expect(presentationForTool("enterprise_document_search").runningText).toBe("正在检索企业知识库…");
    expect(presentationForTool("skill_view").displayName).toBe("查看能力说明");
    expect(presentationForTool("mcp__asset-615__tool_describe").displayName).toBe("查看工具说明");
    expect(presentationForTool("mcp__asset-615__enterprise_sql_context")).toMatchObject({ displayName: "获取数据口径", completedText: "已获取数据口径" });
    expect(presentationForTool("get_enterprise_sql_context").displayName).toBe("获取数据口径");
    expect(presentationForTool("department_primer_loader").displayName).toBe("读取知识说明");
    expect(presentationForTool("enterprise_knowledge_read")).toMatchObject({ displayName: "读取企业知识", completedText: "已读取企业知识" });
    expect(presentationForTool("terminal").displayName).toBe("执行命令");
    expect(presentationForTool("terminal")).toMatchObject({ description: "在隔离运行环境中执行命令。", inputLabel: "执行命令" });
    expect(presentationForTool("unknown_internal_tool")).toMatchObject({
      displayName: "执行操作",
      runningText: "正在执行操作…",
      completedText: "操作已完成",
      failedText: "操作失败",
      category: "generic"
    });
  });

  it("resolves namespaced MCP tools and keeps their query in the trace label", () => {
    const mcpSearch: ToolMessage = {
      ...tool("mcp__asset_615__search_web"),
      input: JSON.stringify({ arguments: { query: "深圳今天天气" } })
    };

    expect(presentationForTool(mcpSearch.name)).toMatchObject({ displayName: "搜索网页", category: "web" });
    expect(detailForTool(mcpSearch).input).toBe("深圳今天天气");
    expect(traceLabelForTool(mcpSearch)).toBe("调用搜索网页，查询“深圳今天天气”");
    expect(traceLabelForTool(tool("mcp__asset-615__search_web"))).toBe("已完成网页搜索");
  });

  it("returns localized text for every status", () => {
    expect(statusText(tool("write_file", undefined, "running"))).toBe("正在保存文件…");
    expect(statusText(tool("write_file", undefined, "completed"))).toBe("已保存文件");
    expect(statusText(tool("write_file", undefined, "failed"))).toBe("保存文件失败");
  });

  it("distinguishes enterprise preparation and query operations", () => {
    const context: ToolMessage = { ...tool("enterprise_sql_context"), input: JSON.stringify({ query: "上海区上周商机口径" }) };
    const query: ToolMessage = { ...tool("enterprise_sql_query"), input: JSON.stringify({ sql: "SELECT COUNT(*) FROM crm_project_info_t" }) };
    const read: ToolMessage = { ...tool("enterprise_knowledge_read"), input: JSON.stringify({ relative_path: "PRIMER.md" }) };

    expect(traceLabelForTool(context)).toBe("调用获取数据口径，获取“上海区上周商机口径”");
    expect(traceLabelForTool(query)).toBe("调用查询企业数据，查询“SELECT COUNT(*) FROM crm_project_info_t”");
    expect(traceLabelForTool(read)).toBe("调用读取企业知识，读取“PRIMER.md”");
  });

  it("builds safe summaries from recognized structured results", () => {
    const sensitiveField = ["api", "key"].join("_");
    expect(detailForTool(tool("read_excel", JSON.stringify({ rows: 286, [sensitiveField]: "secret" })))).toMatchObject({
      summary: "已读取 286 行",
      formatted: expect.stringContaining(`"${sensitiveField}": "••••"`)
    });
    expect(detailForTool(tool("search_files", '{"matches":["/root/a.txt","/root/b.txt"]}')).summary).toBe("找到 2 个文件");
    expect(detailForTool(tool("web_search", '{"results":[1,2,3]}')).summary).toBe("找到 3 条相关结果");
    expect(detailForTool(tool("write_file", '{"path":"/root/report.docx"}')).summary).toBe("已保存 report.docx");
    expect(detailForTool(tool("write_file", '{"error":"Write denied","path":"/root/report.docx"}', "failed")).summary).toBe("Write denied");
  });

  it("renders terminal input and output without JSON wrapper noise", () => {
    const terminal: ToolMessage = {
      ...tool("terminal", '{"output":"v22.22.3","exit_code":0}'),
      input: '{"command":"node -v"}'
    };

    expect(detailForTool(terminal)).toMatchObject({ input: "node -v", result: "v22.22.3" });
    expect(detailForTool(terminal).formatted).toContain("调用参数");
    expect(detailForTool(terminal).formatted).toContain("执行结果");
  });

  it("uses fixed concise copy for the tools seen in a presentation task", () => {
    const skill: ToolMessage = {
      ...tool("skill_view", '{"content":"long skill guide"}'),
      input: '{"name":"powerpoint"}'
    };
    const todo: ToolMessage = {
      ...tool("todo", '{"todos":[{"id":"1"},{"id":"2"}]}'),
      input: '{"todos":[{"id":"1"},{"id":"2"}]}'
    };
    const patch: ToolMessage = {
      ...tool("patch", '{"success":true,"diff":"very long diff"}'),
      input: '{"path":"<path>/create_ice_ppt.js","old_string":"a","new_string":"b"}'
    };
    const write: ToolMessage = {
      ...tool("write_file", '{"bytes_written":14154,"lint":{"status":"error"}}'),
      input: '{"path":"<path>/create_ice_ppt.js","content":"long source"}'
    };

    expect(detailForTool(skill)).toMatchObject({ input: "powerpoint", result: "已读取能力说明" });
    expect(detailForTool(todo)).toMatchObject({ input: "更新 2 个任务步骤", result: "任务步骤已更新" });
    expect(detailForTool(patch)).toMatchObject({ input: "<path>/create_ice_ppt.js", result: "文件已修改" });
    expect(detailForTool(write)).toMatchObject({ input: "<path>/create_ice_ppt.js", result: "文件已保存" });
  });

  it("unwraps one persisted JSON-string layer before extracting a file path", () => {
    const nestedInput = JSON.stringify('{"path":"<path>/create_ppt.js","content":"very long source"}');
    const write: ToolMessage = { ...tool("write_file", '{"bytes_written":10}'), input: nestedInput };

    expect(detailForTool(write)).toMatchObject({ input: "<path>/create_ppt.js", result: "文件已保存" });
    expect(detailForTool(write).formatted).toContain('"path": "<path>/create_ppt.js"');
  });

  it("uses the fixed skill identifier in a skill-view group header", () => {
    const skill: ToolMessage = { ...tool("skill_view", '{"success":true}'), input: '{"name":"powerpoint"}' };

    expect(headerNameForTool(skill)).toBe("查看 powerpoint 技能能力");
  });

  it("distinguishes a skill guide read from the skill overview", () => {
    const guide: ToolMessage = {
      ...tool("skill_view", '{"success":true}'),
      input: '{"name":"powerpoint","file_path":"pptxgenjs.md"}'
    };

    expect(headerNameForTool(guide)).toBe("查看 powerpoint 技能 · pptxgenjs.md");
    expect(detailForTool(guide)).toMatchObject({ input: "powerpoint", context: "pptxgenjs.md" });
  });

  it("redacts legacy details defensively", () => {
    expect(sanitizeToolDetail('{"secret":"x","nested":{"token":"y"}}')).toContain('"secret": "••••"');
    expect(sanitizeToolDetail("Bearer abc.def.ghi")).toBe("••••");
    expect(sanitizeToolDetail("http://127.0.0.1:8000/api?q=1&token=x")).toBe("<endpoint>/api?q=1&token=••••");
    expect(sanitizeToolDetail("C:\\Users\\alice\\report.xlsx")).toBe("<path>/report.xlsx");
    expect(sanitizeToolDetail("/root/work/report.xlsx")).toBe("<path>/report.xlsx");
    expect(sanitizeToolDetail("x".repeat(12_100)).endsWith("[内容已截断]")).toBe(true);
  });

  it("limits nested content and avoids unsafe generic summaries", () => {
    const nested = JSON.stringify({ a: { b: { c: { d: { e: { f: { g: "secret" } } } } } } });
    expect(sanitizeToolDetail(nested)).toContain("[内容层级过深]");
    expect(detailForTool(tool("unknown", "ordinary business paragraph")).summary).toBeNull();
    expect(detailForTool(tool("unknown", "")).formatted).toBeNull();
  });
});
