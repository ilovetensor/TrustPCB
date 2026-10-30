"""Track connectivity: segments, runs, routes.

Tracks join by shared end points on the same net and layer. A straight track whose side is
touched by another track's end is split there, so T joints are real joints. A *run* is a chain of
segments between two terminals (a pad, a via, a junction of three or more tracks, or an open end).
A *route* joins runs through plain vias (a via with exactly two track ends and no pad), so a
connection that changes layer is one route.

Built once per board and cached on the context.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..pcb import geom
from ..pcb.model import Board, Pad, Track, Via

if TYPE_CHECKING:  # pragma: no cover
    from .base import AnalyzerContext

Pt = tuple[float, float]
NodeKey = tuple[str | None, str, float, float]  # net, layer, x, y

SPLIT_TOL = 0.01
PAD_TOL = 0.02


def _k(p: Pt) -> Pt:
    return (round(p[0], 3), round(p[1], 3))


def _unit(a: Pt, b: Pt) -> Pt:
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n > 1e-12 else (0.0, 0.0)


@dataclass
class Seg:
    idx: int
    parent: str
    net: str | None
    layer: str
    width: float
    pts: list[Pt]
    arc: bool = False

    @property
    def a(self) -> Pt:
        return _k(self.pts[0])

    @property
    def b(self) -> Pt:
        return _k(self.pts[-1])

    @property
    def length(self) -> float:
        return geom.polyline_length(self.pts)

    def tangent_at(self, node: Pt) -> Pt:
        """Unit vector from ``node`` into the segment."""
        if node == self.a:
            return _unit(self.pts[0], self.pts[1])
        return _unit(self.pts[-1], self.pts[-2])

    def other(self, node: Pt) -> Pt:
        return self.b if node == self.a else self.a


@dataclass
class Run:
    id: int
    net: str | None
    layer: str
    segs: list[Seg]  # oriented: each seg.pts runs along the walk
    start: Pt
    end: Pt
    start_attach: dict[str, list[str]] = field(default_factory=dict)  # pads / vias at the start node
    end_attach: dict[str, list[str]] = field(default_factory=dict)
    closed: bool = False

    @property
    def length(self) -> float:
        return sum(s.length for s in self.segs)

    @property
    def tracks(self) -> list[str]:
        out: list[str] = []
        for s in self.segs:
            if s.parent not in out:
                out.append(s.parent)
        return out

    def vertices(self) -> list[Pt]:
        pts: list[Pt] = []
        for s in self.segs:
            pts.extend(s.pts if not pts else s.pts[1:])
        return pts

    def joints(self) -> list[tuple[Pt, Seg, Seg]]:
        """Interior joints along the walk: (node, segment arriving, segment leaving)."""
        return [(_k(self.segs[i].pts[-1]), self.segs[i], self.segs[i + 1]) for i in range(len(self.segs) - 1)]


@dataclass
class Route:
    id: int
    net: str | None
    runs: list[Run]
    vias: list[str]
    start: Pt
    end: Pt

    @property
    def length(self) -> float:
        return sum(r.length for r in self.runs)

    @property
    def tracks(self) -> list[str]:
        out: list[str] = []
        for r in self.runs:
            for t in r.tracks:
                if t not in out:
                    out.append(t)
        return out

    @property
    def layers(self) -> list[str]:
        out: list[str] = []
        for r in self.runs:
            if r.layer not in out:
                out.append(r.layer)
        return out


def _split_straight(tracks: list[Track]) -> list[tuple[Track, list[Pt]]]:
    """Each track as a polyline; straight tracks are cut at other tracks' end points that touch their side."""
    out: list[tuple[Track, list[Pt]]] = []
    ends = [p for t in tracks for p in (tuple(t.start), tuple(t.end))]
    for t in tracks:
        if t.mid is not None:
            out.append((t, [tuple(p) for p in t.points()]))
            continue
        a, b = (t.start[0], t.start[1]), (t.end[0], t.end[1])
        ab = (b[0] - a[0], b[1] - a[1])
        ll = ab[0] ** 2 + ab[1] ** 2
        cuts: list[tuple[float, Pt]] = []
        if ll > 1e-12:
            for e in ends:
                if _k(e) in (_k(a), _k(b)):
                    continue
                s = ((e[0] - a[0]) * ab[0] + (e[1] - a[1]) * ab[1]) / ll
                if 0 < s < 1:
                    px, py = a[0] + s * ab[0], a[1] + s * ab[1]
                    if math.hypot(e[0] - px, e[1] - py) <= SPLIT_TOL:
                        cuts.append((s, (px, py)))
        cuts.sort()
        pts = [a]
        for _, p in cuts:
            if _k(p) != _k(pts[-1]):
                pts.append(p)
        pts.append(b)
        out.append((t, pts))
    return out


