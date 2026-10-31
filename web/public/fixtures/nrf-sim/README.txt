nrf-sim: processed openEMS result for the nRF52840 node antenna
==================================================================

What was run (31 Oct 2026, this Mac, Docker image trustpcb-ems:dev, openEMS v0.37.0)
------------------------------------------------------------------------------------
Board     samples/nrf52840-node/nrf52840_rf_iot_node.kicad_pcb (as in the repository, unchanged)
Antenna   net ANT_FEED: 19 tracks, 31.0 mm meander, 1.0 mm wide, 0.3 mm feed segments, on F.Cu
Port      50 ohm lumped port at pad:L1:2 (32.485, 12.8), the antenna side of the matching network,
          from the F.Cu feed copper down to the B.Cu GND pour (z 1.565 mm to 0 mm)
Matching  schematic network read from the board: L1 2.2 nH series (fitted), C19 and C20 0.5 pF (not fitted)
Band      2.4 to 2.4835 GHz, field planes dumped at 2.44175 GHz, S11 swept 1.587 to 3.296 GHz
Stackup   default FR4 (the board file has no stackup): 1.565 mm between copper sheets, eps_r 4.5, tan d 0.02.
          This is an assumption, not a measurement.
Region    x 0 to 66, y 0 to 43.8 mm (a quarter wavelength around the antenna, clipped to the board edge);
          exact copper inside x 17 to 44.5, y 0 to 17.1, ground pours simplified outside it
Quality   confirm: 198 x 191 x 37 = 1,399,266 cells, smallest cell 0.069 mm, 29.3 cells per wavelength in the
          substrate, 2.2 cells across the narrowest antenna copper (0.3 mm), growth ratio at most 1.54,
          MUR boundary, end criterion -45 dB, 6 solver threads, docker run --cpus 6 --memory 6g
Run time  confirm 417 s (7.0 min), 119,210 timesteps. Screen run of the same antenna: 226 s (3.8 min).
Trust     ok (energy decayed to -46.2 dB, S11 never above 0 dB, mesh rule, port on copper with ground).

Files in this folder are the confirm run: summary.json (read this first), result.json, s11.json, zin.json,
pattern.json, field.json + field_xy.bin + field_xz.bin, current.json + current.bin, pulse.json + pulse.bin,
s11_as_built.json (L1 2.2 nH cascaded onto the antenna S11 with scikit-rf), meta.json (setup, mesh report,
assumptions), scenario.json (exactly what the solver read), model.png (the model that was simulated),
progress.jsonl, tune_matching.json (E24 search on this result, no solver time),
compare_screen_confirm.json.
pulse.bin is packed to keep the first page load small: the run's 80 frames of 212 x 257 values, one byte per value on
the square-root curve the viewer draws with (largest change 1/255 of the colour ramp), then gzip compressed. The solver wrote
17,434,880 bytes of float32; this file is 662,050. Frames, rows, columns and times are unchanged. See web/lib/pulse-codec.mjs
and web/scripts/pack-pulse.py. board.glb is stored as board.glb.gz in ../nrf (the same GLB bytes, gzip compressed).
screen/ holds the summary, S11, impedance, pattern and meta of the screen run of the same board.
whatif_shorten_6mm_screen/ holds a screen run with adjust_antenna_length -6 mm (what-if, model only).

Results (simulated; not a substitute for a real antenna measurement)
--------------------------------------------------------------------
- The antenna as drawn is not tuned to 2.4 GHz in this model. Its first series resonance is at about
  1.70 GHz (confirm) and its deepest S11 dip is 1.869 GHz, -23.2 dB. In the band, antenna-only S11 is
  -0.8 dB at worst (almost everything reflected); with L1 2.2 nH it is -0.5 dB. The antenna looks inductive
  (about 10 + j88 ohm at 2.44 GHz), which means it is electrically long for the band.
- Screen and confirm agree on the dip frequency (1.866 vs 1.869 GHz), the first resonance (within 1.6 percent),
  and the shape of the curve. They differ by 1.4 dB in band S11 and 37 percent in impedance at the band
  centre, just over the 35 percent agreement limit, so compare_screen_confirm.json says the confirm result
  replaces the screen result.
- Maximum directivity 4.3 dBi (screen 4.8) at the dip frequency, estimated radiated/accepted power 0.44
  (FR4 loss and mismatch). 4.3 dBi is a little above the 0 to 3 dBi usually quoted for a small antenna; the
  whole board acts as part of the radiator here, so it is plausible but unverified.
- What-if: shortening the antenna by 6 mm moves the dip from 1.87 to 2.25 GHz (screen), the direction and
  size expected for a 19 percent shorter antenna.
- tune_matching.json: with E24 parts in the three matching positions the best result reaches -6.1 dB at
  worst across the band (-12.5 dB at 2.44 GHz). That does not meet -10 dB across the band. The honest fix
  is the antenna length, not the matching network.

Conventions
-----------
- The worker flips nothing, so the scenario uses y = minus board y (right-handed). Sidecar v_mm is in scenario
  millimetres: board y = -v. Image rows of field_xy.bin and current.bin run in increasing scenario y, so row 0
  is the largest board y. Flip the rows to draw them with the board (the antenna is then at the top).
- field planes are at the band centre, the radiation pattern is at the deepest S11 dip (pattern.json f_hz).
- Not simulated: component bodies, solder mask, the 0.15 mm pour clearance and pad gaps (not resolved by the
  mesh), the USB connector end of the board (region ends at y 43.8), real stackup.

How to reproduce
----------------
server/trustpcb/rf: plan_setup(board) -> scenario_for(board, setup, "confirm") -> run_scenario(...) in lab.py,
or build_job(...) then
  docker run --rm --cpus 6 --memory 6g -v <job>:/job trustpcb-ems:dev python3 /app/run_scenario.py /job
