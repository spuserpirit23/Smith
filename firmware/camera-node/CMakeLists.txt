cmake_minimum_required(VERSION 3.16)
list(APPEND EXTRA_COMPONENT_DIRS ${CMAKE_CURRENT_LIST_DIR}/../components)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
# ESP-IDF v5.3's esp_clock_output.c (camera XCLK) trips GCC 14+'s -Warray-bounds; v5.3 targets
# GCC 13. Keep it a warning when building v5.3 with a newer toolchain.
idf_build_set_property(COMPILE_OPTIONS "-Wno-error=array-bounds" APPEND)
project(mitra_camera_node)
