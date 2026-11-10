// Shared by the main board and the ESP32-CAM nodes: device credentials, Wi-Fi with SoftAP
// provisioning and reconnect backoff, and SHA-256-verified OTA with rollback.
#pragma once

#include <stdbool.h>

typedef struct {
    char device_id[48];
    char secret[64];
    char ws_url[160];     // main board only
    char mqtt_url[160];
    char api_base[160];
} mitra_creds_t;

// Reads the 'mitra' namespace of the 'mitra_cfg' NVS partition (tools/flash_credentials.sh).
bool mitra_creds_load(mitra_creds_t *out);

typedef void (*mitra_wifi_cb_t)(bool up);
// Starts Wi-Fi. If no network is stored, starts SoftAP provisioning "<prefix>-XXXXXX" with the
// given proof-of-possession (use Espressif's "ESP SoftAP Prov" app). Returns true if provisioned.
bool mitra_wifi_start(const char *ap_prefix, const char *pop, mitra_wifi_cb_t cb);
void mitra_wifi_reset_provisioning(void);
int mitra_wifi_rssi(void);

typedef void (*mitra_ota_report_t)(const char *status, const char *version, const char *err);
void mitra_ota_start(const char *url, const char *sha256_hex, const char *version, mitra_ota_report_t report);
void mitra_ota_confirm_running_app(void);
