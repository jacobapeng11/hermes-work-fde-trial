---
name: live-data-report-builder
description: 生成可发布到看板区的多文件 ZIP HTML 看板。凡用户要求生成看板、报告、报表、BI 或数据分析 HTML 时一律使用本技能，无需用户提到实时或活数据。看板统一采用平台活数据机制：根 index.html 内嵌冻结查询，打开时由平台按当前用户权限实时注入数据，不得把业务数据预下载或内嵌进 HTML；仅基线/均值等静态参考数据可内嵌。Use for any dashboard, report, or BI HTML request.
---

# 看板区 HTML 看板生成

## 目标

凡用户要求生成**看板、报告、报表、BI 或数据分析 HTML**，一律生成一个可发布到看板区的 **ZIP 看板包**，并**统一采用平台活数据机制**：看板不内嵌真实业务数据、不发起网络请求；发布后由平台在每次打开时按当前用户权限查询数据，并把结果注入到当前页面的 `hermes-report-data` 节点。用户无需理解"活数据"这一术语，你只需按本技能默认实现即可。

用户只需要表达业务想法，例如：

```text
帮我做一个近 90 天产出报表，按区域和产品线统计金额，再显示 Top 20 客户。
```

你负责把需求转换成渲染逻辑和冻结 SQL Manifest。

## 数据安全边界（禁止内嵌业务数据）

