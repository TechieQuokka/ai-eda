// Blink the LED: on for 1 s, off for 1 s.
#include "pico/stdlib.h"
#include "pins.h"
#include "pins_init.h"

#define HALF_PERIOD_MS 1000u

int main(void) {
    pins_init();
    bool on = false;
    while (true) {
        on = !on;
        gpio_put(PIN_LED, on);
        sleep_ms(HALF_PERIOD_MS);
    }
}
