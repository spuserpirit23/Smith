// Mitra main board: boot, then a single app task owns all state transitions.
#include "freertos/FreeRTOS.h"
#include <string.h>

#include "cJSON.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "freertos/task.h"
#include "mitra.h"
#include "nvs.h"
#include "nvs_flash.h"

static const char *TAG = "mitra";

app_shared_t g_app = {.state = ST_BOOT, .volume = CONFIG_MITRA_VOLUME_DEFAULT};
static QueueHandle_t s_events;
static mitra_creds_t s_creds;
static bool s_in_turn;            // something is in flight: a dropped link should be announced
static bool s_hello_ok;

const char *app_state_name(app_state_t s)
{
    static const char *names[] = {"boot", "provisioning", "offline", "idle", "listening",
                                  "processing", "responding", "error", "disabled"};
    return s <= ST_DISABLED ? names[s] : "?";
}

bool app_post(app_event_type_t type, int32_t a, int32_t b, char *str)
{
    app_event_t ev = {.type = type, .a = a, .b = b, .str = str};
    if (xQueueSend(s_events, &ev, pdMS_TO_TICKS(50)) != pdTRUE) {
        free(str);
        return false;
    }
    return true;
}

static void set_state(app_state_t s)
{
    if (g_app.state == s) return;
    ESP_LOGI(TAG, "state %s -> %s", app_state_name(g_app.state), app_state_name(s));
    g_app.state = s;
}

static void send_hello(void)
{
    char msg[320];
    snprintf(msg, sizeof(msg),
             "{\"type\":\"hello\",\"version\":1,\"fw\":\"%s\",\"wake\":\"%s\","
             "\"audio\":{\"codec\":\"pcm16\",\"sample_rate\":16000,\"channels\":1,\"frame_ms\":20,"
             "\"playback_buffer_ms\":%d}}",
             MITRA_FW_VERSION,
#if CONFIG_MITRA_WAKE_CLOUD
             "cloud",
#elif CONFIG_MITRA_WAKE_DEVICE
             "device",
#else
             "ptt",
#endif
             CONFIG_MITRA_PLAYBACK_BUFFER_MS);
    ws_link_send_json(msg);
}

static void wifi_cb(bool up)
{
    app_post(up ? EV_WIFI_UP : EV_WIFI_DOWN, 0, 0, NULL);
}

static void ota_report(const char *status, const char *version, const char *err)
{
    char msg[200];
    snprintf(msg, sizeof(msg), "{\"type\":\"status\",\"ota\":{\"status\":\"%s\",\"version\":\"%s\",\"err\":\"%s\"}}",
             status, version, err ? err : "");
    ws_link_send_json(msg);
}

void status_report(void)
{
    int rssi = mitra_wifi_rssi();
    char msg[240];
    snprintf(msg, sizeof(msg),
             "{\"type\":\"status\",\"state\":\"%s\",\"fw\":\"%s\",\"rssi\":%d,\"muted\":%s,\"volume\":%d,"
             "\"heap\":%lu,\"min_heap\":%lu,\"uptime_s\":%lld}",
             app_state_name(g_app.state), MITRA_FW_VERSION, rssi, g_app.muted ? "true" : "false", g_app.volume,
             (unsigned long)esp_get_free_heap_size(), (unsigned long)esp_get_minimum_free_heap_size(),
             esp_timer_get_time() / 1000000);
    ws_link_send_json(msg);
}

