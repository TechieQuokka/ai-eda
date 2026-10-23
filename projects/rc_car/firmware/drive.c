// Motor: 20 kHz PWM (above hearing) on both bridge inputs, slow decay: for forward IN1
// stays high and IN2 is low for the drive fraction of each period (high for the rest:
// brake); reverse swaps them. Both high is a brake, so speed 0 holds the car.
// Steering: the servo gets a pulse every 20 ms, 1.5 ms for straight ahead.
#include "drive.h"

#include <stdlib.h>

#include "hardware/pwm.h"
#include "pins.h"

#define MOTOR_TOP 6249u  // 125 MHz / 6250 = 20 kHz, full clock speed
#define MOTOR_HIGH (MOTOR_TOP + 1u)  // a level above TOP keeps the output high

#define SERVO_DIV 125.0f   // 1 MHz count: one count per microsecond
#define SERVO_TOP 19999u   // 20 ms period
// Pulse widths; adjust to the steering linkage: the servo must not push against the
// chassis at full lock (a stalled servo draws 650 mA).
#define STEER_CENTRE_US 1500
#define STEER_RANGE_US 500  // full lock either side: 1.0 / 2.0 ms

void drive_init(void) {
    uint motor = pwm_gpio_to_slice_num(PIN_MOT_IN1);  // IN1 and IN2 share the slice
    pwm_config config = pwm_get_default_config();
    pwm_config_set_wrap(&config, MOTOR_TOP);
    pwm_init(motor, &config, false);
    motor_set(0);
    pwm_set_enabled(motor, true);

    uint servo = pwm_gpio_to_slice_num(PIN_SERVO);
    config = pwm_get_default_config();
    pwm_config_set_clkdiv(&config, SERVO_DIV);
    pwm_config_set_wrap(&config, SERVO_TOP);
    pwm_init(servo, &config, false);
    steering_set(0);
    pwm_set_enabled(servo, true);
}

void motor_set(int speed) {
    if (speed > SPEED_MAX) speed = SPEED_MAX;
    if (speed < -SPEED_MAX) speed = -SPEED_MAX;
    uint drive = (uint)abs(speed) * MOTOR_HIGH / SPEED_MAX;
    uint pulsed = MOTOR_HIGH - drive;  // the other input: high (brake) except while driving
    pwm_set_gpio_level(PIN_MOT_IN1, speed >= 0 ? MOTOR_HIGH : pulsed);
    pwm_set_gpio_level(PIN_MOT_IN2, speed >= 0 ? pulsed : MOTOR_HIGH);
}

void steering_set(int steer) {
    if (steer > 100) steer = 100;
    if (steer < -100) steer = -100;
    pwm_set_gpio_level(PIN_SERVO, (uint16_t)(STEER_CENTRE_US + steer * STEER_RANGE_US / 100));
}
