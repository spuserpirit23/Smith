// Mitra camera node (ESP32-CAM, PRD §5.5): captures one JPEG only when the backend asks.
//
//   MQTT (over WSS)  mitra/<id>/cmd        <- {"type":"capture","job_id","upload_url","framesize","quality"}
//                    mitra/<id>/state      -> retained {"online","state":"idle|capturing|error","fw","rssi"}
//                    mitra/<id>/telemetry  -> {"type":"capture","job_id","status":"ok|error","bytes","ms","err"}
//   HTTPS POST       upload_url (signed, single use, 30 s) with the JPEG body
//
// No continuous video, no local storage (FR-5.1/5.7). The red LED is on while capturing
// (FR-5.1 indicator) and the sensor is powered down after an idle period.
#include <string.h>

#include "cJSON.h"
#include "driver/gpio.h"
#include "esp_camera.h"
#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "mitra_common.h"
#include "mqtt_client.h"
#include "nvs_flash.h"

#define FW_VERSION "cam-0.1.0"
static const char *TAG = "mitra-cam";

// AI-Thinker ESP32-CAM pin map (see menuconfig to change the indicator LED).
#define PWDN 32
#define XCLK 0
#define SIOD 26
#define SIOC 27
#define Y9 35
#define Y8 34
#define Y7 39
#define Y6 36
#define Y5 21
#define Y4 19
#define Y3 18
#define Y2 5
#define VSYNC 25
#define HREF 23
#define PCLK 22

typedef struct {
    char job_id[24];
    char url[320];
    framesize_t size;
    int quality;
} job_t;

static mitra_creds_t s_creds;
static esp_mqtt_client_handle_t s_mqtt;
static QueueHandle_t s_jobs;
static char s_t_cmd[96], s_t_state[96], s_t_tele[96];
static bool s_cam_on, s_connected;
static int64_t s_last_use_us;

static void led(bool on)
{
#if CONFIG_MITRA_CAM_LED_GPIO >= 0
    gpio_set_level(CONFIG_MITRA_CAM_LED_GPIO, CONFIG_MITRA_CAM_LED_ACTIVE_LOW ? !on : on);
#endif
}

static void publish_state(const char *state)
{
    if (!s_connected) return;
    char msg[160];
    snprintf(msg, sizeof(msg), "{\"online\":true,\"state\":\"%s\",\"fw\":\"%s\",\"rssi\":%d,\"heap\":%lu}", state,
             FW_VERSION, mitra_wifi_rssi(), (unsigned long)esp_get_free_heap_size());
    esp_mqtt_client_publish(s_mqtt, s_t_state, msg, 0, 1, 1);
}

static void publish_result(const char *job, const char *status, size_t bytes, int ms, const char *err)
{
    char msg[200];
    snprintf(msg, sizeof(msg), "{\"type\":\"capture\",\"job_id\":\"%s\",\"status\":\"%s\",\"bytes\":%u,\"ms\":%d,\"err\":\"%s\"}",
             job, status, (unsigned)bytes, ms, err ? err : "");
    esp_mqtt_client_publish(s_mqtt, s_t_tele, msg, 0, 0, 0);
}

static bool camera_on(void)
{
    if (s_cam_on) return true;
    camera_config_t c = {
        .pin_pwdn = PWDN, .pin_reset = -1, .pin_xclk = XCLK, .pin_sccb_sda = SIOD, .pin_sccb_scl = SIOC,
        .pin_d7 = Y9, .pin_d6 = Y8, .pin_d5 = Y7, .pin_d4 = Y6, .pin_d3 = Y5, .pin_d2 = Y4, .pin_d1 = Y3,
        .pin_d0 = Y2, .pin_vsync = VSYNC, .pin_href = HREF, .pin_pclk = PCLK,
        .xclk_freq_hz = 20000000, .ledc_timer = LEDC_TIMER_0, .ledc_channel = LEDC_CHANNEL_0,
        .pixel_format = PIXFORMAT_JPEG, .frame_size = FRAMESIZE_VGA, .jpeg_quality = 12,
        .fb_count = 1, .fb_location = CAMERA_FB_IN_PSRAM, .grab_mode = CAMERA_GRAB_LATEST,
    };
    esp_err_t err = esp_camera_init(&c);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "camera init failed: %s (check module, PSRAM and 5 V supply)", esp_err_to_name(err));
        return false;
    }
    s_cam_on = true;
    return true;
}

