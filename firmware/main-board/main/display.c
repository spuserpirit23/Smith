// SSD1306 128x64 face + status icons, and the status LED (FR-1.2, FR-2.5, FR-5.8).
//
// Top-left icons: filled dot = audio is leaving the device; crossed mic = muted; small camera =
// a camera is capturing; "x" = offline. The red LED mirrors the streaming indicator.
#include "freertos/FreeRTOS.h"
#include <math.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_ssd1306.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/task.h"
#include "mitra.h"

static const char *TAG = "display";
#define W 128
#define H 64

static uint8_t s_fb[W * H / 8];
static esp_lcd_panel_handle_t s_panel;
static bool s_ok;

static void px(int x, int y, bool on)
{
    if (x < 0 || x >= W || y < 0 || y >= H) return;
    uint8_t *b = &s_fb[(y / 8) * W + x];
    if (on) *b |= 1 << (y % 8); else *b &= ~(1 << (y % 8));
}

static void rect(int x, int y, int w, int h, bool fill)
{
    for (int i = 0; i < w; i++)
        for (int j = 0; j < h; j++)
            if (fill || i == 0 || j == 0 || i == w - 1 || j == h - 1) px(x + i, y + j, true);
}

static void disc(int cx, int cy, int r)
{
    for (int y = -r; y <= r; y++)
        for (int x = -r; x <= r; x++)
            if (x * x + y * y <= r * r) px(cx + x, cy + y, true);
}

static void line(int x0, int y0, int x1, int y1)
{
    int dx = abs(x1 - x0), sx = x0 < x1 ? 1 : -1, dy = -abs(y1 - y0), sy = y0 < y1 ? 1 : -1, err = dx + dy;
    while (true) {
        px(x0, y0, true);
        if (x0 == x1 && y0 == y1) break;
        int e2 = 2 * err;
        if (e2 >= dy) { err += dy; x0 += sx; }
        if (e2 <= dx) { err += dx; y0 += sy; }
    }
}

static void smile(int cx, int cy, int r, int depth)
{
    for (int x = -r; x <= r; x++) {
        int y = depth * (r * r - x * x) / (r * r);
        px(cx + x, cy + y, true);
        px(cx + x, cy + y + 1, true);
    }
}

static void draw_face(app_state_t st, uint32_t tick)
{
    const int ly = 26, lx = 40, rx = 88;
    bool blink = (tick % 40) == 0;   // ~every 4 s at 10 fps
    switch (st) {
    case ST_LISTENING:
        disc(lx, ly, 11);
        disc(rx, ly, 11);
        rect(54, 48, 20, 4, true);
        break;
    case ST_PROCESSING: {
        rect(lx - 9, ly - 6, 18, 8, true);
        rect(rx - 9, ly - 6, 18, 8, true);
        int dot = tick % 3;
        for (int i = 0; i < 3; i++) {
            if (i == dot) disc(52 + i * 12, 50, 3); else disc(52 + i * 12, 50, 1);
        }
        break;
    }
    case ST_RESPONDING: {
        disc(lx, ly, 8);
        disc(rx, ly, 8);
        int open = 2 + (int)(5 * fabsf(sinf(tick * 0.9f)));
        rect(54, 50 - open / 2, 20, open, true);
        break;
    }
    case ST_ERROR:
    case ST_DISABLED:
        line(lx - 7, ly - 7, lx + 7, ly + 7); line(lx - 7, ly + 7, lx + 7, ly - 7);
        line(rx - 7, ly - 7, rx + 7, ly + 7); line(rx - 7, ly + 7, rx + 7, ly - 7);
        smile(64, 54, 10, -4);
        break;
    case ST_OFFLINE:
    case ST_PROVISIONING:
        rect(lx - 8, ly, 16, 3, true);
        rect(rx - 8, ly, 16, 3, true);
        rect(58, 50, 12, 2, true);
        break;
    default:   // idle / boot
        if (blink) {
            rect(lx - 8, ly, 16, 2, true);
            rect(rx - 8, ly, 16, 2, true);
        } else {
            disc(lx, ly, 8);
            disc(rx, ly, 8);
        }
        smile(64, 48, 10, 4);
        break;
    }
}

static void draw_icons(void)
{
    int x = 2;
    if (g_app.streaming) { disc(x + 3, 4, 3); x += 10; }
    if (g_app.muted) {      // mic with a slash
        rect(x + 2, 0, 4, 6, false);
        line(x, 8, x + 8, 0);
        x += 12;
    }
    if (g_app.camera_active) {
        rect(x, 2, 9, 6, false);
        disc(x + 4, 5, 1);
        rect(x + 2, 0, 3, 2, true);
        x += 12;
    }
    if (g_app.state == ST_OFFLINE || g_app.state == ST_PROVISIONING) {
        line(W - 9, 0, W - 2, 7);
        line(W - 9, 7, W - 2, 0);
    }
}

static void display_task(void *arg)
{
    uint32_t tick = 0;
    while (true) {
        if (CONFIG_MITRA_LED_GPIO >= 0) {
            bool led = g_app.streaming || (g_app.state == ST_ERROR && (tick & 2));
            gpio_set_level(CONFIG_MITRA_LED_GPIO, led);
        }
        if (s_ok) {
            memset(s_fb, 0, sizeof(s_fb));
            draw_face(g_app.state, tick);
            draw_icons();
            esp_lcd_panel_draw_bitmap(s_panel, 0, 0, W, H, s_fb);
        }
        tick++;
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

void led_init(void)
{
    if (CONFIG_MITRA_LED_GPIO < 0) return;
    gpio_reset_pin(CONFIG_MITRA_LED_GPIO);
    gpio_set_direction(CONFIG_MITRA_LED_GPIO, GPIO_MODE_OUTPUT);
    gpio_set_level(CONFIG_MITRA_LED_GPIO, 0);
}

void display_init(void)
{
    i2c_master_bus_handle_t bus;
    i2c_master_bus_config_t bus_cfg = {
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .i2c_port = I2C_NUM_0,
        .sda_io_num = CONFIG_MITRA_OLED_SDA,
        .scl_io_num = CONFIG_MITRA_OLED_SCL,
        .flags.enable_internal_pullup = true,
    };
    esp_lcd_panel_io_handle_t io;
    esp_lcd_panel_io_i2c_config_t io_cfg = {
        .dev_addr = CONFIG_MITRA_OLED_ADDR,
        .scl_speed_hz = 400000,
        .control_phase_bytes = 1,
        .dc_bit_offset = 6,
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
    };
    esp_lcd_panel_ssd1306_config_t ssd_cfg = {.height = H};
    esp_lcd_panel_dev_config_t panel_cfg = {.bits_per_pixel = 1, .reset_gpio_num = -1, .vendor_config = &ssd_cfg};
    // A missing/miswired display must not stop the robot from talking.
    s_ok = i2c_new_master_bus(&bus_cfg, &bus) == ESP_OK && esp_lcd_new_panel_io_i2c(bus, &io_cfg, &io) == ESP_OK &&
           esp_lcd_new_panel_ssd1306(io, &panel_cfg, &s_panel) == ESP_OK && esp_lcd_panel_reset(s_panel) == ESP_OK &&
           esp_lcd_panel_init(s_panel) == ESP_OK && esp_lcd_panel_disp_on_off(s_panel, true) == ESP_OK;
    if (!s_ok) ESP_LOGE(TAG, "OLED init failed; continuing without display");
    xTaskCreatePinnedToCore(display_task, "display", 3072, NULL, 3, NULL, 0);
}
