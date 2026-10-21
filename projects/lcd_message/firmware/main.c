// Text on a 16x2 LCD: line 1 a message ("msg <text>" on the UART replaces it), line 2
// the uptime in seconds, rewritten once a second.
#include <stdio.h>
#include <string.h>

#include "hardware/uart.h"
#include "lcd.h"
#include "pico/stdlib.h"
#include "pins.h"
#include "pins_init.h"

#define UART_ID uart0
#define BAUD 115200

static char command[64];
static unsigned command_length;

// Collects a line from the UART; "msg <text>" shows <text> on line 1.
static void poll_uart(void) {
    while (uart_is_readable(UART_ID)) {
        char c = uart_getc(UART_ID);
        if (c != '\n' && c != '\r') {
            if (command_length < sizeof command - 1) command[command_length++] = c;
            continue;
        }
        command[command_length] = '\0';
        if (strncmp(command, "msg ", 4) == 0) {
            lcd_print_line(0, command + 4);
            printf("shown: %s\n", command + 4);
        } else if (command_length > 0) {
            printf("error: %s\n", command);
        }
        command_length = 0;
    }
}

int main(void) {
    stdio_init_all();
    pins_init();
    uart_init(UART_ID, BAUD);
    lcd_init();
    lcd_print_line(0, "Hello, world!");
    printf("lcd ready\n");
    unsigned shown = ~0u;
    while (true) {
        unsigned seconds = to_ms_since_boot(get_absolute_time()) / 1000u;
        if (seconds != shown) {
            char line[17];
            snprintf(line, sizeof line, "Uptime %5u s", seconds);
            lcd_print_line(1, line);
            shown = seconds;
        }
        poll_uart();
        sleep_ms(1);
    }
}