static void camera_off(void)
{
    if (!s_cam_on) return;
    esp_camera_deinit();
    gpio_set_direction(PWDN, GPIO_MODE_OUTPUT);
    gpio_set_level(PWDN, 1);    // sensor power-down between captures (FR-5.7)
    s_cam_on = false;
    ESP_LOGI(TAG, "sensor powered down (idle)");
}

static esp_err_t upload(const char *url, const uint8_t *jpeg, size_t len, int *status)
{
    esp_http_client_config_t cfg = {.url = url, .method = HTTP_METHOD_POST, .timeout_ms = 8000,
                                    .crt_bundle_attach = esp_crt_bundle_attach};
    esp_http_client_handle_t h = esp_http_client_init(&cfg);
    esp_http_client_set_header(h, "Content-Type", "image/jpeg");
    esp_err_t err = esp_http_client_open(h, len);
    if (err == ESP_OK) {
        int w = esp_http_client_write(h, (const char *)jpeg, len);
        if (w != (int)len) err = ESP_FAIL;
        esp_http_client_fetch_headers(h);
        *status = esp_http_client_get_status_code(h);
    }
    esp_http_client_cleanup(h);
    return err;
}

static void capture_task(void *arg)
{
    job_t job;
    esp_task_wdt_add(NULL);
    while (true) {
        esp_task_wdt_reset();
        if (xQueueReceive(s_jobs, &job, pdMS_TO_TICKS(1000)) != pdTRUE) {
            if (s_cam_on && esp_timer_get_time() - s_last_use_us > (int64_t)CONFIG_MITRA_CAM_IDLE_OFF_S * 1000000) {
                camera_off();
            }
            continue;
        }
        int64_t t0 = esp_timer_get_time();
        led(true);
        publish_state("capturing");
        const char *err = NULL;
        size_t bytes = 0;
        if (!camera_on()) {
            err = "camera_init";
        } else {
            sensor_t *s = esp_camera_sensor_get();
            if (s) {
                s->set_framesize(s, job.size);
                s->set_quality(s, job.quality);
            }
            // The first frame after power-up / a size change is often under-exposed: drop one.
            camera_fb_t *fb = esp_camera_fb_get();
            if (fb) esp_camera_fb_return(fb);
            fb = esp_camera_fb_get();
            if (!fb) {
                err = "capture";
            } else {
                int status = 0;
                bytes = fb->len;
                if (upload(job.url, fb->buf, fb->len, &status) != ESP_OK || status != 200) err = "upload";
                esp_camera_fb_return(fb);   // frame is discarded immediately (FR-5.5)
            }
        }
        s_last_use_us = esp_timer_get_time();
        int ms = (int)((s_last_use_us - t0) / 1000);
        led(false);
        publish_result(job.job_id, err ? "error" : "ok", bytes, ms, err);
        publish_state(err ? "error" : "idle");
        ESP_LOGI(TAG, "job %s %s (%u bytes, %d ms)", job.job_id, err ? err : "ok", (unsigned)bytes, ms);
    }
}

static framesize_t parse_size(const char *s)
{
    if (!s) return FRAMESIZE_VGA;
    if (!strcmp(s, "QVGA")) return FRAMESIZE_QVGA;
    if (!strcmp(s, "SVGA")) return FRAMESIZE_SVGA;
    if (!strcmp(s, "XGA")) return FRAMESIZE_XGA;
    return FRAMESIZE_VGA;
}

static void ota_report(const char *status, const char *version, const char *err)
{
    char msg[160];
    snprintf(msg, sizeof(msg), "{\"type\":\"ota\",\"status\":\"%s\",\"version\":\"%s\",\"err\":\"%s\"}", status, version,
             err ? err : "");
    esp_mqtt_client_publish(s_mqtt, s_t_tele, msg, 0, 0, 0);
}

