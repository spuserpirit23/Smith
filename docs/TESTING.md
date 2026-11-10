# Testing

## Automated (no hardware, no cloud account)

```bash
cd backend
pip install -e '.[dev,aws]'
pytest                                   # 33 tests, in-memory DB, mock providers (~70 s)

# against real infrastructure (what CI should run too):
MITRA_TEST_DATABASE_URL=postgresql://postgres@localhost:5432/mitra_test \
MITRA_TEST_MOSQUITTO=$(which mosquitto) pytest

# real openWakeWord engine + espeak-ng speech (stock "hey_jarvis" model as stand-in):
pip install '.[wake]' && pip install --no-deps openwakeword==0.6.0
MITRA_TEST_OWW=1 pytest tests/test_openwakeword.py
```

The suites drive a real uvicorn server over real WebSockets with `mitra.sim.SimDevice`, which
speaks the same protocol as the firmware. Coverage of PRD scenarios:

| PRD | Test |
|---|---|
| Scenario 1 / FR-4.1 full turn, follow-up window | `test_ptt_full_turn_and_follow_up_window`, `test_cloud_wake_full_turn_with_server_vad` |
| Scenario 2 / FR-1.3 silence after wake | `test_cloud_wake_then_no_speech_returns_to_idle_with_cue` |
| Scenario 3 / FR-1.4 unclear → repeat ×2 → idle | `test_unclear_speech_asks_to_repeat_twice_then_gives_up` |
| FR-4.4 fault injection (STT/LLM timeout, LLM error, TTS failure) | `test_fault_injection_plays_fallback[*]`, `test_tts_failure_on_all_providers_plays_fallback` |
| FR-2.2/2.4 nothing reaches STT before wake | `test_idle_audio_without_wake_never_reaches_stt` |
| FR-4.2 context, FR-4.5 safety prompt present | `test_context_is_kept_between_turns` |
| Milestone B: 20 cycles without restart | `test_twenty_consecutive_cycles` |
| Scenario 5 / FR-5.6 camera via MQTT + upload | `test_camera_question_injects_observation`, `test_camera_capture_over_real_mqtt` |
| Scenario 6 / FR-4.3 history delete, opt-in, kill switch | `test_history_delete_and_kill_switch`, `test_history_not_persisted_without_opt_in` |
| FR-6.5 TTS fallback chain | `test_tts_fallback_on_error`, `test_tts_fallback_on_slow_first_chunk` |
| OTA delivery + signed URL binding | `test_status_report_updates_shadow_and_ota_goes_over_ws` |

## Load / soak with the simulator (against any deployment)

```bash
mitra provision sim-001 --out sim.csv
SECRET=$(grep ^secret, sim.csv | cut -d, -f4)
mitra-sim --url wss://<host>/v1/device/ws --device sim-001 --secret "$SECRET" \
          --mode ptt --cycles 20 --wav question.wav --save-replies replies/
mitra latency --device sim-001          # median / p95 per stage
```

## Hardware test protocol (Milestones B–E)

Record every run in the test report; export raw data with `GET /v1/admin/turns.csv`.

1. **Audio loop (B).** USB power. 20 PTT cycles in the test room; log heap/min_heap from
   `mitra devices`. Pass: 20/20, no reboot, intelligible at 1–2 m.
2. **Mic placement (FR-1.5).** Same 10 phrases from two placements; compare `stt_confidence`.
3. **Wake word (C).** 50 "Hey Smith" trials at 1–2 m, quiet room. After each, label it:
   `mitra activations --device mitra-001` then `mitra label <id> --true|--false`.
   Detection rate = true activations / 50 (target ≥ 90 %).
4. **False activations.** 1 h of normal conversation including people named Smith and the
   phrase "Smith & Sky", plus TV/background audio. Count activations labelled false
   (target ≤ 1/h). Tune `wake.threshold`.
5. **Self-hearing.** 30 min of continuous Q&A with the speaker at max allowed volume; any wake
   or turn triggered by Mitra's own voice is a failure (target 0).
6. **Latency (D).** 30 turns; report `end_to_first_audio_ms` median/p95 (target < 2.5 s / < 4 s).
   Wake→chime: record the room with a phone and measure end-of-phrase to chime (target < 1 s).
7. **Fault injection (D).** Pull Wi-Fi mid-answer (expect `no_wifi` clip); stop the API
   container (expect `no_wifi`, reconnect with backoff); set `MITRA_LLM_PROVIDER=mock` and say
   "trigger llm timeout" (expect `no_brain`). Target: 100 % produce a spoken or visual fallback.
8. **Camera (E).** "What do you see?" at 0.5 / 1 / 2 m, bright / dim light; record description
   and `vision`/`capture_ms` from the CSV; note where results become "not sure" (FR-5.10).
