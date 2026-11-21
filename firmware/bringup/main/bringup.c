// Mitra mic + speaker test. No Wi-Fi, no backend.
//
//   At boot : 3 beeps on the speaker (speaker check).
//   Idle    : live mic level bar on the OLED; level printed to the serial log.
//   Hold BOOT and talk, release: the board plays back what it recorded (mic -> speaker check).
//
// Each recording prints its level and the MITRA_MIC_GAIN_SHIFT that suits it, for the main
// firmware's menuconfig. Pins are the ones confirmed on this PCB (same as main-board sdkconfig).
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define MIC_SCK 26
#define MIC_WS 25
#define MIC_SD 32
#define SPK_BCLK 14
#define SPK_LRC 27
#define SPK_DIN 33
#define OLED_SDA 4
#define OLED_SCL 15
#define OLED_ADDR 0x3C
#define BTN 0          // BOOT button, active low

#define RATE 16000
#define BLOCK 256      // stereo frames per mic read (16 ms)

static const char *TAG = "test";

// ---------------------------------------------------------------------------------------------
// OLED (SSD1306 128x64, raw I2C)

static i2c_master_dev_handle_t s_oled;
static uint8_t s_fb[1 + 128 * 8];   // [0] = 0x40 data prefix

static bool oled_cmds(const uint8_t *cmds, size_t n)
{
    uint8_t buf[32];
    buf[0] = 0x00;
    memcpy(buf + 1, cmds, n);
    return i2c_master_transmit(s_oled, buf, n + 1, 50) == ESP_OK;
}

static void oled_flush(void)
{
    if (!s_oled) return;
    static const uint8_t win[] = {0x21, 0, 127, 0x22, 0, 7};
    oled_cmds(win, sizeof(win));
    s_fb[0] = 0x40;
    i2c_master_transmit(s_oled, s_fb, sizeof(s_fb), 100);
}

static void fb_clear(void) { memset(s_fb + 1, 0, sizeof(s_fb) - 1); }

static void fb_rect(int x, int y, int w, int h, bool fill)
{
    for (int i = x; i < x + w; i++)
        for (int j = y; j < y + h; j++)
            if (i >= 0 && i < 128 && j >= 0 && j < 64 &&
                (fill || i == x || j == y || i == x + w - 1 || j == y + h - 1))
                s_fb[1 + (j / 8) * 128 + i] |= 1 << (j % 8);
}

static void oled_init(void)
{
    i2c_master_bus_config_t bc = {
        .i2c_port = I2C_NUM_0, .sda_io_num = OLED_SDA, .scl_io_num = OLED_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT, .glitch_ignore_cnt = 7, .flags.enable_internal_pullup = true,
    };
    i2c_master_bus_handle_t bus;
    i2c_device_config_t dc = {.dev_addr_length = I2C_ADDR_BIT_LEN_7, .device_address = OLED_ADDR,
                              .scl_speed_hz = 400000};
    if (i2c_new_master_bus(&bc, &bus) != ESP_OK || i2c_master_bus_add_device(bus, &dc, &s_oled) != ESP_OK) {
        ESP_LOGW(TAG, "OLED init failed (continuing without it)");
        s_oled = NULL;
        return;
    }
    static const uint8_t init[] = {0xAE, 0xD5, 0x80, 0xA8, 0x3F, 0xD3, 0x00, 0x40, 0x8D, 0x14, 0x20, 0x00,
                                   0xA1, 0xC8, 0xDA, 0x12, 0x81, 0xCF, 0xD9, 0xF1, 0xDB, 0x40, 0xA4, 0xA6, 0xAF};
    if (!oled_cmds(init, sizeof(init))) {
        ESP_LOGW(TAG, "OLED not answering at 0x%02X on SDA=%d SCL=%d", OLED_ADDR, OLED_SDA, OLED_SCL);
        s_oled = NULL;
    }
}

// ---------------------------------------------------------------------------------------------
// Audio

static i2s_chan_handle_t s_rx, s_tx;
static int32_t s_raw[BLOCK * 2];

static void audio_init(void)
{
    i2s_chan_config_t rc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    rc.dma_desc_num = 8;      // 8 x 512 frames = 256 ms of buffering against slow loop iterations
    rc.dma_frame_num = 512;
    ESP_ERROR_CHECK(i2s_new_channel(&rc, NULL, &s_rx));
    i2s_std_config_t rx = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {.mclk = I2S_GPIO_UNUSED, .bclk = MIC_SCK, .ws = MIC_WS, .dout = I2S_GPIO_UNUSED, .din = MIC_SD},
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_rx, &rx));
    ESP_ERROR_CHECK(i2s_channel_enable(s_rx));

    i2s_chan_config_t tc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
    tc.auto_clear = true;   // silence (not a buzz) when nothing is being written
    ESP_ERROR_CHECK(i2s_new_channel(&tc, &s_tx, NULL));
    i2s_std_config_t tx = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(RATE),
        // Stereo, each sample written to both slots: classic-ESP32 16-bit MONO can swap sample
        // pairs, which makes speech sound rough/blurry. The amp (SD pin floating) plays (L+R)/2.
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {.mclk = I2S_GPIO_UNUSED, .bclk = SPK_BCLK, .ws = SPK_LRC, .dout = SPK_DIN, .din = I2S_GPIO_UNUSED},
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_tx, &tx));
    ESP_ERROR_CHECK(i2s_channel_enable(s_tx));
    ESP_LOGI(TAG, "mic SCK=%d WS=%d SD=%d | speaker BCLK=%d LRC=%d DIN=%d", MIC_SCK, MIC_WS, MIC_SD, SPK_BCLK,
             SPK_LRC, SPK_DIN);
}

