idf_component_register(
    SRCS "app_main.c" "audio_io.c" "uplink.c" "playback.c" "ws_link.c" "display.c" "input.c"
    INCLUDE_DIRS "."
    REQUIRES mitra_common esp_driver_i2s esp_driver_i2c esp_driver_gpio esp_lcd nvs_flash json
             esp_timer esp_ringbuf esp_websocket_client
    # Local fallback audio (16 kHz mono s16le). Regenerate spoken clips with `mitra render-clips`.
    EMBED_FILES "clips/chime.pcm" "clips/no_brain.pcm" "clips/repeat.pcm" "clips/no_wifi.pcm" "clips/standby.pcm"
)
