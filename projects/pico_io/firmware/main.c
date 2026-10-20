// Blink the status LED, faster while the button is held; print the pot reading.
#include <stdio.h>

#include "hardware/adc.h"
#include "pico/stdlib.h"
#include "pins.h"
#include "pins_init.h"

int main(void) {
    pins_init();
    stdio_init_all();  // printf -> UART0 (PIN_UART_TX), 115200 baud
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
