from types import SimpleNamespace

from platform_supervisor.edge import _prompt_with_attachments


def _grant() -> SimpleNamespace:
    return SimpleNamespace(
        attachments=[
            SimpleNamespace(
                file_id="12",
                display_name="截图.png",
                relative_path="workspace/uploads/截图.png",
                mime_type="image/png",
            ),
            SimpleNamespace(
                file_id="13",
                display_name="报告.txt",
                relative_path="workspace/uploads/报告.txt",
                mime_type="text/plain",
            ),
        ]
    )


def test_image_attachment_prompt_avoids_tool_detours() -> None:
    prompt = _prompt_with_attachments(_grant(), ["12"], "这是什么")

    assert "workspace/uploads/截图.png" not in prompt
    assert "- 截图.png" in prompt
    assert "Attachment-specific delivery instructions below override" in prompt
    assert "Do not call vision_analyze, terminal, file/workspace tools" in prompt
    assert "session_search" in prompt


def test_mixed_attachment_prompt_only_hides_image_paths() -> None:
    prompt = _prompt_with_attachments(_grant(), ["12", "13"], "分析附件")

    assert "workspace/uploads/截图.png" not in prompt
    assert "- 截图.png" in prompt
    assert "- 报告.txt: workspace/uploads/报告.txt" in prompt


def test_non_image_attachment_prompt_keeps_workspace_path() -> None:
    prompt = _prompt_with_attachments(_grant(), ["13"], "总结附件")

    assert "- 报告.txt: workspace/uploads/报告.txt" in prompt
    assert "vision channel" not in prompt
