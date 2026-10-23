// RC car: drive commands arrive from the remote over the HC-05 (UART0, 9600 baud) as text
// lines "D,<throttle>,<steer>\n", both -100..100 (throttle: + forward; steer: + right),
// about 20 per second. The motor follows the throttle through a ramp; the steering
// follows at once. Without a valid command for FAILSAFE_MS, or when the HC-05 reports
// the link down, the car brakes to a stop and straightens the steering. Every second it
// reports its battery as "B,<millivolts>\n", measured while the motor stands still.
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>

#include "drive.h"
#include "hardware/adc.h"
#include "hardware/uart.h"
#include "pico/stdlib.h"
#include "pins.h"
#include "pins_init.h"

#define UART_ID uart0
#define BAUD 9600  // HC-05 data mode default

#define FAILSAFE_MS 300  // no command for this long: stop
// Speed changes at most RAMP_PER_MS per millisecond: 0 to full in 250 ms, so starting
// does not pull the cells down at once.
#define RAMP_PER_MS 4
// Changing direction: brake at standstill this long first, so the motor (and the car)
// slows down before it is driven the other way (driving against it would draw up to
// twice the stall current).
#define REVERSE_BRAKE_MS 500

#define REPORT_MS 1000
#define BATTERY_LOW_MV 4400  // four cells at 1.1 V
#define BATTERY_OK_MV 4600   // hysteresis

static int target;     // throttle wanted, -SPEED_MAX..SPEED_MAX
static int speed;      // motor speed now (ramped)
static int last_sign;  // direction of the last motion: -1, 0, 1
static uint32_t brake_until;
static uint32_t last_command;  // time of the last valid command, ms
static bool commanded;         // a valid command came within FAILSAFE_MS
static bool battery_low;

static uint32_t now_ms(void) { return to_ms_since_boot(get_absolute_time()); }

// "D,<int>,<int>" with both in -100..100; anything else is ignored.
static bool parse_int(const char **text, int *value) {
    char *end;
    long number = strtol(*text, &end, 10);
    if (end == *text || number < -100 || number > 100) return false;
    *value = (int)number;
    *text = end;
    return true;
}

static bool parse_command(const char *line, int *throttle, int *steer) {
    if (line[0] != 'D' || line[1] != ',') return false;
    const char *p = line + 2;
    if (!parse_int(&p, throttle) || *p++ != ',') return false;
    return parse_int(&p, steer) && *p == '\0';
}

static void receive(void) {
    static char line[24];
    static unsigned length;
    while (uart_is_readable(UART_ID)) {
        char c = uart_getc(UART_ID);
        if (c == '\r') continue;
        if (c != '\n') {
            if (length < sizeof line - 1) line[length] = c;
            length++;
            continue;
        }
        int throttle, steer;
        // While the HC-05 reports the link down, stray bytes are not commands.
        if (length < sizeof line && gpio_get(PIN_BT_STATE)) {
            line[length] = '\0';
            if (parse_command(line, &throttle, &steer)) {
                target = throttle * SPEED_MAX / 100;
                steering_set(steer);
                last_command = now_ms();
                commanded = true;
            }
        }
        length = 0;
    }
}

static void failsafe(void) {
    bool link = gpio_get(PIN_BT_STATE);
    bool late = now_ms() - last_command > FAILSAFE_MS;
    if (commanded && (late || !link)) {
        commanded = false;
        target = 0;
        steering_set(0);
        uart_puts(UART_ID, link ? "failsafe: no commands\n" : "failsafe: link down\n");
    }
}

// One millisecond of the speed ramp, with a pause at standstill before reversing.
static void ramp(void) {
    uint32_t now = now_ms();
    int wanted = target;
    int sign = (wanted > 0) - (wanted < 0);
    if (speed == 0 && sign != 0 && last_sign != 0 && sign != last_sign) {
        brake_until = now + REVERSE_BRAKE_MS;
        last_sign = 0;
    }
    if ((int32_t)(now - brake_until) < 0) wanted = 0;
    if (speed * wanted < 0) wanted = 0;  // through standstill first
    if (speed < wanted) speed = speed + RAMP_PER_MS > wanted ? wanted : speed + RAMP_PER_MS;
    if (speed > wanted) speed = speed - RAMP_PER_MS < wanted ? wanted : speed - RAMP_PER_MS;
    if (speed != 0) last_sign = speed > 0 ? 1 : -1;
    motor_set(speed);
}

static int battery_mv(void) {
    uint32_t sum = 0;
    for (int i = 0; i < 16; i++) sum += adc_read();
    return (int)(sum * 3300u * 3u / (16u * 4095u));  // RS1/RS2 divide by 3
}

// The cells are measured only while the motor stands still: under load their internal
// resistance pulls the voltage down by up to a volt or two, which would read as an empty
// battery. While driving, the last reading at rest is reported again.
static int rest_mv;

static void measure_at_rest(void) {
    if (speed == 0) rest_mv = battery_mv();
}

static void report(void) {
    measure_at_rest();
    int mv = rest_mv;
    battery_low = battery_low ? mv < BATTERY_OK_MV : mv < BATTERY_LOW_MV;
    char text[16];
    snprintf(text, sizeof text, "B,%d\n", mv);
    uart_puts(UART_ID, text);
}

// On while driving under commands, 1 Hz blink while waiting, 4 Hz with a low battery.
static void status_led(void) {
    uint32_t now = now_ms();
    bool on = battery_low ? (now / 125) % 2 : commanded ? true : (now / 500) % 2;
    gpio_put(PIN_LED, on);
}

int main(void) {
    pins_init();
    drive_init();
    uart_init(UART_ID, BAUD);
    adc_init();
    adc_select_input(PIN_BAT_SENSE - 26);
    uart_puts(UART_ID, "ready\n");
    measure_at_rest();
    uint32_t next_report = now_ms() + REPORT_MS;
    absolute_time_t tick = get_absolute_time();
    while (true) {
        receive();
        failsafe();
        ramp();
        status_led();
        if ((int32_t)(now_ms() - next_report) >= 0) {
            next_report += REPORT_MS;
            report();
        }
        tick = delayed_by_ms(tick, 1);
        sleep_until(tick);
    }
}
