# Testing on a PC (laptop mic, speaker, webcam)

`mitra-pc` stands in for the hardware. The laptop mic and speaker act as the ESP32 main board and
the webcam acts as the ESP32-CAM. It speaks protocol v1 over the same WebSocket, so the backend
can't tell it apart from the real board. The one difference is the camera: the client sends
`camera: true` in its `hello`, and when no ESP32-CAM is paired the backend sends the `capture`
command over that WebSocket instead of MQTT. The upload still goes to the normal signed
`/v1/camera/.../upload` URL.

```powershell
cd backend
pip install -e ".[pc,dev]"

# 1. Hardware check, no backend needed: records 3 s, plays it back, takes one webcam photo
mitra-pc check                 # add --mic N / --speaker N / --camera N to pick devices
mitra-pc devices               # list audio device indexes

# 2. Start the backend (terminal 1). --env-file is needed so the provider choices in .env
#    (MITRA_STT_PROVIDER=mock, ...) reach the ${VAR} interpolation in config/mitra.yaml.
python -m uvicorn mitra.main:app_factory --factory --env-file .env --port 8080

# 3. Run the PC device (terminal 2). Auto-provisions "pc-001" with MITRA_ADMIN_TOKEN from .env
#    (the dev DB is in-memory, so this runs again on every backend restart).
mitra-pc
```

Keys: **Enter** starts talking and Enter again stops (push-to-talk) · **l** runs the full camera
path (capture, upload, vision) and prints the result · **s** saves a webcam snapshot · **q** quits.

While you talk, a live dBFS meter is shown. The server's VAD counts anything above about
−52 dBFS (adaptive) as speech. The client saves `last_utterance.wav` and `last_capture.jpg` under
`$MITRA_DATA_DIR/pc/`, so you can check exactly what was sent.

With the default mock providers:
* STT returns a fixed sentence whenever the audio is loud enough, so it proves the mic works but
  not what you said.
* The reply is played as tones, one per word.
* Vision returns a fixed description, but only after a real JPEG from your webcam was uploaded.
  Use **l** to test it. Voice questions like "what do you see" only trigger the camera once a
  real STT provider is configured.

To hear real transcripts and speech, change `MITRA_STT_PROVIDER` / `MITRA_TTS_PROVIDERS` /
`MITRA_LLM_PROVIDER` in `.env` (see `config/mitra.yaml`) and restart the backend.
