#!/usr/bin/env python3
"""Write a scenario.json for the 50 x 60 mm probe-fed patch (spec section 10).

    python3 worker/make_patch_scenario.py JOB_DIR [--mesh-mm 1.9] [--threads 4]

Geometry (mm): FR-4 board 60 (x) by 50 (y) by 1.6, Er 4.3, tan d 0.02, ground
plane on the bottom face, patch W 36.83 (y) by L 28.54 (x) on top, and a lumped
50 ohm port acting as the probe, 8 mm from the patch centre along x. The domain
is the one used for the spec's measured table: x +-55, y +-60, z -16.67..33.33.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from meshing import smooth_lines  # noqa: E402

W, L, H = 36.83, 28.54, 1.6
BOARD_X, BOARD_Y = 60.0, 50.0
ER, TAND = 4.3, 0.02
FEED_X = -8.0


def build(mesh_mm: float, threads: int, f0: float, fc: float, quality: str) -> dict:
    def rect(x0, y0, x1, y1):
        a, b, c, d = [x0, y0], [x1, y0], [x1, y1], [x0, y1]
        return [[a, b, c], [a, c, d]]

    zsub = [round(H * i / 4, 6) for i in range(5)]
    x = smooth_lines([-55, -BOARD_X / 2, -L / 2, FEED_X, 0.0, L / 2, BOARD_X / 2, 55], mesh_mm)
    y = smooth_lines([-60, -BOARD_Y / 2, -W / 2, 0.0, W / 2, BOARD_Y / 2, 60], mesh_mm)
    z = smooth_lines([-50 / 3, *zsub, 100 / 3], mesh_mm)
    return {
        "name": "patch_50x60",
        "quality": quality,
        "f0_hz": f0,
        "band_hz": [f0 - fc, f0 + fc],
        "fc_hz": fc,
        "unit_mm": 1,
        "mesh": {"x": x, "y": y, "z": z},
        "materials": [],
        "metals": [
            {"name": "ground", "z_mm": 0.0,
             "triangles": rect(-BOARD_X / 2, -BOARD_Y / 2, BOARD_X / 2, BOARD_Y / 2)},
            {"name": "patch", "z_mm": H, "triangles": rect(-L / 2, -W / 2, L / 2, W / 2)},
        ],
        "vias": [],
        "substrates": [{
            "name": "fr4", "zmin": 0.0, "zmax": H, "epsilon_r": ER, "loss_tangent": TAND,
            "box": [-BOARD_X / 2, -BOARD_Y / 2, BOARD_X / 2, BOARD_Y / 2],
        }],
        "port": {"start": [FEED_X, 0.0, 0.0], "end": [FEED_X, 0.0, H], "impedance_ohm": 50.0},
        "boundary": "MUR",
        "dumps": {
            "field_xy": {"z_mm": H + 2.0},
            "field_xz": {"y_mm": 0.0},
            "pulse": {"z_mm": H + 2.0, "max_frames": 80},
            "current": {"metal": "patch"},
            "max_px": 256,
        },
        "pattern_center_mm": [0.0, 0.0, H],
        "threads": threads,
        "end_criteria": -40.0,
        "max_timesteps": 60000,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("job_dir")
    ap.add_argument("--mesh-mm", type=float, default=1.9)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--f0", type=float, default=2.5e9)
    ap.add_argument("--fc", type=float, default=1.0e9)
    ap.add_argument("--quality", default="screen", choices=["screen", "confirm"])
    a = ap.parse_args()
    job = pathlib.Path(a.job_dir)
    job.mkdir(parents=True, exist_ok=True)
    sc = build(a.mesh_mm, a.threads, a.f0, a.fc, a.quality)
    (job / "scenario.json").write_text(json.dumps(sc))
    n = [len(sc["mesh"][k]) for k in "xyz"]
    print(f"wrote {job / 'scenario.json'}: mesh {n[0]}x{n[1]}x{n[2]} = {n[0]*n[1]*n[2]} cells")


if __name__ == "__main__":
    main()
