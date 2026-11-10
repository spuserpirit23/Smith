// I2S microphone (INMP441) and speaker amplifier (MAX98357A) on two separate I2S ports.
#include "freertos/FreeRTOS.h"
#include <string.h>

#include "driver/i2s_std.h"
#include "esp_check.h"
#include "esp_log.h"
#include "mitra.h"

static const char *TAG = "audio";
static i2s_chan_handle_t s_rx, s_tx;
static int32_t s_raw[FRAME_SAMPLES];

void audio_init(void)
{
    // Microphone: I2S0 RX, 32-bit slots, left channel (INMP441 L/R pin tied low).
    i2s_chan_config_t rx_chan = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    rx_chan.dma_desc_num = 6;
    rx_chan.dma_frame_num = FRAME_SAMPLES;
    ESP_ERROR_CHECK(i2s_new_channel(&rx_chan, NULL, &s_rx));
    i2s_std_config_t rx_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = CONFIG_MITRA_MIC_BCLK,
            .ws = CONFIG_MITRA_MIC_WS,
            .dout = I2S_GPIO_UNUSED,
            .din = CONFIG_MITRA_MIC_DIN,
        },
    };
    rx_cfg.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_rx, &rx_cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(s_rx));

    // Speaker: I2S1 TX, 16-bit mono. auto_clear outputs silence on underrun instead of a buzz.
    i2s_chan_config_t tx_chan = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
    tx_chan.dma_desc_num = 6;
    tx_chan.dma_frame_num = FRAME_SAMPLES;
    tx_chan.auto_clear = true;
    ESP_ERROR_CHECK(i2s_new_channel(&tx_chan, &s_tx, NULL));
    i2s_std_config_t tx_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = CONFIG_MITRA_SPK_BCLK,
            .ws = CONFIG_MITRA_SPK_WS,
            .dout = CONFIG_MITRA_SPK_DOUT,
            .din = I2S_GPIO_UNUSED,
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_tx, &tx_cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(s_tx));
    ESP_LOGI(TAG, "i2s ready: mic %d/%d/%d spk %d/%d/%d", CONFIG_MITRA_MIC_BCLK, CONFIG_MITRA_MIC_WS,
             CONFIG_MITRA_MIC_DIN, CONFIG_MITRA_SPK_BCLK, CONFIG_MITRA_SPK_WS, CONFIG_MITRA_SPK_DOUT);
}

int audio_mic_read(int16_t *out, int samples, int timeout_ms)
{
    if (samples > FRAME_SAMPLES) samples = FRAME_SAMPLES;
    size_t got = 0;
    esp_err_t err = i2s_channel_read(s_rx, s_raw, samples * sizeof(int32_t), &got, pdMS_TO_TICKS(timeout_ms));
    if (err != ESP_OK) return 0;
    int n = got / sizeof(int32_t);
    for (int i = 0; i < n; i++) {
        int32_t v = s_raw[i] >> CONFIG_MITRA_MIC_GAIN_SHIFT;
        out[i] = v > 32767 ? 32767 : (v < -32768 ? -32768 : (int16_t)v);
    }
    return n;
}

void audio_spk_write(const int16_t *pcm, int samples)
{
    static int16_t scaled[FRAME_SAMPLES];
    int vol = g_app.volume;
    if (vol > CONFIG_MITRA_VOLUME_CAP) vol = CONFIG_MITRA_VOLUME_CAP;
    // Perceptual-ish curve: square of the linear setting.
    int32_t gain_q15 = (int32_t)(vol * vol * 32767 / 10000);
    while (samples > 0) {
        int n = samples > FRAME_SAMPLES ? FRAME_SAMPLES : samples;
        for (int i = 0; i < n; i++) scaled[i] = (int16_t)(((int32_t)pcm[i] * gain_q15) >> 15);
        size_t written = 0;
        i2s_channel_write(s_tx, scaled, n * sizeof(int16_t), &written, portMAX_DELAY);
        pcm += n;
        samples -= n;
    }
}
