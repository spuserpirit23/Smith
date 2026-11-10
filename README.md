# Mitra — Smith & Sky companion robot (software)

ESP32 robot that wakes on "Hey Smith", talks through a cloud speech pipeline, and can look
through two ESP32-CAM nodes. Built from *Mitra Robot — Software PRD (ESP32 + AWS) v0.1*, made
**cloud-agnostic** and **cheap to run**: the whole backend is one Docker Compose stack on a single
VM (AWS EC2 by default), and every AI service sits behind a swappable provider.

```
backend/      Python 3.11+ FastAPI service: device WebSocket, conversation state machine,
              pluggable STT/LLM/TTS/wake/vision, MQTT control plane, admin API + CLI, simulator
firmware/     ESP-IDF 5.3: main-board (classic ESP32) · camera-node (ESP32-CAM) · shared component
deploy/       docker-compose (Caddy, API, Postgres, Mosquitto), Terraform for one EC2 box, scripts
tools/        flash_credentials.sh
docs/         ARCHITECTURE · PROTOCOL · COST · FIRMWARE · TESTING
```

## Run it locally in 2 minutes (no AWS, no hardware)

```bash
# Requires Python 3.11 or newer (use `py -3.14` instead of `python` on Windows if needed).
cd backend && python -m venv .venv && . .venv/bin/activate && python -m pip install -U pip && pip install -e '.[dev]'
export MITRA_ADMIN_TOKEN=dev MITRA_SIGNING_SECRET=dev MITRA_LOG_JSON=false \
       MITRA_STT_PROVIDER=mock MITRA_LLM_PROVIDER=mock MITRA_TTS_PROVIDERS=mock \
       MITRA_VISION_PROVIDER=mock MITRA_WAKE_PROVIDER=mock MITRA_DATA_DIR=/tmp/mitra
uvicorn mitra.main:app_factory --factory --port 8080 &

export MITRA_API=http://localhost:8080
mitra provision sim-001 --out sim.csv
mitra-sim --device sim-001 --secret "$(grep ^secret, sim.csv | cut -d, -f4)" --cycles 3 --gap-s 1
mitra latency
```

Swap any `mock` for a real provider (`bedrock`, `aws_transcribe`, `polly`, `anthropic`,
`openai_compat`, …) — see `backend/config/mitra.yaml`.

## Deploy on AWS (≈ $15/month fixed + ≈ $0.005 per conversation turn)

```bash
cd deploy/terraform/aws
cp terraform.tfvars.example terraform.tfvars      # set acme_email, budget email, repo_url
terraform init && terraform apply
$(terraform output -raw admin_token_command)       # operator token, generated on the box
```

The instance (Ubuntu 24.04 arm64, m7g.xlarge, ap-south-1) installs Docker, clones the repo, writes
`deploy/.env` with fresh secrets, and starts the stack behind automatic HTTPS at
`https://<elastic-ip>.sslip.io` (or your `domain`). By default the LLM (Qwen3 1.7B), vision
(Gemma 3 4B) and TTS (Piper) run on the instance itself, so there's no per-use AI bill. See
[docs/LOCAL_MODELS.md](docs/LOCAL_MODELS.md), and [docs/COST.md](docs/COST.md) for when hosted APIs
(Bedrock / Polly, a config switch) work out cheaper.

**Any other cloud / on-prem:** create a Linux VM, open 80/443, install Docker, clone the repo, run
`sudo deploy/scripts/bootstrap.sh` (or `deploy/scripts/push_deploy.sh user@host` from your laptop).
Set non-AWS providers in `deploy/.env`.

## Bring up a robot

1. `mitra provision mitra-001` → `tools/flash_credentials.sh mitra-001.nvs.csv /dev/ttyUSB0`
2. `cd firmware/main-board && idf.py menuconfig build flash monitor` (check pins!)
3. Provision Wi-Fi with the *ESP SoftAP Prov* app (SoftAP `MITRA-XXXXXX`)
4. Hold BOOT and talk, or say the wake word if cloud wake is enabled
5. Cameras: `mitra provision cam-001 --kind camera --paired-main mitra-001 --role user`, flash
   `firmware/camera-node`, then `mitra look mitra-001`

Details: [docs/FIRMWARE.md](docs/FIRMWARE.md).

## Operating

| Task | Command |
|---|---|
| List devices / online / firmware / RSSI | `mitra devices` |
| Delete conversation history (FR-4.3) | `mitra delete-history mitra-001` |
| Delete all data for a device | `mitra delete-data mitra-001` |
| Kill switch | `mitra disable mitra-001` / `mitra enable mitra-001` |
| Latency report (FR-3.4) | `mitra latency --device mitra-001 --hours 24`, CSV at `/v1/admin/turns.csv` |
| Wake-word test labelling (FR-2.7) | `mitra activations` · `mitra label <id> --true/--false` |
| OTA | `mitra ota mitra-001 build/mitra_main_board.bin 0.2.0` |
| Change persona / voice / model | edit `backend/config/mitra.yaml` or `.env`, `docker compose restart api` |
| Backup DB | `deploy/scripts/backup_db.sh` (plus nightly EBS snapshots via Terraform) |

## Status vs PRD milestones

| Milestone | State |
|---|---|
| A. Audit | **Open** — confirm board, PSRAM, pins, mic/speaker models; firmware pin defaults are guesses |
| B. Audio loop | Software done and simulated (20-cycle test passes); needs hardware run |
| C. Wake word | Cloud wake (openWakeWord) wired and tested with a stock model; **"Hey Smith" model still to be trained**; on-device spike not attempted |
| D. AI integration | Done with Transcribe/Bedrock/Polly + alternatives; fallbacks, history controls, latency report; needs a real-AWS latency run |
| E. Camera | Option A (scene description) done end to end incl. MQTT, indicators, frame discard; needs hardware |
| F. Custom voice | Provider interface + automatic fallback in place; no custom voice integrated (needs consent + vendor choice) |
| G. Handover | IaC, README, docs, tests in place; test report pending hardware |

Decisions still needed from Smith & Sky are listed in PRD §9; the ones that block code are:
camera scope (A only for now), languages (default `en-IN`, Polly voice Kajal), whether any memory
may be retained (off by default), and approval for continuous cloud audio for wake detection.

## Safety

Mitra's system prompt (`backend/config/system_prompt.md`) states it is a companion, not a
clinician, never diagnoses, cannot call for help, and points people in crisis to someone they
trust or 112. **Smith & Sky should review that wording before any client demo** (PRD FR-4.5).
