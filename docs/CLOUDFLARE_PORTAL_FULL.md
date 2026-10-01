# Full MCP behind Cloudflare MCP Portal

## Endpoint

Origin: `https://mcp-origin.conexaoazul.com/transcribe-full/mcp`

Portal: `https://mcp.conexaoazul.com/mcp`

Cloudflare AI Controls server id: `blue-transcription`.

## Security boundary

The origin path is protected by Cloudflare Access. Anonymous requests must be redirected to Access (HTTP 302). A dedicated Cloudflare service token is attached through a non-identity policy so the MCP Portal can reach the origin.

The service-token secret is not stored in this repository.

The origin backend listens only on the WireGuard interface of azul2 and is reached through Traefik. The backend itself is an authless FastMCP facade because authentication is terminated at Cloudflare Access.

## Exposed tools

The full facade exposes the eight tools from the candidate server:
- transcribe_file
- transcribe_base64
- transcribe_batch
- transcribe_zip
- transcribe_zip_base64
- transcribe_url
- transcribe_youtube
- transcribe_podcast

For web clients, inline/base64 and URL tools are the practical paths. Local path tools require the caller-visible path to exist under an allowed input root; the portal does not automatically stage browser uploads into the server filesystem.

## Runtime controls

- host bind: WireGuard only, currently `10.77.0.1:18086`;
- container read-only;
- ephemeral tmpfs output;
- Traefik request cap: 40 MiB;
- Traefik rate limit: 120/min, burst 40;
- max in-flight at edge: 8;
- Whisper remains private on Docker network.

## Cloudflare objects

Create and validate four objects:
1. origin Access app for `mcp-origin.conexaoazul.com/transcribe-full`;
2. dedicated service token + non-identity policy;
3. AI Controls MCP server `blue-transcription`;
4. Access app type `mcp` for the server.

Attach `blue-transcription` to portal `conexao-azul-odoo` with `default_disabled=false`, `on_behalf=false`.

Never commit the service-token client secret.

## Acceptance

- anonymous origin => 302 Access;
- origin health via service-token path succeeds;
- Cloudflare server status => ready;
- authentication_status => connected;
- sync returns all 8 tools;
- portal entry present and enabled by default;
- existing `/transcribe/mcp` and `/transcribe-public/mcp` remain unchanged.
