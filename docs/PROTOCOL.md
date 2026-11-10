# Device protocol v1

## Main board ⇄ backend: WebSocket

`wss://<host>/v1/device/ws` with headers `X-Device-Id: <id>` and `Authorization: Bearer <secret>`.
Unknown device, wrong secret or a disabled device → handshake refused (HTTP 403).

* **Binary frames** = audio, pcm16 little-endian, 16 kHz, mono. Uplink frames are 20–60 ms;
  downlink frames are ≤ 100 ms (3200 bytes), paced to ≤ 75 % of the board's buffer ahead of real time.
* **Text frames** = one JSON object with a `type`.

### Device → server

| type | fields | meaning |
|---|---|---|
| `hello` | `version:1, fw, wake:"cloud"\|"device"\|"ptt", audio:{codec:"pcm16", sample_rate:16000, channels:1, frame_ms, playback_buffer_ms}` | must be the first message (5 s) |
| `listen` | `state:"start", mode:"ptt"` | button pressed; audio follows immediately |
| `listen` | `state:"start", mode:"device_wake", confidence` | on-device wake word fired (if FR-2.1 spike passes) |
| `listen` | `state:"stop"` | button released = end of utterance |
| `playback_done` | `first_audio_to_speaker_ms?, underrun?, cue?` | speaker finished (TTS stream or local cue), after the mic-gate tail |
| `abort` | | cancel the current turn |
| `mute` | `state: bool` | hardware mute switch |
| `status` | free-form: `state, fw, rssi, heap, min_heap, volume, muted, ota{…}` | merged into the device's reported shadow |
| `ping` | `t` | → `pong` |

### Server → device

| type | fields | device action |
|---|---|---|
| `hello` | `session_id, audio, wake, config{no_speech_timeout_s, follow_up_window_s, playback_tail_ms, …}` | `wake` is authoritative: `cloud` only if the server has a wake engine |
| `state` | `state: idle\|listening\|processing\|responding`, `mode?` | show it (OLED/LED); stream every frame while `listening` |
| `wake` | `engine, confidence, activation_id` | play chime (mic gated), go `listening` |
| `stt` | `text` | optional display/debug |
| `tts` | `state:"start"` → `"sentence", text` … → `"stop", reason?` | buffer & play binary audio; after `stop` drains → `playback_done` |
| `cue` | `name: repeat\|standby` | play local clip, then `playback_done{cue}` |
| `error` | `code, cue:"no_brain"` | play local clip, then `playback_done{cue}` |
| `camera` | `active: bool, camera_id` | camera-active indicator (FR-5.8) |
| `config` | e.g. `volume` | apply + persist |
| `ota` | `url, sha256, version` | download, verify SHA-256, reboot; rollback if the new image never reaches the backend |
| `goodbye` | `reason: disabled\|replaced\|server_shutdown` | `disabled` = kill switch: stop reconnecting for an hour |

Error codes: `stt_timeout, stt_error, llm_timeout, llm_error, llm_empty, tts_timeout, tts_error, internal_error`.

## Cameras ⇄ backend: MQTT (over `wss://<host>/mqtt`)

Username = device id, password = device secret, client id = device id. ACLs confine each device
to `mitra/<id>/…`.

| topic | dir | payload |
|---|---|---|
| `mitra/<id>/cmd` | ← | `{"type":"capture","job_id","upload_url","framesize":"VGA","quality":12}` · `ota` · `reboot` · `identify` · `disable` |
| `mitra/<id>/state` | → retained, LWT | `{"online":true,"state":"idle\|capturing\|error","fw","rssi"}` |
| `mitra/<id>/telemetry` | → | `{"type":"capture","job_id","status":"ok\|error","bytes","ms","err"}` |

Upload: `POST <upload_url>` with `Content-Type: image/jpeg`, body = one JPEG (≤ 400 KB). The URL
token is HMAC-signed, bound to camera + job, valid 30 s, single use.
