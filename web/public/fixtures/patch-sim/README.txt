patch-sim: real solver output, nothing synthetic

What this is
  The processed output of one run of worker/run_scenario.py (openEMS v0.37.0, FDTD, built
  from source) on the 50 x 60 mm probe-fed patch from spec section 10: FR-4 1.6 mm,
  Er 4.3, tan d 0.02, patch 36.83 (y) x 28.54 (x) mm, 50 ohm lumped port as the probe 8 mm
  from the patch centre along -x, ground plane on the bottom face.
  Mesh 1.9 mm (screen quality), 64 x 67 x 38 = 162,944 cells, MUR boundaries.
  Generated with: python3 worker/make_patch_scenario.py JOB --mesh-mm 1.9
  then: docker run --rm --cpus 4 --memory 6g -v JOB:/job trustpcb-ems:dev python3 /app/run_scenario.py /job
  scenario.json is the exact input. Raw solver files were deleted by the worker.

Headline numbers (this run; repeat runs vary by about 0.5 dB in S11 depth)
  resonance 2.36 GHz, S11 -27.6 dB, Zin 47.0 + j2.8 ohm, max directivity 6.30 dBi,
  trust status ok (energy decayed -51 dB, 22 cells per wavelength in the substrate).
  This is a simplified model (probe port instead of a feed line). Not a measurement.
  "Simulated. Not a substitute for a real antenna measurement."

Coordinates
  Scenario millimetres. x right, y up, z up from the bottom copper face (the ground plane
  is z = 0, the patch is z = 1.6). The patch is centred on the origin. The worker does no y
  flip: when the server builds scenarios from KiCad boards it must negate y consistently.

Files
  s11.json      {freq_hz, s11_db, s11_re, s11_im}, 401 points, 1.5 to 3.5 GHz
  zin.json      {freq_hz, z_re, z_im, z0_ohm}
  pattern.json  directivity_dbi[theta_index 37][phi_index 73] at the resonance, 5 degree grid
                (theta 0..180 from +z, phi 0..360 from +x toward +y), floor -40 dBi
  field.json    sidecar for field_xy.bin and field_xz.bin (shape, plane position, scale)
  field_xy.bin  float32 little-endian [2, 133, 122]: real plane then imag plane, rows = y
                increasing, columns = x increasing. Complex Ex at 2.5 GHz on the plane z = 2.90 mm.
  field_xz.bin  float32 little-endian [2, 57, 127]: rows = z increasing, columns = x increasing.
                Complex Ez at 2.5 GHz on the plane y = -0.92 mm.
                Both divided by "scale" (99.5th percentile of the complex magnitude), arbitrary units.
                Animate with  value(t) = real * cos(wt) - imag * sin(wt).
  pulse.json    sidecar for pulse.bin
  pulse.bin     float32 little-endian [80, 133, 122]: |E| vector magnitude of the time-domain
                field on the plane z = 2.90 mm, 80 frames from 0.61 ns to 3.51 ns (times_s in the
                sidecar), divided by the maximum over all stored frames (so 0..1).
  current.json  sidecar for current.bin
  current.bin   float32 little-endian [135, 124]: surface current magnitude on the patch copper at
                2.5 GHz, rows = y increasing, columns = x increasing, zero outside copper,
                divided by the 99.5th percentile over copper (edge and probe peaks exceed 1).
  result.json   summary and trust block
  progress.jsonl solver progress log as streamed during the run

Note: the field and current planes are at the excitation centre frequency f0 = 2.5 GHz, which is
about 140 MHz above this patch's resonance. That is the scenario's target frequency, not the
resonance.
