# 新浪网当日实时头条抓取（验证于 2026-10-02）

## 核心方法（最有效）

直接调用 MCP `parse_url` 解析新浪新闻首页，即可拿到**当天实时**的中文头条全列表（含日期、比分、热点、分类），**无需启动浏览器**，比 `search_web` 更实时可靠。

```
mcp__asset_615__parse_url(params={"url": "https://news.sina.com.cn/"})
```

返回 JSON：`output.content` 为整页正文（large — 约 80KB，可能被持久化到 `/tmp/hermes-results/chatcmpl-*.txt`，用 Python 先 `json.loads` 外层再 `json.loads(data['result'])` 取 `output.content`）。

页面内容结构：顶部"热榜"（新浪全站热点）→ "要闻"（含 `北京时间：YYYY.M.D 周X`，当日时政/民生/国际头条）→ 各分类（军事/国际/体育/财经科技/娱乐/汽车等，各段带"每日排行"）。

## 为什么不用 search_web 兜底

本环境实测（2026-10-02）：
- `search_web(query="今日热点新闻")` → `output: null`（空）。
- `search_web(query="今天头条新闻 国内")` → 返回的是**9-16 的旧闻**（李家超施政报告等），非当日。
- `parse_url("https://news.sina.com.cn/")` → 拿到**10-02 当天**全部头条。

结论：**查"今天有什么新闻"优先直接 parse_url 新闻门户首页**（新浪/网易/腾讯等），search_web 作为补充搜索特定事件时用。

## 大结果解析模板

```python
import json
with open('/tmp/hermes-results/chatcmpl-XXX.txt', encoding='utf-8') as f:
    raw = f.read()
data = json.loads(raw)                    # 外层 {"result": "..."}
inner = json.loads(data['result'])        # {"output": {"content": "整页正文"}}
content = inner['output']['content']
print(content)
```

## 展示要点（对用户）

按板块整理（时政/热点/体育/国际/财经科技），保留原文标题与事实，不二次翻译、不编造。可主动提供"深挖某条原文"的后续。

