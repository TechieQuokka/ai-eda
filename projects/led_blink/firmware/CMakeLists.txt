# Build with the Raspberry Pi pico-sdk:  cmake -B build -DPICO_SDK_PATH=<sdk> && cmake --build build
cmake_minimum_required(VERSION 3.13)
include($ENV{PICO_SDK_PATH}/external/pico_sdk_import.cmake)
project(led_blink C CXX ASM)
pico_sdk_init()
add_executable(led_blink main.c)
target_link_libraries(led_blink pico_stdlib)
pico_add_extra_outputs(led_blink)
target_compile_options(led_blink PRIVATE -Wall -Wextra)
