import { describe, expect, it } from "vitest";
import type { OrganizationDirectoryNode } from "@hermes-cloud/schemas";
import {
  buildScopeTreeData,
  checkedKeysToScope,
  filterScopeTreeData,
  flattenScopeDirectory,
  scopeNodeKey,
  scopeToCheckedKeys
} from "./appScopeModel";

const directory: OrganizationDirectoryNode[] = [
  {
    nodeType: "organization",
    organizationId: 49,
    name: "示例业务技术",
    children: [
      {
        nodeType: "department",
        departmentId: "virtual:root",
        virtual: true,
        name: "全员分组",
        children: []
      },
      {
        nodeType: "department",
        departmentId: "49",
        name: "总经办",
        children: [
          {
            nodeType: "user",
            userId: 1,
            name: "张三",
            userCode: "z001",
            loggedIn: true,
            children: []
          },
          {
            nodeType: "user",
            userId: 2,
            name: "李四",
            userCode: "l002",
            loggedIn: true,
            children: []
          }
        ]
      },
      {
        nodeType: "department",
        departmentId: "50",
        name: "研发中心",
        children: [
          {
            nodeType: "department",
            departmentId: "51",
            name: "前端组",
            children: [
              {
                nodeType: "user",
                userId: 3,
                name: "王五",
                userCode: "w003",
                loggedIn: true,
                children: []
              }
            ]
          }
        ]
      },
      {
        nodeType: "department",
        departmentId: "60",
        name: "销售部",
        children: [
          {
            nodeType: "user",
            name: "赵六",
            userCode: "z006",
            loggedIn: false,
            children: []
          }
        ]
      }
    ]
  }
];

const metas = flattenScopeDirectory(directory);

describe("appScopeModel", () => {
  it("组织根勾选短路为 organization scope，覆盖其余选择", () => {
    const scope = checkedKeysToScope(
      ["organization:49", "department:50", "user:1"],
      metas
    );
    expect(scope).toEqual({ organization: true, departmentIds: [], userIds: [] });
  });

  it("virtual:* 合成部门即使被勾选也不进入 scope", () => {
    const scope = checkedKeysToScope(["department:virtual:root"], metas);
    expect(scope).toEqual({ organization: false, departmentIds: [], userIds: [] });
  });

  it("公司根使用真实 com_id，是合法的部门授权目标", () => {
    // 公司根 dept_id 直接使用 com_id（49/51/53），不是 virtual 节点。
    const scope = checkedKeysToScope(["department:49"], metas);
    expect(scope.departmentIds).toEqual(["49"]);
  });

  it("部门祖先勾选后去冗余后代部门", () => {
    // 同时勾选「研发中心(50)」与其子「前端组(51)」：51 被祖先 50 吸收，去冗余。
    const scope = checkedKeysToScope(["department:50", "department:51"], metas);
    expect(scope.departmentIds).toEqual(["50"]);
  });

  it("已勾选祖先部门时，其后代人员去冗余", () => {
    // 勾选「总经办(49部门)」后再勾选其下人员：人员被部门覆盖，不重复进入 userIds。
    const scope = checkedKeysToScope(["department:49", "user:1"], metas);
    expect(scope.departmentIds).toEqual(["49"]);
    expect(scope.userIds).toEqual([]);
  });

  it("userIds 去重", () => {
    const scope = checkedKeysToScope(["user:1", "user:1", "user:2"], metas);
    expect(scope.userIds).toEqual([1, 2]);
  });

  it("未 provision 人员（无 userId）被过滤，不进入 scope", () => {
    // 赵六未激活（userId null）：key 是 user:unprovisioned:z006，规范化时必须丢弃；
    // 组件层负责先 provision 再以真实 id 入草稿。
    const key = scopeNodeKey((directory[0] as Extract<OrganizationDirectoryNode, { nodeType: "organization" }>).children[3].children[0]);
    expect(key).toBe("user:unprovisioned:z006");
    const scope = checkedKeysToScope([key], metas);
    expect(scope.userIds).toEqual([]);
  });

  it("scope ↔ checkedKeys 往返：organization 与部门/人员两种形态", () => {
    expect(scopeToCheckedKeys({ organization: true, departmentIds: [], userIds: [] }, directory))
      .toEqual(["organization:49"]);
    expect(
      scopeToCheckedKeys(
        checkedKeysToScope(["department:50", "user:1", "user:2"], metas),
        directory
      ).sort()
    ).toEqual(["department:50", "user:1", "user:2"]);
  });

  it("buildScopeTreeData 标记虚拟部门 disabled，用户名带工号", () => {
    const treeData = buildScopeTreeData(directory);
    const org = treeData[0];
    expect(org.key).toBe("organization:49");
    const [virtualNode, generalOffice] = org.children!;
    expect(virtualNode.disabled).toBe(true);
    expect(generalOffice.children![0].title).toBe("张三（z001）");
  });

  it("filterScopeTreeData 按名称或工号保留命中子树", () => {
    const byName = filterScopeTreeData(directory, "前端");
    expect(byName[0].children).toHaveLength(1);
    const byCode = filterScopeTreeData(directory, "w003");
    const dept = byCode[0].children![0];
    expect(dept.nodeType === "department" && dept.children).toHaveLength(1);
    const none = filterScopeTreeData(directory, "不存在");
    expect(none).toHaveLength(0);
  });
});
