// HD44780 driver: timing from the datasheet (enable pulse >= 230 ns, 37 us per
// instruction, 1.52 ms for clear/home), initialization by instruction for 4-bit mode.
#include "lcd.h"

#include "pico/stdlib.h"
#include "pins.h"

#define COLUMNS 16

static void pulse_enable(void) {
    gpio_put(PIN_LCD_E, 1);
    sleep_us(1);  // E high >= 230 ns
    gpio_put(PIN_LCD_E, 0);
    sleep_us(1);  // hold, and E cycle >= 500 ns
}

static void write_nibble(uint8_t nibble) {
    gpio_put(PIN_LCD_D4, nibble & 1u);
    gpio_put(PIN_LCD_D5, (nibble >> 1) & 1u);
    gpio_put(PIN_LCD_D6, (nibble >> 2) & 1u);
    gpio_put(PIN_LCD_D7, (nibble >> 3) & 1u);
    pulse_enable();
}

static void write_byte(bool data, uint8_t value) {
    gpio_put(PIN_LCD_RS, data);
    write_nibble(value >> 4);
    write_nibble(value & 0x0f);
    // RW is tied low, so the busy flag cannot be read: wait out the execution time.
    sleep_us(!data && value <= 0x03 ? 2000 : 50);
}

void lcd_init(void) {
    sleep_ms(50);  // > 40 ms after the supply reaches 2.7 V
    gpio_put(PIN_LCD_RS, 0);
    write_nibble(0x3);  // function set, 8-bit, three times: syncs any interface state
    sleep_ms(5);
    write_nibble(0x3);
    sleep_us(150);
    write_nibble(0x3);
    sleep_us(150);
    write_nibble(0x2);  // 4-bit interface from here on
    sleep_us(150);
    write_byte(false, 0x28);  // 4-bit, two lines, 5x8 dots
    write_byte(false, 0x0c);  // display on, cursor off, no blink
    lcd_clear();
    write_byte(false, 0x06);  // entry mode: increment, no display shift
}

void lcd_clear(void) { write_byte(false, 0x01); }

void lcd_goto(unsigned row, unsigned column) {
    write_byte(false, 0x80 | ((row ? 0x40 : 0x00) + column));
}

void lcd_print(const char *text) {
    while (*text) write_byte(true, (uint8_t)*text++);
}

void lcd_print_line(unsigned row, const char *text) {
    lcd_goto(row, 0);
    unsigned column = 0;
    for (; *text && column < COLUMNS; column++) write_byte(true, (uint8_t)*text++);
    for (; column < COLUMNS; column++) write_byte(true, ' ');
}

void lcd_define_char(unsigned code, const uint8_t rows[8]) {
    write_byte(false, 0x40 | ((code & 7u) << 3));  // CGRAM address of the character
    for (unsigned i = 0; i < 8; i++) write_byte(true, rows[i]);
    write_byte(false, 0x80);  // back to DDRAM
}
