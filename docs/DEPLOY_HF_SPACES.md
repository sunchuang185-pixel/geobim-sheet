# Deploy Live Public Demo on Hugging Face Spaces (Free Docker Space)

Hugging Face Spaces provides a free **2 vCPU / 16 GB RAM** Docker runtime (no credit card required), which is ideal for hosting the GeoBIM Sheet & Mining Revision Review Agent public demo.

## Option 1: Create a Docker Space & Push via Git (2 minutes)

1. Go to https://huggingface.co/new-space and create a Space:
   - **Space name**: `geobim-sheet`
   - **License**: `mit`
   - **Select the Space SDK**: **Docker** → **Blank**
   - **Hardware**: `CPU basic · 2 vCPU · 16 GB · FREE`
   - **Visibility**: `Public`

2. Add the Hugging Face Space YAML header to the Space's `README.md` (or let our deploy helper push it):

```yaml
---
title: GeoBIM Sheet - Mining & Industrial Revision Review Agent
emoji: 🗺️
colorFrom: green
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: GIS/BIM design revision review agent (IFC + GeoJSON/DXF + MCP)
---
```

3. Push your repo to the Space:
```bash
git remote add hf https://huggingface.co/spaces/<YOUR_HF_USERNAME>/geobim-sheet
git push hf main
```

Once built (~2–3 minutes), your live URLs are:
- Drawing Workspace: `https://<YOUR_HF_USERNAME>-geobim-sheet.hf.space/`
- Revision Review Agent: `https://<YOUR_HF_USERNAME>-geobim-sheet.hf.space/agent`

## Option 2: Enable a Real Cloud LLM on Your Space (Optional)

In your Space **Settings → Variables and secrets**:
- **Variables**:
  - `LLM_PROVIDER` = `openai`
  - `LLM_BASE_URL` = `https://generativelanguage.googleapis.com/v1beta/openai` (or OpenAI / DeepSeek endpoint)
  - `LLM_MODEL` = `gemini-2.5-flash` (or `gpt-4o-mini`)
  - `GEOBIM_ALLOW_UPLOAD` = `false` (recommended for public demos)
- **Secrets** (never commit keys to Git):
  - `LLM_API_KEY` = `<your-api-key>`

## Why this `Dockerfile` works cleanly on HF Spaces
- Runs as non-root **UID `1000`** (`user`), matching Hugging Face Spaces security requirements.
- Binds **`0.0.0.0:7860`** via **`gunicorn --workers 1 --threads 4`** (single worker preserves in-memory project & task continuity across pause/resume while serving concurrent requests cleanly).
- Writes temporary tasks/exports to `/tmp/geobim_review` (writable under UID 1000).
- Defaults to `GEOBIM_ALLOW_UPLOAD=false` in the container image so public visitors use the 7 bundled synthetic scenarios safely; `docker-compose.yml` overrides `GEOBIM_ALLOW_UPLOAD=true` for self-hosted deployments.
