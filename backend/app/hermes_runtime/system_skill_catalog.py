"""Read-only descriptions copied from the Runtime image Skill frontmatter.

This module is the Backend-side release artifact.  The runtime image build must
refresh it whenever a platform default Skill changes, and the completeness test
below prevents the UI from silently falling back to one generic description.
"""

from __future__ import annotations

from app.hermes_runtime.skill_visibility_policy import PLATFORM_DEFAULT_SKILL_NAMES


SYSTEM_SKILL_DESCRIPTION_CATALOG: dict[str, str] = {
    "hermes-agent": "Configure, extend, or contribute to Hermes Agent.",
    "hermes-agent-skill-authoring": (
        "Author in-repo SKILL.md: frontmatter, validator, structure, and "
        "writing-quality principles."
    ),
    "hermes-browser-zh": (
        "使用 Hermes 原生 browser_* 工具进行匿名、无持久状态的中文网页浏览与基础交互。"
        "适用于公开网页检索、阅读、页面内搜索、填写普通表单草稿和提取公开信息；"
        "不用于登录、上传、下载、验证码、支付或发布。"
    ),
    "live-data-report-builder": (
        "生成可发布到看板区的多文件 ZIP HTML 看板；凡看板、报告、报表、BI、数据分析 HTML "
        "需求统一使用，采用平台活数据机制按当前用户权限实时注入数据，不内嵌业务数据。"
    ),
    "minimax-docx": "使用镜像内 OpenXML CLI 创建、编辑、校验和预览中文 DOCX 文档。",
    "minimax-pdf": "中文 PDF 的创建、填表、重排、预览与校验。所有默认输出使用镜像内 Noto CJK SC 字体和离线工具。",
    "minimax-xlsx": "创建、读取、编辑、重算和校验中文 XLSX 工作簿，默认使用 Noto Sans CJK SC。",
    "ppt-master": (
        "生成、美化、编辑原生可编辑的中文 PPTX 演示文稿，支持版式、图表、模板与既有文档编辑；"
        "离线使用镜像内字体与工具。"
    ),
}


def system_skill_descriptions() -> dict[str, str]:
    """Return a validated copy so callers cannot mutate the catalog."""
    if set(SYSTEM_SKILL_DESCRIPTION_CATALOG) != set(PLATFORM_DEFAULT_SKILL_NAMES):
        raise RuntimeError("system Skill description catalog is out of date")
    return dict(SYSTEM_SKILL_DESCRIPTION_CATALOG)


__all__ = ["SYSTEM_SKILL_DESCRIPTION_CATALOG", "system_skill_descriptions"]
