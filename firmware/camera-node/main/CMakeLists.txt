idf_component_register(
    SRCS "camera_main.c"
    INCLUDE_DIRS "."
    REQUIRES mitra_common esp32-camera mqtt esp_http_client esp_driver_gpio nvs_flash json esp_timer
)
