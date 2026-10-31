"""Pack a solver pulse.bin for the bundled sample runs (see lib/pulse-codec.mjs for the format).

    python3 web/scripts/pack-pulse.py <run folder with the solver's float32 pulse.bin and pulse.json> <out folder>

Every frame, row, column and time of the run is kept. Values are stored as one byte each on the square-root curve the
shaders draw with, then gzip compressed. The largest change to a drawn value is 1/255 of the colour ramp, the same step the
256-entry ramp itself has. Needs numpy.
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import numpy as np


def pack(src: Path, out: Path) -> None:
    meta = json.loads((src / "pulse.json").read_text())
    if meta.get("dtype") == "uint8":
        raise SystemExit("this pulse.bin is already packed; start from the solver's float32 file")
    frames, rows, cols = meta["shape"]
    raw = np.fromfile(src / meta["file"], dtype="<f4")
    if raw.size != frames * rows * cols:
        raise SystemExit(f"{meta['file']} holds {raw.size} values, pulse.json says {frames * rows * cols}")
    q = np.round(255.0 * np.sqrt(np.clip(raw, 0.0, 1.0))).astype(np.uint8)
    out.mkdir(parents=True, exist_ok=True)
    # mtime 0 keeps the file byte for byte the same each time it is packed
    (out / "pulse.bin").write_bytes(gzip.compress(q.tobytes(), compresslevel=9, mtime=0))
    packed = dict(meta)
    packed["dtype"] = "uint8"
    packed["encoding"] = "gzip"
    packed["layout"] = "uint8 [frames, rows(y), cols(x)], row-major, q = round(255 * sqrt(|E| / scale)), gzip compressed"
    packed["packed_from"] = f"the solver's float32 pulse.bin ({raw.nbytes} bytes), same frames, rows, columns and times; only the value precision differs"
    (out / "pulse.json").write_text(json.dumps(packed, separators=(",", ":")))
    err = float(np.abs(q / 255.0 - np.sqrt(np.clip(raw, 0.0, 1.0))).max())
    print(f"{src} -> {out}: {raw.nbytes} -> {(out / 'pulse.bin').stat().st_size} bytes, largest error {err:.5f} (in sqrt units)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    pack(Path(sys.argv[1]), Path(sys.argv[2]))
