// Wi-Fi with SoftAP provisioning and exponential reconnect backoff, plus credential loading.
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "mitra_common.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "wifi_provisioning/manager.h"
#include "wifi_provisioning/scheme_softap.h"

static const char *TAG = "wifi";
static int s_retry;
static esp_timer_handle_t s_reconnect_timer;
static mitra_wifi_cb_t s_cb;

bool mitra_creds_load(mitra_creds_t *out)
{
    memset(out, 0, sizeof(*out));
    esp_err_t err = nvs_flash_init_partition("mitra_cfg");
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "mitra_cfg partition missing (%s). Flash it with tools/flash_credentials.sh",
                 esp_err_to_name(err));
        return false;
    }
    nvs_handle_t h;
    if (nvs_open_from_partition("mitra_cfg", "mitra", NVS_READONLY, &h) != ESP_OK) return false;
    struct {
        const char *key;
        char *dst;
        size_t cap;
        bool required;
    } fields[] = {
        {"device_id", out->device_id, sizeof(out->device_id), true},
        {"secret", out->secret, sizeof(out->secret), true},
        {"ws_url", out->ws_url, sizeof(out->ws_url), false},
        {"mqtt_url", out->mqtt_url, sizeof(out->mqtt_url), false},
        {"api_base", out->api_base, sizeof(out->api_base), false},
    };
    bool ok = true;
    for (size_t i = 0; i < sizeof(fields) / sizeof(fields[0]); i++) {
        size_t len = fields[i].cap;
        if (nvs_get_str(h, fields[i].key, fields[i].dst, &len) != ESP_OK && fields[i].required) ok = false;
    }
    nvs_close(h);
    if (ok) ESP_LOGI(TAG, "device id %s", out->device_id);
    return ok;
}

static void reconnect_cb(void *arg)
{
    esp_wifi_connect();
}

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_cb) s_cb(false);
        int delay_ms = 1000 << (s_retry < 5 ? s_retry : 5);   // 1, 2, 4 ... 30 s
        if (delay_ms > 30000) delay_ms = 30000;
        s_retry++;
        // Never block the default event loop; reconnect from a one-shot timer instead.
        esp_timer_stop(s_reconnect_timer);
        esp_timer_start_once(s_reconnect_timer, (uint64_t)delay_ms * 1000);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        s_retry = 0;
        if (s_cb) s_cb(true);
    } else if (base == WIFI_PROV_EVENT && id == WIFI_PROV_END) {
        wifi_prov_mgr_deinit();
    }
}

void mitra_wifi_reset_provisioning(void)
{
    wifi_prov_mgr_config_t cfg = {.scheme = wifi_prov_scheme_softap,
                                  .scheme_event_handler = WIFI_PROV_EVENT_HANDLER_NONE};
    if (wifi_prov_mgr_init(cfg) == ESP_OK) {
        wifi_prov_mgr_reset_provisioning();
        wifi_prov_mgr_deinit();
    }
}

int mitra_wifi_rssi(void)
{
    wifi_ap_record_t ap = {0};
    return esp_wifi_sta_get_ap_info(&ap) == ESP_OK ? ap.rssi : 0;
}

bool mitra_wifi_start(const char *ap_prefix, const char *pop, mitra_wifi_cb_t cb)
{
    s_cb = cb;
    const esp_timer_create_args_t targs = {.callback = reconnect_cb, .name = "wifi_reconnect"};
    ESP_ERROR_CHECK(esp_timer_create(&targs, &s_reconnect_timer));
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    esp_netif_create_default_wifi_ap();
    wifi_init_config_t wcfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&wcfg));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_PROV_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));

    wifi_prov_mgr_config_t pcfg = {.scheme = wifi_prov_scheme_softap,
                                   .scheme_event_handler = WIFI_PROV_EVENT_HANDLER_NONE};
    ESP_ERROR_CHECK(wifi_prov_mgr_init(pcfg));
    bool provisioned = false;
    ESP_ERROR_CHECK(wifi_prov_mgr_is_provisioned(&provisioned));
    if (!provisioned) {
        uint8_t mac[6];
        esp_read_mac(mac, ESP_MAC_WIFI_STA);
        char service[24];
        snprintf(service, sizeof(service), "%s-%02X%02X%02X", ap_prefix, mac[3], mac[4], mac[5]);
        ESP_LOGW(TAG, "not provisioned: join SoftAP %s, PoP '%s' in the ESP SoftAP Prov app", service, pop);
        ESP_ERROR_CHECK(wifi_prov_mgr_start_provisioning(WIFI_PROV_SECURITY_1, pop, service, NULL));
    } else {
        wifi_prov_mgr_deinit();
        ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
        ESP_ERROR_CHECK(esp_wifi_start());
    }
    // Modem sleep keeps Wi-Fi associated with lower average current; latency impact is small.
    esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
    return provisioned;
}
