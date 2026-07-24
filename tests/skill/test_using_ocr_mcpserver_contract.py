from pathlib import Path
import re
import yaml


ROOT = Path(__file__).parents[2]
SKILL = ROOT / "skills" / "using-ocr-mcpserver"


def test_skill_has_required_structure() -> None:
    assert (SKILL / "SKILL.md").is_file()
    assert (SKILL / "agents" / "openai.yaml").is_file()
    assert (SKILL / "scripts" / "ocr-transfer.ps1").is_file()


def test_skill_metadata_triggers_local_ocr_workflow() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    assert match is not None
    metadata = yaml.safe_load(match.group(1))
    assert metadata["name"] == "using-ocr-mcpserver"
    assert metadata["description"] == (
        "Use when local PDF or image paths must be processed through OCR MCP "
        "Server, especially when the remote MCP rejects local paths, files need "
        "REST upload first, OCR task progress must be monitored, artifacts must "
        "be downloaded, or an existing batch_id must be resumed."
    )


def test_skill_preserves_three_tool_boundary() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert "parse_documents" in text
    assert "get_task_status" in text
    assert "reparse_with_page_orientation" in text
    assert "upload_document" not in text
    assert "base64" not in text.lower()
    assert "engine" not in text.lower()


def test_skill_encodes_complete_workflow_and_security_rules() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    required = [
        "本地路径",
        "HTTPS URL",
        "file_id",
        "batch_id",
        "ocr-transfer.ps1",
        "OCR_MCP_API_KEY",
        "parse_documents",
        "get_task_status",
        "15",
        "artifact_id",
        "completed_with_errors",
        "ocr-results",
        "final.md",
    ]
    for token in required:
        assert token in text

    ordered_steps = [
        "输入分类",
        "-Action upload",
        "parse_documents",
        "保存 `batch_id`",
        "get_task_status",
        "-Action download",
    ]
    positions = [text.index(step) for step in ordered_steps]
    assert positions == sorted(positions)

    assert "不要输出 API Key" in text
    assert "不要读取或输出 OCR 正文" in text
    assert "只汇报变化" in text
    assert "所有 `artifact_id`" in text
    assert "保留已成功" in text
    assert "ocr-results/<batch_id>/" in text


def test_skill_encodes_resume_and_orientation_gate() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert "已有 `batch_id` 时，跳过上传和提交" in text
    assert "已有终止状态时，直接进入下载阶段" in text
    assert "只有用户明确指定方向恢复时才调用 `reparse_with_page_orientation`" in text


def test_skill_encodes_safe_status_contract() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    for line in (
        "已提交：batch_id、文件数",
        "处理中：批次百分比、文件序号、稳定阶段",
        "已完成：成功数、失败数、ZIP 路径、解压目录",
        "可恢复：batch_id、最后状态、下一步",
    ):
        assert line in text


def test_openai_metadata_matches_skill() -> None:
    metadata = yaml.safe_load(
        (SKILL / "agents" / "openai.yaml").read_text(encoding="utf-8")
    )
    assert set(metadata) == {"interface"}
    interface = metadata["interface"]
    assert set(interface) == {
        "display_name",
        "short_description",
        "default_prompt",
    }
    assert interface["display_name"] == "OCR MCP 文档处理"
    assert interface["short_description"] == (
        "上传本地文档并通过 OCR MCP 解析、跟踪和下载结果"
    )
    assert "$using-ocr-mcpserver" in interface["default_prompt"]
    assert "进度" in interface["default_prompt"]
