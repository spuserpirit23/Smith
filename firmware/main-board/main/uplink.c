// Microphone -> VAD -> pre-roll -> WebSocket.
//
// What leaves the device (PRD FR-2.3/2.5, privacy NFR):
//   * muted, or speaker busy / tail (FR-3.2)   -> nothing
//   * LISTENING (server asked for the question) -> every frame
//   * PTT held                                 -> every frame
//   * IDLE + cloud wake                        -> only while the VAD hears speech, plus ~0.8 s pre-roll
//                                                 and a short hangover, so "Hey Smith" isn't clipped
//   * anything else                            -> nothing
#include "freertos/FreeRTOS.h"
#include <math.h>
#include <string.h>

#include "esp_log.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "freertos/task.h"
#include "mitra.h"

static const char *TAG = "uplink";

#define PREROLL_FRAMES (CONFIG_MITRA_PREROLL_MS / 20)
#define HANGOVER_FRAMES (CONFIG_MITRA_VAD_HANGOVER_MS / 20)
#define QUEUE_FRAMES 25   // 500 ms between mic and network

typedef struct {
    int16_t pcm[FRAME_SAMPLES];
} frame_t;

static QueueHandle_t s_q;
static frame_t s_preroll[PREROLL_FRAMES];
static int s_pr_head, s_pr_count;
static volatile uint32_t s_dropped;

// ---------------------------------------------------------------- VAD
void vad_reset(vad_t *v)
{
    v->noise_db = -60.f;
    v->speech_frames = v->silence_frames = 0;
    v->in_speech = false;
}

static float frame_dbfs(const int16_t *x, int n)
{
    int64_t acc = 0;
    for (int i = 0; i < n; i++) acc += (int32_t)x[i] * x[i];
    float rms = sqrtf((float)acc / n);
    return rms < 1.f ? -100.f : 20.f * log10f(rms / 32768.f);
}

int vad_process(vad_t *v, const int16_t *frame, int n)
{
    float db = frame_dbfs(frame, n);
    float thr = v->noise_db + 12.f;
    if (thr < -50.f) thr = -50.f;
    bool voiced = db > thr;
    if (!voiced && !v->in_speech) v->noise_db = 0.95f * v->noise_db + 0.05f * db;
    if (voiced) {
        v->silence_frames = 0;
        if (++v->speech_frames >= 6 && !v->in_speech) {   // 120 ms of energy
            v->in_speech = true;
            return 1;
        }
    } else {
        if (!v->in_speech) {
            if (v->speech_frames > 0) v->speech_frames--;
        } else if (++v->silence_frames >= HANGOVER_FRAMES) {
            v->in_speech = false;
            v->speech_frames = 0;
            return -1;
        }
    }
    return 0;
}

// ---------------------------------------------------------------- helpers
static void enqueue(const int16_t *pcm)
{
    frame_t f;
    memcpy(f.pcm, pcm, FRAME_BYTES);
    if (xQueueSend(s_q, &f, 0) != pdTRUE) {
        frame_t old;
        xQueueReceive(s_q, &old, 0);   // drop oldest: latency matters more than completeness
        xQueueSend(s_q, &f, 0);
        s_dropped++;
    }
}

static void preroll_push(const int16_t *pcm)
{
    memcpy(s_preroll[s_pr_head].pcm, pcm, FRAME_BYTES);
    s_pr_head = (s_pr_head + 1) % PREROLL_FRAMES;
    if (s_pr_count < PREROLL_FRAMES) s_pr_count++;
}

static void preroll_flush(void)
{
    int start = (s_pr_head - s_pr_count + PREROLL_FRAMES) % PREROLL_FRAMES;
    for (int i = 0; i < s_pr_count; i++) enqueue(s_preroll[(start + i) % PREROLL_FRAMES].pcm);
    s_pr_count = 0;
}

static bool mic_gated(void)
{
    return g_app.muted || g_app.speaker_busy || esp_timer_get_time() < g_app.mic_gate_until_us;
}

// ---------------------------------------------------------------- tasks
static void mic_task(void *arg)
{
    static int16_t pcm[FRAME_SAMPLES];
    vad_t vad;
    vad_reset(&vad);
    bool vad_streaming = false;
    esp_task_wdt_add(NULL);
    while (true) {
        esp_task_wdt_reset();
        int n = audio_mic_read(pcm, FRAME_SAMPLES, 100);
        if (n != FRAME_SAMPLES) continue;

        if (mic_gated() || !g_app.ws_connected) {
            vad_reset(&vad);
            vad_streaming = false;
            s_pr_count = 0;          // never send audio captured while the speaker was talking
            g_app.streaming = false;
            continue;
        }

        app_state_t st = g_app.state;
        bool send = false;
        if (st == ST_LISTENING || g_app.ptt_held) {
            send = true;
            vad_streaming = false;
        } else if (st == ST_IDLE && strcmp(g_app.wake_mode, "cloud") == 0) {
            int ev = vad_process(&vad, pcm, n);
            if (ev == 1) {
                vad_streaming = true;
                preroll_flush();
            } else if (ev == -1) {
                vad_streaming = false;
            }
            send = vad_streaming;
            if (!send) preroll_push(pcm);   // RAM only, overwritten continuously (FR-2.4)
        }
        g_app.streaming = send;
        if (send) enqueue(pcm);
    }
}

static void sender_task(void *arg)
{
    // Batch 3 frames (60 ms) per WebSocket message: fewer TLS records, lower CPU.
    static uint8_t batch[FRAME_BYTES * 3];
    size_t fill = 0;
    frame_t f;
    while (true) {
        if (xQueueReceive(s_q, &f, pdMS_TO_TICKS(40)) == pdTRUE) {
            memcpy(batch + fill, f.pcm, FRAME_BYTES);
            fill += FRAME_BYTES;
        }
        if (fill == sizeof(batch) || (fill > 0 && uxQueueMessagesWaiting(s_q) == 0)) {
            if (!ws_link_send_audio(batch, fill)) s_dropped += fill / FRAME_BYTES;
            fill = 0;
        }
        static uint32_t last_report;
        if (s_dropped > last_report + 50) {
            ESP_LOGW(TAG, "dropped %lu uplink frames so far", (unsigned long)s_dropped);
            last_report = s_dropped;
        }
    }
}

void uplink_start(void)
{
    s_q = xQueueCreate(QUEUE_FRAMES, sizeof(frame_t));
    // Mic on core 1 at high priority (Wi-Fi lives on core 0); sender on core 0.
    xTaskCreatePinnedToCore(mic_task, "mic", 4096, NULL, 10, NULL, 1);
    xTaskCreatePinnedToCore(sender_task, "uplink", 4096, NULL, 6, NULL, 0);
}