static void spk_write(const int16_t *pcm, int n)
{
    static int16_t st[512];   // interleaved L,R
    while (n > 0) {
        int m = n > 256 ? 256 : n;
        for (int i = 0; i < m; i++) st[2 * i] = st[2 * i + 1] = pcm[i];
        size_t w;
        i2s_channel_write(s_tx, st, m * 2 * sizeof(int16_t), &w, portMAX_DELAY);
        pcm += m;
        n -= m;
    }
}

static void beep(int ms)
{
    static int16_t buf[256];
    static float ph;
    int total = RATE * ms / 1000;
    while (total > 0) {
        int n = total > 256 ? 256 : total;
        for (int i = 0; i < n; i++) {
            buf[i] = (int16_t)(28000 * sinf(ph));   // near full scale: the loudest this amp can be
            ph += 2.0f * (float)M_PI * 1000.0f / RATE;
            if (ph > 2.0f * (float)M_PI) ph -= 2.0f * (float)M_PI;
        }
        spk_write(buf, n);
        total -= n;
    }
}

static void silence(int ms)
{
    static const int16_t z[256];
    int total = RATE * ms / 1000;
    while (total > 0) {
        int n = total > 256 ? 256 : total;
        spk_write(z, n);
        total -= n;
    }
}

// Reads one block; returns per-slot RMS in 24-bit units and copies raw 32-bit samples of each slot.
static int mic_block(double rms[2], int32_t *slot0, int32_t *slot1)
{
    size_t got = 0;
    esp_err_t err = i2s_channel_read(s_rx, s_raw, sizeof(s_raw), &got, pdMS_TO_TICKS(200));
    static int errs;
    if (err != ESP_OK && errs++ < 3)
        ESP_LOGW(TAG, "i2s read returned %s with %u bytes (keeping the data)", esp_err_to_name(err), (unsigned)got);
    int frames = got / 8;   // keep whatever arrived, even if the call reported an error
    if (frames == 0) return 0;
    double sq[2] = {0};
    for (int i = 0; i < frames; i++) {
        int32_t a = s_raw[2 * i], b = s_raw[2 * i + 1];
        if (slot0) slot0[i] = a;
        if (slot1) slot1[i] = b;
        double va = a >> 8, vb = b >> 8;
        sq[0] += va * va;
        sq[1] += vb * vb;
    }
    rms[0] = frames ? sqrt(sq[0] / frames) : 0;
    rms[1] = frames ? sqrt(sq[1] / frames) : 0;
    return frames;
}

static double dbfs(double rms24) { return rms24 > 1 ? 20.0 * log10(rms24 / 8388608.0) : -140; }

static void draw_level(double db, bool recording)
{
    int w = (int)((db + 90.0) * 128.0 / 80.0);   // -90..-10 dBFS -> 0..128 px
    if (w < 0) w = 0;
    if (w > 128) w = 128;
    fb_clear();
    fb_rect(0, 20, w, 24, true);
    if (recording) fb_rect(0, 0, 128, 64, false);   // frame = recording
    oled_flush();
}

// ---------------------------------------------------------------------------------------------

// Logs every BOOT button change, so we can see whether the button reaches GPIO0 at all.
static void watch_button(void)
{
    static int last = -1;
    int lv = gpio_get_level(BTN);
    if (lv != last) {
        ESP_LOGI(TAG, "BOOT button (GPIO%d) is now %s", BTN, lv == 0 ? "PRESSED" : "released");
        last = lv;
    }
}

