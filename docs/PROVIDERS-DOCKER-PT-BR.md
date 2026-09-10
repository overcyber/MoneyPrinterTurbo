# MoneyPrinterTurbo — Providers Docker e Plano de Evolução

## Objetivo

Esta reconstrução preserva o MoneyPrinterTurbo como orquestrador e adiciona uma camada de **runtime providers** para serviços locais, Docker e APIs HTTP, sem remover:

- o fluxo original da Skill (`docs/skill/mpt_agent.py`);
- a API FastAPI atual;
- a registry existente de LLMs;
- os providers atuais de materiais;
- TTS, legendas, BGM e composição;
- os estados atuais de task e a lógica de recuperação de falhas.

A migração dos subsistemas para uma interface homogênea deve ser incremental.

---

## Estado atual

### Pontos fortes

1. **Registry de LLMs centralizada** em `app/models/llm_provider.py`.
2. **Task state estruturado** em `app/services/task.py`, incluindo falha por estágio e progresso.
3. **Materiais maduros** em `app/services/material.py`, com stock, vídeo gerado, `openai_image`, cache e provenance.
4. **API e WebUI separadas**, já executáveis em Docker.
5. **Suíte de testes ampla**.

### Principal problema arquitetural

A abstração de provider é diferente em cada subsistema:

```text
LLM       -> registry central
materiais -> branches por video_source
TTS       -> voice.py
BGM       -> serviços específicos
vídeo IA  -> serviço por fornecedor
Docker    -> empacota API/WebUI, mas não orquestra inference workers
```

Isso dificulta troca `local/cloud/container` por job, health unificado, GPU affinity, fallback controlado e observabilidade.

---

## Provider Gateway

A nova camada usa:

```text
providers.docker.json
        |
        v
app/services/provider_gateway.py
        |
        +--> discovery
        +--> health
        +--> invoke
        +--> protocol adapters
```

### Endpoints

```text
GET  /api/v1/providers
GET  /api/v1/providers/health
GET  /api/v1/providers/{provider_id}/health
POST /api/v1/providers/{provider_id}/invoke/{action}
```

As chamadas são limitadas a actions declaradas no arquivo do servidor. O cliente não fornece uma URL arbitrária, reduzindo o risco de SSRF/proxy aberto.

---

## Provider de imagem local

O primeiro protocolo especializado é:

```text
mpt_image_v1
```

Contrato:

```text
GET  /health
POST /v1/images
GET  /files/{relative_path}
```

Request esperado:

```json
{
  "prompt": "...",
  "negative_prompt": "...",
  "save_dir": "moneyprinterturbo",
  "width": 1536,
  "height": 1024,
  "seed": 42
}
```

Response esperada:

```json
{
  "relative_path": "...png",
  "url": "...",
  "model": "...",
  "gpu": {},
  "seed": 42,
  "elapsed_seconds": 12.4
}
```

### Ponte OpenAI Images

Foi adicionada:

```text
POST /api/v1/providers/{provider_id}/images/generations
```

Ela converte:

```text
OpenAI Images -> mpt_image_v1 -> API local
```

e retorna `b64_json`.

Com isso, `video_source=openai_image` continua usando o pipeline já existente de:

```text
geração on-demand
 -> PNG
 -> render PNG para clipe
 -> controle de duração
 -> composição
 -> TTS
 -> legendas
 -> BGM
 -> MP4 final
```

Não é necessário duplicar o renderer de imagem em vídeo.

---

## Docker

`docker-compose.yml` continua com `webui` e `api`, mas passa a declarar:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

Isso permite que containers Linux acessem providers rodando no host.

`docker-compose.providers.yml` usa profiles. O primeiro é:

```text
provider-ollama
```

Um provider pesado não sobe automaticamente.

Futuros profiles recomendados:

```text
provider-image
provider-tts
provider-asr
provider-video
provider-avatar
provider-lipsync
state-redis
```

---

## Skill v2

A Skill anterior proibia Docker e API. A v2 cria três transports:

### Native

```text
Skill -> mpt_agent.py -> CLI
```

Preserva o comportamento original.

### API

```text
Skill
 -> POST /api/v1/videos
 -> GET /api/v1/tasks/{id}
 -> download MP4
```

### Docker

```text
Skill
 -> Docker Compose
 -> MoneyPrinterTurbo API
 -> providers
 -> task
 -> MP4 final
```

O original não foi removido.

---

# Melhorias recomendadas

## P0 — Provider selection por task

Hoje muitas escolhas ficam em `config.toml`, portanto são globais. O próximo passo deveria permitir algo como:

```json
{
  "providers": {
    "llm": "ollama-local",
    "image": "dual-gpu-image",
    "tts": "piper-ptbr",
    "asr": "whisper-local",
    "video": "video-worker-a"
  }
}
```

Assim duas tasks concorrentes podem usar stacks diferentes.

## P0 — Capabilities, não nomes de produtos

Evoluir para:

```text
text.chat
image.generate
video.generate
image.to_video
tts.speech
asr.transcribe
avatar.animate
lipsync
music.generate
```

O scheduler escolhe um provider que ofereça a capability requerida.

## P0 — Storyboard / scene graph

O pipeline principal ainda é essencialmente:

