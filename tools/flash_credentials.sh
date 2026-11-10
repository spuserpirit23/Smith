#!/usr/bin/env bash
# Write a device's credentials into its 'mitra_cfg' NVS partition (offset 0x12000, 24 KB).
#
#   tools/flash_credentials.sh mitra-001.nvs.csv /dev/ttyUSB0
#
# The CSV comes from `mitra provision`. Credentials never enter the firmware image or the repo.
# Requires an ESP-IDF shell (for esptool + nvs_partition_gen) or: pip install esptool esp-idf-nvs-partition-gen
set -euo pipefail
CSV="$1"; PORT="$2"
BIN="$(mktemp --suffix=.nvs.bin)"
trap 'rm -f "$BIN"' EXIT

if python -c "import esp_idf_nvs_partition_gen" 2>/dev/null; then
  python -m esp_idf_nvs_partition_gen generate "$CSV" "$BIN" 0x6000
else
  python "$IDF_PATH/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py" generate "$CSV" "$BIN" 0x6000
fi
python -m esptool --chip esp32 -p "$PORT" write_flash 0x12000 "$BIN"
echo "Credentials flashed. Now delete $CSV (it contains the device secret)."
