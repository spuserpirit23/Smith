// Speaker path: jitter-buffered TTS stream from the backend + local clips from flash.
//
// The mic is gated whenever the speaker is busy and for CONFIG_MITRA_PLAYBACK_TAIL_MS after
// (FR-3.2). Underruns while the stream is still open wait up to 3 s, then stop cleanly (FR-3.3).
#include "freertos/FreeRTOS.h"
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/ringbuf.h"
#include "freertos/task.h"
#include "mitra.h"

static const char *TAG = "playback";

#define BUF_BYTES (CONFIG_MITRA_PLAYBACK_BUFFER_MS * 32)
#define PREBUF_BYTES (CONFIG_MITRA_PLAYBACK_PREBUFFER_MS * 32)
#define UNDERRUN_ABORT_US (3 * 1000 * 1000)

// Clips embedded from main/clips/*.pcm (16 kHz mono s16le). Regenerate the spoken ones with
// `mitra render-clips firmware/main-board/main/clips` so they use the same voice as the cloud.
#define CLIP(name)                                                     \
    extern const uint8_t name##_start[] asm("_binary_" #name "_pcm_start"); \
    extern const uint8_t name##_end[] asm("_binary_" #name "_pcm_end");
CLIP(chime)
CLIP(no_brain)
CLIP(repeat)
CLIP(no_wifi)
CLIP(standby)

typedef struct {
    const char *name;
    const uint8_t *start, *end;
} clip_t;

static const clip_t CLIPS[] = {
    {"chime", chime_start, chime_end},       {"no_brain", no_brain_start, no_brain_end},
    {"repeat", repeat_start, repeat_end},    {"no_wifi", no_wifi_start, no_wifi_end},
    {"standby", standby_start, standby_end},
};

typedef enum { CMD_CLIP, CMD_STREAM } cmd_kind_t;
typedef struct {
    cmd_kind_t kind;
    const clip_t *clip;
    bool report_done;
} cmd_t;

static RingbufHandle_t s_rb;
static QueueHandle_t s_cmds;
static volatile bool s_stream_open, s_stream_ended, s_abort;
static volatile int64_t s_first_rx_us;

static void speaker_busy(bool busy)
{
    if (busy) {
        g_app.speaker_busy = true;
    } else {
        g_app.mic_gate_until_us = esp_timer_get_time() + (int64_t)CONFIG_MITRA_PLAYBACK_TAIL_MS * 1000;
        g_app.speaker_busy = false;
    }
}

static void play_clip(const clip_t *c)
{
    speaker_busy(true);
    const int16_t *p = (const int16_t *)c->start;
    int samples = (c->end - c->start) / 2;
    while (samples > 0 && !s_abort) {
        int n = samples > FRAME_SAMPLES ? FRAME_SAMPLES : samples;
        audio_spk_write(p, n);
        p += n;
        samples -= n;
    }
    speaker_busy(false);
}

static void play_stream(void)
{
    speaker_busy(true);
    int64_t first_play_us = 0, starved_since = 0;
    bool started = false, underrun = false;
    // Wait for the pre-buffer (or the end of a very short reply).
    while (!s_abort) {
        UBaseType_t waiting = 0;
        vRingbufferGetInfo(s_rb, NULL, NULL, NULL, NULL, &waiting);
        if (waiting >= PREBUF_BYTES || s_stream_ended) break;
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    while (!s_abort) {
        size_t len = 0;
        uint8_t *data = xRingbufferReceiveUpTo(s_rb, &len, pdMS_TO_TICKS(20), FRAME_BYTES);
        if (data) {
            if (!started) {
                started = true;
                first_play_us = esp_timer_get_time();
            }
            starved_since = 0;
            audio_spk_write((const int16_t *)data, len / 2);
            vRingbufferReturnItem(s_rb, data);
            continue;
        }
        if (s_stream_ended) break;                         // drained
        if (!starved_since) starved_since = esp_timer_get_time();
        if (esp_timer_get_time() - starved_since > UNDERRUN_ABORT_US) {
            ESP_LOGW(TAG, "underrun > 3 s, stopping");
            underrun = true;
            break;
        }
    }
    // Discard anything left (abort / underrun).
    size_t len;
    void *junk;
    while ((junk = xRingbufferReceiveUpTo(s_rb, &len, 0, BUF_BYTES)) != NULL) vRingbufferReturnItem(s_rb, junk);
    s_stream_open = false;
    speaker_busy(false);
    int32_t latency_ms = (started && s_first_rx_us) ? (int32_t)((first_play_us - s_first_rx_us) / 1000) : -1;
    if (!s_abort) app_post(EV_PLAYBACK_DONE, latency_ms, underrun, NULL);
}

static void playback_task(void *arg)
{
    cmd_t cmd;
    while (true) {
        if (xQueueReceive(s_cmds, &cmd, portMAX_DELAY) != pdTRUE) continue;
        s_abort = false;
        if (cmd.kind == CMD_CLIP) {
            play_clip(cmd.clip);
            if (cmd.report_done && !s_abort) app_post(EV_CLIP_DONE, 0, 0, strdup(cmd.clip->name));
        } else {
            play_stream();
        }
    }
}

void playback_init(void)
{
    s_rb = xRingbufferCreate(BUF_BYTES, RINGBUF_TYPE_BYTEBUF);
    s_cmds = xQueueCreate(4, sizeof(cmd_t));
    xTaskCreatePinnedToCore(playback_task, "playback", 4096, NULL, 9, NULL, 1);
}

void playback_stream_begin(void)
{
    s_stream_ended = false;
    s_first_rx_us = 0;
    s_stream_open = true;
    cmd_t c = {.kind = CMD_STREAM};
    xQueueSend(s_cmds, &c, 0);
}

void playback_stream_push(const uint8_t *pcm, size_t len)
{
    if (!s_stream_open) return;
    if (!s_first_rx_us) s_first_rx_us = esp_timer_get_time();
    // The server paces to ~75% of our buffer; if we're full anyway, drop rather than block Wi-Fi.
    if (xRingbufferSend(s_rb, pcm, len, pdMS_TO_TICKS(50)) != pdTRUE) {
        ESP_LOGW(TAG, "jitter buffer full, dropped %u bytes", (unsigned)len);
    }
}

void playback_stream_end(void)
{
    s_stream_ended = true;
}

void playback_stop(void)
{
    s_abort = true;
    s_stream_ended = true;
}

void playback_clip(const char *name, bool report_done)
{
    for (size_t i = 0; i < sizeof(CLIPS) / sizeof(CLIPS[0]); i++) {
        if (strcmp(CLIPS[i].name, name) == 0) {
            cmd_t c = {.kind = CMD_CLIP, .clip = &CLIPS[i], .report_done = report_done};
            xQueueSend(s_cmds, &c, pdMS_TO_TICKS(100));
            return;
        }
    }
    ESP_LOGW(TAG, "unknown clip %s", name);
    if (report_done) app_post(EV_CLIP_DONE, 0, 0, strdup(name));
}
