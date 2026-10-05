# OCI Trial canary — worker-2

Purpose: validate Blue Transcription Trial on `blueops-oci-worker-2` without
changing production services, exposing ports, or altering Swarm manager roles.

The stack in `deploy/oci-trial-canary.yml` is intentionally inert:
**every service starts with replicas=0**.

## Preconditions

- Candidate image is an immutable GHCR digest.
- Candidate CI is green.
- Existing Docker secret `transcription_mcp_auth` is present.
- `blueops-oci-worker-2` is Ready/Active.
- Do not change manager quorum in this runbook.
- Do not prune Docker storage as part of this canary.
- Public Trial remains closed.

## Render only

```bash
export CANDIDATE_IMAGE='ghcr.io/conexaoazul/blue-transcription-mcp-candidate@sha256:<digest>'
docker stack config -c deploy/oci-trial-canary.yml >/tmp/oci-trial-canary.rendered.yml
```

Verify that the rendered services contain the placement constraint:

```
node.hostname == blueops-oci-worker-2
```

and that no service publishes a host port.

## Deploy inert stack

```bash
docker stack deploy -c deploy/oci-trial-canary.yml oci-trial-canary
```

All four services must remain at 0 replicas.

## 1. Initialize writable volumes

```bash
docker service scale oci-trial-canary_init-permissions=1
docker service ps oci-trial-canary_init-permissions --no-trunc
docker service logs oci-trial-canary_init-permissions
```

Required evidence: `OCI_TRIAL_CANARY_INIT_OK`.

Then return it to zero:

```bash
docker service scale oci-trial-canary_init-permissions=0
```

## 2. Start private Whisper only

```bash
docker service scale oci-trial-canary_whisper-canary=1
docker service ps oci-trial-canary_whisper-canary --no-trunc
```

Gate:
- task is on `blueops-oci-worker-2`;
- task is Running;
- container health is healthy;
- no published port exists.

If the official whisper.cpp image exits with SIGILL or another CPU-specific
failure, stop here. Do not change the production service. Build/publish a
worker-2-compatible image separately and rerun this gate.

## 3. Start metered MCP

```bash
docker service scale oci-trial-canary_mcp-metered-canary=1
docker service ps oci-trial-canary_mcp-metered-canary --no-trunc
```

Gate:
- task is on worker-2;
- health reports `metering_enabled=true`;
- no published port exists.

## 4. Run one-shot synthetic lifecycle smoke

```bash
docker service scale oci-trial-canary_smoke=1
docker service ps oci-trial-canary_smoke --no-trunc
docker service logs oci-trial-canary_smoke
```

Required terminal evidence: JSON with `"status":"OCI_TRIAL_CANARY_OK"`.

The smoke proves:
- tenant creation;
- 120-minute quota metadata;
- max concurrency;
- hourly and total call caps;
- per-tenant synchronous duration ceiling;
- one real Whisper inference;
- API-key rotation revokes the old key;
- suspend/activate auth behavior;
- replay does not double-charge;
- an over-ceiling request is rejected before usage;
- a third new request is blocked by call caps;
- Trial→Pro upgrade preserves prior usage;
- audit events do not contain plaintext API keys.

The smoke uses synthetic audio only. It does **not** prove transcription
accuracy.

## 5. Benchmark before promotion

Record at minimum:
- 10s, 30s and 60s speech fixtures where authorized;
- wall-clock inference time;
- real-time factor;
- CPU/RAM pressure;
- model used;
- number of concurrent lanes tested.

Do not copy the azul2 RTF into worker-2 capacity planning.

## 6. Real fixture gate

Only after the synthetic smoke is green, run an authorized real media fixture
(WhatsApp/reunião/ZIP) and record:
- duration;
- hash/manifest;
- operationally useful transcript result;
- no raw customer media committed to Git.

## Cleanup / rollback

```bash
docker stack rm oci-trial-canary
```

Verify all canary services are gone. Named canary volumes may be retained for
forensics/model-cache if explicitly desired; otherwise remove only the
canary-specific volumes after verifying no service references them.

Production `transcription_whisper`, `transcription_mcp`, public ingress, and
Swarm node roles are not modified by this canary.
