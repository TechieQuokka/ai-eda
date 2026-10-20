// Blink the status LED, faster while the button is held; print the pot reading.
// The command "rate <ms>" on the UART sets the normal toggle interval.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "hardware/adc.h"
#include "pico/stdlib.h"
#include "pins.h"
#include "pins_init.h"

#define FAST_MS 100u

static unsigned slow_ms = 500u;

// Handle one received line; returns true when it was a valid command.
static bool handle_command(const char *line) {
    if (strncmp(line, "rate ", 5) == 0) {
        unsigned value = (unsigned)strtoul(line + 5, NULL, 10);
        if (value >= 10u && value <= 10000u) {
            slow_ms = value;
            printf("rate=%u\n", slow_ms);
            return true;
        }
    }
    printf("error: %s\n", line);
    return false;
}

int main(void) {
    pins_init();
    stdio_init_all();  // printf/getchar on UART0 (PIN_UART_TX, PIN_UART_RX), 115200 baud
    adc_init();
    adc_select_input(0);  // ADC0 = PIN_POT

    bool led = false;
    char line[32];
    size_t length = 0;
    absolute_time_t next_toggle = get_absolute_time();
    while (true) {
        int c = getchar_timeout_us(0);
        if (c == '\n' || c == '\r') {
            if (length > 0) {
                line[length] = '\0';
                handle_command(line);
                length = 0;
            }
        } else if (c >= 0 && length < sizeof line - 1) {
            line[length++] = (char)c;
        }

        if (time_reached(next_toggle)) {
            bool pressed = !gpio_get(PIN_BUTTON);  // active low, pulled up
            led = !led;
            gpio_put(PIN_LED, led);
            printf("pot=%u\n", adc_read());
            next_toggle = make_timeout_time_ms(pressed ? FAST_MS : slow_ms);
        }
        sleep_ms(1);
    }
}
