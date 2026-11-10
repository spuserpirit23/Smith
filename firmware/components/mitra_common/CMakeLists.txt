idf_component_register(
    SRCS "wifi.c" "ota.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_wifi esp_netif esp_event nvs_flash wifi_provisioning esp_timer
             esp_http_client app_update mbedtls
)
