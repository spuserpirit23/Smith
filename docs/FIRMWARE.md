# Firmware

Two ESP-IDF **v5.3** projects sharing `components/mitra_common`:

| Project | Target | Talks to backend over |
|---|---|---|
| `firmware/main-board` | classic ESP32 (WROOM-32, no PSRAM) | one WSS connection (audio + control) |
| `firmware/camera-node` | ESP32-CAM AI-Thinker (OV2640, 4 MB PSRAM) | MQTT over WSS + one HTTPS POST per capture |

Both compile cleanly (`-Wall -Werror=all`) against ESP-IDF v5.3.2. Main board image ≈ 1.35 MB
(28 % of its OTA slot free); camera image ≈ 1.03 MB. **Neither has been run on hardware yet** — the
pin map, mic gain and audio levels must be confirmed on the actual PCB (Milestone A).

## Build & flash

```bash
. $IDF_PATH/export.sh
cd firmware/main-board
idf.py set-target esp32
idf.py menuconfig          # "Mitra hardware": pins; "Mitra behaviour": wake mode, volume cap
idf.py build flash monitor
```

Dependencies (`esp_websocket_client`, `esp32-camera`) come from the ESP Component Registry via
each `main/idf_component.yml`; `idf.py` fetches them on first build.

## Give a device its identity

```bash
export MITRA_API=https://<host> MITRA_ADMIN_TOKEN=<token>
mitra provision mitra-001                                           # main board
mitra provision cam-001 --kind camera --paired-main mitra-001 --role user
tools/flash_credentials.sh mitra-001.nvs.csv /dev/ttyUSB0           # writes the mitra_cfg partition
rm mitra-001.nvs.csv
```

The secret lives only in the device's `mitra_cfg` NVS partition (0x12000) and as hashes on the
server. Re-provision to rotate. Many ESP32-CAM boards need an external USB-serial adapter and IO0
pulled low to flash (PRD FR-5.9).

## Wi-Fi

First boot (or after holding **BOOT/PTT for 5 s at power-on**) the board starts a SoftAP
`MITRA-XXXXXX` (cameras: `MITRA-CAM-XXXXXX`). Use Espressif's **ESP SoftAP Prov** app with the
proof-of-possession from menuconfig (default `mitra-setup` — change it). Reconnects back off
1 → 30 s.

## Main board behaviour

* **Wake mode** (menuconfig): `cloud` (default) streams VAD-gated speech for server-side
  "Hey Smith"; `ptt` sends nothing unless the button is held; `device` is reserved for a passing
  on-device spike (FR-2.1). The server's `hello` can downgrade `cloud` → `ptt` if no wake engine
  is configured, and the board obeys.
* **PTT** (BOOT/GPIO0) works in every mode (FR-2.8) and doubles as barge-in: pressing it stops
  playback and starts a new question.
* **Mic gating**: nothing is captured while the speaker plays, plus a 350 ms tail (FR-3.2). The
  pre-roll ring is cleared whenever the gate closes, so the robot never sends its own voice.
* **Indicators**: OLED face per state; top-left dot + red LED whenever audio leaves the device;
  crossed-mic icon when muted; camera icon while a camera captures; ✕ when offline.
* **Fallback clips** in flash: `chime`, `no_brain`, `repeat`, `no_wifi`, `standby`
  (`main/clips/*.pcm`, 16 kHz s16le). The committed ones are espeak placeholders — regenerate in
  Mitra's real voice with `mitra render-clips firmware/main-board/main/clips` and rebuild.
* **Reliability**: task watchdog (10 s) on the mic and app tasks, brownout detector, WebSocket
  reconnect with backoff, OTA rollback until the new image completes a backend `hello`.
* **RAM**: ~68 KB static DRAM incl. 26 KB pre-roll and a 32 KB jitter buffer; mbedTLS uses dynamic
  buffers. The board reports `heap`/`min_heap` in `status` every minute — watch it during the soak test.

## Camera node behaviour

Idle until a `capture` command; LED on while capturing; drops the first frame after power-up for
exposure; uploads one JPEG and frees it; sensor powered down after 60 s idle (FR-5.7). LWT marks it
offline on the broker. Power each camera from a stable 5 V USB supply (FR-5.9).

## Production hardening (any device leaving the lab, PRD §6)

Secure Boot v1 + flash encryption + NVS encryption are **one-way eFuse operations**. Lines are
staged, commented out, in `sdkconfig.defaults`. Enable in *Release* mode only, on a device you
are prepared to lock, after reading Espressif's Security Guide for the classic ESP32. Keep the
signing key out of the repo.

## Known gaps

* On-device "Hey Smith" spike (FR-2.1) not attempted; cloud wake + PTT implemented instead.
* No Opus encoding on the uplink yet (raw pcm16, ~32 KB/s).
* Mute switch is wired in code (`MITRA_MUTE_GPIO`) but disabled until a free GPIO is confirmed.
