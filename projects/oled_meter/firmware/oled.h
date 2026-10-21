// SSD1306 128x64 OLED on I2C: a frame buffer in RAM, sent whole with oled_show().
#ifndef OLED_H
#define OLED_H

#include <stdbool.h>
#include <stdint.h>

#define OLED_WIDTH 128
#define OLED_HEIGHT 64

bool oled_init(void);  // false if the module does not answer
void oled_clear(void);
void oled_pixel(int x, int y, bool on);
void oled_rect(int x, int y, int width, int height, bool filled);
void oled_text(int x, int y, const char *text);  // 5x8 font, 6-pixel cells
bool oled_show(void);  // false if the transfer was not acknowledged

#endif
