// Push-to-talk button (FR-2.8) and optional hardware mute switch (FR-2.5). Polled + debounced.
#include "freertos/FreeRTOS.h"
#include "driver/gpio.h"
#include "freertos/task.h"
#include "mitra.h"

static void cfg_input(int gpio)
{
    if (gpio < 0) return;
    gpio_config_t c = {.pin_bit_mask = 1ULL << gpio, .mode = GPIO_MODE_INPUT, .pull_up_en = GPIO_PULLUP_ENABLE};
    gpio_config(&c);
}

static void input_task(void *arg)
{
    int ptt_stable = 1, ptt_count = 0;
    int mute_stable = 1, mute_count = 0;
    if (CONFIG_MITRA_MUTE_GPIO >= 0 && gpio_get_level(CONFIG_MITRA_MUTE_GPIO) == 0) {
        mute_stable = 0;
        app_post(EV_MUTE_ON, 0, 0, NULL);
    }
    while (true) {
        int ptt = gpio_get_level(CONFIG_MITRA_PTT_GPIO);
        if (ptt != ptt_stable && ++ptt_count >= 3) {          // 60 ms debounce
            ptt_stable = ptt;
            ptt_count = 0;
            app_post(ptt == 0 ? EV_PTT_DOWN : EV_PTT_UP, 0, 0, NULL);
        } else if (ptt == ptt_stable) {
            ptt_count = 0;
        }
        if (CONFIG_MITRA_MUTE_GPIO >= 0) {
            int m = gpio_get_level(CONFIG_MITRA_MUTE_GPIO);
            if (m != mute_stable && ++mute_count >= 5) {
                mute_stable = m;
                mute_count = 0;
                app_post(m == 0 ? EV_MUTE_ON : EV_MUTE_OFF, 0, 0, NULL);
            } else if (m == mute_stable) {
                mute_count = 0;
            }
        }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

void input_init(void)
{
    cfg_input(CONFIG_MITRA_PTT_GPIO);
    cfg_input(CONFIG_MITRA_MUTE_GPIO);
    xTaskCreate(input_task, "input", 2560, NULL, 4, NULL);
}
