# Build with the Raspberry Pi pico-sdk:  cmake -B build -DPICO_SDK_PATH=<sdk> && cmake --build build
cmake_minimum_required(VERSION 3.13)
include($ENV{PICO_SDK_PATH}/external/pico_sdk_import.cmake)
project(seg_counter C CXX ASM)
pico_sdk_init()
add_executable(seg_counter main.c)
target_link_libraries(seg_counter pico_stdlib)
pico_add_extra_outputs(seg_counter)
target_compile_options(seg_counter PRIVATE -Wall -Wextra)