static void handle_cmd(const char *data, int len)
{
    cJSON *m = cJSON_ParseWithLength(data, len);
    if (!m) return;
    const char *type = cJSON_GetStringValue(cJSON_GetObjectItem(m, "type"));
    if (type && !strcmp(type, "capture")) {
        job_t j = {0};
        const char *id = cJSON_GetStringValue(cJSON_GetObjectItem(m, "job_id"));
        const char *url = cJSON_GetStringValue(cJSON_GetObjectItem(m, "upload_url"));
        cJSON *q = cJSON_GetObjectItem(m, "quality");
        if (id && url && strlen(url) < sizeof(j.url)) {
            strlcpy(j.job_id, id, sizeof(j.job_id));
            strlcpy(j.url, url, sizeof(j.url));
            j.size = parse_size(cJSON_GetStringValue(cJSON_GetObjectItem(m, "framesize")));
            j.quality = cJSON_IsNumber(q) ? q->valueint : 12;
            if (xQueueSend(s_jobs, &j, 0) != pdTRUE) publish_result(j.job_id, "error", 0, 0, "busy");
        }
    } else if (type && !strcmp(type, "ota")) {
        mitra_ota_start(cJSON_GetStringValue(cJSON_GetObjectItem(m, "url")),
                        cJSON_GetStringValue(cJSON_GetObjectItem(m, "sha256")),
                        cJSON_GetStringValue(cJSON_GetObjectItem(m, "version")), ota_report);
    } else if (type && !strcmp(type, "reboot")) {
        esp_restart();
    } else if (type && !strcmp(type, "disable")) {
        ESP_LOGW(TAG, "disabled by backend");
        esp_mqtt_client_stop(s_mqtt);
    } else if (type && !strcmp(type, "identify")) {
        for (int i = 0; i < 6; i++) { led(i % 2 == 0); vTaskDelay(pdMS_TO_TICKS(200)); }
    }
    cJSON_Delete(m);
}

static void on_mqtt(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    esp_mqtt_event_handle_t e = data;
    switch ((esp_mqtt_event_id_t)id) {
    case MQTT_EVENT_CONNECTED:
        s_connected = true;
        esp_mqtt_client_subscribe(s_mqtt, s_t_cmd, 1);
        publish_state("idle");
        mitra_ota_confirm_running_app();
        break;
    case MQTT_EVENT_DISCONNECTED:
        s_connected = false;
        break;
    case MQTT_EVENT_DATA:
        if (e->current_data_offset == 0 && e->data_len == e->total_data_len) handle_cmd(e->data, e->data_len);
        break;
    default:
        break;
    }
}

static void wifi_cb(bool up)
{
    static bool started;
    if (up && !started) {
        started = true;
        esp_mqtt_client_start(s_mqtt);
    }
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
#if CONFIG_MITRA_CAM_LED_GPIO >= 0
    gpio_reset_pin(CONFIG_MITRA_CAM_LED_GPIO);
    gpio_set_direction(CONFIG_MITRA_CAM_LED_GPIO, GPIO_MODE_OUTPUT);
    led(false);
#endif
    if (!mitra_creds_load(&s_creds) || !s_creds.mqtt_url[0]) {
        ESP_LOGE(TAG, "no credentials; provision with `mitra provision <id> --kind camera ...`");
        return;
    }
    snprintf(s_t_cmd, sizeof(s_t_cmd), "mitra/%s/cmd", s_creds.device_id);
    snprintf(s_t_state, sizeof(s_t_state), "mitra/%s/state", s_creds.device_id);
    snprintf(s_t_tele, sizeof(s_t_tele), "mitra/%s/telemetry", s_creds.device_id);

    esp_mqtt_client_config_t cfg = {
        .broker.address.uri = s_creds.mqtt_url,
        .broker.verification.crt_bundle_attach = esp_crt_bundle_attach,
        .credentials.username = s_creds.device_id,
        .credentials.client_id = s_creds.device_id,
        .credentials.authentication.password = s_creds.secret,
        .session.keepalive = 30,
        .session.last_will = {.topic = s_t_state, .msg = "{\"online\":false,\"state\":\"offline\"}", .qos = 1,
                              .retain = 1},
        .network.reconnect_timeout_ms = 5000,
        .buffer.size = 2048,
    };
    s_mqtt = esp_mqtt_client_init(&cfg);
    esp_mqtt_client_register_event(s_mqtt, ESP_EVENT_ANY_ID, on_mqtt, NULL);
    s_jobs = xQueueCreate(2, sizeof(job_t));
    xTaskCreatePinnedToCore(capture_task, "capture", 6144, NULL, 5, NULL, 1);
    mitra_wifi_start("MITRA-CAM", CONFIG_MITRA_CAM_PROV_POP, wifi_cb);
    ESP_LOGI(TAG, "%s up as %s", FW_VERSION, s_creds.device_id);
}
