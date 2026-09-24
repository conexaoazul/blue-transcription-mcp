# Blue Transcription MCP — Conexão Azul

Fork operacional de `MarcusTseng/mcp-whisper`, adaptado para Docker Swarm.

## Arquitetura

- `transcription_whisper`: `ggml-org/whisper.cpp`, modelo multilingual `small`, CPU-only.
- `transcription_mcp`: FastMCP/Streamable HTTP com Bearer auth via Docker Secret.
- Ambos ficam no `blueops-oci-worker-1`; o leader `azul2` não executa inferência.
- Entrada pública somente via Traefik em `https://mcp-origin.conexaoazul.com/transcribe/mcp`.
- Backend Whisper não publica porta externa.

## Tools

- `transcribe_file`
- `transcribe_base64` (extensão Blue; até 25 MiB por padrão)
- `transcribe_url`
- `transcribe_youtube`
- `transcribe_podcast`

## Segurança

- MCP exige Bearer token e lê o segredo de `/run/secrets/transcription_mcp_auth`.
- Container MCP roda non-root, rootfs read-only e `/tmp` em tmpfs.
- URLs remotas mantêm as proteções SSRF do upstream.
- `transcribe_file` é restrito a `/data/inbox`.

## Deploy

```bash
# criar uma vez, sem imprimir o token
openssl rand -hex 32 | docker secret create transcription_mcp_auth -

# build/push da imagem MCP
TAG=$(git rev-parse --short HEAD)
docker build -t ghcr.io/conexaoazul/blue-transcription-mcp:$TAG .
gh auth token | docker login ghcr.io -u conexaoazul --password-stdin
docker push ghcr.io/conexaoazul/blue-transcription-mcp:$TAG

# deploy
IMAGE_TAG=$TAG docker stack deploy -c deploy/stack.yml transcription --with-registry-auth
```

## Smoke

```bash
curl -fsS https://mcp-origin.conexaoazul.com/transcribe/healthz
# tools/list exige Authorization: Bearer <token>
```

A imagem do `whisper.cpp` está fixada por digest. O modelo é baixado para volume local do worker na primeira inicialização.
