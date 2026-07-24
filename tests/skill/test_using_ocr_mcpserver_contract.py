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
    description = metadata["description"]
    assert description.startswith("Use when")
    for keyword in ("local", "OCR", "upload", "progress", "download"):
        assert keyword.lower() in description.lower()


def test_skill_preserves_three_tool_boundary() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert "parse_documents" in text
    assert "get_task_status" in text
    assert "reparse_with_page_orientation" in text
    assert "upload_document" not in text
