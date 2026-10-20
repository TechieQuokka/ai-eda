// Blink the status LED, faster while the button is held; print the pot reading.
#include <stdio.h>

#include "hardware/adc.h"
#include "hardware/uart.h"
#include "pico/stdlib.h"
#include "pins.h"
#include "pins_init.h"

int main(void) {
    pins_init();
    uart_init(uart0, 115200);
    adc_init();
    adc_select_input(0);  // ADC0 = PIN_POT

    bool led = false;
    while (true) {
        bool pressed = !gpio_get(PIN_BUTTON);  // active low, pulled up
        led = !led;
        gpio_put(PIN_LED, led);
        printf("pot=%u\n", adc_read());
        sleep_ms(pressed ? 100 : 500);
    }
}
