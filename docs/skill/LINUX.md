# MoneyPrinterTurbo Skill v2 — Linux

Esta extensão usa um caminho Docker específico para Linux nativo.

## Por que o Compose normal falha no Linux

No Docker Desktop (Windows/macOS), `host.docker.internal` possui uma ponte própria para o host. No Docker Engine nativo do Linux, `host-gateway` resolve para o gateway da bridge Docker. Um serviço que escuta apenas em `127.0.0.1:8000` no host não está acessível por esse endereço de bridge.

Para não obrigar providers locais a escutarem em `0.0.0.0`, o Linux usa:

```text
docker-compose.linux.yml
```

com:

```yaml
network_mode: host
```

Assim, do container da API:

```text
127.0.0.1:8000
```

é o mesmo loopback do host Linux.

## Atualizar a branch

```bash
git fetch origin
git checkout feature/docker-provider-skill-v2
git pull --ff-only origin feature/docker-provider-skill-v2
```

## Pré-requisitos

```bash
docker --version
docker compose version
```

O usuário precisa conseguir acessar o daemon Docker sem prompt interativo. Valide:

```bash
docker info >/dev/null
```

Se houver `permission denied` no socket Docker, corrija a instalação/permissão do Docker antes de executar a Skill.

## Provider local de imagem

Se a API local está em:

```text
http://127.0.0.1:8000
```

valide primeiro no host:

```bash
curl -fsS http://127.0.0.1:8000/health
```

O provider de exemplo continua declarando `host.docker.internal:8000`. No Compose Linux esse nome é resolvido explicitamente para `127.0.0.1`, porque a API MoneyPrinterTurbo está em host network.

Se quiser sobrescrever o endpoint:

```bash
export MPT_DUAL_GPU_IMAGE_URL='http://127.0.0.1:8000'
```

Se o provider exigir Bearer token:

```bash
export API_TOKEN='SEU_TOKEN'
```

Essas variáveis agora são propagadas para o container da API.

## Configuração inicial

```bash
cp -n providers.docker.example.json providers.docker.json
cp -n config.example.toml config.toml
```

## Subir somente a API no Linux

```bash
docker compose \
  -f docker-compose.linux.yml \
  -f docker-compose.providers.yml \
  up -d --build api
```

Verifique:

```bash
curl -fsS http://127.0.0.1:8080/ping
```

Esperado:

```text
"pong"
```

## Verificar providers

```bash
curl -fsS http://127.0.0.1:8080/api/v1/providers | python3 -m json.tool
```

Provider de imagem:

```bash
curl -fsS \
  http://127.0.0.1:8080/api/v1/providers/dual-gpu-image/health \
  | python3 -m json.tool
```

## Executar a Skill

No Linux, o transporte padrão agora é `auto`.

Se a API já estiver em `127.0.0.1:8080`, a Skill usa `api`. Caso contrário, seleciona `docker` e sobe o Compose Linux.

```bash
python3 docs/skill/mpt_skill.py \
  --subject 'Sistemas multi-agentes de IA' \
  --image-provider dual-gpu-image \
  --aspect 16:9 \
  --language pt-BR \
  --output ./video.mp4
```

Para forçar Docker:

```bash
python3 docs/skill/mpt_skill.py \
  --transport docker \
  --subject 'Sistemas multi-agentes de IA' \
  --image-provider dual-gpu-image \
  --output ./video.mp4
```

## Logs quando a API não sobe

```bash
docker compose \
  -f docker-compose.linux.yml \
  -f docker-compose.providers.yml \
  ps

docker logs --tail 200 moneyprinterturbo-api
```

## Diagnóstico de rede

Do host:

```bash
curl -fsS http://127.0.0.1:8000/health
```

Do container MoneyPrinterTurbo no modo Linux:

```bash
docker exec moneyprinterturbo-api \
  python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).read().decode())"
```

O segundo comando precisa atingir o mesmo serviço local. Se o primeiro funciona e o segundo não, confirme que a API MoneyPrinterTurbo realmente foi criada por `docker-compose.linux.yml` e está com `network_mode: host`.

## Ollama

O Ollama opcional continua podendo ser iniciado pelo profile:

```bash
docker compose \
  -f docker-compose.linux.yml \
  -f docker-compose.providers.yml \
  --profile provider-ollama \
  up -d api ollama
```

Como o container Ollama publica `11434` no host, a API MoneyPrinterTurbo em host network consegue alcançá-lo por `127.0.0.1:11434`.
