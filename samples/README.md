# Sample boards

These boards were designed by the TrustPCB author. The board designs (outline, copper, placement and schematic) are released under the repository's MIT licence for testing and demonstration. The KiCad library footprints, symbols and 3D model references inside them are not: see the licence note below.

| Folder | Board |
|---|---|
| `nrf52840-node` | USB-powered nRF52840 sensor node with a printed meander antenna |
| `nrf52840-node-v2` | The same board after the engineer's hand edits for the free re-tune the review confirmed: L1 becomes a 0.51 pF series capacitor, and C20 and C19 are fitted with 0.3 pF and 0.36 pF. Copper and geometry are unchanged, for version rechecks |
| `patch-2g5` | 2.5 GHz patch antenna on FR-4 |

Symbols, footprints and 3D model references that come from the KiCad libraries (the `.pretty` footprint folders, the `.kicad_sym` symbol files, and the `${KICAD10_3DMODEL_DIR}` model paths in the boards) remain under the KiCad library licence, CC BY-SA 4.0 with the KiCad libraries exception, which does not bind designs that use them. The 3D board model used by the web app's bundled sample (`web/public/fixtures/nrf/`) is built from those same library models and is covered the same way. All three boards are saved by KiCad 10.

`nrf52840-node-v2` is derived from `nrf52840-node`: only the three edited parts differ, so you can diff the two folders to see the change.

These boards are test material. They are unverified designs and contain real faults, which is why they are useful here. Do not manufacture them as they are.
