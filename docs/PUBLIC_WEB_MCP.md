# Public Web MCP façade

## Goal

Provide one remote Streamable HTTP MCP endpoint that works with hosted web clients
without OAuth, while keeping the authenticated/internal MCP unchanged.

Public URL:

`https://mcp-origin.conexaoazul.com/transcribe-public/mcp`

## Exposed tools

Only:
- `transcribe_base64`
- `transcribe_zip_base64`

The façade does not expose local-file reads, remote URL fetches, YouTube, podcast,
Odoo, Chatwoot, Drive, or any other private source.

## Runtime boundary

The public façade talks only to the private Whisper backend. It is stateless from
the client perspective. Output uses ephemeral storage.

Recommended edge controls:
- HTTPS only through Traefik;
- request body cap around 40 MiB;
- rate limit;
- in-flight request limit;
- host/origin validation;
- backend host port bound only to WireGuard/private interface.

Current decoded public payload cap: 25 MiB per media file or ZIP.

## Hosted-client compatibility

The server is standard Streamable HTTP MCP and can operate without OAuth.
This is intentionally different from the authenticated internal endpoint
`/transcribe/mcp`.

## Validation

Required smoke:
1. GET `/healthz`;
2. MCP `initialize`;
3. `notifications/initialized`;
4. `tools/list` returns exactly the two public tools;
5. one real `transcribe_base64` call;
6. internal endpoint remains authenticated.

## Rollback

Remove the edge route and stop the public façade container/service. The internal
authenticated transcription MCP is independent and must remain unaffected.
