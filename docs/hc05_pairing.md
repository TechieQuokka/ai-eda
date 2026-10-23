# Pairing the two HC-05 modules

The car (`projects/rc_car`) and the remote (`projects/rc_remote`) each carry an HC-05
Bluetooth serial module on the common 6-pin breakout (ZS-040 and similar: EN, VCC, GND,
TXD, RXD, STATE). Out of the box both are slaves waiting for a connection. Set them up
once, before they go on the boards: the car's module stays a slave, the remote's becomes a
master that connects only to the car's. After that the two find each other by themselves
every time both are switched on, within a few seconds, and the firmware only sees a serial
line at 9600 baud.

## What you need

- A USB-to-serial adapter with **3.3 V logic** (CP2102, CH340, FT232 boards with a 3.3 V
  setting). The HC-05's RXD is a 3.3 V input: with a 5 V adapter, put a divider
  (1 kohm in series, 2 kohm to GND) in its TX line.
- Four jumper wires, and a serial terminal (`picocom`, `minicom`, PuTTY, the Arduino IDE
  serial monitor) that sends **CR+LF** after each line.

Wiring: adapter 5V (or VCC) to HC-05 VCC, GND to GND, adapter TX to HC-05 RXD, adapter
RX to HC-05 TXD. EN stays open.

## Entering AT mode

1. Unplug the adapter (the module off).
2. Hold the small button on the breakout (it pulls EN high) and plug the adapter in; let
   go after a second.
3. The module's LED now blinks slowly (about once every 2 s): AT mode, **38400 baud**,
   8N1, whatever its data baud rate is.
4. Open the terminal at 38400 baud and type `AT`. It answers `OK`.

Some modules only accept commands while the button is held; hold it while you send each
command if `AT` gets no answer.

## The car's module (slave)

```
AT+ORGL            (factory settings; optional)
AT+ROLE=0          slave
AT+NAME=RC-CAR
AT+PSWD="1234"     (older firmware: AT+PSWD=1234)
AT+UART=9600,0,0   9600 baud, 1 stop bit, no parity: what the firmware uses
AT+ADDR?           answers e.g. +ADDR:98d3:31:fb1a2c  -- write it down
```

## The remote's module (master)

```
AT+ORGL
AT+ROLE=1          master
AT+CMODE=0         connect only to the bound address
AT+BIND=98d3,31,fb1a2c   the car's address from AT+ADDR?, colons replaced by commas
AT+NAME=RC-REMOTE
AT+PSWD="1234"     the same PIN as the car's
AT+UART=9600,0,0
```

`AT+ROLE?`, `AT+CMODE?`, `AT+BIND?`, and `AT+UART?` read the settings back.

## Checking the link

Unplug both, plug the modules in normally (without the button), both powered:

- Searching or waiting: the LED blinks fast (several times a second).
- Linked: the LED gives two short blinks every 2 s, and STATE goes high. The remote's
  LINK LED and the car's status LED follow STATE (the car's firmware brakes as soon as it
  goes low).

With both modules on adapters you can type on one terminal (9600 baud now) and see the
text on the other: `D,50,0` typed on the remote's side is what the car receives.

## If it does not link

- Both modules must have the same PIN and the same baud rate.
- `AT+BIND` takes the address with commas; a mistyped address keeps the master searching.
- Some clone modules (with firmware that answers `AT+VERSION?` with something other than
  `2.0-20100601` or `3.0-20170601`) cannot be a master. Use a module that can, for the
  remote.
- `AT+RMAAD` clears the master's list of paired devices if it keeps trying an old one.
