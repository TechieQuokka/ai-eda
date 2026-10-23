// RC car remote: reads the thumb joystick and sends "D,<throttle>,<steer>\n" (both
// -100..100; throttle: + forward, steer: + right) every 50 ms to the car through the
// HC-05 (UART0, 9600 baud). The stick's centre is taken at power-up (only if it is near
// the middle: a stick held over is not a centre), with a dead zone around it; the
// throttle stays at 0 until the stick has been centred once, so a remote switched on
// with the stick pushed does not start the car. Pressing the stick toggles slow mode
// (half throttle). The car's "B,<millivolts>" reports light the BATT LED when its cells
// run low.
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>

#include "hardware/adc.h"
#include "hardware/uart.h"
#include "pico/stdlib.h"
#include "pins.h"
#include "pins_init.h"

#define UART_ID uart0
#define BAUD 9600  // HC-05 data mode default

#define SEND_MS 50

#define ADC_MID 2048
#define CENTRE_TOLERANCE 614  // 15 % of full scale: a centre further out is not trusted
#define DEAD_ZONE_PCT 8       // of the travel to either side
#define FULL_PCT 90           // real sticks do not reach the ends: full output from here

#define DEBOUNCE_MS 20

#define BATTERY_MS 1000
#define BATTERY_LOW_MV 1800   // two cells near 1.0 V each, less the Schottky drop
#define BATTERY_OK_MV 1900    // hysteresis
#define BATTERY_NONE_MV 300   // below this the Pico runs from USB with the battery off
#define CAR_LOW_MV 4400       // the car's own threshold (four cells at 1.1 V)
#define CAR_OK_MV 4600

static int centre_steer = ADC_MID;
static int centre_throttle = ADC_MID;
static bool throttle_locked = true;  // until the stick has been centred once
static bool slow;
static bool remote_low;
static bool car_low;

static uint32_t now_ms(void) { return to_ms_since_boot(get_absolute_time()); }

static int adc_average(uint input) {
    adc_select_input(input);
    uint32_t sum = 0;
    for (int i = 0; i < 16; i++) sum += adc_read();
    return (int)(sum / 16u);
}

// Stick reading to -100..100: 0 inside the dead zone, then linear up to FULL_PCT of the
// travel on that side of the centre.
static int axis(int raw, int centre) {
    int offset = raw - centre;
    int travel = offset > 0 ? 4095 - centre : centre;
    int dead = travel * DEAD_ZONE_PCT / 100;
    int full = travel * FULL_PCT / 100;
    int distance = abs(offset);
    if (distance <= dead) return 0;
    int value = (distance - dead) * 100 / (full - dead);
    if (value > 100) value = 100;
    return offset > 0 ? value : -value;
}

static int calibrated(int raw) { return abs(raw - ADC_MID) <= CENTRE_TOLERANCE ? raw : ADC_MID; }

static void send_command(void) {
    int steer = axis(adc_average(PIN_STEER_IN - 26), centre_steer);
    int throttle = axis(adc_average(PIN_THROTTLE_IN - 26), centre_throttle);
    if (throttle == 0) throttle_locked = false;
    if (throttle_locked) throttle = 0;
    if (slow) throttle /= 2;
    char text[32];
    snprintf(text, sizeof text, "D,%d,%d\n", throttle, steer);
    uart_puts(UART_ID, text);
}

// Stick button: a press (stable for DEBOUNCE_MS) toggles slow mode.
static void button(void) {
    static bool pressed;
    static uint32_t changed;
    bool now_pressed = !gpio_get(PIN_STICK_SW);
    if (now_pressed == pressed) {
        changed = now_ms();
        return;
    }
    if (now_ms() - changed < DEBOUNCE_MS) return;
    pressed = now_pressed;
    if (pressed) slow = !slow;
}

// "B,<millivolts>" from the car.
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
        if (length < sizeof line) {
            line[length] = '\0';
            char *end;
            long mv = strtol(line + 2, &end, 10);
            if (line[0] == 'B' && line[1] == ',' && end != line + 2 && *end == '\0')
                car_low = car_low ? mv < CAR_OK_MV : mv < CAR_LOW_MV;
        }
        length = 0;
    }
}

static void check_battery(void) {
    int mv = adc_average(PIN_BAT_SENSE - 26) * 3300 * 2 / 4095;  // RS1/RS2 halve it
    bool low = remote_low ? mv < BATTERY_OK_MV : mv < BATTERY_LOW_MV;
    remote_low = low && mv > BATTERY_NONE_MV;
}

static void leds(bool link) {
    uint32_t now = now_ms();
    if (!link) car_low = false;  // the last report is stale
    gpio_put(PIN_LED_LINK, link || (now / 500) % 2);
    gpio_put(PIN_LED_SLOW, slow);
    gpio_put(PIN_LED_BATT, remote_low || (car_low && (now / 125) % 2));
}

int main(void) {
    pins_init();
    uart_init(UART_ID, BAUD);
    adc_init();
    sleep_ms(20);  // let the ADC filter and the stick settle
    centre_steer = calibrated(adc_average(PIN_STEER_IN - 26));
    centre_throttle = calibrated(adc_average(PIN_THROTTLE_IN - 26));
    check_battery();
    uint32_t next_send = now_ms();
    uint32_t next_battery = now_ms() + BATTERY_MS;
    absolute_time_t tick = get_absolute_time();
    while (true) {
        receive();
        button();
        leds(gpio_get(PIN_BT_STATE));
        if ((int32_t)(now_ms() - next_send) >= 0) {
            next_send += SEND_MS;
            send_command();
        }
        if ((int32_t)(now_ms() - next_battery) >= 0) {
            next_battery += BATTERY_MS;
            check_battery();
        }
        tick = delayed_by_ms(tick, 1);
        sleep_until(tick);
    }
}
