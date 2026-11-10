// OTA with SHA-256 verification and automatic rollback (PRD §4.1 "OTA updates with rollback").
//
// The backend sends {"type":"ota","url":...,"sha256":...,"version":...} (over the WebSocket for the
// main board, over MQTT for cameras). We stream the image into
// the inactive slot while hashing it, refuse it on mismatch, then reboot. The bootloader keeps the
// new image in "pending verify" until mitra_ota_confirm_running_app() runs after the device reaches
// the backend; if the new firmware crashes or never reaches the backend, the next reset rolls back.
#include <string.h>

#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/sha256.h"
#include "mitra_common.h"

static const char *TAG = "ota";

typedef struct {
    char url[384];
    char sha[65];
    char version[24];
    mitra_ota_report_t report;
} ota_req_t;

static volatile bool s_busy;


static void ota_task(void *arg)
{
    ota_req_t *r = (ota_req_t *)arg;
    const esp_partition_t *part = esp_ota_get_next_update_partition(NULL);
    esp_ota_handle_t h = 0;
    esp_http_client_handle_t http = NULL;
    const char *err = NULL;
    mbedtls_sha256_context sha;
    mbedtls_sha256_init(&sha);
    mbedtls_sha256_starts(&sha, 0);
    static uint8_t buf[2048];

    esp_http_client_config_t cfg = {.url = r->url, .crt_bundle_attach = esp_crt_bundle_attach, .timeout_ms = 15000};
    http = esp_http_client_init(&cfg);
    if (esp_http_client_open(http, 0) != ESP_OK) { err = "http_open"; goto done; }
    int64_t total = esp_http_client_fetch_headers(http);
    if (esp_http_client_get_status_code(http) != 200) { err = "http_status"; goto done; }
    if (!part || esp_ota_begin(part, total > 0 ? total : OTA_SIZE_UNKNOWN, &h) != ESP_OK) { err = "ota_begin"; goto done; }
    if (r->report) r->report("downloading", r->version, NULL);
    while (true) {
        int n = esp_http_client_read(http, (char *)buf, sizeof(buf));
        if (n < 0) { err = "read"; goto done; }
        if (n == 0) break;
        mbedtls_sha256_update(&sha, buf, n);
        if (esp_ota_write(h, buf, n) != ESP_OK) { err = "ota_write"; goto done; }
    }
    uint8_t digest[32];
    char hex[65];
    mbedtls_sha256_finish(&sha, digest);
    for (int i = 0; i < 32; i++) sprintf(hex + i * 2, "%02x", digest[i]);
    if (strcasecmp(hex, r->sha) != 0) { err = "sha256_mismatch"; goto done; }
    if (esp_ota_end(h) != ESP_OK) { h = 0; err = "image_invalid"; goto done; }
    h = 0;
    if (esp_ota_set_boot_partition(part) != ESP_OK) { err = "set_boot"; goto done; }
    if (r->report) r->report("rebooting", r->version, NULL);
    ESP_LOGW(TAG, "OTA %s verified, rebooting", r->version);
    vTaskDelay(pdMS_TO_TICKS(500));
    esp_restart();

done:
    if (h) esp_ota_abort(h);
    if (http) esp_http_client_cleanup(http);
    mbedtls_sha256_free(&sha);
    ESP_LOGE(TAG, "OTA failed: %s", err);
    if (r->report) r->report("failed", r->version, err);
    free(r);
    s_busy = false;
    vTaskDelete(NULL);
}

void mitra_ota_start(const char *url, const char *sha256_hex, const char *version, mitra_ota_report_t report)
{
    if (s_busy || !url || !sha256_hex || strlen(sha256_hex) != 64) return;
    ota_req_t *r = calloc(1, sizeof(*r));
    if (!r) return;
    strlcpy(r->url, url, sizeof(r->url));
    strlcpy(r->sha, sha256_hex, sizeof(r->sha));
    strlcpy(r->version, version ? version : "?", sizeof(r->version));
    r->report = report;
    s_busy = true;
    xTaskCreate(ota_task, "ota", 8192, r, 4, NULL);
}

void mitra_ota_confirm_running_app(void)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t st;
    if (esp_ota_get_state_partition(running, &st) == ESP_OK && st == ESP_OTA_IMG_PENDING_VERIFY) {
        ESP_LOGI(TAG, "new firmware reached the backend; cancelling rollback");
        esp_ota_mark_app_valid_cancel_rollback();
    }
}
