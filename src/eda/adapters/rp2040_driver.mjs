// Runs firmware in rp2040js (Wokwi's RP2040 emulator) for `eda` co-simulation.
//
// Usage: node rp2040_driver.mjs <rp2040js package dir>
// Protocol: one JSON request per stdin line, one JSON reply per stdout line.
//   {"cmd":"init","bootrom":PATH,"image":PATH.uf2,"pins":[15,...],"uarts":[0,...]}
//     -> {"ok":true}
//   {"cmd":"run","until":NANOS,"inputs":{"14":true},"adc":{"0":2048},"break_on_mode":true,
//    "send":{"0":"text"}}
//     -> {"ok":true,"t":NANOS,"pins":{"15":[[NANOS,"high"],...]},"uart":{"0":"text"},
//         "halted":null|CODE,"halted_at":null|NANOS,"faults":[[NANOS,"message"],...],
//         "early":false}
//   Each pin's list starts with its state at the start of the window, followed by changes.
//   With break_on_mode the run returns early ("early": true) right after a watched pin
//   changes mode (anything but a high<->low output toggle), so the circuit can respond.
//   `send` queues bytes for a UART's receiver; they arrive one character time apart at the
//   baud rate the firmware configured (115200 until it sets one).
//   Transmission is paced the same way: the UART reports its TX FIFO full and busy until
//   the bytes written to it would have been shifted out, so firmware that prints waits as
//   long as on the real chip (rp2040js itself reports an always-empty FIFO).
//   `faults` lists accesses to unmapped memory during the window (a HardFault on the real
//   chip; rp2040js carries on). `halted_at` is when the core stopped on a BKPT.
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
    if (component === 'RP2040' && /undefined address|invalid memory address/i.test(message)) {
      if (faults.length < 10) faults.push([clock.nanos, message]);
    }
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
let haltedAt = null;
let faults = [];
let modeChanged = false;
const rxQueue = {}; // UART index -> bytes still to arrive
let nextRx = 0; // nanos at which the next queued byte arrives
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
    paceTransmit(mcu.uart[index]);
  }
  mcu.onBreak = (code) => {
    if (halted === null) haltedAt = clock.nanos;
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
  for (const [index, text] of Object.entries(request.send ?? {})) {
    const queue = (rxQueue[index] ??= []);
    if (queue.length === 0) nextRx = Math.max(nextRx, clock.nanos);
    queue.push(...Buffer.from(text, 'utf8'));
  }
  segments = {};
  for (const index of watched) segments[index] = [[clock.nanos, STATE[mcu.gpio[index].value]]];
  uart = {};
  faults = [];
  modeChanged = false;
  const until = request.until;
  const breakOnMode = Boolean(request.break_on_mode);
  while (clock.nanos < until && !(breakOnMode && modeChanged)) {
    receive();
    let left = until - clock.nanos;
    if (pendingRx()) left = Math.max(0, Math.min(left, nextRx - clock.nanos));
    if (halted !== null || mcu.core.waiting) {
      const next = clock.nanosToNextAlarm;
      clock.tick(halted === null && next > 0 && next < left ? next : Math.max(left, 1));
    } else {
      clock.tick(mcu.core.executeInstruction() * CYCLE_NANOS);
    }
  }
  const early = breakOnMode && modeChanged && clock.nanos < until;
  const t = Math.min(clock.nanos, until);
  return { ok: true, t, pins: segments, uart, halted, halted_at: haltedAt, faults, early };
}

const UARTDR = 0x0;
const TXFF = 1 << 5;
const TXFE = 1 << 7;
const BUSY = 1 << 3;

function baudOf(port) {
  return port.baudRate > 0 && Number.isFinite(port.baudRate) ? port.baudRate : 115200;
}

// Make a UART's flag register say "TX FIFO full / busy" until the bytes written to UARTDR
// would have left the wire at the configured baud rate.
function paceTransmit(port) {
  let idleAt = 0; // nanos when the last written character has been shifted out
  const charNanos = () => {
    const lcr = port.lineCtrlRegister;
    const bits = 1 + port.wordLength + (lcr & (1 << 3) ? 2 : 1) + (lcr & (1 << 1) ? 1 : 0);
    return (1e9 * bits) / baudOf(port);
  };
  const flags = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(port), 'flags').get;
  Object.defineProperty(port, 'flags', {
    get() {
      let value = flags.call(port);
      const queued = Math.ceil((idleAt - clock.nanos) / charNanos());
      if (queued > 0) value = (value & ~TXFE) | BUSY;
      if (queued >= (port.fifosEnabled ? 32 : 1)) value |= TXFF;
      return value;
    },
  });
  const write = port.writeUint32.bind(port);
  port.writeUint32 = (offset, value) => {
    if (offset === UARTDR) idleAt = Math.max(idleAt, clock.nanos) + charNanos();
    write(offset, value);
  };
}

function pendingRx() {
  return Object.values(rxQueue).some((queue) => queue.length > 0);
}

function receive() {
  if (clock.nanos < nextRx) return;
  let gap = 0;
  for (const [index, queue] of Object.entries(rxQueue)) {
    if (queue.length === 0) continue;
    const port = mcu.uart[Number(index)];
    port.feedByte(queue.shift());
    const baud = port.baudRate > 0 && Number.isFinite(port.baudRate) ? port.baudRate : 115200;
    gap = Math.max(gap, 1e10 / baud); // 10 bits per character (8N1)
  }
  if (gap > 0) nextRx = clock.nanos + gap;
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
