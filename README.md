# Blue Transcription

**Voice-to-Workflow Infrastructure — transforme áudio, vídeo e ZIP em contexto para ChatGPT, agentes, n8n, CRM e automações.**

**MCP-ready · Cloud gerenciada · Self-host · On-Prem**

Produto/serviço Conexão Azul: https://www.conexaoazul.com/blue-transcription-mcp

> O nome do repositório continua `blue-transcription-mcp` por compatibilidade técnica. Comercialmente, o produto é **Blue Transcription** e “MCP-ready” é um selo de integração, não a promessa principal.

Este repositório é um fork operacional de `MarcusTseng/mcp-whisper` mantido pela Conexão Azul. Além do fluxo original de transcrição, a variante Conexão Azul adiciona ingestão segura de ZIP, batch, inline Base64, manifestos com hash e perfil de deploy para infraestrutura privada.

O upstream permanece creditado e licenciado sob MIT. As extensões e modificações feitas neste fork pela Conexão Azul estão documentadas neste repositório e em [NOTICE.md](NOTICE.md).

## Community, Cloud e On-Prem

O core deste repositório é a edição **Community/self-host**: você pode operar o software na sua própria infraestrutura e o volume fica limitado pelo hardware que você provisionar.

A Conexão Azul oferece também operação gerenciada:

- **Trial Cloud** — teste assistido de 7 dias ou até 120 minutos, com até 200 processamentos, máximo de 20 chamadas/h e concorrência 1.
- **Cloud** — endpoint MCP gerenciado, healthcheck, atualização e monitoramento básico.
- **Pro** — limites maiores, mais concorrência, integração assistida com n8n/webhook e suporte prioritário.
- **Private / On-Prem** — runtime isolado ou instalado na infraestrutura do cliente, com política de rede, hardening, capacidade e suporte acordados.

Planos e ativação: https://www.conexaoazul.com/blue-transcription-mcp#planos

A edição Community não tem SLA ou suporte gerenciado incluído. Os planos pagos remuneram operação, integração, capacidade, isolamento, suporte e governança — não o acesso ao código MIT.

## O que a variante Conexão Azul adiciona

- `transcribe_base64` para mídia recebida por clientes MCP sem filesystem compartilhado.
- `transcribe_batch` com concorrência limitada.
- `transcribe_zip` e `transcribe_zip_base64` com allowlist de mídia, rejeição de symlink e limites de expansão.
- SHA-256 do arquivo e dos membros extraídos no manifest.
- Streamable HTTP / FastMCP com bearer auth por arquivo de segredo.
- Deploy Docker/Swarm com backend whisper.cpp privado.
- Uso com ChatGPT por endpoint MCP governado; autenticação externa pode ser protegida por mTLS/OAuth conforme o ambiente.

> A Conexão Azul comercializa a implantação, operação e integração do serviço. whisper.cpp e os componentes upstream mantêm suas próprias licenças e não são apresentados como tecnologia proprietária da Conexão Azul.

---

# mcp-whisper

A local-first MCP server that exposes a whisper.cpp HTTP backend as eight
transcription tools, including safe local batch and ZIP ingestion, with bearer
auth, SSRF guards, and a Docker MCP Toolkit catalog entry.

```
                 ┌────────────────────────────┐
 MCP clients ──► │ mcp-whisper-http :8083/mcp │ ── POST audio ──┐
 (LAN/Tailscale) │ (bearer-auth Streamable    │                 ▼
                 │  HTTP, container, non-root)│       ┌──────────────────────┐
                 └────────────────────────────┘       │ whisper.cpp :8082    │
                                                      │ (Vulkan/AMD, native, │
 Docker MCP gateway ────► same image, stdio mode ──►  │  bound to 127.0.0.1) │
 (Claude / Codex / etc.)                              └──────────────────────┘
```

## Tools

