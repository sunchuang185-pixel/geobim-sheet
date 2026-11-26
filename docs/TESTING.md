# Testing & Verification Guide

GeoBIM Sheet separates **Automated Tests** (deterministic, CI-safe, no external API keys required) from **Real LLM End-to-End Tests** (requiring a live Ollama instance or cloud LLM API key).

## 1. Automated Test Suite

Run all automated tests from the repository root:

```powershell
python -m unittest discover -s tests -v
```

What is covered automatically:
1. **`tests/test_workspace.py` (Baseline regression)**
   - Original sheet prompt clipping, Chinese follow-up commands, PDF/DXF export in projected CRS, rejecting IFC without `IfcMapConversion`.
2. **`tests/test_review_agent.py` (Revision Review Agent & MCP)**
   - **Normal revision (`normal`)**: verifies `P-101` (~7.66 m) and `B-022` (2.50 m) displacement, planar water-pipe proximity, `"待核实高程"` tagging on pipes missing bury depth, PDF structural validity, CSV traceability columns, and DXF validity.
   - **GlobalId changed (`id_changed`) + ambiguous fallback**: verifies unique `(Tag, IfcClass)` candidate matching with rationale when `GlobalId` changes, and verifies `ambiguous_matches` when duplicate/missing tags cannot be uniquely confirmed.
   - **Duplicate pump station disambiguation & resume (`duplicate_pumps`)**: tests Web Agent `/api/agent/tasks` pausing in `waiting_for_user` with candidates (`P-101`, `P-102`), verifying no files are downloadable yet, replying via `/api/agent/tasks/<id>/reply` to select `P-102`, and confirming the resumed task exports verified PDF/CSV for `P-102`.
   - **Missing coordinates (`missing_crs`)**: verifies `open_project` reports missing `IfcMapConversion` explicitly (`status="incomplete_coordinates"`) without guessing or generating bogus drawings.
   - **Real MCP stdio client-server integration**: launches `python -m geobim_sheet.mcp_server` over stdio using the official `mcp` client SDK, verifies all 6 tools are listed, and runs the complete workflow to real PDF/CSV/DXF files on disk.
   - **Web Agent custom 3-file upload + HTTP Tool-Calling LLM protocol**: runs `ReviewAgent` against a local mock OpenAI-compatible & Ollama HTTP server to verify multi-turn tool calls, pause/resume on disambiguation, and verified file download endpoints.

## 2. Real Model End-to-End Testing (Ollama or Cloud LLM)

To verify real LLM decision-making end-to-end:

### Option A: Local Ollama
```powershell
ollama pull qwen2.5:7b
$env:LLM_PROVIDER="ollama"
$env:LLM_BASE_URL="http://127.0.0.1:11434"
$env:LLM_MODEL="qwen2.5:7b"
python -m geobim_sheet
```
Open `http://127.0.0.1:8765/agent`, verify the top-right badge reads `LLM: ollama / qwen2.5:7b`, and run:
1. `normal` scenario with the default Chinese review instruction.
2. `duplicate_pumps` scenario — confirm the model stops to ask between `P-101` and `P-102`, click one candidate, and confirm it finishes and displays downloadable PDF/CSV.
3. `id_changed` and `missing_crs` scenarios.

### Option B: Cloud OpenAI-Compatible API (e.g. OpenAI / Gemini / DeepSeek)
```powershell
$env:LLM_PROVIDER="openai"
$env:LLM_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai"
$env:LLM_MODEL="gemini-2.5-flash"
$env:LLM_API_KEY="your-key"
python -m geobim_sheet
```

### Option C: Live Codex MCP Client Test
Register `docs/codex-mcp.toml` in `~/.codex/config.toml` and run the copyable prompt from `README.md`.
