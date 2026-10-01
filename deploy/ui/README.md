# Transcription UI provenance

The optional UI service is built from:

- Upstream: `Wojciechowski-Marcin/whisper-transcriber`
- Upstream commit: `c7cc4f49e9cd58f98cc499d025465d6556312649`
- License: MIT
- Internal image: `ghcr.io/conexaoazul/blue-transcription-ui:c7cc4f49-ca1`

The image keeps the upstream application code and applies only the Dockerfile
hardening captured in `whisper-transcriber-nonroot.patch`:

- preserve the upstream MIT license in the image;
- run runtime as UID/GID 1000 instead of root;
- pre-create `/data/outputs` with UID 1000 ownership;
- use `/data/outputs` as `TMPDIR`.

Deployment policy:

- pinned to `blueops-oci-worker-2`;
- no published host/public port;
- attached only to the `transcription_transcribe` overlay;
- talks to the existing `whisper.cpp` service on Worker 1;
- container health requires the UI API and the upstream Whisper health to be OK.

Build-time npm audit on the pinned source reported zero runtime dependency
vulnerabilities. The six reported findings are in the frontend build toolchain
(2 moderate, 4 high) and Node is not present in the final Python runtime image.