| Tool | Source | Notes |
|---|---|---|
| `transcribe_file(path, format, language?, request_id?)` | Local audio/video file | Path must resolve under `ALLOWED_INPUT_ROOTS` |
| `transcribe_base64(filename, data_base64, format, language?, request_id?)` | Inline attachment bytes | Capped by `MAX_INLINE_BYTES` |
| `transcribe_batch(paths, format, language?, concurrency?, request_id?)` | Multiple local files | Bounded count/size/concurrency |
| `transcribe_zip(path, format, language?, concurrency?, request_id?)` | Local ZIP containing media | Safe basename extraction; symlink/zip-bomb guards |
| `transcribe_zip_base64(filename, data_base64, format, language?, concurrency?, request_id?)` | Inline ZIP attachment bytes | Same ZIP safety gates; pre-decode size cap; archive/member SHA-256 manifest |
| `transcribe_url(url, format, language?, request_id?)` | Direct http(s) URL | Public hosts only |
| `transcribe_youtube(url, format, language?, request_id?)` | YouTube (yt-dlp) | URL validated *before* yt-dlp runs |
| `transcribe_podcast(rss_url, episode_index, format, language?, request_id?)` | RSS feed episode | Audio enclosure preferred; video fallback |

**Formats:** `text` · `json` · `srt` · `vtt` · `md`. The `md`/`srt`/`vtt` formats
write a file to `OUTPUT_DIR` and return its path; `text`/`json` return inline.

## Quick start

### Prerequisites

1. **whisper.cpp** built with a backend that suits your hardware, running as an
   OpenAI-compatible HTTP server on `127.0.0.1:8082`. Vulkan, CUDA, Metal, or
   CPU-only all work. Example invocation:
   ```bash
   whisper-server --model models/ggml-large-v3-turbo.bin \
       --host 127.0.0.1 --port 8082 \
       --inference-path /v1/audio/transcriptions --convert
   ```
