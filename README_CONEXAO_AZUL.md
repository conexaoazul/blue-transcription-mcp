# Blue Transcription MCP — Conexão Azul

Página comercial: https://www.conexaoazul.com/blue-transcription-mcp

## Posicionamento

Serviço de ingestão, normalização e transcrição para automações e agentes. O foco não é apenas “gerar texto”, mas encaixar áudio, vídeo e ZIPs em fluxos operacionais com limites, rastreabilidade e integração via MCP/HTTP.

## Capacidades

- `transcribe_file`
- `transcribe_base64`
- `transcribe_batch`
- `transcribe_zip`
- `transcribe_zip_base64`
- `transcribe_url`
- `transcribe_youtube`
- `transcribe_podcast`

## Arquitetura recomendada

- backend `whisper.cpp` privado;
- FastMCP/Streamable HTTP na borda de integração;
- token interno lido de arquivo/secret, nunca embutido em prompt ou URL;
- proxy/ingress com TLS e autenticação adequada ao cliente;
- ffmpeg para normalização de formatos;
- armazenamento temporário limitado e manifest por lote.

O ambiente pode ser Docker Compose ou Swarm. A posição do runtime (OCI, on-premises ou outro worker) é decisão de implantação e não deve ser assumida pelo código.

## Segurança

- containers non-root quando suportado e rootfs read-only para o MCP;
- URLs remotas com proteção SSRF;
- roots locais permitidos explicitamente;
- ZIP com allowlist de extensões, rejeição de symlink, limite de entradas, limite por item e limite total de extração;
- concorrência limitada para proteger CPU/RAM;
- hashes SHA-256 para rastrear archive e mídia extraída;
- backend ASR sem porta pública por padrão;
- autenticação do endpoint separada do motor de transcrição.

## Deploy

Use os manifests de `deploy/` como referência e ajuste placement, recursos, modelo e ingress ao ambiente real. Evite acoplar a documentação a um hostname de worker específico.

## Smoke

Valide pelo menos:

1. `/healthz`;
2. MCP `initialize`;
3. `tools/list`;
4. ZIP inválido/base64 inválido;
5. symlink em ZIP rejeitado;
6. ZIP misto selecionando apenas mídia suportada;
7. uma inferência real;
8. manifest com hashes;
9. backend Whisper inacessível externamente.

## Licenças e upstream

Este repositório deriva de `MarcusTseng/mcp-whisper` e usa `whisper.cpp`. A Conexão Azul oferece implantação, integração, hardening e operação; não reivindica propriedade sobre os projetos upstream.