```text
script -> terms -> materiais -> áudio -> composição
```

Para vídeos explicativos, o objeto central deveria ser uma lista de cenas:

```json
{
  "scene_id": "scene-03",
  "duration_target": 18,
  "narration": "...",
  "visual_intent": "...",
  "visual_type": "diagram",
  "provider": "dual-gpu-image",
  "prompt": "...",
  "transition": "fade"
}
```

Benefícios:

- imagem alinhada à fala;
- B-roll por cena;
- diagramas e slides;
- regeneração de apenas uma cena;
- melhor QA visual;
- storyboard editável no WebUI.

## P0 — Separar `task.py` em stages

Estrutura recomendada:

```text
app/pipeline/
  script_stage.py
  planning_stage.py
  audio_stage.py
  subtitle_stage.py
  material_stage.py
  compose_stage.py
  qa_stage.py
  publish_stage.py
```

Interface conceitual:

```python
class Stage:
    name: str
    def run(self, context) -> StageResult: ...
```

Isso habilita retry, cache, métricas e reexecução parcial por estágio.

## P1 — Scheduler de GPU/providers

Registrar por provider:

```text
gpu_id
free_vram
queue_depth
running_jobs
capabilities
model_loaded
estimated_latency
```

Não resolver tudo com `gpus: all`. Workers pesados devem ter afinidade explícita.

## P1 — Liveness e readiness separados

Usar:

```text
/liveness
/readiness
```

`liveness` responde se o processo está vivo. `readiness` só responde saudável quando o modelo realmente pode aceitar um job.

## P1 — Circuit breaker

Estados:

```text
closed
open
half-open
```

Evita retry storm contra provider em OOM, timeout ou indisponibilidade.

## P1 — Fallback explícito

Exemplo:

```json
{
  "image": {
    "primary": "dual-gpu-image",
    "fallback": ["openai-image-cloud"],
    "allow_fallback": false
  }
}
```

Nunca trocar provider local por cloud silenciosamente.

## P1 — Observabilidade

Métricas úteis:

```text
provider_requests_total
provider_failures_total
provider_latency_seconds
provider_queue_depth
provider_jobs_inflight
provider_timeout_total
stage_duration_seconds
task_duration_seconds
```

Adicionar `/metrics` e OpenTelemetry para tracing de task/stages/providers.

## P1 — Redis no Compose de produção

O projeto já possui suporte de estado Redis no código. O Compose deveria oferecer profile `state-redis` para persistência e múltiplos workers.

## P1 — Job queue real

`ThreadPoolExecutor` é suficiente para instalação simples, mas para escala:

```text
API -> queue -> render/media/publish workers
```

Pode usar Celery, RQ, Arq, Dramatiq ou uma fila Redis mínima.

## P1 — Idempotency key

Adicionar `Idempotency-Key` ao `POST /api/v1/videos` e aos providers pagos para evitar jobs duplicados após perda da resposta HTTP.

## P1 — Manifest imutável de artefatos

Cada task deveria terminar com:

```json
{
  "task_id": "...",
  "inputs": {},
  "providers": {},
  "models": {},
  "seeds": {},
  "artifacts": [],
  "timings": {},
  "warnings": [],
  "qa": {}
}
```

Isso melhora reprodução, auditoria e debug.

## P2 — TTS provider registry

Aplicar à voz o padrão já usado pela registry de LLMs, com capabilities como:

```text
speech
voice_clone
timestamps
ssml
streaming
languages
```

## P2 — ASR separado de subtitle rendering

Modelar `asr`, `subtitle_alignment` e `subtitle_render` como etapas diferentes.

## P2 — Templates declarativos

Exemplo:

```yaml
name: technical-youtube
aspect: 16:9
scene_defaults:
  transition: fade
  visual_density: medium
subtitle:
  mode: sentence
```

## P2 — Render incremental

Gerar cenas intermediárias:

```text
scene-001.mp4
scene-002.mp4
scene-003.mp4
```

Mudança em uma cena não deve obrigar a regenerar o vídeo inteiro.

---

# Prioridade

### Fase 1 — infraestrutura

- provider registry;
- health/invoke endpoints;
- bridge de imagem;
- Skill native/API/Docker;
- profiles Docker;
- documentação.

### Fase 2 — qualidade

- storyboard/scene graph;
- provider por task;
- render incremental;
- prompts visuais por cena.

### Fase 3 — escala

- scheduler;
- Redis;
- job queue;
- circuit breaker;
- métricas/tracing.

### Fase 4 — multimodal local

Adapters formais para:

```text
image.generate
video.generate
image_to_video
tts
asr
avatar
lipsync
music
```

---

# Arquitetura-alvo

```text
                         WebUI
                           |
                           v
                        FastAPI
                           |
                     Task / Scene DAG
                           |
            +--------------+--------------+
            |              |              |
            v              v              v
        LLM router     Media router    Audio router
            |              |              |
      Provider Registry / Scheduler / Health
            |
   +--------+---------+----------+-----------+
   |                  |          |           |
 Ollama            Image API     TTS       Video API
 container          container    worker      worker
```

O MoneyPrinterTurbo deve permanecer o **orquestrador de vídeo**; os modelos e serviços pesados devem ser backends substituíveis.
