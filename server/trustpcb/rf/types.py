"""Typed values shared by the RF model builder.

``RfSetup`` is what the model builder needs to turn a parsed board into a solver job.
``NotSimulatable`` is returned instead whenever any step cannot be done with confidence:
the lab then shows "Not tested" with the reason. Nothing here guesses.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

C0 = 299_792_458.0


@dataclass(frozen=True)
class NotSimulatable:
    """The board, or this antenna on it, cannot be simulated. ``code`` is machine readable."""

    code: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)
    simulatable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"simulatable": False, "code": self.code, "reason": self.reason, "details": self.details}


@dataclass
class MatchElement:
    """One passive part of the matching network around the antenna feed."""

    ref: str
    kind: str  # L | C | R
    value: float | None  # henry, farad or ohm; None when the value text could not be read
    value_text: str
    populated: bool
    topology: str  # series | shunt
    #: net names the part joins; for a shunt the second one is ground
    nets: tuple[str, str]
    #: shunt parts hang on this net of the chain; series parts sit between two chain nets
    chain_net: str | None = None
    #: 0 is closest to the antenna
    position: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["nets"] = list(self.nets)
        return d


@dataclass
class MatchNetwork:
    """Series and shunt parts between the antenna and the source (radio chip or connector).

    ``chain`` lists the nets from the antenna side to the source side. ``elements`` are ordered
    from the antenna side. ``understood`` is false when something populated could not be placed.
    """

    chain: list[str]
    elements: list[MatchElement]
    source: str | None  # e.g. "pad:U3:H23" or "pad:J1:1"
    understood: bool = True
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain": self.chain,
            "elements": [e.to_dict() for e in self.elements],
            "source": self.source,
            "understood": self.understood,
            "notes": self.notes,
        }


@dataclass
class RfSetup:
    kind: str  # trace | patch
    antenna_id: str  # net:<name> or zone:<id>
    antenna_net: str
    antenna_layer: str
    antenna_targets: list[str]
    antenna_bbox: list[float]
    open_end: list[float] | None
    #: port position (board mm) and the pad or via it sits on
    port_xy: list[float]
    port_on: str | None
    port_layer: str
    ground_layer: str
    ground_net: str
    port_width_mm: float  # 0 means a point port
    port_axis: str  # x or y: the axis the strip port spans when port_width_mm > 0
    port_impedance_ohm: float
    band_ghz: list[float]  # the band that matters to the user
    f0_hz: float  # excitation centre
    fc_hz: float  # excitation half bandwidth; the solver sweeps f0 +- fc
    region: list[float]  # copper kept exactly or simplified, board mm [x0, y0, x1, y1]
    fine_zone: list[float]  # copper kept exact, everything finer than the far mesh lives here
    narrowest_mm: float  # narrowest antenna copper width or gap
    narrowest_what: str
    network: MatchNetwork | None
    stackup: dict[str, Any]
    assumptions: list[str]
    warnings: list[str] = field(default_factory=list)
    simulatable: bool = True

    @property
    def sweep_hz(self) -> tuple[float, float]:
        return (self.f0_hz - self.fc_hz, self.f0_hz + self.fc_hz)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["network"] = self.network.to_dict() if self.network else None
        return d


@dataclass
class Item:
    """One piece of copper before merging: a track, pad, zone fill or via, as a shapely geometry."""

    kind: str  # track | pad | zone | via
    layer: str
    net: str | None
    geom: Any  # shapely geometry (polygonal)
    source: str  # board target id, e.g. track:ab12cd34
    ref: str | None = None  # footprint ref for pads
    width: float | None = None  # tracks only
    points: list[tuple[float, float]] | None = None  # tracks only: centreline
    antenna: bool = False
