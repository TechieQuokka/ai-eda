// Motor (DRV8833, both bridges in parallel) and steering servo.
#ifndef DRIVE_H
#define DRIVE_H

#include <stdint.h>

#define SPEED_MAX 1000  // speed in 1/1000 of full: -1000 full reverse .. 1000 full forward

void drive_init(void);

// Motor at `speed` (-SPEED_MAX..SPEED_MAX) right away; 0 brakes (both outputs low).
void motor_set(int speed);

// Steering from -100 (full left) through 0 (straight) to 100 (full right).
void steering_set(int steer);

#endif
