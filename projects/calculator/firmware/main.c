// Pocket calculator: keys from the 5 x 4 matrix go to the calculator logic (calc.c), its
// two lines go to the LCD. A mark in the corner of line 2 warns of a low battery.
// The UART logs each key and what the display shows (debug header J2).
#include <stdio.h>
#include <string.h>

#include "calc.h"
#include "hardware/adc.h"
#include "hardware/uart.h"
#include "keypad.h"
#include "lcd.h"
#include "pico/stdlib.h"
#include "pins.h"
#include "pins_init.h"

#define UART_ID uart0
#define BAUD 115200

#define GLYPH_TIMES 0x08    // CGRAM character 0 (0x08 mirrors it, so strings can hold it)
#define GLYPH_BATTERY 0x09  // CGRAM character 1
#define GLYPH_DIVIDE 0x0A   // CGRAM character 2: ROM 0xFD is a division sign only on A00
                            // modules, not on the A02 ones sold just as often

// The battery is read after D1 through RS1/RS2 (half the voltage) on an ADC pin.
#define BATTERY_CHECK_MS 500
#define BATTERY_LOW_V 1.8f   // two cells near 1.0 V each, less the Schottky drop
#define BATTERY_OK_V 1.9f    // hysteresis: the mark goes away above this
#define BATTERY_NONE_V 0.3f  // below this the Pico runs from USB with the battery off

static const uint8_t times_glyph[8] = {0x00, 0x11, 0x0a, 0x04, 0x0a, 0x11, 0x00, 0x00};
static const uint8_t battery_glyph[8] = {0x0e, 0x1b, 0x11, 0x11, 0x11, 0x11, 0x1f, 0x00};
static const uint8_t divide_glyph[8] = {0x00, 0x04, 0x00, 0x1f, 0x00, 0x04, 0x00, 0x00};

static bool battery_low;

static float battery_volts(void) {
    uint16_t raw = adc_read();
    return raw * 3.3f / 4095.0f * 2.0f;
}

static void check_battery(void) {
    float volts = battery_volts();
    bool low = battery_low ? volts < BATTERY_OK_V : volts < BATTERY_LOW_V;
    low = low && volts > BATTERY_NONE_V;
    if (low != battery_low) printf("battery %s (%.2f V)\n", low ? "low" : "ok", (double)volts);
    battery_low = low;
}

// The calculator's ASCII operators as LCD characters.
static void to_display(const char *text, char *out) {
    for (; *text; text++, out++) {
        *out = *text == '*' ? GLYPH_TIMES : *text == '/' ? GLYPH_DIVIDE : *text;
    }
    *out = '\0';
}

static void show(void) {
    static char shown[2][CALC_COLUMNS + 1] = {"\x01", "\x01"};  // nothing shown yet
    char lines[2][CALC_COLUMNS + 1];
    to_display(calc_line(0), lines[0]);
    to_display(calc_line(1), lines[1]);
    if (battery_low) lines[1][0] = GLYPH_BATTERY;
    for (unsigned row = 0; row < 2; row++) {
        if (strcmp(lines[row], shown[row]) != 0) {
            lcd_print_line(row, lines[row]);
            strcpy(shown[row], lines[row]);
        }
    }
}

int main(void) {
    stdio_init_all();
    pins_init();
    uart_init(UART_ID, BAUD);
    adc_init();
    adc_select_input(PIN_BAT_SENSE - 26u);  // ADC channels 0-3 are GP26-GP29
    keypad_init();
    lcd_init();
    lcd_define_char(0, times_glyph);
    lcd_define_char(1, battery_glyph);
    lcd_define_char(2, divide_glyph);
    calc_reset();
    check_battery();
    show();
    printf("calculator ready\n");

    unsigned since_battery = 0;
    while (true) {
        char key = keypad_poll();
        if (key) {
            calc_key(key);
            const char *value = calc_line(1);
            while (*value == ' ') value++;
            printf("key %c: %s | %s\n", key, calc_line(0), value);
        }
        since_battery += KEYPAD_SCAN_MS;
        if (since_battery >= BATTERY_CHECK_MS) {
            since_battery = 0;
            check_battery();
        }
        show();
        sleep_ms(KEYPAD_SCAN_MS);
    }
}
