// Runs firmware in rp2040js (Wokwi's RP2040 emulator) for `eda` co-simulation.
//
// Usage: node rp2040_driver.mjs <rp2040js package dir>
// Protocol: one JSON request per stdin line, one JSON reply per stdout line.
//   {"cmd":"init","bootrom":PATH,"image":PATH.uf2,"pins":[15,...],"uarts":[0,...]}
//     -> {"ok":true}
//   {"cmd":"run","until":NANOS,"inputs":{"14":true},"adc":{"0":2048},"break_on_mode":true}
//     -> {"ok":true,"t":NANOS,"pins":{"15":[[NANOS,"high"],...]},"uart":{"0":"text"},
//         "halted":null|CODE,"early":false}
//   Each pin's list starts with its state at the start of the window, followed by changes.
//   With break_on_mode the run returns early ("early": true) right after a watched pin
//   changes mode (anything but a high<->low output toggle), so the circuit can respond.
//   States: high, low, hiz, pullup, pulldown, keeper.
import { readFileSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';

const pkg = process.argv[2];
const load = (rel) => import(pathToFileURL(`${pkg}/dist/esm/${rel}`).href);
const { RP2040 } = await load('rp2040.js');
const { SimulationClock } = await load('clock/simulation-clock.js');
const { GPIOPinState } = await load('gpio-pin.js');

const STATE = {
  [GPIOPinState.High]: 'high',
  [GPIOPinState.Low]: 'low',
  [GPIOPinState.Input]: 'hiz',
  [GPIOPinState.InputPullUp]: 'pullup',
  [GPIOPinState.InputPullDown]: 'pulldown',
  [GPIOPinState.InputBusKeeper]: 'keeper',
};
const FLASH_START = 0x10000000;
const UF2_MAGIC = 0x0a324655;
const CYCLE_NANOS = 1e9 / 125e6; // rp2040js runs the core at a fixed 125 MHz

const quiet = {
  debug() {},
  info() {},
  warn(component, message) {
    process.stderr.write(`[${component}] ${message}\n`);
  },
  error(component, message) {
    process.stderr.write(`[${component}] ${message}\n`);
  },
};

let clock = null;
let mcu = null;
let watched = [];
let segments = {};
let uart = {};
let halted = null;
let modeChanged = false;
const DRIVEN = new Set(['high', 'low']);

function loadUF2(path) {
  const data = readFileSync(path);
  for (let offset = 0; offset + 512 <= data.length; offset += 512) {
    const view = new DataView(data.buffer, data.byteOffset + offset, 512);
    if (view.getUint32(0, true) !== UF2_MAGIC) continue;
    const address = view.getUint32(12, true);
    const size = view.getUint32(16, true);
    mcu.flash.set(data.subarray(offset + 32, offset + 32 + size), address - FLASH_START);
  }
}

function init(request) {
  clock = new SimulationClock();
  mcu = new RP2040(clock);
  mcu.logger = quiet;
  const rom = readFileSync(request.bootrom);
  mcu.loadBootrom(new Uint32Array(rom.buffer, rom.byteOffset, rom.length / 4));
  loadUF2(request.image);
  watched = request.pins;
  for (const index of watched) {
    mcu.gpio[index].addListener((state) => {
      const list = segments[index];
      const name = STATE[state];
      const previous = list[list.length - 1][1];
      if (previous === name) return;
      if (!DRIVEN.has(previous) || !DRIVEN.has(name)) modeChanged = true;
      if (list[list.length - 1][0] === clock.nanos) list.pop();
      list.push([clock.nanos, name]);
    });
  }
  for (const index of request.uarts) {
    mcu.uart[index].onByte = (value) => {
      uart[index] = (uart[index] ?? '') + String.fromCharCode(value);
    };
  }
  mcu.onBreak = (code) => {
    halted = code;
  };
  mcu.core.PC = FLASH_START;
  return { ok: true };
}

function run(request) {
  for (const [index, value] of Object.entries(request.inputs ?? {})) {
    mcu.gpio[Number(index)].setInputValue(value);
  }
  for (const [channel, code] of Object.entries(request.adc ?? {})) {
    mcu.adc.channelValues[Number(channel)] = code;
  }
  segments = {};
  for (const index of watched) segments[index] = [[clock.nanos, STATE[mcu.gpio[index].value]]];
  uart = {};
  modeChanged = false;
  const until = request.until;
  const breakOnMode = Boolean(request.break_on_mode);
  while (clock.nanos < until && !(breakOnMode && modeChanged)) {
    const left = until - clock.nanos;
    if (halted !== null || mcu.core.waiting) {
      const next = clock.nanosToNextAlarm;
      clock.tick(halted === null && next > 0 && next < left ? next : left);
    } else {
      clock.tick(mcu.core.executeInstruction() * CYCLE_NANOS);
    }
  }
  const early = breakOnMode && modeChanged && clock.nanos < until;
  return { ok: true, t: Math.min(clock.nanos, until), pins: segments, uart, halted, early };
}

const lines = createInterface({ input: process.stdin });
for await (const line of lines) {
  if (!line.trim()) continue;
  let reply;
  try {
    const request = JSON.parse(line);
    if (request.cmd === 'init') reply = init(request);
    else if (request.cmd === 'run') reply = run(request);
    else reply = { ok: false, error: `unknown command ${request.cmd}` };
  } catch (error) {
    reply = { ok: false, error: String(error?.stack ?? error) };
  }
  process.stdout.write(JSON.stringify(reply) + '\n');
}
