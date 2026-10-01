# Blue Transcription MCP — ContextPack Media Contract v1

Status: design / draft
Date: 2026-10-01
Owner: Conexão Azul

## Purpose

Keep `blue-transcription-mcp` focused as a hardened media engine while exposing the primitives required by Blue Context Intelligence.

The MCP should own media validation, metadata, deterministic media transforms and transcription. It should not own WhatsApp parsing, OCR/vision, CRM, Helpdesk or business action routing.

## Proposed tools

### inspect_archive

Input: ZIP path or inline ZIP bytes using the same safety policy already implemented by `transcribe_zip*`.

Output:
- archive filename and SHA-256
- compressed bytes
- entries count
- each member path/name, size, extension and classification
- supported media vs text/image/document/unsupported
- rejection reason when unsafe

Must reuse current traversal/symlink/entry-count/expansion guards.

### probe_media

Return deterministic technical metadata for validated media:
- SHA-256
- bytes
- duration
- container
- audio/video codecs
- sample rate and channels
- width/height/fps when video

No transcription.

### stitch_audio_chunks

For sequential chunks. Preserve caller order and emit:
- output asset
- input hash list
- output SHA-256
- operation receipt

### mix_audio_tracks

For parallel tracks, especially Craig-style multitrack. Never infer participant identity from filenames.

### transcribe_manifest

Accept a lineage manifest with assets plus semantic relation:
- `sequential` => stitch
- `parallel` => mix
- already-normalized single assets => direct transcription

Reuse the existing transcription core and bounded concurrency.

## Authentication for ChatGPT / Agent Plugins

Current production endpoint uses a private bearer token. Do not put that token in plugin files or query strings.

Add a standards-compatible authentication route for remote MCP clients/ChatGPT, preferably OAuth/MCP-standard authorization, while retaining the current private bearer route for internal clients if needed.

Acceptance requires:
- ChatGPT can connect without a static secret embedded in the plugin;
- `tools/list` succeeds;
- one real/sanitized transcription succeeds;
- authorization remains fail-closed;
- no token in logs or URLs.

## Non-goals

- WhatsApp TXT parsing
- OCR/vision
- entity resolution
- CRM/Helpdesk/Odoo writes
- action extraction

Those remain in Blue Context Intelligence / BlueOps.

## Idempotency and evidence

Each operation should be resumable/auditable by:
- source SHA-256
- operation
- relevant config fingerprint
- provider/backend version
- output SHA-256
- correlation_id when supplied

## Validation

- unit tests for all safety gates;
- deterministic stitch/mix fixtures;
- malformed manifest tests;
- canary with sanitized WhatsApp archive;
- CI PASS;
- immutable image;
- no production activation in this PR;
- documented rollback and activation gate.