// auto_ms > 0: record exactly that long (hands-free). auto_ms == 0: record while BOOT is held.
static void record_and_play(int auto_ms)
{
    // Record only the LEFT slot (the mic's L/R pin is grounded), as 16-bit: raw 32-bit word >> 16.
    // 2 bytes/sample keeps 4 s under 128 KB; the ESP32 has no 128 KB+ free block for more.
    size_t largest = heap_caps_get_largest_free_block(MALLOC_CAP_8BIT);
    int max_frames = largest > 16384 ? (int)((largest - 16384) / sizeof(int16_t)) : 0;
    if (max_frames > RATE * 4) max_frames = RATE * 4;
    if (max_frames < RATE / 2) {
        ESP_LOGE(TAG, "not enough RAM to record (largest free block %u bytes)", (unsigned)largest);
        return;
    }
    int16_t *src = malloc((size_t)max_frames * sizeof(int16_t));
    if (!src) { ESP_LOGE(TAG, "malloc failed"); return; }

    if (auto_ms > 0 && auto_ms * (RATE / 1000) < max_frames) max_frames = auto_ms * (RATE / 1000);
    ESP_LOGI(TAG, "RECORDING %.1f s... TALK NOW", max_frames / (double)RATE);
    int n = 0, reads = 0, empty = 0;
    uint32_t t0 = esp_log_timestamp();
    static int32_t tmp0[BLOCK];   // static: the main task stack is only ~3.5 KB
    while ((auto_ms > 0 || gpio_get_level(BTN) == 0) && n + BLOCK <= max_frames) {
        watch_button();
        double rms[2];
        int f = mic_block(rms, tmp0, NULL);
        reads++;
        if (f == 0) empty++;
        if (reads <= 3) ESP_LOGI(TAG, "  read %d: %d frames at +%lu ms", reads, f, (unsigned long)(esp_log_timestamp() - t0));
        for (int i = 0; i < f; i++) src[n + i] = (int16_t)(tmp0[i] >> 16);
        n += f;
        // No OLED refresh while recording: it is slower than one audio block and drops samples.
        if (n == f) draw_level(dbfs(rms[0]), true);
    }
    ESP_LOGI(TAG, "  timing: %d reads (%d empty), %d frames in %lu ms (expected %d ms)", reads, empty, n,
             (unsigned long)(esp_log_timestamp() - t0), n * 1000 / RATE);
    if (n == 0) { free(src); return; }

    double mean = 0, sq = 0;
    int32_t peak = 0;
    for (int i = 0; i < n; i++) mean += src[i];
    mean /= n;
    for (int i = 0; i < n; i++) {
        double v = src[i] - mean;
        sq += v * v;
        int32_t a = (int32_t)fabs(v);
        if (a > peak) peak = a;
    }
    double rms = sqrt(sq / n);
    ESP_LOGI(TAG, "recorded %.2f s | peak %ld (%.1f dBFS) | average %.1f dBFS", n / (double)RATE, (long)peak,
             20.0 * log10((peak + 1) / 32768.0), 20.0 * log10((rms + 1) / 32768.0));

    if (peak < 3) {
        ESP_LOGW(TAG, "NO SOUND from the mic (all ~zero). Check INMP441 solder joints / VDD / L/R pin.");
        free(src);
        return;
    }
    // Normalize: scale the peak to about -0.5 dBFS (DC removed, then a ~100 Hz high-pass below).
    double gain = 31000.0 / (peak + 1);
    // Main firmware converts with raw32 >> SHIFT; we stored raw32 >> 16 and then multiplied by gain.
    int shift = 16 - (int)lround(log2(gain));
    if (shift < 8) shift = 8;
    if (shift > 18) shift = 18;
    ESP_LOGI(TAG, "playback gain x%.1f  ->  suggested MITRA_MIC_GAIN_SHIFT ~ %d (main firmware default 14)",
             gain, shift);

    ESP_LOGI(TAG, "PLAYING BACK...");
    fb_clear();
    fb_rect(0, 0, 128, 64, true);
    oled_flush();
    static int16_t out[256];
    // One-pole high-pass, fc ~100 Hz: removes rumble that makes speech muddy on a small speaker.
    float hp_y = 0, hp_x = 0;
    const float a = 0.962f;
    for (int i = 0; i < n; i += 256) {
        int m = (n - i) > 256 ? 256 : (n - i);
        for (int k = 0; k < m; k++) {
            float x = (float)((src[i + k] - mean) * gain);
            hp_y = a * (hp_y + x - hp_x);
            hp_x = x;
            float v = hp_y;
            out[k] = (int16_t)(v > 32767 ? 32767 : v < -32768 ? -32768 : v);
        }
        spk_write(out, m);
    }
    silence(200);
    free(src);
    ESP_LOGI(TAG, "playback done");
}

void app_main(void)
{
    gpio_config_t io = {.pin_bit_mask = 1ULL << BTN, .mode = GPIO_MODE_INPUT, .pull_up_en = GPIO_PULLUP_ENABLE};
    gpio_config(&io);
    oled_init();
    audio_init();

    ESP_LOGI(TAG, "SPEAKER CHECK: you should hear 3 beeps now");
    for (int i = 0; i < 3; i++) { beep(200); silence(250); }

    // Hands-free loop: short "pip" -> record 3 s (OLED frame) -> play back (OLED white) -> 2 s rest.
    // Holding BOOT instead records for as long as it is held.
    ESP_LOGI(TAG, "MIC CHECK (hands-free): after each short pip, talk for 3 s; it then plays you back.");
    while (true) {
        silence(100);
        beep(60);
        silence(150);
        record_and_play(3000);
        // rest ~2 s: live level bar, and react to BOOT
        for (int i = 0; i < 125; i++) {
            watch_button();
            if (gpio_get_level(BTN) == 0) {
                record_and_play(0);
                while (gpio_get_level(BTN) == 0) { watch_button(); vTaskDelay(pdMS_TO_TICKS(20)); }
                watch_button();
            }
            double rms[2];
            if (mic_block(rms, NULL, NULL)) draw_level(dbfs(rms[0]), false);
        }
    }
}
