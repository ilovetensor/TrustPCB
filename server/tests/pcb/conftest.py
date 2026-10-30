from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[2]
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

SAMPLES = SERVER.parent / "samples"
NRF_DIR = SAMPLES / "nrf52840-node"
NRF_PCB = NRF_DIR / "nrf52840_rf_iot_node.kicad_pcb"
PATCH_DIR = SAMPLES / "patch-2g5"
PATCH_PCB = PATCH_DIR / "patch_antenna.kicad_pcb"

from trustpcb.pcb import kicad_cli  # noqa: E402

HAS_KICAD = kicad_cli.find_kicad_cli() is not None
needs_kicad = pytest.mark.skipif(not HAS_KICAD, reason="kicad-cli not installed")


@pytest.fixture(scope="session")
def nrf_board():
    from trustpcb.pcb.model import parse_board

    return parse_board(NRF_PCB, "v-nrf")


@pytest.fixture(scope="session")
def patch_board():
    from trustpcb.pcb.model import parse_board

    return parse_board(PATCH_PCB, "v-patch")


@pytest.fixture()
def nrf_copy(tmp_path):
    """A throwaway copy of the nRF sample project."""
    dst = tmp_path / "nrf"
    shutil.copytree(NRF_DIR, dst)
    return dst
