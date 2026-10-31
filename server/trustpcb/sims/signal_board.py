"""Board geometry for the signal simulator: stackup slabs, copper index, net paths, cross-sections and return paths.

Everything here reads a parsed ``Board`` and builds in-memory shapes; nothing is written back. The what-if
operations of the simulator arrive here as plain overrides (a changed track width, extra ground patches, extra
ground vias) so a change never touches the design.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

import numpy as np
import shapely
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

from ..analyzers.classify import is_ground, is_power
from ..pcb import geom
from ..pcb.model import Board
from . import fieldsolve as fs

DEFAULT_CU_MM = 0.035
DEFAULT_ER = 4.5
DEFAULT_DIEL_MM = 0.2

# ------------------------------------------------------------------------------------------ stackup


@dataclass
class Stack:
    """The board stackup as slabs. ``z`` is millimetres up from the bottom face of the lowest layer."""

    copper: list[str]  # top to bottom
    cu_z: dict[str, tuple[float, float]]
    cu_t: dict[str, float]
    slabs: list[tuple[float, float, float]]  # bottom to top, covering the stack, with the permittivity of each slab
    top: float
    bottom: float
    source: str
    assumed: bool
    er_between: dict[tuple[str, str], tuple[float, float]] = field(default_factory=dict)  # (upper, lower) -> (height, mean er)

    def refs(self, layer: str) -> list[str]:
        """Copper layers next to ``layer`` (one for an outer layer, two for an inner one)."""
        if layer not in self.copper:
            return []
        i = self.copper.index(layer)
        return [self.copper[j] for j in (i - 1, i + 1) if 0 <= j < len(self.copper)]

    def gap(self, a: str, b: str) -> tuple[float, float]:
        """(dielectric height mm, mean permittivity) between two adjacent copper layers."""
        return self.er_between.get((a, b)) or self.er_between.get((b, a)) or (0.0, DEFAULT_ER)


def build_stack(board: Board) -> Stack:
    st = board.stackup or {}
    layers = [s for s in (st.get("layers") or []) if s.get("kind") in ("copper", "dielectric")]
    if not any(s["kind"] == "copper" for s in layers):
        st = board.default_stackup(board.thickness) if hasattr(board, "default_stackup") else {}
        layers = st.get("layers", [])
    # build from the bottom up
    z = 0.0
    cu_z: dict[str, tuple[float, float]] = {}
    cu_t: dict[str, float] = {}
    raw: list[tuple[str, float, float, float, str]] = []  # kind, z0, z1, eps, name
    for s in reversed(layers):
        if s["kind"] == "copper":
            t = float(s.get("thickness_mm") or DEFAULT_CU_MM)
            raw.append(("copper", z, z + t, 1.0, s["name"]))
            cu_z[s["name"]] = (z, z + t)
            cu_t[s["name"]] = t
            z += t
        else:
            th = float(s.get("thickness_mm") or DEFAULT_DIEL_MM)
            er = float(s.get("epsilon_r") or DEFAULT_ER)
            raw.append(("diel", z, z + th, er, s["name"]))
            z += th
    raw.reverse()  # top to bottom for neighbour lookups
    slabs: list[tuple[float, float, float]] = []
    for i, (kind, z0, z1, er, _) in enumerate(raw):
        if kind == "copper":
            above = raw[i - 1] if i > 0 else None
            below = raw[i + 1] if i + 1 < len(raw) else None
            nb = [x[3] for x in (above, below) if x is not None and x[0] == "diel"]
            er = sum(nb) / len(nb) if len(nb) == 2 else 1.0  # outer faces are air; inner layers are resin filled
        slabs.append((z0, z1, er))
    slabs.sort()
    copper = [r[4] for r in raw if r[0] == "copper"]
    between: dict[tuple[str, str], tuple[float, float]] = {}
    for a, b in zip(copper, copper[1:]):
        h = 0.0
        eh = 0.0
        for kind, z0, z1, er, _ in raw:
            if kind == "diel" and z0 >= cu_z[b][1] - 1e-9 and z1 <= cu_z[a][0] + 1e-9:
                h += z1 - z0
                eh += (z1 - z0) * er
        between[(a, b)] = (h, eh / h if h > 0 else DEFAULT_ER)
    return Stack(copper, cu_z, cu_t, slabs, z, 0.0, st.get("source", "default"), bool(st.get("assumed")), between)


# ------------------------------------------------------------------------------------------ copper index


@dataclass
class Item:
    geom: object
    net: str | None
    kind: str  # track | pad | via | zone | patch
    ident: str
    span: tuple[str, str] | None = None  # barrel items: top and bottom layers


def _is_return_net(net: str | None) -> bool:
    return bool(net) and is_ground(net)


def _is_quiet_net(net: str | None, power_nets: set[str] | None = None) -> bool:
    """Nets treated as AC ground in the field solve: ground, power, and copper with no net."""
    if not net:
        return True
    return is_ground(net) or is_power(net) or (power_nets is not None and net in power_nets)


class Copper:
    """Every copper shape on every layer, with a spatial index per layer."""

    def __init__(
        self,
        board: Board,
        stack: Stack,
        width_override: dict[str, float] | None = None,
        patches: list[tuple[str, object]] | None = None,
        extra_vias: list[tuple[float, float, float, float]] | None = None,
        power_nets: set[str] | None = None,
    ):
        self.board = board
        self.stack = stack
        self.power_nets = power_nets or set()
        self.items: dict[str, list[Item]] = {layer: [] for layer in stack.copper}
        wo = width_override or {}
        for t in board.tracks:
            if t.layer not in self.items:
                continue
            w = float(wo.get(t.net or "", t.width)) if t.net in wo else float(t.width)
            pts = t.points()
            if len(pts) < 2 or w <= 0:
                continue
            self.items[t.layer].append(Item(LineString(pts).buffer(w / 2, quad_segs=3), t.net, "track", t.id))
        for p in board.pads:
            if not p.net and p.type == "np_thru_hole":
                continue
            ring = p.ring
            if not ring or len(ring) < 3:
                continue
            poly = Polygon(ring).buffer(0)
            if poly.is_empty:
                continue
            span = None
            lays = [x for x in p.layers if x in self.items]
            if p.drill and len(lays) >= 2:
                span = (lays[0], lays[-1])
            for layer in lays:
                self.items[layer].append(Item(poly, p.net, "pad", p.id, span))
        for v in board.vias:
            lays = self._via_layers(v.layers)
            if len(lays) < 1:
                continue
            circ = Point(v.x, v.y).buffer(max(v.diameter, 0.2) / 2, quad_segs=3)
            span = (lays[0], lays[-1]) if len(lays) >= 2 else None
            for layer in lays:
                self.items[layer].append(Item(circ, v.net, "via", v.id, span))
        self.approx_zones: set[str] = set()
        for z in board.zones:
            if z.is_keepout:
                continue
            if z.filled:
                for layer, rings in z.filled.items():
                    if layer not in self.items:
                        continue
                    polys = [Polygon(r).buffer(0) for r in rings if len(r) >= 3]
                    polys = [q for q in polys if not q.is_empty]
                    if polys:
                        self.items[layer].append(Item(unary_union(polys), z.net, "zone", z.id))
            elif len(z.outline) >= 3:
                poly = Polygon(z.outline).buffer(0)
                for layer in z.layers:
                    if layer in self.items and not poly.is_empty:
                        self.items[layer].append(Item(poly, z.net, "zone", z.id))
                        self.approx_zones.add(layer)
        for layer, geometry in patches or []:
            if layer in self.items:
                self.items[layer].append(Item(geometry, "GND", "patch", "patch"))
        for i, (x, y, dia, _drill) in enumerate(extra_vias or []):
            circ = Point(x, y).buffer(dia / 2, quad_segs=3)
            top, bot = stack.copper[0], stack.copper[-1]
            for layer in stack.copper:
                self.items[layer].append(Item(circ, "GND", "via", f"stitch:{i}", (top, bot)))
        self.trees: dict[str, STRtree] = {}
        self.geoms: dict[str, np.ndarray] = {}
        for layer, its in self.items.items():
            arr = np.empty(len(its), dtype=object)
            for i, it in enumerate(its):
                arr[i] = it.geom
            self.geoms[layer] = arr
            self.trees[layer] = STRtree(arr) if len(its) else None  # type: ignore[assignment]
        self._ground: dict[str, object] | None = None

    def _via_layers(self, names: list[str]) -> list[str]:
        cs = self.stack.copper
        idx = sorted(cs.index(n) for n in names if n in cs)
        if len(idx) >= 2:
            return cs[idx[0] : idx[-1] + 1]
        return [cs[i] for i in idx]

    # ---- ground copper (return conductors): ground-named nets only, as the layout analyzers use
    def ground(self) -> dict[str, object]:
        if self._ground is None:
            out: dict[str, object] = {}
            for layer, its in self.items.items():
                parts = [it.geom for it in its if _is_return_net(it.net)]
                if parts:
                    g = unary_union(parts)
                    shapely.prepare(g)
                    out[layer] = g
            self._ground = out
        return self._ground

    def ground_vias(self) -> list[tuple[float, float, tuple[str, ...]]]:
        """Ground vias and plated pads: (x, y, layers joined)."""
        out: list[tuple[float, float, tuple[str, ...]]] = []
        for v in self.board.vias:
            if _is_return_net(v.net):
                out.append((v.x, v.y, tuple(self._via_layers(v.layers))))
        for p in self.board.pads:
            if _is_return_net(p.net) and p.drill:
                out.append((p.x, p.y, tuple(x for x in p.layers if x in self.items)))
        seen = set()
        for layer, its in self.items.items():
            for it in its:
                if it.ident.startswith("stitch:") and it.ident not in seen:
                    seen.add(it.ident)
                    c = it.geom.centroid
                    out.append((c.x, c.y, tuple(self.stack.copper)))
        return out

    def ground_covers(self, layer: str, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        g = self.ground().get(layer)
        if g is None:
            return np.zeros(len(x), dtype=bool)
        return shapely.contains_xy(g, x, y)

    # ---- cross-section intervals
    def intervals(self, p: tuple[float, float], n: tuple[float, float], half: float) -> list[tuple[str, float, float, Item]]:
        """Copper met by the line through ``p`` along ``n``: (layer, s0, s1, item) with s measured from ``p``."""
        line = LineString([(p[0] - n[0] * half, p[1] - n[1] * half), (p[0] + n[0] * half, p[1] + n[1] * half)])
        out: list[tuple[str, float, float, Item]] = []
        for layer in self.stack.copper:
            tree = self.trees.get(layer)
            if tree is None:
                continue
            hits = tree.query(line, predicate="intersects")
            if len(hits) == 0:
                continue
            inter = shapely.intersection(self.geoms[layer][hits], line)
            for k, g in zip(hits, inter):
                if g.is_empty:
                    continue
                for part in shapely.get_parts(g):
                    if part.geom_type == "Point":
                        continue
                    cs = np.asarray(part.coords)
                    if len(cs) < 2:
                        continue
                    s = (cs[:, 0] - p[0]) * n[0] + (cs[:, 1] - p[1]) * n[1]
                    out.append((layer, float(s.min()), float(s.max()), self.items[layer][k]))
        return out


# ------------------------------------------------------------------------------------------ net paths


@dataclass
class Piece:
    kind: str  # trace | via
    layer: str
    pts: list[tuple[float, float]]
    width: float
    z_len: float = 0.0
    layer_to: str | None = None
    track: str = ""

    @property
    def length(self) -> float:
        return geom.polyline_length(self.pts) if self.kind == "trace" else self.z_len


@dataclass
class NetPath:
    net: str
    pieces: list[Piece]
    source: str
    sink: str
    total_length_mm: float  # all routed copper of the net
    branch_length_mm: float
    pads: list[str]
    n_vias: int

    @property
    def length_mm(self) -> float:
        return sum(p.length for p in self.pieces)

    @property
    def trace_length_mm(self) -> float:
        return sum(p.length for p in self.pieces if p.kind == "trace")


def _k(layer: str, x: float, y: float) -> tuple[str, float, float]:
    return (layer, round(x, 2), round(y, 2))


def net_path(board: Board, net: str, stack: Stack, widths: dict[str, float] | None = None, source_ref: str | None = None, skip: frozenset[str] | set[str] = frozenset()) -> NetPath | None:
    """The main route of ``net``: from a source pad to the farthest pad (or open end) over the routed copper.

    Branches off this route are counted (``branch_length_mm``) but not part of the profile.
    """
    wo = widths or {}
    tracks = [t for t in board.tracks if t.net == net and t.layer in stack.copper and t.id not in skip]
    if not tracks:
        return None
    # split straight tracks where another track of the net ends on their side
    ends = [(t.layer, tuple(t.start), tuple(t.end)) for t in tracks]
    edges: list[tuple[tuple, tuple, float, Piece]] = []
    for t in tracks:
        w = float(wo.get(net, t.width))
        pts = [tuple(p) for p in t.points()]
        cuts: list[tuple[float, tuple[float, float]]] = []
        if t.mid is None:
            a, b = pts[0], pts[-1]
            ab = (b[0] - a[0], b[1] - a[1])
            ll = ab[0] ** 2 + ab[1] ** 2
            if ll > 1e-12:
                for layer, s, e in ends:
                    if layer != t.layer:
                        continue
                    for q in (s, e):
                        u = ((q[0] - a[0]) * ab[0] + (q[1] - a[1]) * ab[1]) / ll
                        if 0.001 < u < 0.999:
                            px, py = a[0] + u * ab[0], a[1] + u * ab[1]
                            if math.hypot(q[0] - px, q[1] - py) < 0.02:
                                cuts.append((u, (px, py)))
            cuts.sort()
            pts = [a] + [c[1] for c in cuts] + [b]
        for p0, p1 in zip(pts, pts[1:]) if t.mid is None else [(pts[0], pts[-1])]:
            seg_pts = [p0, p1] if t.mid is None else pts
            na, nb = _k(t.layer, *seg_pts[0]), _k(t.layer, *seg_pts[-1])
            length = geom.polyline_length(seg_pts)
            if na == nb or length < 1e-6:
                continue
            edges.append((na, nb, length, Piece("trace", t.layer, [tuple(q) for q in seg_pts], w, track=t.id)))
    adj: dict[tuple, list[tuple[tuple, float, Piece, bool]]] = {}

    def add(a, b, wt, piece):
        adj.setdefault(a, []).append((b, wt, piece, False))
        adj.setdefault(b, []).append((a, wt, piece, True))

    for na, nb, length, pc in edges:
        add(na, nb, length, pc)
    nodes_by_layer: dict[str, list[tuple]] = {}
    for n in list(adj):
        nodes_by_layer.setdefault(n[0], []).append(n)
    # vias on the net join layers
    n_vias = 0
    for v in board.vias:
        if v.net != net:
            continue
        lays = [x for x in stack.copper if x in v.layers] or list(v.layers)
        idx = sorted(stack.copper.index(x) for x in v.layers if x in stack.copper)
        lays = stack.copper[idx[0] : idx[-1] + 1] if len(idx) >= 2 else lays
        vnodes = []
        for layer in lays:
            node = ("via", v.id, layer)
            vnodes.append(node)
            for n in nodes_by_layer.get(layer, []):
                if math.hypot(n[1] - v.x, n[2] - v.y) <= max(0.06, v.diameter / 2):
                    add(node, n, 0.0, Piece("trace", layer, [(v.x, v.y), (v.x, v.y)], 0.0))
        for a, b in zip(vnodes, vnodes[1:]):
            zl = abs(stack.cu_z[a[2]][0] - stack.cu_z[b[2]][0])
            add(a, b, zl, Piece("via", a[2], [(v.x, v.y)], 0.0, zl, b[2]))
            n_vias += 1
    # pads join the nodes they cover
    pad_nodes: list[tuple] = []
    pad_by_node: dict[tuple, object] = {}
    for p in board.pads:
        if p.net != net or len(p.ring) < 3:
            continue
        poly = Polygon(p.ring).buffer(0.03)
        node = ("pad", p.id)
        hit = False
        for layer in p.layers:
            for n in list(nodes_by_layer.get(layer, [])) + [x for x in adj if x[0] == "via" and x[2] == layer]:
                x, y = (n[1], n[2]) if n[0] != "via" else _via_xy(board, n[1])
                if poly.contains(Point(x, y)):
                    add(node, n, 0.0, Piece("trace", layer, [(x, y), (x, y)], 0.0))
                    hit = True
        if hit:
            pad_nodes.append(node)
            pad_by_node[node] = p
    total = sum(e[2] for e in edges)
    if not adj:
        return None

    def sssp(src):
        dist = {src: 0.0}
        prev: dict = {}
        heap = [(0.0, 0, src)]
        cnt = 0
        while heap:
            d, _, u = heapq.heappop(heap)
            if d > dist.get(u, 1e18):
                continue
            for v, wt, pc, rev in adj.get(u, []):
                nd = d + wt
                if nd < dist.get(v, 1e18) - 1e-12:
                    dist[v] = nd
                    prev[v] = (u, pc, rev)
                    cnt += 1
                    heapq.heappush(heap, (nd, cnt, v))
        return dist, prev

    if len(pad_nodes) >= 2:
        src = _choose_source(pad_nodes, pad_by_node, source_ref)
        dist, prev = sssp(src)
        cands = [n for n in pad_nodes if n != src and n in dist]
        if not cands:
            return None
        # connectors first, then ICs and crystals, then passives; the farthest within the best class
        sink = min(cands, key=lambda n: (_pad_class(pad_by_node[n].ref), -dist[n]))
        s_name, t_name = pad_by_node[src].id, pad_by_node[sink].id
    else:
        deg1 = [n for n, v in adj.items() if len(v) == 1 and n[0] not in ("pad", "via")]
        start = deg1[0] if deg1 else next(iter(adj))
        d0, _ = sssp(start)
        src = max(d0, key=lambda n: d0[n])
        dist, prev = sssp(src)
        sink = max(dist, key=lambda n: dist[n])
        s_name = pad_by_node[src].id if src in pad_by_node else f"end:{src[1]},{src[2]}"
        t_name = pad_by_node[sink].id if sink in pad_by_node else f"end:{sink[1]},{sink[2]}"
    if sink == src or sink not in prev:
        return None
    seq: list[Piece] = []
    cur = sink
    while cur != src:
        u, pc, rev = prev[cur]
        # an edge stored (a -> b); walking from u to cur
        if pc.kind == "trace":
            pts = list(pc.pts)
            # orientation: the stored piece runs na -> nb; edges were added both ways with rev flag for the b->a direction
            # walking u -> cur is the reverse of the stored order when rev is set on the entry stored at u
            pts = pts[::-1] if rev else pts
            seq.append(Piece("trace", pc.layer, pts, pc.width, 0.0, None, pc.track))
        else:
            if rev:
                seq.append(Piece("via", pc.layer_to or pc.layer, pc.pts, 0.0, pc.z_len, pc.layer))
            else:
                seq.append(Piece("via", pc.layer, pc.pts, 0.0, pc.z_len, pc.layer_to))
        cur = u
    seq.reverse()
    pieces = [p for p in seq if p.kind == "via" or p.length > 1e-6]
    main_len = sum(p.length for p in pieces if p.kind == "trace")
    return NetPath(net, pieces, s_name, t_name, total, max(0.0, total - main_len), [pad_by_node[n].id for n in pad_nodes], n_vias)


def _via_xy(board: Board, vid: str) -> tuple[float, float]:
    for v in board.vias:
        if v.id == vid:
            return v.x, v.y
    return 0.0, 0.0


def _pad_class(ref: str) -> int:
    r = ref.upper()
    if r.startswith(("J", "P", "CN", "X", "USB", "TP")):
        return 0
    if r.startswith(("U", "Y", "IC", "Q", "M")):
        return 1
    return 2


def _choose_source(pad_nodes, pad_by_node, source_ref: str | None):
    if source_ref:
        for n in pad_nodes:
            if pad_by_node[n].ref == source_ref:
                return n
    ics = [n for n in pad_nodes if pad_by_node[n].ref.upper().startswith("U")]
    pool = ics or pad_nodes
    return sorted(pool, key=lambda n: (pad_by_node[n].ref, pad_by_node[n].number))[0]


# ------------------------------------------------------------------------------------------ sampling


@dataclass
class Sample:
    x: float
    y: float
    tx: float
    ty: float
    layer: str
    width: float
    s: float  # distance along the route, mm
    ds: float  # length of route this sample stands for, mm
    kind: str  # trace | via
    track: str = ""


def sample_path(path: NetPath, step: float) -> list[Sample]:
    out: list[Sample] = []
    s0 = 0.0
    for pc in path.pieces:
        if pc.kind == "via":
            out.append(Sample(pc.pts[0][0], pc.pts[0][1], 1.0, 0.0, pc.layer, 0.0, s0 + pc.z_len / 2, pc.z_len, "via"))
            s0 += pc.z_len
            continue
        for a, b in zip(pc.pts, pc.pts[1:]):
            length = math.dist(a, b)
            if length < 1e-9:
                continue
            tx, ty = (b[0] - a[0]) / length, (b[1] - a[1]) / length
            n = max(1, int(math.ceil(length / step)))
            for k in range(n):
                f = (k + 0.5) / n
                out.append(Sample(a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]), tx, ty, pc.layer, pc.width, s0 + f * length, length / n, "trace", pc.track))
            s0 += length
    return out


# ------------------------------------------------------------------------------------------ cross-section


@dataclass
class SectionBuild:
    section: fs.Section
    key: tuple
    cond_nets: list[str]  # nets of conductors 1..n (1 is the victim)
    ref_cover: float  # fraction of the band around the track with ground on a reference layer
    ref_layers: list[str]
    ref_power: bool  # reference copper exists but none of it is ground-named
    height_mm: float
    er_sub: float
    kind: str  # microstrip | stripline | embedded
    has_return: bool  # any ground-or-AC-ground conductor within the window beyond the box
    gaps: dict[str, float] = field(default_factory=dict)  # aggressor net -> edge to edge lateral distance, mm


def build_section(
    cu: Copper,
    p: tuple[float, float],
    tangent: tuple[float, float],
    layer: str,
    width: float,
    victim: str,
    quant: float = 0.005,
    half_window: float = 12.0,
    box_side: float = 25.0,
    box_z: float = 25.0,
    couple_mm: float | None = None,
    max_aggr: int = 3,
    raw: list[tuple[str, float, float, Item]] | None = None,
) -> SectionBuild:
    stack = cu.stack
    n = (-tangent[1], tangent[0])
    if raw is None:
        raw = cu.intervals(p, n, half_window)
    refs = stack.refs(layer)
    h_ref, er_ref = (0.0, DEFAULT_ER)
    if refs:
        h_ref, er_ref = stack.gap(layer, refs[0])
        if len(refs) == 2:
            h2, e2 = stack.gap(layer, refs[1])
            h_ref = min(h_ref, h2) if h2 > 0 else h_ref
    kind = "microstrip" if len(refs) == 1 else ("stripline" if len(refs) == 2 else "embedded")
    h_eff = h_ref or 0.2
    dc = couple_mm if couple_mm is not None else max(3.0 * h_eff, 4.0 * width)
    # classify intervals
    by_net_dist: dict[str, float] = {}
    quiet = lambda net: _is_quiet_net(net, cu.power_nets)  # noqa: E731
    vic_ivs = [(la, a, b) for la, a, b, it in raw if it.net == victim and la == layer]
    # the victim interval nearest the centre (the track itself)
    centre = [(abs(0.5 * (a + b)), a, b) for la, a, b in vic_ivs if a - 1e-9 <= 0 <= b + 1e-9]
    vx0, vx1 = (min(c[1] for c in centre), max(c[2] for c in centre)) if centre else (-width / 2, width / 2)
    for la, a, b, it in raw:
        if it.net == victim or it.kind in ("zone", "patch") or quiet(it.net) or it.net is None:
            continue
        d = 0.0
        if b < vx0:
            d = vx0 - b
        elif a > vx1:
            d = a - vx1
        # a conductor on another layer couples through the dielectric, so measure its lateral distance only
        by_net_dist[it.net] = min(by_net_dist.get(it.net, 1e9), d)
    aggr = [nm for nm, d in sorted(by_net_dist.items(), key=lambda kv: kv[1]) if d <= dc][:max_aggr]
    cond_of = {victim: 1}
    for i, nm in enumerate(aggr):
        cond_of[nm] = 2 + i
    # intervals per (layer, cond); barrels span layers
    rects_iv: dict[tuple[str, int], list[tuple[float, float]]] = {}
    barrels: dict[tuple[str, int, str], list[tuple[float, float]]] = {}
    for la, a, b, it in raw:
        cond = cond_of.get(it.net, 0) if it.net else 0
        if it.span and it.kind in ("via", "pad"):
            key = (it.span[0], cond, it.span[1])
            barrels.setdefault(key, []).append((a, b))
        rects_iv.setdefault((la, cond), []).append((a, b))

    def merge(ivs: list[tuple[float, float]]) -> list[tuple[float, float]]:
        ivs = sorted(ivs)
        out: list[list[float]] = []
        for a, b in ivs:
            if out and a <= out[-1][1] + 1e-6:
                out[-1][1] = max(out[-1][1], b)
            else:
                out.append([a, b])
        return [(a, b) for a, b in out]

    sig_iv = [(a, b) for (la, cond), ivs in rects_iv.items() if cond >= 1 for a, b in ivs]

    def q(v: float) -> float:
        # positions near a signal conductor keep ``quant``; far copper is rounded more coarsely, which changes the
        # answer little and lets neighbouring cross-sections share one solve
        dist = min([max(0.0, a - v, v - b) for a, b in sig_iv] or [0.0])
        step = max(quant, 0.04 * dist)
        return round(round(v / step) * step, 6)

    rects: list[fs.Rect] = []
    sx = box_side
    for (la, cond), ivs in sorted(rects_iv.items()):
        z0, z1 = stack.cu_z[la]
        for a, b in merge(ivs):
            if a <= -half_window + 1e-3:
                a = -sx
            if b >= half_window - 1e-3:
                b = sx
            qa, qb = q(a), q(b)
            if qb - qa < quant:
                qb = qa + quant
            rects.append(fs.Rect(qa, qb, z0, z1, cond))
    for (top, cond, bot), ivs in sorted(barrels.items()):
        z0 = stack.cu_z[bot][0]
        z1 = stack.cu_z[top][1]
        for a, b in merge(ivs):
            qa, qb = q(a), q(b)
            if qb - qa < quant:
                qb = qa + quant
            rects.append(fs.Rect(qa, qb, z0, z1, cond))
    # make sure the victim exists as drawn
    if not any(r.cond == 1 and r.z0 == stack.cu_z[layer][0] and r.x0 <= 0 <= r.x1 for r in rects):
        rects.append(fs.Rect(q(-width / 2), q(width / 2), stack.cu_z[layer][0], stack.cu_z[layer][1], 1))
    # reference support: ground-named copper on a reference layer across a band around the track
    band = (min(-width / 2, vx0) - 0.5 * h_eff, max(width / 2, vx1) + 0.5 * h_eff)
    cover = 0.0
    ref_power = False
    ref_used: list[str] = []
    if refs:
        best = 0.0
        for rl in refs:
            gv = merge([(a, b) for la, a, b, it in raw if la == rl and _is_return_net(it.net)])
            tot = band[1] - band[0]
            got = sum(max(0.0, min(b, band[1]) - max(a, band[0])) for a, b in gv)
            frac = got / tot if tot > 0 else 0.0
            if frac > best:
                best = frac
                ref_used = [rl]
        cover = best
        if cover < 0.5:
            anyq = any(la in refs and (it.kind == "zone" or quiet(it.net)) and not _is_return_net(it.net) for la, a, b, it in raw if a <= band[1] and b >= band[0])
            ref_power = anyq
    has_ret = any(r.cond == 0 for r in rects)
    top_z = stack.top
    sec = fs.Section(
        rects=rects,
        slabs=[(-box_z, 0.0, 1.0)] + [(a, b, e) for a, b, e in stack.slabs] + [(top_z, top_z + box_z, 1.0)],
        xmin=-sx,
        xmax=sx,
        zmin=-box_z,
        zmax=top_z + box_z,
        n_cond=1 + len(aggr),
    )
    sig = tuple(sorted((r.cond, r.z0, r.z1, r.x0, r.x1) for r in rects))
    return SectionBuild(sec, sig, [victim] + aggr, cover, ref_used or refs[:1], ref_power, h_ref, er_ref, kind, has_ret, {nm: by_net_dist[nm] for nm in aggr})


# ------------------------------------------------------------------------------------------ return paths


@dataclass
class ReturnResult:
    ok: bool
    return_mm: float = 0.0
    direct_mm: float = 0.0
    detour_mm: float = 0.0
    max_offset_mm: float = 0.0
    loop_area_mm2: float = 0.0
    path: list[tuple[float, float, str]] = field(default_factory=list)
    layers_used: list[str] = field(default_factory=list)
    vias_used: int = 0
    reason: str = ""


def shortest_return(
    cu: Copper,
    a: tuple[float, float],
    b: tuple[float, float],
    track_pts: list[tuple[float, float]],
    ref_layer: str | None,
    margin: float = 6.0,
    max_nodes: int = 120000,
) -> ReturnResult:
    """Shortest path through ground copper (any layer, joined by ground vias) from ``a`` to ``b``.

    ``track_pts`` is the signal path between the same two points; its length is the direct distance a
    return current would cover if the plane were whole. Moves go to 16 neighbours, so the length error of
    the raster path is about 1 percent.
    """
    g = cu.ground()
    if not g:
        return ReturnResult(False, reason="no ground copper on the board")
    pts = np.array(track_pts + [a, b])
    x0, y0 = pts.min(axis=0) - margin
    x1, y1 = pts.max(axis=0) + margin
    layers = [la for la in cu.stack.copper if la in g]
    px = max(0.1, math.sqrt((x1 - x0) * (y1 - y0) * len(layers) / max_nodes))
    nxp, nyp = int((x1 - x0) / px) + 1, int((y1 - y0) / px) + 1
    xs = x0 + (np.arange(nxp) + 0.5) * px
    ys = y0 + (np.arange(nyp) + 0.5) * px
    X, Y = np.meshgrid(xs, ys)  # [ny, nx]
    masks = {la: shapely.contains_xy(g[la], X.ravel(), Y.ravel()).reshape(nyp, nxp) for la in layers}
    npix = nxp * nyp
    lid = {la: i for i, la in enumerate(layers)}
    rows: list[np.ndarray] = []
    cols: list[np.ndarray] = []
    wts: list[np.ndarray] = []
    idx = np.arange(npix).reshape(nyp, nxp)
    moves = [(1, 0), (0, 1), (1, 1), (1, -1), (2, 1), (1, 2), (2, -1), (1, -2)]
    for la in layers:
        m = masks[la]
        base = lid[la] * npix
        for dx_, dy_ in moves:
            ys0, ys1 = (0, nyp - dy_) if dy_ >= 0 else (-dy_, nyp)
            xs0, xs1 = 0, nxp - dx_
            if xs1 <= xs0 or ys1 <= ys0:
                continue
            m0 = m[ys0:ys1, xs0:xs1]
            m1 = m[ys0 + dy_ : ys1 + dy_, xs0 + dx_ : xs1 + dx_]
            both = m0 & m1
            if not both.any():
                continue
            i0 = idx[ys0:ys1, xs0:xs1][both] + base
            i1 = idx[ys0 + dy_ : ys1 + dy_, xs0 + dx_ : xs1 + dx_][both] + base
            rows.append(i0)
            cols.append(i1)
            wts.append(np.full(len(i0), math.hypot(dx_, dy_) * px))
    # ground vias join the layers
    n_via_edges = 0
    for vx, vy, vl in cu.ground_vias():
        if not (x0 <= vx <= x1 and y0 <= vy <= y1):
            continue
        ix, iy = int((vx - x0) / px), int((vy - y0) / px)
        if not (0 <= ix < nxp and 0 <= iy < nyp):
            continue
        ls = [la for la in vl if la in lid and masks[la][iy, ix]]
        for la1, la2 in zip(ls, ls[1:]):
            zl = abs(cu.stack.cu_z[la1][0] - cu.stack.cu_z[la2][0])
            rows.append(np.array([lid[la1] * npix + idx[iy, ix]]))
            cols.append(np.array([lid[la2] * npix + idx[iy, ix]]))
            wts.append(np.array([max(zl, 1e-3)]))
            n_via_edges += 1
    if not rows:
        return ReturnResult(False, reason="no connected ground copper near the gap")
    r = np.concatenate(rows)
    c = np.concatenate(cols)
    w = np.concatenate(wts)
    nn = npix * len(layers)
    graph = coo_matrix((np.concatenate([w, w]), (np.concatenate([r, c]), np.concatenate([c, r]))), shape=(nn, nn)).tocsr()

    def snap(pt: tuple[float, float]) -> int | None:
        best = None
        order = [ref_layer] + [la for la in layers if la != ref_layer] if ref_layer in lid else layers
        for la in order:
            m = masks[la]
            ys_, xs_ = np.nonzero(m)
            if len(xs_) == 0:
                continue
            d = np.hypot(xs[xs_] - pt[0], ys[ys_] - pt[1])
            k = int(np.argmin(d))
            if d[k] <= 3.0 and (best is None or d[k] < best[0] - 1.5):
                best = (d[k], lid[la] * npix + int(idx[ys_[k], xs_[k]]))
                if la == order[0] and d[k] <= 1.0:
                    break
        return None if best is None else best[1]

    src, dst = snap(a), snap(b)
    if src is None or dst is None:
        return ReturnResult(False, reason="no ground copper within 3 mm of the gap ends")
    dist, pred = dijkstra(graph, directed=False, indices=src, return_predecessors=True)
    if not np.isfinite(dist[dst]):
        return ReturnResult(False, reason="the ground copper on each side of the gap is not connected")
    path: list[tuple[float, float, str]] = []
    cur = dst
    used: list[str] = []
    vias = 0
    last_layer = None
    while cur != src and cur >= 0:
        la = layers[cur // npix]
        pix = cur % npix
        path.append((float(xs[pix % nxp]), float(ys[pix // nxp]), la))
        if last_layer is not None and la != last_layer:
            vias += 1
        last_layer = la
        if la not in used:
            used.append(la)
        cur = pred[cur]
    path.reverse()
    ret = float(dist[dst])
    direct = geom.polyline_length(track_pts) if len(track_pts) > 1 else math.dist(a, b)
    # lateral offset and loop area between the signal path and the return path
    sig = LineString(track_pts) if len(track_pts) > 1 else None
    max_off = 0.0
    area = 0.0
    if sig is not None and len(path) >= 2:
        pxy = [(q[0], q[1]) for q in path]
        pts_arr = shapely.points(np.array(pxy))
        max_off = float(np.max(shapely.distance(pts_arr, sig)))
        ring = list(track_pts) + pxy[::-1]
        try:
            poly = Polygon(ring).buffer(0)
            area = float(poly.area)
        except Exception:  # noqa: BLE001
            area = 0.0
    return ReturnResult(True, ret, direct, max(0.0, ret - direct), max_off, area, path, used, vias)