class Topology:
    def __init__(self, board: Board):
        self.board = board
        self.segs: list[Seg] = []
        self.nodes: dict[NodeKey, list[int]] = {}
        self.node_pads: dict[NodeKey, list[str]] = {}
        self.node_vias: dict[NodeKey, list[str]] = {}
        self.node_zones: dict[NodeKey, list[str]] = {}
        self.runs: list[Run] = []
        self.routes: list[Route] = []
        self.via_by_id = {v.id: v for v in board.vias}
        self.pad_by_id = {p.id: p for p in board.pads}
        self.copper = board.copper_layers()
        self._build()

    # ---------------------------------------------------------------- build
    def _via_layers(self, v: Via) -> set[str]:
        if len(v.layers) >= 2 and self.copper:
            lo, hi = sorted(self.copper.index(x) for x in (v.layers[0], v.layers[-1]) if x in self.copper) or (0, len(self.copper) - 1)
            return set(self.copper[lo : hi + 1])
        return set(v.layers)

    def _build(self) -> None:
        groups: dict[tuple[str | None, str], list[Track]] = {}
        for t in sorted(self.board.tracks, key=lambda t: t.id):
            groups.setdefault((t.net, t.layer), []).append(t)
        for key in sorted(groups, key=lambda k: (k[0] or "", k[1])):
            for t, pts in _split_straight(groups[key]):
                for i in range(len(pts) - 1):
                    if t.mid is not None and i > 0:
                        break
                    seg_pts = pts if t.mid is not None else [pts[i], pts[i + 1]]
                    seg = Seg(len(self.segs), t.id, t.net, t.layer, t.width, [(p[0], p[1]) for p in seg_pts], t.mid is not None)
                    self.segs.append(seg)
        for s in self.segs:
            for node in (s.a, s.b):
                self.nodes.setdefault((s.net, s.layer, node[0], node[1]), []).append(s.idx)
        # pads and vias that touch a node
        pads_by_net: dict[str | None, list[Pad]] = {}
        for p in self.board.pads:
            pads_by_net.setdefault(p.net, []).append(p)
        vias_by_net: dict[str | None, list[Via]] = {}
        for v in self.board.vias:
            vias_by_net.setdefault(v.net, []).append(v)
        via_layers = {v.id: self._via_layers(v) for v in self.board.vias}
        for nk in sorted(self.nodes, key=lambda k: (k[0] or "", k[1], k[2], k[3])):
            net, layer, x, y = nk
            for p in pads_by_net.get(net, []):
                if layer in p.layers and self._pad_touches(p, x, y):
                    self.node_pads.setdefault(nk, []).append(p.id)
            for v in vias_by_net.get(net, []):
                if layer in via_layers[v.id] and math.hypot(v.x - x, v.y - y) <= v.diameter / 2 + PAD_TOL:
                    self.node_vias.setdefault(nk, []).append(v.id)
        self._attach_zones()
        self._build_runs()
        self._build_routes()

    def _attach_zones(self) -> None:
        """Open track ends that sit in a copper pour of their own net are connected to it."""
        zones = [z for z in self.board.zones if z.net and not z.is_keepout]
        for nk, idxs in self.nodes.items():
            if len(idxs) != 1 or nk in self.node_pads or nk in self.node_vias:
                continue
            net, layer, x, y = nk
            for z in zones:
                if z.net != net:
                    continue
                rings = z.filled.get(layer) or ([z.outline] if layer in z.layers and not z.filled and z.outline else [])
                for r in rings:
                    box = geom.bbox_of(r)
                    if box and box[0] - PAD_TOL <= x <= box[2] + PAD_TOL and box[1] - PAD_TOL <= y <= box[3] + PAD_TOL and geom.point_in_ring(x, y, r):
                        self.node_zones.setdefault(nk, []).append(z.id)
                        break

    @staticmethod
    def _pad_touches(p: Pad, x: float, y: float) -> bool:
        if math.hypot(p.x - x, p.y - y) > max(p.w, p.h) / 2 + 0.5 + PAD_TOL:
            return False
        if geom.point_in_ring(x, y, p.ring):
            return True
        # near the edge of the ring
        return any(_dist_point_seg((x, y), p.ring[i], p.ring[(i + 1) % len(p.ring)]) <= PAD_TOL for i in range(len(p.ring)))

    def _is_terminal(self, nk: NodeKey) -> bool:
        return len(self.nodes[nk]) != 2 or nk in self.node_pads or nk in self.node_vias or nk in self.node_zones

    def _build_runs(self) -> None:
        used: set[int] = set()
        order = sorted(self.nodes, key=lambda k: (k[0] or "", k[1], k[2], k[3]))

        def walk(first_seg: Seg, start_node: Pt) -> Run:
            net, layer = first_seg.net, first_seg.layer
            segs: list[Seg] = []
            node = start_node
            seg = first_seg
            while True:
                used.add(seg.idx)
                oriented = Seg(seg.idx, seg.parent, seg.net, seg.layer, seg.width, list(seg.pts) if seg.a == node else list(reversed(seg.pts)), seg.arc)
                segs.append(oriented)
                node = seg.other(node)
                nk = (net, layer, node[0], node[1])
                if self._is_terminal(nk):
                    break
                nxt = [i for i in self.nodes[nk] if i != seg.idx]
                if not nxt or nxt[0] in used:
                    break
                seg = self.segs[nxt[0]]
            run = Run(len(self.runs), net, layer, segs, _k(segs[0].pts[0]), _k(segs[-1].pts[-1]))
            sk = (net, layer, run.start[0], run.start[1])
            ek = (net, layer, run.end[0], run.end[1])
            run.start_attach = {"pads": list(self.node_pads.get(sk, [])), "vias": list(self.node_vias.get(sk, [])), "zones": list(self.node_zones.get(sk, []))}
            run.end_attach = {"pads": list(self.node_pads.get(ek, [])), "vias": list(self.node_vias.get(ek, [])), "zones": list(self.node_zones.get(ek, []))}
            run.closed = run.start == run.end
            return run

        for nk in order:
            if not self._is_terminal(nk):
                continue
            for idx in sorted(self.nodes[nk]):
                if idx not in used:
                    self.runs.append(walk(self.segs[idx], (nk[2], nk[3])))
        for nk in order:  # closed loops of plain joints
            for idx in sorted(self.nodes[nk]):
                if idx not in used:
                    self.runs.append(walk(self.segs[idx], (nk[2], nk[3])))

    def _build_routes(self) -> None:
        # ends that land on a via and nothing else
        by_via: dict[str, list[tuple[Run, str]]] = {}
        for r in self.runs:
            for which, attach in (("start", r.start_attach), ("end", r.end_attach)):
                if attach.get("vias") and not attach.get("pads"):
                    by_via.setdefault(attach["vias"][0], []).append((r, which))
        link: dict[tuple[int, str], tuple[Run, str, str]] = {}
        for vid, ends in by_via.items():
            if len(ends) == 2 and ends[0][0].layer != ends[1][0].layer:
                (r1, w1), (r2, w2) = ends
                link[(r1.id, w1)] = (r2, w2, vid)
                link[(r2.id, w2)] = (r1, w1, vid)
        visited: set[int] = set()

        def build(first: Run, enter: str) -> Route:
            runs: list[Run] = []
            vias: list[str] = []
            run, entering = first, enter
            while True:
                visited.add(run.id)
                runs.append(_oriented(run, entering))
                leaving = "end" if entering == "start" else "start"
                nxt = link.get((run.id, leaving))
                if not nxt or nxt[0].id in visited:
                    break
                vias.append(nxt[2])
                run, entering = nxt[0], nxt[1]
            return Route(len(self.routes), first.net, runs, vias, runs[0].start, runs[-1].end)

        for r in self.runs:  # routes that begin at an unlinked end
            if r.id in visited:
                continue
            if (r.id, "start") not in link:
                self.routes.append(build(r, "start"))
            elif (r.id, "end") not in link:
                self.routes.append(build(r, "end"))
        for r in self.runs:  # loops of linked runs
            if r.id not in visited:
                self.routes.append(build(r, "start"))

    # ---------------------------------------------------------------- queries
    def runs_of_net(self, net: str) -> list[Run]:
        return [r for r in self.runs if r.net == net]

    def junction_nodes(self, net: str) -> list[Pt]:
        return sorted({(k[2], k[3]) for k, segs in self.nodes.items() if k[0] == net and len(segs) >= 3})


def _oriented(run: Run, entering: str) -> Run:
    if entering == "start":
        return run
    segs = [Seg(s.idx, s.parent, s.net, s.layer, s.width, list(reversed(s.pts)), s.arc) for s in reversed(run.segs)]
    return Run(run.id, run.net, run.layer, segs, run.end, run.start, run.end_attach, run.start_attach, run.closed)


def _dist_point_seg(p: Pt, a: Pt | list[float], b: Pt | list[float]) -> float:
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    dx, dy = bx - ax, by - ay
    ll = dx * dx + dy * dy
    if ll < 1e-18:
        return math.hypot(p[0] - ax, p[1] - ay)
    s = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / ll))
    return math.hypot(p[0] - (ax + s * dx), p[1] - (ay + s * dy))


def get_topology(board: Board, ctx: AnalyzerContext | None = None) -> Topology:
    cache: dict[str, Any] | None = ctx.cache if ctx is not None else None
    if cache is not None and cache.get("topology_board") is board:
        return cache["topology"]
    topo = Topology(board)
    if cache is not None:
        cache["topology"] = topo
        cache["topology_board"] = board
    return topo
