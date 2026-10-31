from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[2]
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))
ROOT = SERVER.parent
NRF_PCB = ROOT / "samples" / "nrf52840-node" / "nrf52840_rf_iot_node.kicad_pcb"
PATCH_PCB = ROOT / "samples" / "patch-2g5" / "patch_antenna.kicad_pcb"


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: runs the solver in Docker (minutes)")


@pytest.fixture(scope="session")
def nrf_board():
    from trustpcb.pcb.model import parse_board

    return parse_board(NRF_PCB, "v-nrf")


@pytest.fixture(scope="session")
def patch_board():
    from trustpcb.pcb.model import parse_board

    return parse_board(PATCH_PCB, "v-patch")


@pytest.fixture(scope="session")
def nrf_setup(nrf_board):
    from trustpcb.rf.setup import plan_setup

    s = plan_setup(nrf_board)
    assert s.simulatable
    return s


@pytest.fixture(scope="session")
def patch_setup(patch_board):
    from trustpcb.rf.setup import plan_setup

    s = plan_setup(patch_board)
    assert s.simulatable
    return s
