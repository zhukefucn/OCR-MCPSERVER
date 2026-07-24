---
name: "using-ocr-mcpserver"
description: "Use when local PDF or image files must be uploaded to OCR MCP Server for OCR parsing, progress tracking, or result download."
---

# Using OCR MCP Server

Use the existing OCR MCP tool boundary:

- `parse_documents`
- `get_task_status`
- `reparse_with_page_orientation`

Use `scripts/ocr-transfer.ps1` for local upload transport. Do not invent an MCP upload tool.
