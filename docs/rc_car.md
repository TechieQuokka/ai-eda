# Building the RC car and its remote

Two boards, both ordered bare and soldered by hand (`docs/ordering.md` covers ordering and
soldering in general): `projects/rc_car` and `projects/rc_remote`. Each is 70 x 64 mm, two
layers, through-hole only; upload `out/pcb/<name>-fab.zip`, and `out/assembly.png` shows
where each part goes. The modules (Pico, motor driver, regulators, HC-05) sit on pin
headers, so they can be swapped and reused.

## 1. Shopping list

### Car (`projects/rc_car`)

| Qty | Part | Notes |
|---|---|---|
| 1 | Car chassis with one rear 130-size DC motor (gearbox) and a steerable front axle | the motor's wires go to the M1 pads |
| 1 | SG90 micro servo (or MG90S) | steering; plugs onto S1 (signal, +5 V, GND: orange, red, brown) |
| 1 | 4 x AA battery box with a switch and leads | BT1 pads; the switch carries the motor current |
| 4 | AA cells (alkaline, or NiMH: 4.8 V still drives) | |
| 1 | Raspberry Pi Pico (with or without headers) | U1 |
| 1 | Pololu DRV8833 dual motor driver carrier (#2130) | U3; two 1x8 headers, rows 10.16 mm apart |
| 1 | Pololu S7V8F5 5 V step-up/step-down regulator (#2123) | U2 |
| 1 | HC-05 Bluetooth module on a 6-pin breakout (ZS-040) | U4; set up as the slave: `docs/hc05_pairing.md` |
| 1 | 1N5822 Schottky diode (3 A) | D1, reverse polarity |
| 1 | 1N5817 Schottky diode | D2 |
| 1 | 470 uF 16 V electrolytic, 8 mm | CB, across the motor supply |
| 1 | 220 uF 16 V electrolytic, 6.3 mm | C5, the 5 V rail (servo current steps) |
| 2 | 100 nF ceramic, 5 mm pitch | CM (soldered across the motor's terminals is better still), CS |
| 1 | 200 kohm, 1 | 100 kohm, 1 | 330 ohm resistors (1/4 W) | RS1, RS2, RL1 |
| 1 | 5 mm LED, green | LED1, status |

### Remote (`projects/rc_remote`)

| Qty | Part | Notes |
|---|---|---|
| 1 | 2 x AA battery box with a switch and leads | BT1 pads |
| 2 | AA cells | |
| 1 | Raspberry Pi Pico | U1 |
| 1 | Pololu U1V11F5 5 V step-up regulator (#2562) | U2 |
| 1 | HC-05 Bluetooth module (6-pin breakout) that can be a master | U4; `docs/hc05_pairing.md` |
| 1 | Two-axis thumb joystick module (KY-023 type) | J1 |
| 2 | 1N5817 Schottky diodes | D1, D2 |
| 1 | 100 nF ceramic | CS |
| 2 | 100 kohm, 3 | 330 ohm resistors | RS1, RS2, RL1-RL3 |
| 3 | 5 mm LEDs: green, yellow, red | LINK, SLOW, BATT |

### For both

Female pin headers (2.54 mm) for the modules: per board one pair of 1x20 for the Pico, a
1x4 for the regulator, a 1x6 for the HC-05; on the car two 1x8 for the DRV8833 carrier and
a 1x3 male header for the servo; on the remote a 1x5 for the joystick. Wire for the battery
and motor leads (the motor's: 0.5 mm2 or thicker).

The BOM of each board, grouped, is `out/pcb/fab/<name>-bom.csv`.

## 2. Before soldering

1. Pair the HC-05 modules (`docs/hc05_pairing.md`): car = slave at 9600 baud, remote =
   master bound to the car's address.
2. Build and flash the firmware: `uv run eda fw projects/rc_car --build` (and
   `rc_remote`) writes `out/fw-build/<name>.uf2`. Hold the Pico's BOOTSEL button while
   plugging it in by USB and copy the `.uf2` onto the drive that appears.

## 3. Assembly notes

- Low parts first (resistors, diodes, ceramic capacitors), then the LEDs, the electrolytic
  capacitors (mind the + marking), the headers, the wire pads last.
- Diodes: the band is the cathode, at the square-padded K end on the silkscreen.
- Car: the battery and motor wires enter on the left edge. Solder them directly
  (0.5 mm2 for the motor). The tracks from the battery through the driver to the motor
  are 1 mm wide.
- Remote: the joystick module's header is in the middle of the right half. A KY-023's own
  pins point sideways, so it plugs in upright; for a stick that points up, connect it with
  a short 5-wire female-female cable and mount the module on the case.
- Steering: the firmware's full lock is 1.0 / 2.0 ms pulses (`STEER_RANGE_US` in
  `projects/rc_car/firmware/drive.c`). If the servo hums or pushes against the chassis at
  full lock, reduce it: a stalled SG90 draws about 650 mA.

## 4. First power-up

1. Car on, remote off: the car's LED blinks once a second (no commands). The motor
   stays still.
2. Remote on: after a few seconds its LINK LED stops blinking and stays on; the car's LED
   stays on too.
3. Stick ahead: the car accelerates over about a quarter of a second. Let go: it slows to
   a stop. Stick back: after a short pause at standstill it reverses.
4. Press the stick: the SLOW LED lights, the throttle is halved.
5. Switch the remote off: the car brakes and centres the steering within 0.3 s.
6. BATT LED: steady = the remote's cells are low; blinking = the car's cells are low (the
   car measures them while it stands still).

If the remote is switched on with the stick pushed, the throttle stays at 0 until the
stick has been back in the centre once.
