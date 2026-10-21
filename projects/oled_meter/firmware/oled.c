// SSD1306 driver: the usual init sequence for a 128x64 module with the internal charge
// pump, horizontal addressing, and the whole 1 KB frame buffer in one I2C transfer.
#include "oled.h"

#include <string.h>

#include "font5x8.h"
#include "hardware/i2c.h"
#include "pico/stdlib.h"
#include "pins.h"

#define OLED_I2C i2c0
#define OLED_ADDRESS 0x3c
#define OLED_BAUD 400000

static uint8_t buffer[1 + OLED_WIDTH * OLED_HEIGHT / 8];  // control byte + pixels
static uint8_t *const pixels = buffer + 1;

static bool commands(const uint8_t *list, size_t length) {
    uint8_t message[32];
    message[0] = 0x00;  // Co = 0, D/C = 0: a stream of commands
    memcpy(message + 1, list, length);
    return i2c_write_blocking(OLED_I2C, OLED_ADDRESS, message, length + 1, false) ==
           (int)length + 1;
}

bool oled_init(void) {
    i2c_init(OLED_I2C, OLED_BAUD);
    sleep_ms(10);  // the module's supply settles
    static const uint8_t init[] = {
        0xae,        // display off
        0xd5, 0x80,  // clock divide ratio / oscillator
        0xa8, 0x3f,  // multiplex ratio: 64 rows
        0xd3, 0x00,  // display offset 0
        0x40,        // start line 0
        0x8d, 0x14,  // charge pump on (the module has no external panel supply)
        0x20, 0x00,  // horizontal addressing
        0xa1,        // column 127 to SEG0: left to right
        0xc8,        // COM scan from COM63: top to bottom
        0xda, 0x12,  // COM pins: alternative (128x64 panels)
        0x81, 0xcf,  // contrast
        0xd9, 0xf1,  // pre-charge
        0xdb, 0x40,  // VCOMH
        0xa4,        // show RAM contents
        0xa6,        // normal (not inverted)
        0x2e,        // no scrolling
        0xaf,        // display on
    };
    return commands(init, sizeof init);
}

void oled_clear(void) { memset(pixels, 0, OLED_WIDTH * OLED_HEIGHT / 8); }

void oled_pixel(int x, int y, bool on) {
    if (x < 0 || x >= OLED_WIDTH || y < 0 || y >= OLED_HEIGHT) return;
    uint8_t *byte = &pixels[(y / 8) * OLED_WIDTH + x];
    if (on) *byte |= (uint8_t)(1u << (y % 8));
    else *byte &= (uint8_t)~(1u << (y % 8));
}

void oled_rect(int x, int y, int width, int height, bool filled) {
    for (int j = 0; j < height; j++) {
        for (int i = 0; i < width; i++) {
            bool edge = i == 0 || j == 0 || i == width - 1 || j == height - 1;
            if (filled || edge) oled_pixel(x + i, y + j, true);
        }
    }
}

void oled_text(int x, int y, const char *text) {
    for (; *text; text++, x += 6) {
        unsigned char c = (unsigned char)*text;
        if (c < FONT_FIRST || c > FONT_LAST) c = '?';
        for (int column = 0; column < 5; column++) {
            uint8_t bits = FONT5X8[c - FONT_FIRST][column];
            for (int row = 0; row < 8; row++) oled_pixel(x + column, y + row, bits >> row & 1u);
        }
    }
}

bool oled_show(void) {
    static const uint8_t window[] = {0x21, 0, OLED_WIDTH - 1, 0x22, 0, OLED_HEIGHT / 8 - 1};
    if (!commands(window, sizeof window)) return false;
    buffer[0] = 0x40;  // Co = 0, D/C = 1: display RAM data follows
    return i2c_write_blocking(OLED_I2C, OLED_ADDRESS, buffer, sizeof buffer, false) ==
           (int)sizeof buffer;
}
