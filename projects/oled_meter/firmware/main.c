// OLED meter: the potentiometer's ADC reading as a number, a voltage, and a bar graph,
// redrawn every 100 ms. Reports on the UART (stdio) whether the OLED answered.
#include <stdio.h>

#include "hardware/adc.h"
#include "oled.h"
#include "pico/stdlib.h"
#include "pins.h"
#include "pins_init.h"

#define BAR_X 4
#define BAR_Y 48
#define BAR_W 120
#define BAR_H 12

int main(void) {
    stdio_init_all();
    pins_init();
    adc_init();
    adc_select_input(0);  // POT on ADC0
    if (!oled_init()) printf("oled: no answer\n");
    while (true) {
        unsigned raw = adc_read();
        unsigned millivolts = raw * 3300u / 4095u;
        char line[22];
        oled_clear();
        oled_text(0, 0, "eda OLED meter");
        snprintf(line, sizeof line, "Pot: %4u", raw);
        oled_text(0, 16, line);
        snprintf(line, sizeof line, "%u.%02u V", millivolts / 1000u, millivolts % 1000u / 10u);
        oled_text(0, 28, line);
        oled_rect(BAR_X, BAR_Y, BAR_W, BAR_H, false);
        oled_rect(BAR_X + 2, BAR_Y + 2, (int)((BAR_W - 4) * raw / 4095u), BAR_H - 4, true);
        if (!oled_show()) printf("oled: no answer\n");
        sleep_ms(100);
    }
}
