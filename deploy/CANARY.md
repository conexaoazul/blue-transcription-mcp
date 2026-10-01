# Canary runbook — Blue Transcription MCP

Use this only with an immutable GHCR digest produced by CI from the dedicated candidate package `ghcr.io/conexaoazul/blue-transcription-mcp-candidate`. The canary is isolated from
the production MCP service: no published port, no production service update, no change
to the Whisper service, and temporary output only.

## Preconditions

- CI for the candidate SHA is green.
- `CANDIDATE_IMAGE` is an immutable `ghcr.io/...@sha256:...` reference.
- Existing overlay network `transcription_transcribe` is healthy.
- Docker Secret `transcription_mcp_auth` already exists.
- Production `transcription_mcp` is healthy before starting.

## Deploy canary

```bash
export CANDIDATE_IMAGE='ghcr.io/conexaoazul/blue-transcription-mcp-candidate@sha256:<digest>'
docker stack config -c deploy/canary.yml >/tmp/transcription-canary.rendered.yml
docker stack deploy -c deploy/canary.yml transcription-canary
docker service ps transcription-canary_mcp-canary --no-trunc
```

Do **not** publish a port and do **not** update `transcription_mcp`.

## Readiness

```bash
docker service inspect transcription-canary_mcp-canary \
  --format '{{json .UpdateStatus}} {{json .ServiceStatus}}'
```

The task must be Running and its container health must be healthy.

## Smoke gates

1. `tools/list` exposes `transcribe_zip_base64`.
2. Invalid base64 is rejected.
3. Wrong archive extension is rejected.
4. ZIP symlink member is rejected.
5. A valid ZIP with supported + unsupported entries selects only supported media.
6. Manifest contains:
   - archive SHA-256,
   - archive byte count,
   - selected member names,
   - per-member bytes + SHA-256,
   - transcription result/status.
7. Whisper inference succeeds through `transcription_whisper:8082`.
8. Production MCP remains healthy throughout the test.

For the 30/09/2026 WhatsApp acceptance fixture, expected reconciliation is:

- archive SHA-256:
  `92af73f19d8647754700fc493dfb2cb37f4851b925634c8c79ed4af2098e88f0`
- ZIP entries: 8
- supported media: 5
- selected media bytes: 451468
- reference n8n execution: `893093`
- reference transcribed duration: 197 seconds
- expected filename set/order after `.opus -> .ogg` normalization: 5/5 match

Do not store the real WhatsApp archive in Git or in a public artifact.

## Cleanup

```bash
docker stack rm transcription-canary
```

Verify the canary service is gone and production remains unchanged.

## Promotion gate

A production image change is allowed only after:

- CI green;
- candidate digest recorded;
- canary healthy;
- tool-list gate passes;
- ZIP safety gates pass;
- real-fixture reconciliation passes;
- current production image digest captured for rollback.

Promotion itself must be a separate, explicit step with rollback evidence.
