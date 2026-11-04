from platform_supervisor.edge import _public_history_source
from platform_supervisor.execution.manager import (
    _runtime_attachment_payloads,
    _source_detail_with_runtime_attachments,
)


def test_runtime_attachments_are_validated_and_bound_to_source_detail() -> None:
    attachments = _runtime_attachment_payloads(
        [
            {
                "file_id": "12",
                "display_name": "截图.png",
                "relative_path": "workspace/uploads/截图.png",
                "mime_type": "image/png",
            }
        ]
    )
    detail = _source_detail_with_runtime_attachments(None, attachments)

    assert attachments == [
        {
            "file_id": "12",
            "display_name": "截图.png",
            "relative_path": "workspace/uploads/截图.png",
            "mime_type": "image/png",
        }
    ]
    assert detail == {
        "runtime_attachments": attachments,
        "attachment_file_ids": ["12"],
    }


def test_public_history_source_hides_runtime_paths() -> None:
    public = _public_history_source(
        {
            "attachment_file_ids": ["12"],
            "runtime_attachments": [
                {
                    "file_id": "12",
                    "display_name": "截图.png",
                    "relative_path": "workspace/uploads/截图.png",
                    "mime_type": "image/png",
                }
            ],
        }
    )

    assert public == {
        "attachment_file_ids": ["12"],
        "attachments": [
            {
                "attachmentId": 12,
                "displayName": "截图.png",
                "mimeType": "image/png",
            }
        ],
    }
