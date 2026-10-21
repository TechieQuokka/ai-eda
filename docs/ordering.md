# Ordering a board and soldering it yourself

From a passing design to a bare board from a PCB house and parts you solder by hand. Assembly services (PCBA) are not covered: the BOM has no distributor part numbers and there is no pick-and-place file in their formats.

## 1. Get the design ready

1. Give the project a board and name the PCB house in `design/board.yaml`:

   ```yaml
   board: {}      # size, placement, and routing are automatic
   fab: jlcpcb    # check everything against JLCPCB's limits
   ```

   For another house, copy its limits from its capabilities page instead, e.g. `fab: { name: myhouse, min_track: 0.15mm, min_clearance: 0.15mm, min_drill: 0.3mm, min_pad_ring: 0.15mm }` (only the limits given are checked; the field list is in `DESIGN_DECISIONS.md` §23).

2. Give a part number to every part whose value alone does not name what to buy, as `mpn:` in `design/circuit.yaml`. The generic `opamp` needs one; it is a model, not a chip:

   ```yaml
   U1: { type: opamp, gbw: 1MHz, mpn: UA741 }
   ```

3. Run `uv run eda run projects/<name> -m "Board for ordering"` until it exits with 0, and read the warnings:
   - `fab_limit` (error): something is finer than the house can make; the message names the rule and the limit.
   - `bom_generic_part`: a generic model without `mpn:`.
   - `unused_pads`: unused pins of an IC; check the datasheet (unused op-amp channels are usually tied off).
   - `drc_*`, `unrouted`: the board itself is not finished.

4. Look at `out/board.png` (copper) and `out/assembly.png` (parts) or the Board card in `eda watch`. Move a part by hand only for a mechanical reason, e.g. a connector at the edge (`placement:` in `board.yaml`).

## 2. Order the bare board

The file to upload is `out/pcb/<name>-fab.zip`: Gerbers for both copper layers, solder mask, silkscreen, paste, and the board outline, plus the drill file. The BOM and position file inside it are ignored by a bare-board order.

At the PCB house (JLCPCB, PCBWay, and others take the same zip):

| Option | Choose | Why |
|---|---|---|
| Layers | 2 | every board here is two-layer |
| Size | as detected | the house reads it from the outline; it matches `data.pcb.size_mm` |
| Thickness | 1.6 mm | the standard; through-hole parts and headers expect it |
| Copper | 1 oz | the limits in `fab: jlcpcb` assume 1 oz |
| Surface finish | HASL (lead-free if you like) | cheapest, easy to solder by hand |
| Quantity | the minimum (usually 5) | |
| Assembly | off | you solder the parts |

Check the house's own Gerber preview before paying: the outline, the holes, and the silkscreen text should look like `out/board.png`.

## 3. Buy the parts

`out/pcb/fab/<name>-bom.csv` is the shopping list, one row per item:

| Column | Meaning |
|---|---|
| Qty | how many to buy (buy a few spare small parts) |
| References | where they go on the board |
| Part, Value | what it is: `Resistor 10kΩ`, `LED green` |
| MPN | the part number you gave with `mpn:` |
| Footprint | the package the board is made for: `R_Axial_DIN0207_..._P10.16mm` is a 1/4 W axial resistor on a 10.16 mm (0.4 in) pitch, `LED_D5.0mm` a 5 mm LED, `C_Disc_D5.0mm_..._P5.00mm` a ceramic disc capacitor on a 5 mm pitch, `PinHeader_1x02_P2.54mm` a 2.54 mm header |
| Note | `supply / signal input` rows are the connectors where power or a signal comes in; `generic model` rows still need a real part |

The BOM does not say what the simulation cannot know: pick capacitors rated for at least twice the voltage across them, and resistors whose power rating (1/4 W for the default footprint) covers `current(R)² · R` from the simulation.

A package that does not match the footprint will not fit: an `SOIC-8` row needs the surface-mount chip, a `DIP-8` row the through-hole one.

## 4. Solder

Print `out/assembly.svg` at 100 % scale: it is the board at true size, with every part's outline, reference, and value.

- Solder low parts first: resistors and diodes, then ICs, LEDs, buttons, pots, and connectors last.
- Pad 1 is the square pad. Mind it on polarized parts:
  - LED: pad 1 is the cathode (short leg, flat side of the body).
  - Connectors made from sources: pad 1 is `+`, pad 2 is `-`.
  - ICs: pad 1 matches the dot or notch on the chip.
- Surface-mount ICs (SOIC) need a fine tip and flux: tack one corner pin, align, then solder the rest.
- The Raspberry Pi Pico sits on its through-hole row; solder it with its pins or with headers.

## 5. Power it up

1. Before applying power, measure the resistance between each supply connector's pins: it must not be a short.
2. Use a bench supply with a current limit a little above the expected current (supply currents are in `data.sim.tests.<test>.operating_point`).
3. Compare node voltages with the same operating point. A voltage that differs a lot points at a wrong part, a reversed part, or a solder bridge.
