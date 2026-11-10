// Mitra main board — shared types and module interfaces.
//
// Hardware baseline (PRD §11/§12): classic ESP32 (WROOM-32, no PSRAM), INMP441 I2S mic,
// MAX98357A I2S amp, SSD1306 0.96" OLED, status LED, BOOT button as push-to-talk.
// All pins are in menuconfig ("Mitra hardware") — confirm against the PCB in Milestone A.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "mitra_common.h"

#define MITRA_FW_VERSION "0.1.0"
#define SAMPLE_RATE 16000
#define FRAME_SAMPLES 320                  // 20 ms
#define FRAME_BYTES (FRAME_SAMPLES * 2)

// ---------------------------------------------------------------------------------------------
// Device state (FR-1.2). Mirrors the server phases plus local-only states.
typedef enum {
    ST_BOOT = 0,
    ST_PROVISIONING,   // waiting for Wi-Fi credentials (SoftAP)
    ST_OFFLINE,        // no Wi-Fi or no backend
    ST_IDLE,
    ST_LISTENING,
    ST_PROCESSING,
    ST_RESPONDING,
    ST_ERROR,
    ST_DISABLED,       // kill switch from the backend
} app_state_t;

const char *app_state_name(app_state_t s);

// Events posted to the app task (single consumer, keeps all state changes on one thread).
typedef enum {
    EV_WIFI_UP, EV_WIFI_DOWN,
    EV_WS_UP, EV_WS_DOWN, EV_WS_REJECTED,
    EV_WS_JSON,          // payload: heap-allocated JSON string (app frees)
    EV_PTT_DOWN, EV_PTT_UP,
    EV_MUTE_ON, EV_MUTE_OFF,
    EV_PLAYBACK_DONE,    // a = first_audio_to_speaker_ms, b = underrun flag, str = cue name or NULL
    EV_CLIP_DONE,        // str = clip name
} app_event_type_t;

typedef struct {
    app_event_type_t type;
    int32_t a, b;
    char *str;
} app_event_t;

bool app_post(app_event_type_t type, int32_t a, int32_t b, char *str);

// Shared flags read by the audio tasks (written by app task / playback task).
typedef struct {
    volatile app_state_t state;
    volatile bool ws_connected;
    volatile bool muted;
    volatile bool ptt_held;
    volatile bool streaming;        // audio is currently leaving the device (FR-2.5 indicator)
    volatile bool camera_active;    // FR-5.8 indicator, driven by the backend
    volatile bool speaker_busy;     // speaker playing (mic gated, FR-3.2)
    volatile int64_t mic_gate_until_us;
    volatile int volume;            // 0..100, capped by CONFIG_MITRA_VOLUME_CAP
    char wake_mode[8];              // "cloud" | "device" | "ptt" — as agreed in hello
} app_shared_t;

extern app_shared_t g_app;

// ---------------------------------------------------------------------------------------------
// Modules
void audio_init(void);                         // I2S mic + speaker
int  audio_mic_read(int16_t *out, int samples, int timeout_ms);   // returns samples read
void audio_spk_write(const int16_t *pcm, int samples);             // blocks; applies volume

void uplink_start(void);                       // mic task + VAD + pre-roll + sender task

void playback_init(void);
void playback_stream_begin(void);              // "tts start"
void playback_stream_push(const uint8_t *pcm, size_t len);   // binary frames from WS
void playback_stream_end(void);                // "tts stop" -> EV_PLAYBACK_DONE when drained
void playback_stop(void);                      // abort (button barge-in, error)
void playback_clip(const char *name, bool report_done);      // local clip from flash

void ws_link_start(const mitra_creds_t *c);
bool ws_link_send_json(const char *json);
bool ws_link_send_audio(const uint8_t *pcm, size_t len);
void ws_link_pause(uint32_t seconds);          // kill switch / auth failure backoff

void display_init(void);
void led_init(void);
void input_init(void);
void status_report(void);                      // {"type":"status"} shadow update over WS

// Simple energy VAD with adaptive noise floor (device side, gates what is streamed).
typedef struct {
    float noise_db;
    int speech_frames;
    int silence_frames;
    bool in_speech;
} vad_t;

void vad_reset(vad_t *v);
// returns +1 on speech start, -1 on speech end, 0 otherwise
int vad_process(vad_t *v, const int16_t *frame, int samples);
