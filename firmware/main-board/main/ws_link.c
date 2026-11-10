// TLS WebSocket to the backend (protocol v1, docs/PROTOCOL.md).
// One connection carries audio both ways and all control messages. We manage reconnects
// ourselves to get exponential backoff and a long pause after a kill switch / auth rejection.
#include "freertos/FreeRTOS.h"
#include <string.h>

#include "esp_crt_bundle.h"
#include "esp_log.h"
#include "esp_websocket_client.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "mitra.h"

static const char *TAG = "ws";

#define BIT_UP BIT0
#define BIT_DOWN BIT1

static esp_websocket_client_handle_t s_client;
static EventGroupHandle_t s_ev;
static char s_headers[256];
static char *s_text_buf;          // reassembly for fragmented text frames
static size_t s_text_cap;
static volatile uint32_t s_pause_s;
static volatile bool s_was_upgraded;

static void on_ws_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    esp_websocket_event_data_t *d = (esp_websocket_event_data_t *)data;
    switch (id) {
    case WEBSOCKET_EVENT_CONNECTED:
        s_was_upgraded = true;
        g_app.ws_connected = true;
        xEventGroupSetBits(s_ev, BIT_UP);
        app_post(EV_WS_UP, 0, 0, NULL);
        break;
    case WEBSOCKET_EVENT_DISCONNECTED:
    case WEBSOCKET_EVENT_CLOSED:
        if (g_app.ws_connected) app_post(EV_WS_DOWN, 0, 0, NULL);
        g_app.ws_connected = false;
        xEventGroupSetBits(s_ev, BIT_DOWN);
        break;
    case WEBSOCKET_EVENT_ERROR:
        xEventGroupSetBits(s_ev, BIT_DOWN);
        break;
    case WEBSOCKET_EVENT_DATA:
        // Server frames are <= 3200 bytes (< buffer_size), so continuation frames (op 0) are not
        // expected; treat them by whatever we were assembling.
        if (d->op_code == 0x2 || (d->op_code == 0x0 && s_text_cap == 0)) {
            playback_stream_push((const uint8_t *)d->data_ptr, d->data_len);
        } else if (d->op_code == 0x1 || d->op_code == 0x0) {
            size_t need = d->payload_len + 1;
            if (d->payload_offset == 0) {
                free(s_text_buf);
                s_text_buf = malloc(need);
                s_text_cap = s_text_buf ? need : 0;
            }
            if (!s_text_buf || d->payload_offset + d->data_len > d->payload_len) break;
            memcpy(s_text_buf + d->payload_offset, d->data_ptr, d->data_len);
            if (d->payload_offset + d->data_len == d->payload_len) {
                s_text_buf[d->payload_len] = 0;
                app_post(EV_WS_JSON, 0, 0, s_text_buf);   // ownership passes to the app task
                s_text_buf = NULL;
                s_text_cap = 0;
            }
        } else if (d->op_code == 0x8 && d->data_len >= 2) {
            uint16_t code = ((uint8_t)d->data_ptr[0] << 8) | (uint8_t)d->data_ptr[1];
            ESP_LOGW(TAG, "server closed with %u", code);
            if (code == 4401 || code == 4403) app_post(EV_WS_REJECTED, code, 0, NULL);
        }
        break;
    default:
        break;
    }
}

static void link_task(void *arg)
{
    uint32_t backoff_s = 1;
    while (true) {
        if (s_pause_s) {
            uint32_t p = s_pause_s;
            s_pause_s = 0;
            ESP_LOGW(TAG, "pausing reconnects for %lu s", (unsigned long)p);
            vTaskDelay(pdMS_TO_TICKS(p * 1000));
        }
        xEventGroupClearBits(s_ev, BIT_UP | BIT_DOWN);
        s_was_upgraded = false;
        esp_websocket_client_start(s_client);
        EventBits_t b = xEventGroupWaitBits(s_ev, BIT_UP | BIT_DOWN, pdFALSE, pdFALSE, pdMS_TO_TICKS(15000));
        if (b & BIT_UP) {
            backoff_s = 1;
            xEventGroupWaitBits(s_ev, BIT_DOWN, pdTRUE, pdFALSE, portMAX_DELAY);
        }
        esp_websocket_client_stop(s_client);
        if (!s_was_upgraded) {
            // Handshake refused before upgrade = bad credentials or disabled device (HTTP 403).
            backoff_s = backoff_s < 60 ? backoff_s * 2 : 300;
        }
        ESP_LOGI(TAG, "reconnecting in %lu s", (unsigned long)backoff_s);
        vTaskDelay(pdMS_TO_TICKS(backoff_s * 1000));
        if (backoff_s < 30) backoff_s *= 2;
    }
}

void ws_link_start(const mitra_creds_t *c)
{
    s_ev = xEventGroupCreate();
    snprintf(s_headers, sizeof(s_headers), "X-Device-Id: %s\r\nAuthorization: Bearer %s\r\n", c->device_id,
             c->secret);
    esp_websocket_client_config_t cfg = {
        .uri = c->ws_url,
        .headers = s_headers,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .disable_auto_reconnect = true,
        .network_timeout_ms = 10000,
        .ping_interval_sec = 15,
        .pingpong_timeout_sec = 30,
        .buffer_size = 4096,
        .task_stack = 6144,
        .task_prio = 7,
    };
    s_client = esp_websocket_client_init(&cfg);
    esp_websocket_register_events(s_client, WEBSOCKET_EVENT_ANY, on_ws_event, NULL);
    xTaskCreatePinnedToCore(link_task, "ws_link", 3072, NULL, 5, NULL, 0);
}

bool ws_link_send_json(const char *json)
{
    if (!g_app.ws_connected) return false;
    return esp_websocket_client_send_text(s_client, json, strlen(json), pdMS_TO_TICKS(1000)) >= 0;
}

bool ws_link_send_audio(const uint8_t *pcm, size_t len)
{
    if (!g_app.ws_connected) return false;
    return esp_websocket_client_send_bin(s_client, (const char *)pcm, len, pdMS_TO_TICKS(500)) >= 0;
}

void ws_link_pause(uint32_t seconds)
{
    s_pause_s = seconds;
}