业务数据**绝不允许预下载或内嵌**到任何交付物中——包括聊天里的 ` ```html ` 代码块、静态 HTML 文件、ZIP 包及其 `assets/*.js` 数据文件。内嵌数据会以"生成者权限"固化进文件，文件被下载或分享后，任何拿到文件的人都能越权看到，绕过平台按打开者权限过滤的权限模型。

- **必须走平台注入**：凡会随用户、权限或时间变化的业务明细/汇总数据，一律通过根 `index.html` 的 manifest 冻结查询，由平台在打开时按当前用户权限实时查询注入。
- **可内嵌的例外**：目标值、基线值、行业/历史均值等**静态参考数据**——这类数据不随用户、权限、时间变化，可作为常量直接写进 HTML/JS。判据：换一个用户、换一个时间打开，这个值是否仍应完全相同？若是，才可内嵌；否则一律走平台注入。

## 对用户沟通口径

- 对用户统一称"**看板**"，不要对用户使用"活数据 / manifest / 注入 / sql_manifest"等技术术语。
- 解释机制时用大白话："**打开时自动获取最新数据，且只显示你有权限看的内容**"。

## 必须交付的文件

交付一个 ZIP 压缩包，根目录必须包含完整 `index.html`。简单报表可以只打包 `index.html`；复杂看板可以包含多页面和包内相对资源，推荐结构：

```text
report.zip
├── index.html
├── pages/detail.html
├── assets/app.js
├── assets/style.css
└── assets/logo.svg
```

以 `assets/live-report-template.html` 作为 `index.html` 的基础修改。不得引入外部 CDN、外链 JS/CSS/字体/图片，也不得引用 ZIP 包外的文件。

打包时必须把 ZIP 写入云端任务文件目录，不能只放在 `.work/`、本地临时目录或只在回复中输出代码。可在任务目录使用 Python 标准库打包：

```python
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

source = Path("report")  # 按实际源码目录调整
with ZipFile("report.zip", "w", compression=ZIP_DEFLATED) as archive:
    for path in source.rglob("*"):
        if path.is_file():
            archive.write(path, path.relative_to(source).as_posix())
```

最终回复必须告知 ZIP 文件名，并提醒用户从预览面板分享该 ZIP。

根 `index.html` 必须包含：

```html
<script type="application/json" id="hermes-report-manifest">
{
  "engine": "sql_manifest_v1",
  "data_mode": "live",
  "queries": [
    {
      "id": "meaningful_query_id",
      "sql": "SELECT ...",
      "page": 1,
      "page_size": 200
    }
  ]
}
</script>
```

`page` 和 `page_size` 可省略；省略时平台默认 `page=1`、`page_size=200`。

### 多页面 manifest

- 根 `index.html` 的 manifest 是看板进入活数据链路的必要标记，必须有效。
- `pages/**/*.html` 中需要实时数据的页面，可以各自携带同格式 `hermes-report-manifest`；打开该页面时只执行该页查询。
- 不需要实时数据的页面可以不带 manifest，仅作为静态壳页或导航页。
- 全包 query id 必须唯一，所有页面查询总数不超过 8 条。
- 多页面之间用相对链接（例如 `href="pages/detail.html"`）导航，不得把页面拼成一个超长 HTML。

## SQL 规则

1. 先根据用户需求确定指标、维度、时间范围和过滤条件；信息不足时先提出一个澄清问题。
2. 优先使用企业知识 data_service 已配置、已具备权限注入规则的业务表。必要时先用企业知识 SQL Context 获取可用表和字段，不要猜测字段。
3. 每个报表最多 8 条查询；能合并的简单维度应合并，避免为了布局拆出重复 SQL。
4. 每条 SQL 必须是一条只读 `SELECT`。
5. 禁止：
   - `INSERT/UPDATE/DELETE/DROP` 等写操作；
   - 多语句；
   - `UNION`；
   - CTE；
   - 子查询；
   - 权限系统内部表；
   - 未在 data_service 配置的表。
6. 优先返回聚合结果，避免 `SELECT *` 和明细大表全量导出。每个查询默认 `page_size=200`，确有必要时最大 500。
7. 查询 `id` 使用 `A-Z/a-z/0-9/_/-`，最长 64，且不能重复。`id` 应表达业务含义，例如 `output_by_region`。
8. SQL 中不要拼接用户敏感凭据或会话信息。平台会按当前打开者自动做行级权限过滤。
9. 对时间口径要明确。例如商机流量默认按 `apply_date`，项目存量/预签口径按 `advance_date`；产出报表应使用产出表的事实日期字段。

## 渲染规则

页面渲染逻辑从以下节点读取平台注入的数据：

```js
const report = JSON.parse(
  document.getElementById("hermes-report-data").textContent
);
```

数据结构：

```json
{
  "request_id": "...",
  "generated_at": "...",
  "user": {"name": "...", "user_code": "..."},
  "queries": {
    "meaningful_query_id": {
      "row_count": 10,
      "total": 10,
      "page": 1,
      "page_size": 200,
      "total_pages": 1,
      "data": [],
      "semantic_warnings": []
    }
  }
}
```

要求：

1. 每个可能独立打开的 HTML 页面都要能读取当前页面自己的 `hermes-report-data`；该节点不存在时，显示“请从看板区打开该报表”的空态，不得报错白屏。
2. 遍历 `report.queries` 动态渲染，不要硬编码假定每个 query id 一定存在。
3. 对单元格值做 HTML 转义。
4. 显示当前用户姓名和工号、`generated_at`、每个查询的行数。
5. 刷新按钮使用：
   ```js
   location.reload();
   ```
6. 禁止 `fetch`、XHR、WebSocket、表单提交、外部链接、`localStorage`、`sessionStorage`、Cookie 读取。
7. 共享逻辑可放入 `assets/*.js`，页面用相对路径引用；图表动画可使用包内 JS/CSS/SVG/Canvas 实现，不得加载外部库。
8. 空数据、查询失败和语义警告要有清晰提示；不要用假数据填充。

## 发布口径

最终提醒用户（用大白话，不出现技术术语）：

1. 看板必须发布为根目录含 `index.html` 的 ZIP，可以包含多页面和 `assets/` 等包内相对资源；
2. 从看板区卡片打开时才会自动获取最新数据；
3. 直接访问匿名直出 URL 只能看到空态或示例数据，且多文件资源不保证完整预览；
4. 查询会随看板版本冻结；修改查询必须更新看板包；
5. 不同用户打开同一看板，看到的数据范围按各自权限过滤。

若用户要求本地预览，可以展示模板空态，但不得编造真实业务数据。