static void save_volume(int v)
{
    nvs_handle_t h;
    if (nvs_open("mitra", NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_i32(h, "volume", v);
        nvs_commit(h);
        nvs_close(h);
    }
}

static void load_settings(void)
{
    nvs_handle_t h;
    int32_t v;
    if (nvs_open("mitra", NVS_READONLY, &h) == ESP_OK) {
        if (nvs_get_i32(h, "volume", &v) == ESP_OK) g_app.volume = v;
        nvs_close(h);
    }
}

// ---------------------------------------------------------------- server messages
static void handle_json(const char *text)
{
    cJSON *m = cJSON_Parse(text);
    if (!m) return;
    const char *type = cJSON_GetStringValue(cJSON_GetObjectItem(m, "type"));
    if (!type) goto out;

    if (!strcmp(type, "hello")) {
        const char *wake = cJSON_GetStringValue(cJSON_GetObjectItem(m, "wake"));
        strlcpy(g_app.wake_mode, wake ? wake : "ptt", sizeof(g_app.wake_mode));
        s_hello_ok = true;
        mitra_ota_confirm_running_app();
        set_state(ST_IDLE);
        status_report();
    } else if (!strcmp(type, "state")) {
        const char *st = cJSON_GetStringValue(cJSON_GetObjectItem(m, "state"));
        if (!st) goto out;
        if (!strcmp(st, "idle")) { set_state(ST_IDLE); s_in_turn = false; }
        else if (!strcmp(st, "listening")) { set_state(ST_LISTENING); s_in_turn = true; }
        else if (!strcmp(st, "processing")) set_state(ST_PROCESSING);
        else if (!strcmp(st, "responding")) set_state(ST_RESPONDING);
    } else if (!strcmp(type, "wake")) {
        playback_clip("chime", false);             // FR-2.6; mic is gated while it plays
        set_state(ST_LISTENING);
        s_in_turn = true;
    } else if (!strcmp(type, "tts")) {
        const char *st = cJSON_GetStringValue(cJSON_GetObjectItem(m, "state"));
        if (st && !strcmp(st, "start")) { set_state(ST_RESPONDING); playback_stream_begin(); }
        else if (st && !strcmp(st, "stop")) playback_stream_end();
    } else if (!strcmp(type, "cue") || !strcmp(type, "error")) {
        const char *name = cJSON_GetStringValue(cJSON_GetObjectItem(m, !strcmp(type, "cue") ? "name" : "cue"));
        if (!strcmp(type, "error")) {
            ESP_LOGW(TAG, "server error: %s", cJSON_GetStringValue(cJSON_GetObjectItem(m, "code")));
        }
        playback_clip(name ? name : "no_brain", true);   // EV_CLIP_DONE -> playback_done
    } else if (!strcmp(type, "camera")) {
        g_app.camera_active = cJSON_IsTrue(cJSON_GetObjectItem(m, "active"));
    } else if (!strcmp(type, "config")) {
        cJSON *vol = cJSON_GetObjectItem(m, "volume");
        if (cJSON_IsNumber(vol)) {
            int v = vol->valueint < 0 ? 0 : vol->valueint > 100 ? 100 : vol->valueint;
            g_app.volume = v;
            save_volume(v);
        }
        status_report();
    } else if (!strcmp(type, "ota")) {
        mitra_ota_start(cJSON_GetStringValue(cJSON_GetObjectItem(m, "url")),
                        cJSON_GetStringValue(cJSON_GetObjectItem(m, "sha256")),
                        cJSON_GetStringValue(cJSON_GetObjectItem(m, "version")), ota_report);
    } else if (!strcmp(type, "reboot")) {
        esp_restart();
    } else if (!strcmp(type, "goodbye")) {
        const char *reason = cJSON_GetStringValue(cJSON_GetObjectItem(m, "reason"));
        if (reason && !strcmp(reason, "disabled")) {
            set_state(ST_DISABLED);                 // kill switch: stay quiet for an hour
            ws_link_pause(3600);
        }
    }
out:
    cJSON_Delete(m);
}

// ---------------------------------------------------------------- app task
static void app_task(void *arg)
{
    app_event_t ev;
    int64_t last_status = 0;
    esp_task_wdt_add(NULL);
    while (true) {
        esp_task_wdt_reset();
        if (g_app.ws_connected && esp_timer_get_time() - last_status > 60LL * 1000000) {
            last_status = esp_timer_get_time();
            status_report();
        }
        if (xQueueReceive(s_events, &ev, pdMS_TO_TICKS(1000)) != pdTRUE) continue;
        switch (ev.type) {
        case EV_WIFI_UP:
            ESP_LOGI(TAG, "wifi up");
            break;
        case EV_WIFI_DOWN:
        case EV_WS_DOWN:
            s_hello_ok = false;
            if (g_app.state == ST_DISABLED) break;
            if (s_in_turn || g_app.state == ST_RESPONDING) {   // PRD scenario 4
                playback_stop();
                playback_clip("no_wifi", false);
            }
            s_in_turn = false;
            g_app.ptt_held = false;
            set_state(ST_OFFLINE);
            break;
        case EV_WS_UP:
            send_hello();
            break;
        case EV_WS_REJECTED:
            ESP_LOGE(TAG, "backend rejected this device (%ld): check credentials / enable flag", (long)ev.a);
            set_state(ev.a == 4403 ? ST_DISABLED : ST_ERROR);
            ws_link_pause(ev.a == 4403 ? 3600 : 300);
            break;
        case EV_WS_JSON:
            handle_json(ev.str);
            break;
        case EV_PTT_DOWN:
            if (!g_app.ws_connected || !s_hello_ok) {
                playback_clip("no_wifi", false);
                break;
            }
            if (g_app.muted) break;
            playback_stop();                         // button barge-in
            g_app.ptt_held = true;
            ws_link_send_json("{\"type\":\"listen\",\"state\":\"start\",\"mode\":\"ptt\"}");
            set_state(ST_LISTENING);
            s_in_turn = true;
            break;
        case EV_PTT_UP:
            if (g_app.ptt_held) {
                g_app.ptt_held = false;
                ws_link_send_json("{\"type\":\"listen\",\"state\":\"stop\"}");
            }
            break;
        case EV_MUTE_ON:
        case EV_MUTE_OFF:
            g_app.muted = ev.type == EV_MUTE_ON;
            ws_link_send_json(g_app.muted ? "{\"type\":\"mute\",\"state\":true}" : "{\"type\":\"mute\",\"state\":false}");
            if (g_app.muted) { playback_stop(); set_state(ST_IDLE); }
            status_report();
            break;
        case EV_PLAYBACK_DONE: {
            char msg[120];
            snprintf(msg, sizeof(msg), "{\"type\":\"playback_done\",\"first_audio_to_speaker_ms\":%ld,\"underrun\":%s}",
                     (long)ev.a, ev.b ? "true" : "false");
            if (ev.a < 0) snprintf(msg, sizeof(msg), "{\"type\":\"playback_done\",\"underrun\":%s}", ev.b ? "true" : "false");
            ws_link_send_json(msg);
            break;
        }
        case EV_CLIP_DONE: {
            char msg[96];
            snprintf(msg, sizeof(msg), "{\"type\":\"playback_done\",\"cue\":\"%s\"}", ev.str ? ev.str : "");
            ws_link_send_json(msg);
            break;
        }
        }
        free(ev.str);
    }
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    load_settings();
    s_events = xQueueCreate(24, sizeof(app_event_t));

    led_init();
    display_init();
    audio_init();
    playback_init();

    // Hold the PTT button for 5 s while powering on to forget Wi-Fi and re-provision.
    gpio_config_t c = {.pin_bit_mask = 1ULL << CONFIG_MITRA_PTT_GPIO, .mode = GPIO_MODE_INPUT,
                       .pull_up_en = GPIO_PULLUP_ENABLE};
    gpio_config(&c);
    int held = 0;
    while (gpio_get_level(CONFIG_MITRA_PTT_GPIO) == 0 && held < 50) { vTaskDelay(pdMS_TO_TICKS(100)); held++; }
    if (held >= 50) {
        ESP_LOGW(TAG, "PTT held at boot: resetting Wi-Fi provisioning");
        mitra_wifi_reset_provisioning();
    }

    if (!mitra_creds_load(&s_creds)) {
        set_state(ST_ERROR);
        ESP_LOGE(TAG, "no device credentials; provision with `mitra provision` + tools/flash_credentials.sh");
        xTaskCreatePinnedToCore(app_task, "app", 6144, NULL, 8, NULL, 0);
        return;
    }
#if CONFIG_MITRA_WAKE_CLOUD
    strlcpy(g_app.wake_mode, "cloud", sizeof(g_app.wake_mode));
#else
    strlcpy(g_app.wake_mode, "ptt", sizeof(g_app.wake_mode));
#endif
    set_state(ST_OFFLINE);
    xTaskCreatePinnedToCore(app_task, "app", 6144, NULL, 8, NULL, 0);
    input_init();
    if (!mitra_wifi_start("MITRA", CONFIG_MITRA_PROV_POP, wifi_cb)) set_state(ST_PROVISIONING);
    ws_link_start(&s_creds);
    uplink_start();
    ESP_LOGI(TAG, "Mitra %s up, free heap %lu", MITRA_FW_VERSION, (unsigned long)esp_get_free_heap_size());
}
