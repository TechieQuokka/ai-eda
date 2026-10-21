// Stopwatch on a multiplexed 4-digit 7-segment display: tenths of a second since start
// (or since the reset button was released), shown as "SSS.d" with leading blanks.
// One digit is lit at a time for DIGIT_US; all four are refreshed 125 times a second.
#include "pico/stdlib.h"
#include "pins.h"
#include "pins_init.h"

#define DIGIT_US 2000u

// Segment patterns for 0-9, bit 0 = a ... bit 6 = g.
static const uint8_t FONT[10] = {0x3f, 0x06, 0x5b, 0x4f, 0x66, 0x6d, 0x7d, 0x07, 0x7f, 0x6f};
#define BLANK 0x00u
#define DOT 0x80u  // bit 7 = decimal point

static const uint SEGMENTS[8] = {PIN_SEG_A, PIN_SEG_B, PIN_SEG_C, PIN_SEG_D,
                                 PIN_SEG_E, PIN_SEG_F, PIN_SEG_G, PIN_SEG_DP};
static const uint DIGITS[4] = {PIN_DIG1, PIN_DIG2, PIN_DIG3, PIN_DIG4};

// Light one digit with a pattern: every digit off first, so the new segments never
// show on the previous digit.
static void show(unsigned digit, uint8_t pattern) {
    for (unsigned d = 0; d < 4; d++) gpio_put(DIGITS[d], 0);
    for (unsigned s = 0; s < 8; s++) gpio_put(SEGMENTS[s], (pattern >> s) & 1u);
    gpio_put(DIGITS[digit], 1);
}

// The four patterns for a count of tenths (0-9999): "SSS.d", leading zeros blank.
static void render(uint32_t tenths, uint8_t out[4]) {
    out[3] = FONT[tenths % 10];
    out[2] = FONT[(tenths / 10) % 10] | DOT;
    out[1] = tenths >= 100 ? FONT[(tenths / 100) % 10] : BLANK;
    out[0] = tenths >= 1000 ? FONT[(tenths / 1000) % 10] : BLANK;
}

int main(void) {
    pins_init();
    absolute_time_t start = get_absolute_time();
    uint8_t patterns[4];
    while (true) {
        for (unsigned digit = 0; digit < 4; digit++) {
            absolute_time_t now = get_absolute_time();
            if (!gpio_get(PIN_BUTTON)) start = now;  // held: stay at zero
            uint64_t elapsed = (uint64_t)absolute_time_diff_us(start, now);
            render((uint32_t)((elapsed / 100000u) % 10000u), patterns);
            show(digit, patterns[digit]);
            sleep_us(DIGIT_US);
        }
    }
}