2. **Docker** (Engine or Desktop).
3. **ffmpeg** in `PATH` (only needed if you also run whisper-server's `--convert`).

### Run

```bash
git clone https://github.com/conexaoazul/blue-transcription-mcp
cd blue-transcription-mcp

# Generate auth token
echo "MCP_AUTH_TOKEN=$(openssl rand -hex 32)" > .env
chmod 0600 .env

# Build + run
docker compose up -d --build
```

Verify:
```bash
TOKEN=$(grep MCP_AUTH_TOKEN .env | cut -d= -f2)
curl -X POST http://localhost:8083/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'
```

## Configuration

All knobs are environment variables (see `.env.example`):

| Var | Default | Purpose |
|---|---|---|
| `MCP_AUTH_TOKEN` | — (required for HTTP) | Bearer token clients send |
| `TRANSPORT` | `stdio` | `stdio` for local MCP clients, `http` for daemon mode |
| `HOST` / `PORT` | `0.0.0.0` / `8083` | HTTP bind |
| `WHISPER_URL` | `http://host.docker.internal:8082/v1/audio/transcriptions` | Where to POST audio |
| `ALLOWED_INPUT_ROOTS` | `/home/marcus/Downloads:/home/marcus/Music:/home/marcus/whisper.cpp/samples` | Colon-list of roots `transcribe_file` may read |
| `OUTPUT_DIR` | `/home/marcus/Documents/Obsidian Vault/Transcripts` | Where md/srt/vtt files are written |
| `MAX_DOWNLOAD_BYTES` | `524288000` (500MB) | Cap for httpx + yt-dlp downloads |
| `MAX_INLINE_BYTES` | `26214400` (25MB) | Decoded cap for inline base64 media |
| `MAX_INLINE_ZIP_BYTES` | same as `MAX_INLINE_BYTES` | Decoded cap for inline base64 ZIP archives |
| `MAX_BATCH_FILES` | `100` | Maximum media items accepted by batch/ZIP |
| `BATCH_CONCURRENCY` | `1` | Operator ceiling for simultaneous batch inference |
| `MAX_BATCH_ITEM_BYTES` | `262144000` (250MB) | Per-item limit for batch/ZIP |
| `MAX_ZIP_EXTRACT_BYTES` | `1073741824` (1GiB) | Aggregate uncompressed ZIP cap |
| `MAX_ZIP_ENTRIES` | `5000` | Maximum central-directory entries accepted in a ZIP |
| `METERING_ENABLED` | `0` | Enable tenant API keys, quotas and usage accounting |
| `METERING_DB` | `/data/metering/metering.sqlite3` | SQLite usage ledger path |
| `METERING_REQUIRE_REQUEST_ID` | `1` | Require stable request IDs for idempotent tenant charging |
| `METERING_LEASE_SECONDS` | `1800` | Timeout for in-flight usage reservations |

Managed tenants can also carry per-tenant entitlements such as quota, concurrency,
hourly/total call caps and a **synchronous media-duration ceiling**
(`max_sync_seconds`). A value of `0` keeps that ceiling disabled for backward
compatibility. Managed plans should set it from the commercial/control-plane
policy rather than hard-coding it in the MCP process.

The synchronous ceiling is intentionally separate from the overall quota:
long media may fit within a monthly/trial allowance but still be inappropriate
for one interactive MCP request. Media above the synchronous ceiling should be
handled by an asynchronous job flow once that surface is enabled.

## Wiring into MCP clients

### Claude Code / Codex / Cursor (remote HTTP)

```json
{
  "mcpServers": {
    "whisper": {
      "url": "http://your-host:8083/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

### Docker MCP Toolkit (stdio gateway)

The repo includes `catalog-entry.yaml`. On a Docker Desktop machine:

```bash
docker mcp catalog create local-mcp:latest \
    --title "Local Custom MCP" \
    --server file://$PWD/catalog-entry.yaml
docker mcp profile server add default \
    --server catalog://local-mcp:latest/whisper-transcribe
```

Then `whisper-transcribe` appears in Docker Desktop's MCP Toolkit panel and any
client wired to the Docker MCP gateway (e.g. via `MCP_DOCKER` server) sees all
eight tools.

## Security model

Hardened in line with a Codex + Gemini cross-review. See [SECURITY.md](SECURITY.md) for the full threat model.

- whisper.cpp **bound to `127.0.0.1`** — LAN cannot bypass the MCP bearer auth
- Container runs as **UID 1000** with `read_only: true` rootfs and `tmpfs: /tmp`
- **Narrow mounts**: only the input roots (RO) and the output dir (RW) — no `~/.ssh`, `.gnupg`, etc.
- `transcribe_file` rejects paths outside `ALLOWED_INPUT_ROOTS` (catches `../`, symlink escape)
- `_validate_remote_url` rejects non-http(s) schemes, private/loopback/link-local IPs, and `host.docker.internal`; applied to all three remote tools *before* yt-dlp/httpx see the URL
- **Fail-closed auth**: HTTP transport refuses to start if `MCP_AUTH_TOKEN` is unset; `hmac.compare_digest` for the check
- **DoS cap**: 500MB ceiling on httpx streams; yt-dlp invoked with `--max-filesize 500M --no-config --no-call-home --no-cache-dir`
- Inline base64 media/ZIP payloads are size-checked **before decode**; inline ZIP then reuses the same entry-count, symlink, extension, per-member and total-extraction gates as local ZIP ingestion
- RSS feed fetched via httpx (validated, capped) — feedparser never does its own networking

## Weekly auto-updater

`update.sh` + `mcp-whisper-update.{service,timer}` (systemd user units, fire
Sunday 04:00 with 15min jitter, `Persistent=true`). It:

1. `git pull` whisper.cpp → rebuild if HEAD moved → restart whisper-server
2. Rebuild `mcp-whisper:latest` with `--no-cache --pull`
3. Compare pinned package versions inside the image before/after
4. On a real version change: `compose up -d --force-recreate` → health-check → re-register Docker MCP catalog → remove old image
5. Discord webhook only on real changes or errors (silent on no-op)

Install:
```bash
cp systemd/mcp-whisper-update.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mcp-whisper-update.timer
```

## Project layout

```
mcp-whisper/
├── server.py              FastMCP server, 8 tools, dual stdio/http transport
├── Dockerfile             python:3.12-slim + ffmpeg + 5 pip deps, non-root user
├── compose.yml            Long-running HTTP daemon, narrow RO mounts
├── catalog-entry.yaml     Docker MCP Toolkit catalog server spec
├── update.sh              Weekly updater
├── systemd/               Systemd user units (timer, service)
├── .env.example           Copy to .env and fill in MCP_AUTH_TOKEN
└── README.md
```

## License

MIT — see [LICENSE](LICENSE).
