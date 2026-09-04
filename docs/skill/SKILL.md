---
name: moneyprinterturbo-video
description: Create a finished video with MoneyPrinterTurbo from a topic, title, idea, prompt or script. Supports the original native Skill workflow plus an already-running API and Docker/provider deployments. Use this skill for finished video delivery, installation/configuration, provider health diagnostics, local-model routing, Docker runtime setup, failed generation repair, or locating/downloading the generated MP4.
compatibility: Requires an AI agent with terminal, network and filesystem support. Native mode preserves the upstream helper. API/Docker modes support Linux, macOS and Windows where Docker/API connectivity is available.
metadata:
  author: "MoneyPrinterTurbo contributors + overcyber extension"
  version: "2.0.0"
  upstream: "https://github.com/harry0703/MoneyPrinterTurbo"
  fork: "https://github.com/overcyber/MoneyPrinterTurbo"
---

# MoneyPrinterTurbo Video Generation — Skill v2

Produce the final video, not only instructions. Preserve the original workflow and add API/Docker/provider routing.

## Execution modes

### `native` — original Skill

Use this when the user asks for the upstream/original behavior or no API exists.

```bash
python mpt_skill.py --transport native --subject "<video topic>"
```

The original helper remains available and must not be removed:

```bash
uv run --no-project --python 3.11 python mpt_agent.py --subject "<video topic>"
```

`mpt_skill.py --transport native` delegates to that helper. This is the compatibility path.

### `api` — already-running MoneyPrinterTurbo API

```bash
python mpt_skill.py \
  --transport api \
  --api-base-url http://127.0.0.1:8080 \
  --subject "<video topic>" \
  --aspect 16:9 \
  --language pt-BR
```

Protected API:

```bash
export MPT_API_KEY='<value>'
```

The helper submits `POST /api/v1/videos`, follows `GET /api/v1/tasks/{task_id}` and downloads the final MP4.

### `docker` — API + runtime providers

```bash
cp providers.docker.example.json providers.docker.json

python docs/skill/mpt_skill.py \
  --transport docker \
  --subject "<video topic>"
```

The Docker API reads:

```text
MPT_PROVIDER_CONFIG=/MoneyPrinterTurbo/providers.docker.json
```

and can reach host services through `host.docker.internal`.

## Runtime provider endpoints

```text
GET  /api/v1/providers
GET  /api/v1/providers/health
GET  /api/v1/providers/{provider_id}/health
POST /api/v1/providers/{provider_id}/invoke/{action}
```

Only server-declared actions can be invoked. Never turn this into an arbitrary URL proxy.

### OpenAI-compatible image bridge

Configured image providers expose:

```text
POST /api/v1/providers/{provider_id}/images/generations
```

This lets MoneyPrinterTurbo keep its existing `openai_image` material pipeline while actual inference is performed by a native local/Docker provider.

For the Dual-GPU local image provider declared in `providers.docker.example.json`:

```text
provider id: dual-gpu-image
GET  http://host.docker.internal:8000/health
POST http://host.docker.internal:8000/v1/images
GET  http://host.docker.internal:8000/files/{relative_path}
```

The bridge maps an OpenAI Images request such as:

```json
{
  "model": "provider-default",
  "prompt": "technical visualization of a multi-agent architecture",
  "n": 1,
  "size": "1536x1024"
}
```

to the provider's native request:

```json
{
  "prompt": "technical visualization of a multi-agent architecture",
  "negative_prompt": "...",
  "save_dir": "moneyprinterturbo",
  "width": 1536,
  "height": 1024
}
```

and returns `b64_json` to the existing MoneyPrinterTurbo image source.

## Docker + local image generation

```bash
python docs/skill/mpt_skill.py \
  --transport docker \
  --subject "Sistemas Multi-Agentes de IA" \
  --image-provider dual-gpu-image \
  --aspect 16:9 \
  --language pt-BR \
  --output ./multi-agentes.mp4
```

In Docker mode the helper points the existing `openai_image` configuration at:

```text
http://127.0.0.1:8080/api/v1/providers/dual-gpu-image
```

The effective pipeline is:

```text
script
 -> search terms
 -> openai_image material stage
 -> provider bridge
 -> local image API
 -> PNG
 -> MoneyPrinterTurbo image-to-video clip
 -> composition
 -> TTS/subtitles/BGM
 -> final MP4
```

Do not duplicate the mature image-to-video logic in `app/services/material.py`.

## Provider configuration

Create a local file:

```bash
cp providers.docker.example.json providers.docker.json
```

`providers.docker.json` is ignored by Git. Store endpoints and environment-variable names there, not secret values.

Example:

```json
{
  "kind": "image",
  "protocol": "mpt_image_v1",
  "base_url": "http://host.docker.internal:8000",
  "base_url_env": "MPT_DUAL_GPU_IMAGE_URL",
  "auth": {
    "type": "bearer_env",
    "env": "API_TOKEN",
    "optional": true
  },
  "actions": {
    "health": {"method": "GET", "path": "/health"},
    "generate_image": {"method": "POST", "path": "/v1/images"}
  }
}
```

## Provider containers

Base application:

```bash
docker compose up -d api
```

Provider override:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.providers.yml \
  up -d api
```

Optional Ollama:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.providers.yml \
  --profile provider-ollama \
  up -d api ollama
```

Do not start heavy providers that the current job does not need.

## Existing models and sources remain supported

Do not replace the project's existing LLM registry. It already includes local, cloud, gateway and OpenAI-compatible providers, including Ollama, OneAPI and LiteLLM paths.

Preserve existing video/material source families:

- Pexels, Pixabay, Coverr;
- WaveSpeed;
- VolcEngine Seedance;
- OFox;
- Metaso MiniMax;
- LoomLoom;
- `openai_image`;
- local uploads.

Runtime providers are an additional transport/discovery layer, not a replacement for these implementations.

## Required agent behavior

1. Deliver the final MP4 when generation succeeds.
2. Preserve `native`; never force Docker.
3. Prefer `api` when a healthy API is already running.
4. Prefer `docker` when the user explicitly requests containers/providers or reproducibility.
5. Check provider health before submitting a provider-dependent job.
6. Never print API keys, Bearer tokens, complete `config.toml`, or secret environment values.
7. Never silently substitute cloud when the user requested a local provider.
8. Never repeatedly create a paid generation job when remote state is unknown.
9. Keep MoneyPrinterTurbo task state as the source of truth.
10. Use `docs/PROVIDERS-DOCKER-PT-BR.md` for the architecture and evolution plan.

## Failure handling

For `native`, keep the original `mpt_agent.py` exit semantics.

For `api`/`docker`:

- provider health failure: report provider ID and connectivity error;
- task state `-1`: report `failed_stage` and `error`;
- timeout: report the task ID and do not resubmit automatically;
- Docker startup failure: report Compose failure and do not switch to cloud;
- provider generation uncertainty: do not create another paid task blindly.

## Architecture

```text
                  MoneyPrinterTurbo
                        |
       +----------------+----------------+
       |                |                |
       v                v                v
     LLMs             TTS/BGM         Materials
       |                                 |
 existing registry               existing sources
                                         |
                                  provider bridge
                                         |
                  +----------------------+----------------+
                  |                      |                |
                  v                      v                v
             local Docker          host service       cloud API
```

MoneyPrinterTurbo remains the video orchestrator; providers become replaceable execution backends.
