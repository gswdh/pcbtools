import glob as glob
import math
import os
import re
import xml.etree.ElementTree as ET
from collections import defaultdict

import pandas as pd
from invoke import task

exclusions = ["MECH", "TP", "LG"]


def _natural_sort_key(s):
    """Sort key that handles embedded numbers naturally (R1, R2, R10 not R1, R10, R2)."""
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', s)]


def _parse_sch(sch_file):
    """Parse an EAGLE .sch XML file and return a list of part dicts.

    Each dict has keys: name, value, device, package, lcsc.
    Parts without a physical package (e.g. GND/VCC symbols) are excluded.
    """
    tree = ET.parse(sch_file)
    root = tree.getroot()

    # Build library lookup: lib_name -> deviceset_name -> device_name -> {package, attributes}
    lib_lookup = {}
    libraries = root.find('.//libraries')
    if libraries is not None:
        for lib in libraries.findall('library'):
            lib_name = lib.get('name')
            lib_lookup[lib_name] = {}
            for ds in lib.findall('.//deviceset'):
                ds_name = ds.get('name')
                lib_lookup[lib_name][ds_name] = {}
                for dev in ds.findall('.//device'):
                    dev_name = dev.get('name', '')
                    pkg = dev.get('package', '')
                    # Collect attributes from all technologies, keyed by tech name
                    tech_attrs = {}
                    for tech in dev.findall('.//technology'):
                        t_name = tech.get('name', '')
                        attrs = {}
                        for attr in tech.findall('attribute'):
                            attrs[attr.get('name')] = attr.get('value', '')
                        tech_attrs[t_name] = attrs
                    lib_lookup[lib_name][ds_name][dev_name] = {
                        'package': pkg,
                        'tech_attrs': tech_attrs,
                    }

    parts_data = []
    parts_section = root.find('.//parts')
    if parts_section is None:
        return parts_data

    for part in parts_section.findall('part'):
        name = part.get('name')
        lib_name = part.get('library')
        ds_name = part.get('deviceset')
        dev_name = part.get('device', '')
        tech_name = part.get('technology', '')
        value = part.get('value', '')

        dev_info = lib_lookup.get(lib_name, {}).get(ds_name, {}).get(dev_name)
        if dev_info is None or not dev_info['package']:
            continue  # no physical footprint

        package = dev_info['package']

        # Library attributes for this technology (fall back to '' tech if not found)
        tech_attrs = dev_info['tech_attrs']
        attrs = dict(tech_attrs.get(tech_name, tech_attrs.get('', {})))

        # Part-level attribute overrides take precedence over library defaults
        part_attrs = {a.get('name'): a.get('value', '') for a in part.findall('attribute')}
        attrs.update(part_attrs)

        # Only a part-level VALUE attribute overrides the schematic value;
        # the library default VALUE must not overwrite an explicit value like "NO FIT"
        if part_attrs.get('VALUE'):
            value = part_attrs['VALUE']

        parts_data.append({
            'name': name,
            'value': value,
            'device': ds_name,
            'package': package,
            'lcsc': attrs.get('LCSC', ''),
        })

    return parts_data


@task
def all(ctx):
    schs = glob.glob("*.sch")
    for sch in schs:
        bom(ctx, sch)
    brds = glob.glob("*.brd")
    for brd in brds:
        cpl(ctx, brd)
        gerbers(ctx, brd)
    clean(ctx)


@task
def process(ctx, filename):
    bom(ctx, filename + ".sch")
    cpl(ctx, filename + ".brd")


@task
def bom(ctx, sch_file, output=None):
    """Generate BOM directly from an EAGLE .sch file (no Eagle required)."""
    print(f"BoMing {sch_file}")
    parts_data = _parse_sch(sch_file)

    # Apply exclusions (same logic as original: exclude if name contains exclusion string)
    for exclusion in exclusions:
        parts_data = [p for p in parts_data if exclusion not in p['name']]

    # Group by (device, package, value, lcsc) and combine designators
    groups = defaultdict(list)
    group_meta = {}
    for p in parts_data:
        key = (p['device'], p['package'], p['value'], p['lcsc'])
        groups[key].append(p['name'])
        group_meta[key] = p

    rows = []
    for key in sorted(groups.keys()):
        meta = group_meta[key]
        designators = sorted(groups[key], key=_natural_sort_key)
        no_fit = meta['value'].strip().upper() == 'NO FIT'
        rows.append({
            'Comment': 'NO FIT' if no_fit else meta['device'],
            'Designator': ', '.join(designators),
            'Footprint': meta['package'],
            'LCSC': 'NO FIT' if no_fit else meta['lcsc'],
        })

    df = pd.DataFrame(rows)
    if output is None:
        output = sch_file.replace('.sch', '_bom.xlsx')
    df.to_excel(output, index=False)
    total = sum(len(v) for v in groups.values())
    print(f"  {len(rows)} unique parts, {total} total components → {output}")


@task
def cpl(ctx, brd_file, output=None):
    """Generate CPL directly from an EAGLE .brd file (no Eagle required)."""
    print(f"CPLing {brd_file}")
    tree = ET.parse(brd_file)
    root = tree.getroot()

    rows = []
    for el in root.findall('.//element'):
        name = el.get('name')
        if any(excl in name for excl in exclusions):
            continue

        x = float(el.get('x', 0))
        y = float(el.get('y', 0))
        rot_str = el.get('rot', 'R0')

        # 'M' prefix = mirrored = Bottom layer; bare 'R' or no prefix = Top
        if rot_str.startswith('MR'):
            layer = 'Bottom'
            angle = float(rot_str[2:] or 0)
        elif rot_str.startswith('M'):
            layer = 'Bottom'
            angle = float(rot_str[1:] or 0)
        elif rot_str.startswith('R'):
            layer = 'Top'
            angle = float(rot_str[1:] or 0)
        else:
            layer = 'Top'
            angle = float(rot_str or 0)

        rows.append({
            'Designator': name,
            'Mid X': f'{x:.3f}',
            'Mid Y': f'{y:.3f}',
            'Layer': layer,
            'Rotation': angle,
        })

    df = pd.DataFrame(rows)
    if output is None:
        output = brd_file.replace('.brd', '_cpl.csv')
    df.to_csv(output, index=False)
    print(f"  {len(rows)} components → {output}")


@task
def clean(ctx):
    ctx.run('find . -name "*b#*" -delete')
    ctx.run('find . -name "*s#*" -delete')
    csvs = glob.glob("*.csv")
    for csv in csvs:
        if "cpl" in csv:
            continue
        if "bom" in csv:
            continue
        print(f"Removing {csv}")
        ctx.run(f'rm -f {csv}')
    mnts = glob.glob("*.mnt")
    for mnt in mnts:
        print(f"Removing {mnt}")
        ctx.run(f'rm -f {mnt}')
    ctx.run(f'git clean -fdX')


@task
def setup_repo(ctx, repo_path, pcb_name=""):
	ctx.run(f"cp example.gitignore {repo_path}/.gitignore")
	ctx.run(f"ln tasks.py {repo_path}/tasks.py")
	if pcb_name:
		ctx.run(f"cp eagle_template.sch {repo_path}/{pcb_name}.sch")
		ctx.run(f"cp eagle_template.brd {repo_path}/{pcb_name}.brd")


# ---------------------------------------------------------------------------
# Gerber / Excellon generation
# ---------------------------------------------------------------------------

# Coordinate scaling: Eagle coords are mm, Gerber format 3.4 = units of 0.0001 mm
_SCALE = 10000  # mm -> Gerber units (0.0001 mm per unit)


def _g(v):
    """Convert mm float to Gerber integer string (no leading zeros, sign if negative)."""
    i = round(v * _SCALE)
    return str(i)


def _parse_rot(rot_str):
    """Return (mirrored, angle_degrees) from Eagle rotation string like 'R90', 'MR0', 'M180'."""
    if rot_str is None:
        return False, 0.0
    s = rot_str.strip()
    mirrored = s.startswith('M')
    if mirrored:
        s = s[1:]
    if s.startswith('R'):
        s = s[1:]
    try:
        angle = float(s) if s else 0.0
    except ValueError:
        angle = 0.0
    return mirrored, angle


def _transform_point(px, py, ex, ey, mirrored, angle_deg):
    """Transform package point (px,py) by element placement (ex,ey,mirror,rotate).

    Eagle: if mirrored, reflect about Y axis first, then rotate CCW by angle.
    """
    if mirrored:
        px = -px
    rad = math.radians(angle_deg)
    cos_a = math.cos(rad)
    sin_a = math.sin(rad)
    rx = px * cos_a - py * sin_a
    ry = px * sin_a + py * cos_a
    return rx + ex, ry + ey


def _transform_angle(local_angle, mirrored, element_angle):
    """Transform a local orientation angle into board frame."""
    if mirrored:
        local_angle = -local_angle
    return (local_angle + element_angle) % 360


def _transform_layer(layer, mirrored):
    """Flip copper/mask/silk layer numbers when element is mirrored (bottom)."""
    if not mirrored:
        return layer
    flip_map = {1: 16, 16: 1, 21: 22, 22: 21, 25: 26, 26: 25,
                29: 30, 30: 29, 31: 32, 32: 31}
    return flip_map.get(layer, layer)


def _arc_center(x1, y1, x2, y2, curve_deg):
    """Return (cx, cy, r) for an Eagle arc wire.

    curve_deg: positive = CCW arc; the included angle (in degrees) of the arc.
    """
    # Chord midpoint
    mx = (x1 + x2) / 2
    my = (y1 + y2) / 2
    # Chord length
    chord = math.hypot(x2 - x1, y2 - y1)
    if chord < 1e-10:
        return mx, my, 0.0
    # Included angle
    theta = abs(curve_deg)
    if theta < 1e-6:
        return mx, my, 1e9  # straight line
    r = chord / (2 * math.sin(math.radians(theta / 2)))
    # Perpendicular to chord (unit vector)
    dx = x2 - x1
    dy = y2 - y1
    # Perpendicular (rotated 90 CCW)
    px = -dy / chord
    py = dx / chord
    # Distance from midpoint to center
    d = r * math.cos(math.radians(theta / 2))
    # For a CCW arc (positive curve), the center is to the LEFT of the chord
    # (perp rotated CCW from chord direction), so sign is -1 (subtract in -perp direction).
    # For a CW arc (negative curve), center is to the right, sign = +1.
    sign = -1 if curve_deg > 0 else 1
    cx = mx - sign * d * px
    cy = my - sign * d * py
    return cx, cy, r


def _gerber_arc(x1, y1, x2, y2, curve_deg):
    """Return list of Gerber command strings for an arc from (x1,y1) to (x2,y2)."""
    cx, cy, r = _arc_center(x1, y1, x2, y2, curve_deg)
    # I, J are offsets from START point to center
    i_off = cx - x1
    j_off = cy - y1
    lines = []
    lines.append(f"X{_g(x1)}Y{_g(y1)}D02*")
    # Use G75 (multi-quadrant arc mode)
    if curve_deg > 0:
        lines.append(f"G75*G03X{_g(x2)}Y{_g(y2)}I{_g(i_off)}J{_g(j_off)}D01*")
    else:
        lines.append(f"G75*G02X{_g(x2)}Y{_g(y2)}I{_g(i_off)}J{_g(j_off)}D01*")
    return lines


def _parse_dru_mm(s):
    """Parse an Eagle DRU value string to mm.
    E.g. '3mil' -> 0.0762, '0.15mm' -> 0.15, '0.1' -> 0.1 (dimensionless ratio).
    """
    s = str(s).strip()
    if s.endswith('mil'):
        return float(s[:-3]) * 0.0254
    elif s.endswith('mm'):
        return float(s[:-2])
    elif s.endswith('um'):
        return float(s[:-2]) * 0.001
    else:
        return float(s)


def _read_dru(root):
    """Return dict of design-rule param name -> value string from .brd."""
    return {p.get('name'): p.get('value', '')
            for p in root.findall('.//designrules/param')}


def _via_diameter(drill, dru, outer=True):
    """Compute via copper pad diameter from design rules.

    Eagle formula: annular = clamp(drill * rv, rl_min, rl_max)
                   pad_dia  = drill + 2 * annular
    """
    if outer:
        rv     = _parse_dru_mm(dru.get('rvViaOuter',    '0.25'))
        rl_min = _parse_dru_mm(dru.get('rlMinViaOuter', '4mil'))
        rl_max = _parse_dru_mm(dru.get('rlMaxViaOuter', '20mil'))
    else:
        rv     = _parse_dru_mm(dru.get('rvViaInner',    '0.25'))
        rl_min = _parse_dru_mm(dru.get('rlMinViaInner', '4mil'))
        rl_max = _parse_dru_mm(dru.get('rlMaxViaInner', '20mil'))
    annular = max(rl_min, min(rl_max, drill * rv))
    return drill + 2 * annular


def _th_pad_diameter(drill, diameter_attr, shape, dru=None):
    """Return copper pad diameter for a through-hole pad."""
    if diameter_attr and float(diameter_attr) > 0:
        return float(diameter_attr)
    if dru is None:
        dru = {}
    rv     = _parse_dru_mm(dru.get('rvPadTop',    '0.25'))
    rl_min = _parse_dru_mm(dru.get('rlMinPadTop', '10mil'))
    rl_max = _parse_dru_mm(dru.get('rlMaxPadTop', '20mil'))
    annular = max(rl_min, min(rl_max, drill * rv))
    return drill + 2 * annular


# ---------------------------------------------------------------------------
# Hershey vector font for text rendering
# ---------------------------------------------------------------------------

_HERSHEY_FONT_PATH = '/usr/share/inkscape/extensions/svg_fonts/HersheySansMed.svg'
_HERSHEY_CAP_HEIGHT = 662.0   # measured from 'H' glyph: top at y=662, baseline y=0
_SVG_NS = 'http://www.w3.org/2000/svg'
_hershey_cache = None


def _parse_svg_path_strokes(d):
    """Parse SVG path data (M/L commands only) into a list of polylines.
    Each polyline is a list of (x, y) tuples in font units (Y positive upward).
    """
    if not d.strip():
        return []
    polylines = []
    current = []
    tokens = d.replace(',', ' ').split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == 'M':
            if len(current) > 1:
                polylines.append(current)
            current = [(float(tokens[i + 1]), float(tokens[i + 2]))]
            i += 3
        elif tok == 'L':
            current.append((float(tokens[i + 1]), float(tokens[i + 2])))
            i += 3
        else:
            i += 1
    if len(current) > 1:
        polylines.append(current)
    return polylines


def _load_hershey_font():
    """Load and cache the HersheySansMed SVG font.  Returns dict: char -> (adv, polylines)."""
    global _hershey_cache
    if _hershey_cache is not None:
        return _hershey_cache
    _hershey_cache = {}
    try:
        tree = ET.parse(_HERSHEY_FONT_PATH)
        froot = tree.getroot()
        default_adv = 500.0
        for elem in froot.iter():
            if elem.tag in (f'{{{_SVG_NS}}}font', 'font'):
                default_adv = float(elem.get('horiz-adv-x', default_adv))
                break
        for elem in froot.iter():
            if elem.tag not in (f'{{{_SVG_NS}}}glyph', 'glyph'):
                continue
            uni = elem.get('unicode', '')
            if not uni:
                continue
            adv = float(elem.get('horiz-adv-x', default_adv))
            strokes = _parse_svg_path_strokes(elem.get('d', ''))
            _hershey_cache[uni] = (adv, strokes)
    except Exception as e:
        print(f"Warning: Hershey font not loaded: {e}")
    return _hershey_cache


def _emit_text(layers, prim, text_str, ex, ey, el_mirrored, el_angle):
    """Convert a text primitive to wire strokes and append to layers.

    prim must have: x, y, size, layer, ratio, rot.
    text_str is the resolved string (>NAME / >VALUE already substituted).
    ex/ey/el_mirrored/el_angle are the enclosing element's board-space transform
    (use 0/0/False/0 for board-level text where prim x/y is already in board space).
    """
    font = _load_hershey_font()
    if not font or not text_str:
        return

    lyr = _transform_layer(prim['layer'], el_mirrored)
    tx = prim['x']
    ty = prim['y']
    size_mm = float(prim['size'])
    ratio = float(prim.get('ratio', 8))
    text_mirrored, text_angle = _parse_rot(prim.get('rot', 'R0'))

    scale = size_mm / _HERSHEY_CAP_HEIGHT
    line_width = max(size_mm * ratio / 100.0, 0.05)
    line_spacing = size_mm * 1.5

    trad = math.radians(text_angle)
    tc, ts = math.cos(trad), math.sin(trad)

    for line_idx, line in enumerate(text_str.replace('\\n', '\n').split('\n')):
        line_y_local = -line_idx * line_spacing
        cursor_x = 0.0
        for ch in line:
            ch_data = font.get(ch)
            if ch_data is None:
                cursor_x += size_mm * 0.5
                continue
            adv, polylines = ch_data
            for poly in polylines:
                for seg in range(len(poly) - 1):
                    fx1, fy1 = poly[seg]
                    fx2, fy2 = poly[seg + 1]
                    # Scale and cursor offset in local text space
                    lx1 = fx1 * scale + cursor_x
                    ly1 = fy1 * scale + line_y_local
                    lx2 = fx2 * scale + cursor_x
                    ly2 = fy2 * scale + line_y_local
                    # Text-local mirroring (reflect about Y axis)
                    if text_mirrored:
                        lx1, lx2 = -lx1, -lx2
                    # Text-local rotation + translate to text anchor in package space
                    rx1 = lx1 * tc - ly1 * ts + tx
                    ry1 = lx1 * ts + ly1 * tc + ty
                    rx2 = lx2 * tc - ly2 * ts + tx
                    ry2 = lx2 * ts + ly2 * tc + ty
                    # Element transform (mirror + rotate + translate to board space)
                    bx1, by1 = _transform_point(rx1, ry1, ex, ey, el_mirrored, el_angle)
                    bx2, by2 = _transform_point(rx2, ry2, ex, ey, el_mirrored, el_angle)
                    layers[lyr].append({'type': 'wire',
                                        'x1': bx1, 'y1': by1,
                                        'x2': bx2, 'y2': by2,
                                        'width': line_width,
                                        'curve': None, 'net': ''})
            cursor_x += adv * scale


def _smd_aperture_key(dx, dy, roundness):
    """Return (aperture_type, w, h) for an SMD pad."""
    r = float(roundness) if roundness else 0
    w = float(dx)
    h = float(dy)
    if r >= 100:
        d = min(w, h)
        return ('C', d, d)
    elif r > 0:
        return ('O', w, h)
    else:
        return ('R', w, h)


def _collect_board_primitives(root):
    """Return per-layer list of drawing primitives from the board.
    Returns a dict: layer_num -> list of primitive dicts.
    Each primitive has a 'type' key plus type-specific fields.
    All coordinates are in mm (board frame).
    """
    layers = defaultdict(list)
    dru = _read_dru(root)

    # Build library package lookup: lib_name -> pkg_name -> list of primitives
    lib_packages = {}
    libraries = root.find('.//libraries')
    if libraries is not None:
        for lib in libraries.findall('library'):
            lib_name = lib.get('name')
            lib_packages[lib_name] = {}
            for pkg in lib.findall('.//package'):
                pkg_name = pkg.get('name')
                prims = _package_primitives(pkg)
                lib_packages[lib_name][pkg_name] = prims

    def add_primitive(layer_num, prim):
        layers[layer_num].append(prim)

    def process_wire(w, offset_x=0, offset_y=0, mirrored=False, angle=0, net=''):
        x1 = float(w.get('x1'))
        y1 = float(w.get('y1'))
        x2 = float(w.get('x2'))
        y2 = float(w.get('y2'))
        lyr = int(w.get('layer'))
        width = float(w.get('width', 0))
        curve = w.get('curve')
        x1, y1 = _transform_point(x1, y1, offset_x, offset_y, mirrored, angle)
        x2, y2 = _transform_point(x2, y2, offset_x, offset_y, mirrored, angle)
        lyr = _transform_layer(lyr, mirrored)
        prim = {'type': 'wire', 'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2,
                'width': width, 'curve': float(curve) if curve else None, 'net': net}
        if mirrored and curve:
            prim['curve'] = -float(curve)
        add_primitive(lyr, prim)

    def process_circle(c, offset_x=0, offset_y=0, mirrored=False, angle=0):
        cx = float(c.get('x'))
        cy = float(c.get('y'))
        lyr = int(c.get('layer'))
        radius = float(c.get('radius'))
        width = float(c.get('width', 0))
        cx, cy = _transform_point(cx, cy, offset_x, offset_y, mirrored, angle)
        lyr = _transform_layer(lyr, mirrored)
        add_primitive(lyr, {'type': 'circle', 'x': cx, 'y': cy,
                            'radius': radius, 'width': width})

    def process_rectangle(r, offset_x=0, offset_y=0, mirrored=False, angle=0):
        x1 = float(r.get('x1'))
        y1 = float(r.get('y1'))
        x2 = float(r.get('x2'))
        y2 = float(r.get('y2'))
        lyr = int(r.get('layer'))
        rot_str = r.get('rot', 'R0')
        _, local_angle = _parse_rot(rot_str)
        lyr = _transform_layer(lyr, mirrored)
        # Center of rectangle
        lcx = (x1 + x2) / 2
        lcy = (y1 + y2) / 2
        w = abs(x2 - x1)
        h = abs(y2 - y1)
        bx, by = _transform_point(lcx, lcy, offset_x, offset_y, mirrored, angle)
        total_angle = _transform_angle(local_angle, mirrored, angle)
        add_primitive(lyr, {'type': 'rectangle', 'x': bx, 'y': by,
                            'w': w, 'h': h, 'angle': total_angle})

    def process_polygon(poly, offset_x=0, offset_y=0, mirrored=False, angle=0, net=''):
        lyr = int(poly.get('layer'))
        width = float(poly.get('width', 0))
        lyr = _transform_layer(lyr, mirrored)
        isolate_str = poly.get('isolate')
        vertices = []
        for v in poly.findall('vertex'):
            vx = float(v.get('x'))
            vy = float(v.get('y'))
            vc = v.get('curve')
            bx, by = _transform_point(vx, vy, offset_x, offset_y, mirrored, angle)
            curve_val = float(vc) if vc else None
            if mirrored and curve_val is not None:
                curve_val = -curve_val
            vertices.append({'x': bx, 'y': by, 'curve': curve_val})
        if len(vertices) >= 2:
            add_primitive(lyr, {'type': 'polygon', 'vertices': vertices, 'width': width,
                                'net': net, 'isolate': float(isolate_str) if isolate_str else None})

    def process_smd(smd, offset_x=0, offset_y=0, mirrored=False, angle=0):
        sx = float(smd.get('x'))
        sy = float(smd.get('y'))
        dx = float(smd.get('dx'))
        dy = float(smd.get('dy'))
        lyr = int(smd.get('layer'))  # always 1 in package def
        roundness = smd.get('roundness', '0')
        rot_str = smd.get('rot', 'R0')
        _, local_angle = _parse_rot(rot_str)
        # Check cream/stop flags
        cream = smd.get('cream', 'yes')
        stop = smd.get('stop', 'yes')

        lyr = _transform_layer(lyr, mirrored)  # 1 or 16 depending on mirror
        bx, by = _transform_point(sx, sy, offset_x, offset_y, mirrored, angle)
        total_angle = _transform_angle(local_angle, mirrored, angle)
        r = float(roundness) if roundness else 0.0

        # Rotate dx/dy by total_angle (only 90 deg increments in practice)
        # For 90/270 degree rotations, swap dx and dy
        norm_angle = total_angle % 180
        if 45 < norm_angle < 135:
            eff_dx, eff_dy = dy, dx
        else:
            eff_dx, eff_dy = dx, dy

        prim = {'type': 'smd', 'x': bx, 'y': by, 'dx': eff_dx, 'dy': eff_dy,
                'roundness': r, 'angle': total_angle,
                'cream': cream, 'stop': stop, 'layer': lyr}
        add_primitive(lyr, prim)
        # Soldermask
        mask_lyr = 29 if lyr == 1 else 30
        if stop.lower() != 'no':
            expand = 0.1016
            add_primitive(mask_lyr, {'type': 'smd', 'x': bx, 'y': by,
                                     'dx': eff_dx + 2 * expand,
                                     'dy': eff_dy + 2 * expand,
                                     'roundness': r, 'angle': total_angle,
                                     'cream': 'no', 'stop': 'yes', 'layer': mask_lyr})
        # Paste
        paste_lyr = 31 if lyr == 1 else 32
        if cream.lower() != 'no':
            add_primitive(paste_lyr, {'type': 'smd', 'x': bx, 'y': by,
                                      'dx': eff_dx, 'dy': eff_dy,
                                      'roundness': r, 'angle': total_angle,
                                      'cream': 'yes', 'stop': 'no', 'layer': paste_lyr})

    def process_pad(pad, offset_x=0, offset_y=0, mirrored=False, angle=0):
        px = float(pad.get('x'))
        py = float(pad.get('y'))
        drill = float(pad.get('drill'))
        diameter = pad.get('diameter', '0')
        shape = pad.get('shape', 'round')
        rot_str = pad.get('rot', 'R0')
        _, local_angle = _parse_rot(rot_str)

        bx, by = _transform_point(px, py, offset_x, offset_y, mirrored, angle)
        total_angle = _transform_angle(local_angle, mirrored, angle)
        pad_diam = _th_pad_diameter(drill, diameter, shape, dru)

        prim = {'type': 'pad', 'x': bx, 'y': by, 'drill': drill,
                'diameter': pad_diam, 'shape': shape, 'angle': total_angle}
        # Copper on all layers 1-16 + layer 17
        for lyr in list(range(1, 17)) + [17]:
            add_primitive(lyr, dict(prim))
        # Soldermask on top and bottom
        expand = 0.1016
        for mask_lyr in [29, 30]:
            add_primitive(mask_lyr, {'type': 'smd', 'x': bx, 'y': by,
                                     'dx': pad_diam + 2 * expand,
                                     'dy': pad_diam + 2 * expand,
                                     'roundness': 100.0, 'angle': 0.0,
                                     'cream': 'no', 'stop': 'yes',
                                     'layer': mask_lyr})
        # Drill record
        add_primitive('drill', {'type': 'drill', 'x': bx, 'y': by,
                                'drill': drill, 'plated': True})

    # Process board-level plain section
    plain = root.find('.//board/plain')
    if plain is not None:
        for w in plain.findall('wire'):
            process_wire(w)
        for c in plain.findall('circle'):
            process_circle(c)
        for r in plain.findall('rectangle'):
            process_rectangle(r)
        for poly in plain.findall('polygon'):
            process_polygon(poly)
        for h in plain.findall('hole'):
            hx = float(h.get('x'))
            hy = float(h.get('y'))
            hd = float(h.get('drill'))
            add_primitive('drill', {'type': 'drill', 'x': hx, 'y': hy,
                                    'drill': hd, 'plated': False})
        for txt in plain.findall('text'):
            prim = {'x': float(txt.get('x', 0)), 'y': float(txt.get('y', 0)),
                    'size': float(txt.get('size', 1.27)),
                    'layer': int(txt.get('layer', 21)),
                    'ratio': float(txt.get('ratio', 8)),
                    'rot': txt.get('rot', 'R0')}
            _emit_text(layers, prim, txt.text or '', 0, 0, False, 0)

    # Build pad→net map from contactrefs: (element_name, pad_name) -> net_name
    pad_net_map = {}
    signals = root.find('.//board/signals')
    if signals is not None:
        for sig in signals.findall('signal'):
            net_name = sig.get('name', '')
            for cr in sig.findall('contactref'):
                pad_net_map[(cr.get('element', ''), cr.get('pad', ''))] = net_name

    # Process signals section (wires, vias, polygons in nets)
    if signals is not None:
        for sig in signals.findall('signal'):
            net_name = sig.get('name', '')
            for w in sig.findall('wire'):
                process_wire(w, net=net_name)
            for poly in sig.findall('polygon'):
                process_polygon(poly, net=net_name)
            for via in sig.findall('via'):
                vx = float(via.get('x'))
                vy = float(via.get('y'))
                drill = float(via.get('drill'))
                diam_attr = via.get('diameter', '0')
                extent = via.get('extent', '1-16')
                # Parse extent range
                parts_ex = extent.split('-')
                lo = int(parts_ex[0]) if len(parts_ex) > 0 else 1
                hi = int(parts_ex[1]) if len(parts_ex) > 1 else 16
                outer_diam = float(diam_attr) if float(diam_attr) > 0 else _via_diameter(drill, dru, outer=True)
                inner_diam = float(diam_attr) if float(diam_attr) > 0 else _via_diameter(drill, dru, outer=False)
                for lyr in range(lo, hi + 1):
                    diam = outer_diam if lyr in (1, 16) else inner_diam
                    add_primitive(lyr, {'type': 'via', 'x': vx, 'y': vy,
                                        'drill': drill, 'diameter': diam, 'net': net_name})
                # Also layer 18 (Vias) — use outer size
                add_primitive(18, {'type': 'via', 'x': vx, 'y': vy,
                                   'drill': drill, 'diameter': outer_diam, 'net': net_name})
                # Drill record
                add_primitive('drill', {'type': 'drill', 'x': vx, 'y': vy,
                                        'drill': drill, 'plated': True})

    # Process placed elements
    elements_section = root.find('.//board/elements')
    if elements_section is not None:
        for el in elements_section.findall('element'):
            el_name = el.get('name')
            lib_name = el.get('library')
            pkg_name = el.get('package')
            ex = float(el.get('x', 0))
            ey = float(el.get('y', 0))
            rot_str = el.get('rot', 'R0')
            mirrored, angle = _parse_rot(rot_str)

            pkg_prims = lib_packages.get(lib_name, {}).get(pkg_name, [])
            for prim in pkg_prims:
                t = prim['type']
                if t == 'wire':
                    # Create a fake ET element to reuse process_wire
                    _emit_wire(layers, prim, ex, ey, mirrored, angle)
                elif t == 'circle':
                    _emit_circle(layers, prim, ex, ey, mirrored, angle)
                elif t == 'rectangle':
                    _emit_rectangle(layers, prim, ex, ey, mirrored, angle)
                elif t == 'polygon':
                    _emit_polygon(layers, prim, ex, ey, mirrored, angle)
                elif t == 'smd':
                    smd_net = pad_net_map.get((el_name, prim.get('name', '')), '')
                    _emit_smd(layers, prim, ex, ey, mirrored, angle, net=smd_net)
                elif t == 'pad':
                    pad_net = pad_net_map.get((el_name, prim.get('name', '')), '')
                    _emit_th_pad(layers, prim, ex, ey, mirrored, angle, net=pad_net, dru=dru)
                elif t == 'hole':
                    hx2, hy2 = _transform_point(prim['x'], prim['y'],
                                                ex, ey, mirrored, angle)
                    layers['drill'].append({'type': 'drill', 'x': hx2, 'y': hy2,
                                            'drill': prim['drill'], 'plated': False})
                elif t == 'text':
                    txt_str = prim['text']
                    if txt_str == '>NAME':
                        txt_str = el_name
                    elif txt_str == '>VALUE':
                        txt_str = el.get('value', '')
                    _emit_text(layers, prim, txt_str, ex, ey, mirrored, angle)

    return layers


def _package_primitives(pkg):
    """Extract all drawing primitives from a library package element."""
    prims = []
    for w in pkg.findall('wire'):
        prims.append({'type': 'wire',
                      'x1': float(w.get('x1')), 'y1': float(w.get('y1')),
                      'x2': float(w.get('x2')), 'y2': float(w.get('y2')),
                      'width': float(w.get('width', 0)),
                      'layer': int(w.get('layer')),
                      'curve': float(w.get('curve')) if w.get('curve') else None})
    for c in pkg.findall('circle'):
        prims.append({'type': 'circle',
                      'x': float(c.get('x')), 'y': float(c.get('y')),
                      'radius': float(c.get('radius')),
                      'width': float(c.get('width', 0)),
                      'layer': int(c.get('layer'))})
    for r in pkg.findall('rectangle'):
        x1 = float(r.get('x1'))
        y1 = float(r.get('y1'))
        x2 = float(r.get('x2'))
        y2 = float(r.get('y2'))
        rot_str = r.get('rot', 'R0')
        _, local_angle = _parse_rot(rot_str)
        prims.append({'type': 'rectangle',
                      'x': (x1 + x2) / 2, 'y': (y1 + y2) / 2,
                      'w': abs(x2 - x1), 'h': abs(y2 - y1),
                      'layer': int(r.get('layer')),
                      'angle': local_angle})
    for poly in pkg.findall('polygon'):
        vertices = []
        for v in poly.findall('vertex'):
            vertices.append({'x': float(v.get('x')), 'y': float(v.get('y')),
                             'curve': float(v.get('curve')) if v.get('curve') else None})
        prims.append({'type': 'polygon', 'layer': int(poly.get('layer')),
                      'width': float(poly.get('width', 0)), 'vertices': vertices})
    for smd in pkg.findall('smd'):
        prims.append({'type': 'smd', 'name': smd.get('name', ''),
                      'x': float(smd.get('x')), 'y': float(smd.get('y')),
                      'dx': float(smd.get('dx')), 'dy': float(smd.get('dy')),
                      'layer': int(smd.get('layer')),
                      'roundness': float(smd.get('roundness', 0)),
                      'rot': smd.get('rot', 'R0'),
                      'cream': smd.get('cream', 'yes'),
                      'stop': smd.get('stop', 'yes')})
    for pad in pkg.findall('pad'):
        prims.append({'type': 'pad', 'name': pad.get('name', ''),
                      'x': float(pad.get('x')), 'y': float(pad.get('y')),
                      'drill': float(pad.get('drill')),
                      'diameter': pad.get('diameter', '0'),
                      'shape': pad.get('shape', 'round'),
                      'rot': pad.get('rot', 'R0')})
    for h in pkg.findall('hole'):
        prims.append({'type': 'hole',
                      'x': float(h.get('x')), 'y': float(h.get('y')),
                      'drill': float(h.get('drill'))})
    for txt in pkg.findall('text'):
        prims.append({'type': 'text',
                      'x': float(txt.get('x', 0)), 'y': float(txt.get('y', 0)),
                      'size': float(txt.get('size', 1.27)),
                      'layer': int(txt.get('layer', 21)),
                      'ratio': float(txt.get('ratio', 8)),
                      'rot': txt.get('rot', 'R0'),
                      'text': txt.text or ''})
    return prims


def _emit_wire(layers, prim, ex, ey, mirrored, angle):
    lyr = _transform_layer(prim['layer'], mirrored)
    x1, y1 = _transform_point(prim['x1'], prim['y1'], ex, ey, mirrored, angle)
    x2, y2 = _transform_point(prim['x2'], prim['y2'], ex, ey, mirrored, angle)
    curve = prim.get('curve')
    if mirrored and curve is not None:
        curve = -curve
    layers[lyr].append({'type': 'wire', 'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2,
                        'width': prim['width'], 'curve': curve})


def _emit_circle(layers, prim, ex, ey, mirrored, angle):
    lyr = _transform_layer(prim['layer'], mirrored)
    cx, cy = _transform_point(prim['x'], prim['y'], ex, ey, mirrored, angle)
    layers[lyr].append({'type': 'circle', 'x': cx, 'y': cy,
                        'radius': prim['radius'], 'width': prim['width']})


def _emit_rectangle(layers, prim, ex, ey, mirrored, angle):
    lyr = _transform_layer(prim['layer'], mirrored)
    bx, by = _transform_point(prim['x'], prim['y'], ex, ey, mirrored, angle)
    total_angle = _transform_angle(prim['angle'], mirrored, angle)
    layers[lyr].append({'type': 'rectangle', 'x': bx, 'y': by,
                        'w': prim['w'], 'h': prim['h'], 'angle': total_angle})


def _emit_polygon(layers, prim, ex, ey, mirrored, angle):
    lyr = _transform_layer(prim['layer'], mirrored)
    new_verts = []
    for v in prim['vertices']:
        bx, by = _transform_point(v['x'], v['y'], ex, ey, mirrored, angle)
        vc = v.get('curve')
        if mirrored and vc is not None:
            vc = -vc
        new_verts.append({'x': bx, 'y': by, 'curve': vc})
    layers[lyr].append({'type': 'polygon', 'vertices': new_verts,
                        'width': prim['width']})


def _emit_smd(layers, prim, ex, ey, mirrored, angle, net=''):
    sx = prim['x']
    sy = prim['y']
    dx = prim['dx']
    dy = prim['dy']
    lyr = _transform_layer(prim['layer'], mirrored)
    _, local_angle = _parse_rot(prim.get('rot', 'R0'))
    cream = prim.get('cream', 'yes')
    stop = prim.get('stop', 'yes')
    r = prim.get('roundness', 0.0)

    bx, by = _transform_point(sx, sy, ex, ey, mirrored, angle)
    total_angle = _transform_angle(local_angle, mirrored, angle)

    # Swap dx/dy if rotated 90/270
    norm_angle = total_angle % 180
    if 45 < norm_angle < 135:
        eff_dx, eff_dy = dy, dx
    else:
        eff_dx, eff_dy = dx, dy

    layers[lyr].append({'type': 'smd', 'x': bx, 'y': by,
                        'dx': eff_dx, 'dy': eff_dy, 'roundness': r,
                        'angle': total_angle, 'layer': lyr,
                        'cream': cream, 'stop': stop, 'net': net})
    # Soldermask
    mask_lyr = 29 if lyr == 1 else 30
    if stop.lower() != 'no':
        expand = 0.1016
        layers[mask_lyr].append({'type': 'smd', 'x': bx, 'y': by,
                                 'dx': eff_dx + 2 * expand,
                                 'dy': eff_dy + 2 * expand,
                                 'roundness': r, 'angle': total_angle,
                                 'layer': mask_lyr,
                                 'cream': 'no', 'stop': 'yes'})
    # Paste
    paste_lyr = 31 if lyr == 1 else 32
    if cream.lower() != 'no':
        layers[paste_lyr].append({'type': 'smd', 'x': bx, 'y': by,
                                  'dx': eff_dx, 'dy': eff_dy,
                                  'roundness': r, 'angle': total_angle,
                                  'layer': paste_lyr,
                                  'cream': 'yes', 'stop': 'no'})


def _emit_th_pad(layers, prim, ex, ey, mirrored, angle, net='', dru=None):
    px = prim['x']
    py = prim['y']
    drill = prim['drill']
    diameter = prim.get('diameter', '0')
    shape = prim.get('shape', 'round')
    _, local_angle = _parse_rot(prim.get('rot', 'R0'))

    bx, by = _transform_point(px, py, ex, ey, mirrored, angle)
    total_angle = _transform_angle(local_angle, mirrored, angle)
    pad_diam = _th_pad_diameter(drill, diameter, shape, dru)

    pad_prim = {'type': 'pad', 'x': bx, 'y': by, 'drill': drill,
                'diameter': pad_diam, 'shape': shape, 'angle': total_angle, 'net': net}
    for lyr in list(range(1, 17)) + [17]:
        layers[lyr].append(dict(pad_prim))
    expand = 0.1016
    for mask_lyr in [29, 30]:
        layers[mask_lyr].append({'type': 'smd', 'x': bx, 'y': by,
                                 'dx': pad_diam + 2 * expand,
                                 'dy': pad_diam + 2 * expand,
                                 'roundness': 100.0, 'angle': 0.0,
                                 'layer': mask_lyr,
                                 'cream': 'no', 'stop': 'yes'})
    layers['drill'].append({'type': 'drill', 'x': bx, 'y': by,
                            'drill': drill, 'plated': True})


def _aperture_for_prim(prim):
    """Return (aperture_type, w, h) tuple key for a primitive."""
    t = prim['type']
    if t in ('via', 'pad'):
        d = prim['diameter']
        return ('C', round(d, 6), round(d, 6))
    elif t == 'smd':
        dx = prim['dx']
        dy = prim['dy']
        r = prim.get('roundness', 0.0)
        if r >= 100:
            d = min(dx, dy)
            return ('C', round(d, 6), round(d, 6))
        elif r > 0:
            return ('O', round(dx, 6), round(dy, 6))
        else:
            return ('R', round(dx, 6), round(dy, 6))
    elif t == 'wire':
        w = prim['width']
        return ('C', round(w, 6), round(w, 6))
    elif t == 'circle':
        w = prim['width']
        if w <= 0:
            # Filled circle: use a round aperture of diameter = 2*radius
            d = prim['radius'] * 2
            return ('C', round(d, 6), round(d, 6))
        return ('C', round(w, 6), round(w, 6))
    elif t == 'rectangle':
        return ('R', round(prim['w'], 6), round(prim['h'], 6))
    return None


def _write_gerber(filepath, layer_name, primitives):
    """Write a Gerber RS-274X file for the given primitives."""
    # Collect all apertures
    aperture_map = {}  # key -> aperture number
    aperture_num = 10

    for prim in primitives:
        key = _aperture_for_prim(prim)
        if key is not None and key not in aperture_map:
            aperture_map[key] = aperture_num
            aperture_num += 1

    # Pre-register clearance apertures for every pour polygon
    pour_polys_pre = [p for p in primitives if p['type'] == 'polygon' and p.get('isolate') is not None]
    indiv_pre = [p for p in primitives if p['type'] != 'polygon']
    for pour in pour_polys_pre:
        iso = pour.get('isolate', 0.25)
        pour_net = pour.get('net', '')
        for p in indiv_pre:
            if p.get('net', '') == pour_net:
                continue
            t = p['type']
            if t in ('via', 'pad'):
                key = ('C', round(p['diameter'] + 2 * iso, 6), round(p['diameter'] + 2 * iso, 6))
            elif t == 'smd':
                dx, dy = p['dx'] + 2 * iso, p['dy'] + 2 * iso
                r = p.get('roundness', 0.0)
                key = ('C', round(min(dx,dy), 6), round(min(dx,dy), 6)) if r >= 100 else \
                      ('O', round(dx, 6), round(dy, 6)) if r > 0 else \
                      ('R', round(dx, 6), round(dy, 6))
            elif t == 'wire':
                key = ('C', round(p['width'] + 2 * iso, 6), round(p['width'] + 2 * iso, 6))
            else:
                continue
            if key not in aperture_map:
                aperture_map[key] = aperture_num
                aperture_num += 1

    lines = []
    lines.append("G04 EAGLE Gerber RS-274X export*")
    lines.append("G75*")
    lines.append("%MOMM*%")
    lines.append("%FSLAX34Y34*%")
    lines.append("%LPD*%")
    lines.append(f"%IN{layer_name}*%")
    lines.append("%IPPOS*%")
    lines.append("%AMOC8*")
    lines.append("5,1,8,0,0,1.08239X$1,22.5*%")
    lines.append("G01*")

    # Aperture definitions
    for key, num in sorted(aperture_map.items(), key=lambda x: x[1]):
        atype, w, h = key
        if atype == 'C':
            lines.append(f"%ADD{num}C,{w:.6f}*%")
        elif atype == 'R':
            lines.append(f"%ADD{num}R,{w:.6f}X{h:.6f}*%")
        elif atype == 'O':
            lines.append(f"%ADD{num}O,{w:.6f}X{h:.6f}*%")
    lines.append("")

    # Separate copper pours (polygon with isolate set) from decorative polygons
    pour_polys = [p for p in primitives if p['type'] == 'polygon' and p.get('isolate') is not None]
    deco_polys  = [p for p in primitives if p['type'] == 'polygon' and p.get('isolate') is None]
    pad_prims   = [p for p in primitives if p['type'] in ('via', 'pad', 'smd')]
    wire_prims  = [p for p in primitives if p['type'] == 'wire']
    circle_prims = [p for p in primitives if p['type'] == 'circle']
    rect_prims  = [p for p in primitives if p['type'] == 'rectangle']

    def _emit_polygon_region(lines, prim):
        """Write G36/G37 region for a polygon primitive."""
        verts = prim['vertices']
        if len(verts) < 2:
            return
        lines.append("G36*")
        lines.append(f"X{_g(verts[0]['x'])}Y{_g(verts[0]['y'])}D02*")
        for i, v in enumerate(verts):
            next_v = verts[(i + 1) % len(verts)]
            curve = v.get('curve')
            if curve:
                cx, cy, _ = _arc_center(v['x'], v['y'], next_v['x'], next_v['y'], curve)
                i_off, j_off = cx - v['x'], cy - v['y']
                cmd = "G03" if curve > 0 else "G02"
                lines.append(f"G75*{cmd}X{_g(next_v['x'])}Y{_g(next_v['y'])}I{_g(i_off)}J{_g(j_off)}D01*")
            else:
                lines.append(f"X{_g(next_v['x'])}Y{_g(next_v['y'])}D01*")
        lines.append("G37*")

    def _emit_clearance_flash(lines, prim, iso, aperture_map):
        """Flash a clearance region (LPC) around a primitive, expanded by iso mm."""
        t = prim['type']
        if t in ('via', 'pad'):
            key = ('C', round(prim['diameter'] + 2 * iso, 6), round(prim['diameter'] + 2 * iso, 6))
        elif t == 'smd':
            dx = prim['dx'] + 2 * iso
            dy = prim['dy'] + 2 * iso
            r = prim.get('roundness', 0.0)
            if r >= 100:
                d = min(dx, dy)
                key = ('C', round(d, 6), round(d, 6))
            elif r > 0:
                key = ('O', round(dx, 6), round(dy, 6))
            else:
                key = ('R', round(dx, 6), round(dy, 6))
        else:
            return
        num = aperture_map.get(key)
        if num is None:
            return
        lines.append(f"D{num}*")
        lines.append(f"X{_g(prim['x'])}Y{_g(prim['y'])}D03*")

    def _emit_clearance_wire(lines, prim, iso, aperture_map):
        """Draw a clearance track (LPC) along a wire, widened by iso mm each side."""
        key = ('C', round(prim['width'] + 2 * iso, 6), round(prim['width'] + 2 * iso, 6))
        num = aperture_map.get(key)
        if num is None:
            return
        lines.append(f"D{num}*")
        lines.append(f"X{_g(prim['x1'])}Y{_g(prim['y1'])}D02*")
        curve = prim.get('curve')
        if curve:
            cx, cy, _ = _arc_center(prim['x1'], prim['y1'], prim['x2'], prim['y2'], curve)
            i_off, j_off = cx - prim['x1'], cy - prim['y1']
            cmd = "G03" if curve > 0 else "G02"
            lines.append(f"G75*{cmd}X{_g(prim['x2'])}Y{_g(prim['y2'])}I{_g(i_off)}J{_g(j_off)}D01*")
        else:
            lines.append(f"X{_g(prim['x2'])}Y{_g(prim['y2'])}D01*")

    # Decorative polygons (no pour/isolate — silkscreen arrows etc.)
    for prim in deco_polys:
        _emit_polygon_region(lines, prim)

    if deco_polys:
        lines.append("%LPD*%")

    # Pour polygons: fill → LPC clearances for foreign-net features → LPD re-draw
    for pour in pour_polys:
        iso = pour.get('isolate', 0.25)
        pour_net = pour.get('net', '')

        # 1. Draw the solid fill in LPD (already in LPD from header / previous step)
        _emit_polygon_region(lines, pour)

        # 2. Switch to clear polarity and cut isolation gaps around foreign-net features
        lines.append("%LPC*%")
        cur_ap = None
        for p in pad_prims:
            if p.get('net', '') == pour_net:
                continue  # same net — merges with pour, no clearance needed
            t = p['type']
            if t in ('via', 'pad'):
                key = ('C', round(p['diameter'] + 2 * iso, 6), round(p['diameter'] + 2 * iso, 6))
            elif t == 'smd':
                dx, dy = p['dx'] + 2 * iso, p['dy'] + 2 * iso
                r = p.get('roundness', 0.0)
                key = ('C', round(min(dx,dy), 6), round(min(dx,dy), 6)) if r >= 100 else \
                      ('O', round(dx, 6), round(dy, 6)) if r > 0 else \
                      ('R', round(dx, 6), round(dy, 6))
            else:
                continue
            num = aperture_map.get(key)
            if num is None:
                continue
            if num != cur_ap:
                lines.append(f"D{num}*")
                cur_ap = num
            lines.append(f"X{_g(p['x'])}Y{_g(p['y'])}D03*")

        for p in wire_prims:
            if p.get('net', '') == pour_net:
                continue
            key = ('C', round(p['width'] + 2 * iso, 6), round(p['width'] + 2 * iso, 6))
            num = aperture_map.get(key)
            if num is None:
                continue
            if num != cur_ap:
                lines.append(f"D{num}*")
                cur_ap = num
            lines.append(f"X{_g(p['x1'])}Y{_g(p['y1'])}D02*")
            curve = p.get('curve')
            if curve:
                cx, cy, _ = _arc_center(p['x1'], p['y1'], p['x2'], p['y2'], curve)
                i_off, j_off = cx - p['x1'], cy - p['y1']
                cmd = "G03" if curve > 0 else "G02"
                lines.append(f"G75*{cmd}X{_g(p['x2'])}Y{_g(p['y2'])}I{_g(i_off)}J{_g(j_off)}D01*")
            else:
                lines.append(f"X{_g(p['x2'])}Y{_g(p['y2'])}D01*")

        # 3. Back to dark and re-draw all individual features to restore cleared copper
        lines.append("%LPD*%")

    # Pads (flash with D03)
    current_aperture = None
    for prim in pad_prims:
        key = _aperture_for_prim(prim)
        if key is None:
            continue
        num = aperture_map[key]
        if num != current_aperture:
            lines.append(f"D{num}*")
            current_aperture = num
        lines.append(f"X{_g(prim['x'])}Y{_g(prim['y'])}D03*")

    # Rectangles (flash)
    for prim in rect_prims:
        key = _aperture_for_prim(prim)
        if key is None:
            continue
        num = aperture_map[key]
        if num != current_aperture:
            lines.append(f"D{num}*")
            current_aperture = num
        lines.append(f"X{_g(prim['x'])}Y{_g(prim['y'])}D03*")

    # Circles
    for prim in circle_prims:
        cx = prim['x']
        cy = prim['y']
        radius = prim['radius']
        width = prim['width']
        if width <= 0:
            # Filled circle: flash with aperture
            key = _aperture_for_prim(prim)
            if key is None:
                continue
            num = aperture_map[key]
            if num != current_aperture:
                lines.append(f"D{num}*")
                current_aperture = num
            lines.append(f"X{_g(cx)}Y{_g(cy)}D03*")
        else:
            # Circle outline: draw as arc segments
            # Use full circle as arc: break into 4 quadrants
            key = _aperture_for_prim(prim)
            if key is None:
                continue
            num = aperture_map[key]
            if num != current_aperture:
                lines.append(f"D{num}*")
                current_aperture = num
            # Draw 4 arcs (quadrants)
            pts = [
                (cx + radius, cy),
                (cx, cy + radius),
                (cx - radius, cy),
                (cx, cy - radius),
                (cx + radius, cy),
            ]
            lines.append(f"X{_g(pts[0][0])}Y{_g(pts[0][1])}D02*")
            for k in range(4):
                x1, y1 = pts[k]
                x2, y2 = pts[k + 1]
                i_off = cx - x1
                j_off = cy - y1
                lines.append(f"G75*G02X{_g(x2)}Y{_g(y2)}I{_g(i_off)}J{_g(j_off)}D01*")

    # Wires
    for prim in wire_prims:
        key = _aperture_for_prim(prim)
        if key is None:
            continue
        num = aperture_map.get(key)
        if num is None:
            continue
        if num != current_aperture:
            lines.append(f"D{num}*")
            current_aperture = num
        x1, y1, x2, y2 = prim['x1'], prim['y1'], prim['x2'], prim['y2']
        curve = prim.get('curve')
        if curve:
            cx_arc, cy_arc, r = _arc_center(x1, y1, x2, y2, curve)
            i_off = cx_arc - x1
            j_off = cy_arc - y1
            lines.append(f"X{_g(x1)}Y{_g(y1)}D02*")
            if curve > 0:
                lines.append(f"G75*G03X{_g(x2)}Y{_g(y2)}I{_g(i_off)}J{_g(j_off)}D01*")
            else:
                lines.append(f"G75*G02X{_g(x2)}Y{_g(y2)}I{_g(i_off)}J{_g(j_off)}D01*")
        else:
            lines.append(f"X{_g(x1)}Y{_g(y1)}D02*")
            lines.append(f"X{_g(x2)}Y{_g(y2)}D01*")

    lines.append("M02*")

    with open(filepath, 'w') as f:
        f.write('\n'.join(lines) + '\n')


def _write_drill(filepath, drill_prims):
    """Write an Excellon drill file."""
    # Group by drill size
    sizes = sorted(set(round(p['drill'], 4) for p in drill_prims))
    tool_map = {size: i + 1 for i, size in enumerate(sizes)}

    lines = []
    lines.append("M48")
    lines.append("METRIC,TZ")
    for size in sizes:
        t = tool_map[size]
        lines.append(f"T{t}C{size:.3f}")
    lines.append("%")
    lines.append("G90")
    lines.append("G05")

    by_tool = defaultdict(list)
    for p in drill_prims:
        t = tool_map[round(p['drill'], 4)]
        by_tool[t].append(p)

    for t in sorted(by_tool.keys()):
        lines.append(f"T{t}")
        for p in by_tool[t]:
            # METRIC,TZ = trailing zeros suppressed, 3 decimal places
            # Coordinate in 0.001 mm: x=1.234mm -> X1234
            xi = round(p['x'] * 1000)
            yi = round(p['y'] * 1000)
            lines.append(f"X{xi}Y{yi}")

    lines.append("M30")

    with open(filepath, 'w') as f:
        f.write('\n'.join(lines) + '\n')


def _brd_to_gerbers(brd_file, output_dir):
    """Parse EAGLE .brd and write all Gerber + Excellon files to output_dir."""
    tree = ET.parse(brd_file)
    root = tree.getroot()

    name = os.path.splitext(os.path.basename(brd_file))[0]
    os.makedirs(output_dir, exist_ok=True)

    print(f"  Parsing {brd_file}...")
    layers = _collect_board_primitives(root)

    # Layer mapping: output file -> list of eagle layer numbers to include
    outputs = [
        (f"{name}_copper_l1.GTL",       "Top Copper",           [1, 17, 18]),
        (f"{name}_copper_l2.G1",         "Inner Copper Layer 1", [2, 17, 18]),
        (f"{name}_copper_l3.G2",         "Inner Copper Layer 2", [3, 17, 18]),
        (f"{name}_copper_l4.G3",         "Inner Copper Layer 3", [4, 17, 18]),
        (f"{name}_copper_l5.G4",         "Inner Copper Layer 4", [5, 17, 18]),
        (f"{name}_copper_l6.GBL",        "Bottom Copper",        [16, 17, 18]),
        (f"{name}_Soldermask_Top.GTS",   "Soldermask Top",       [29]),
        (f"{name}_Soldermask_Bot.GBS",   "Soldermask Bottom",    [30]),
        (f"{name}_Paste_Top.GTP",        "Paste Top",            [31]),
        (f"{name}_Paste_Bot.GBP",        "Paste Bottom",         [32]),
        (f"{name}_Legend_Top.GTO",       "Silkscreen Top",       [21, 25]),
        (f"{name}_Legend_Bot.GBO",       "Silkscreen Bottom",    [22, 26]),
        (f"{name}_Profile_NP.GKO",       "Board Outline",        [20, 46]),
    ]

    for filename, layer_name, layer_nums in outputs:
        filepath = os.path.join(output_dir, filename)
        print(f"  Writing {filename}")
        # Deduplicate: vias/pads appear on multiple layers; avoid double-writing
        # For layers 17 and 18, only include via/pad when they don't appear already
        # in the main copper layer in this output group
        prims = []
        seen_flash = set()
        for lnum in layer_nums:
            for p in layers.get(lnum, []):
                # Deduplicate via/pad flashes at same position
                if p['type'] in ('via', 'pad'):
                    key = (round(p['x'], 4), round(p['y'], 4), round(p['diameter'], 4))
                    if key in seen_flash:
                        continue
                    seen_flash.add(key)
                prims.append(p)
        _write_gerber(filepath, layer_name, prims)

    # Drill file
    drill_prims = layers.get('drill', [])
    drill_file = os.path.join(output_dir, f"{name}_drill.XLN")
    print(f"  Writing {name}_drill.XLN")
    _write_drill(drill_file, drill_prims)

    total = sum(len(v) for k, v in layers.items() if k != 'drill')
    print(f"  Done: {total} primitives across all layers, {len(drill_prims)} drill hits")


@task
def gerbers(ctx, brd_file):
    """Generate Gerber RS-274X and Excellon drill files from an EAGLE .brd file (no Eagle required).
    Output is zipped as <name>_<datetime>_<githash>.zip in the same directory as the .brd file.
    """
    import zipfile
    from datetime import datetime

    print(f"Generating Gerbers from {brd_file}")

    # Build zip filename
    name = os.path.splitext(os.path.basename(brd_file))[0]
    dt = datetime.now().strftime("%Y%m%d")
    try:
        git_hash = ctx.run("git rev-parse --short HEAD", hide=True).stdout.strip()
    except Exception:
        git_hash = "unknown"
    brd_dir = os.path.dirname(os.path.abspath(brd_file))
    zip_name = f"{name}_{dt}_{git_hash}.zip"
    zip_path = os.path.join(brd_dir, zip_name)

    # Write Gerbers into a temp dir then zip
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        _brd_to_gerbers(brd_file, tmp)
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(os.listdir(tmp)):
                zf.write(os.path.join(tmp, f), f)

    print(f"  → {zip_name}")


@task
def pins(ctx, sch_file, part_name, pickle=None):
	"""
	Print all signals connected to all pins of a given part.

	Example: invoke pins main.sch U6
	Example: invoke pins main.sch U6 --pickle=old
	Output format: Pad, Pin, Net
	If pickle is provided, exports data as pickled DataFrame to that filename.
	"""
	print(f"Analyzing {part_name} in {sch_file}")

	# Parse the XML file
	tree = ET.parse(sch_file)
	root = tree.getroot()

	# Find the part
	part = None
	parts_section = root.find('.//parts')
	if parts_section is not None:
		for p in parts_section.findall('part'):
			if p.get('name') == part_name:
				part = p
				break

	if part is None:
		print(f"Error: Part {part_name} not found in {sch_file}")
		return

	# Get deviceset and device information
	library_name = part.get('library')
	deviceset_name = part.get('deviceset')
	device_name = part.get('device', '')

	# Find the deviceset in libraries
	pin_to_pad = {}
	library = root.find(f'.//library[@name="{library_name}"]')
	if library is not None:
		deviceset = library.find(f'.//deviceset[@name="{deviceset_name}"]')
		if deviceset is not None:
			# Find the device (devices are inside a <devices> element)
			devices_element = deviceset.find('devices')
			device = None
			if devices_element is not None:
				for d in devices_element.findall('device'):
					if d.get('name') == device_name:
						device = d
						break
				# If no device name match, use the first device
				if device is None and len(devices_element.findall('device')) > 0:
					device = devices_element.findall('device')[0]

			if device is not None:
				# Build pin-to-pad mapping
				connects = device.find('connects')
				if connects is not None:
					for connect in connects.findall('connect'):
						pin = connect.get('pin')
						pad = connect.get('pad')
						gate = connect.get('gate')
						# Store as (gate, pin) -> pad(s)
						key = (gate, pin)
						if key not in pin_to_pad:
							pin_to_pad[key] = []
						# Pad can be multiple pads separated by spaces
						pin_to_pad[key].extend(pad.split())

	# Find all nets with pinrefs for this part
	# Nets can be in sheets, so search in all sheets
	connections = []  # List of (pad, pin, net_name)

	# Search in all sheets
	sheets = root.findall('.//sheet')
	for sheet in sheets:
		nets = sheet.findall('.//net')
		for net in nets:
			net_name = net.get('name', '')
			for segment in net.findall('segment'):
				for pinref in segment.findall('pinref'):
					if pinref.get('part') == part_name:
						gate = pinref.get('gate', 'G$1')
						pin = pinref.get('pin')
						# Get pad name(s) for this pin
						pads = pin_to_pad.get((gate, pin), [])
						if not pads:
							pads = ['?']  # Unknown pad

						# Add connection for each pad
						for pad in pads:
							connections.append((pad, pin, net_name))

	# Sort by pad name (treating numeric parts properly)
	def pad_sort_key(item):
		pad = item[0]
		# Try to extract numeric part for sorting
		import re
		match = re.match(r'([A-Z]+)(\d+)', pad)
		if match:
			return (match.group(1), int(match.group(2)))
		return (pad, 0)

	connections.sort(key=pad_sort_key)

	# Create DataFrame
	df = pd.DataFrame(connections, columns=['Pad', 'Pin', 'Net'])

	# Export to pickle if requested
	if pickle is not None:
		df.to_pickle(pickle)
		print(f"Data exported to {pickle}")
		return

	# Print results
	print(f"\nConnections for {part_name}:")
	print(f"{'Pad':<10} {'Pin':<25} {'Net':<30}")
	print("-" * 70)
	for pad, pin, net in connections:
		print(f"{pad:<10} {pin:<25} {net:<30}")

	print(f"\nTotal: {len(connections)} connection(s)")


@task
def pins_compare(ctx, old, new, deltas=False, output=None):
	"""
	Compare two pickled pin connection DataFrames.

	Loads two pickled DataFrames (from pins task) and joins them on the Pad column.

	Example: invoke pins-compare old.pkl new.pkl
	Example: invoke pins-compare old.pkl new.pkl --deltas
	Example: invoke pins-compare old.pkl new.pkl --output=comparison.xlsx
	Example: invoke pins-compare old.pkl new.pkl --deltas --output=changes.csv
	"""
	# Load the pickled DataFrames
	df_old = pd.read_pickle(old)
	df_new = pd.read_pickle(new)

	# Join on Pad column
	df_merged = pd.merge(df_old, df_new, on='Pad', how='outer', suffixes=('_old', '_new'))

	# Since pins are the same, keep only one Pin column
	if 'Pin_old' in df_merged.columns and 'Pin_new' in df_merged.columns:
		# Use Pin_old (or Pin_new, they should be the same) and rename to Pin
		df_merged['Pin'] = df_merged['Pin_old'].fillna(df_merged['Pin_new'])
		df_merged = df_merged.drop(columns=['Pin_old', 'Pin_new'])

	# Rename Net_old and Net_new to Old and New
	df_merged = df_merged.rename(columns={'Net_old': 'Old', 'Net_new': 'New'})

	# Filter to only show differences if deltas flag is set
	if deltas:
		# Show rows where Old != New (handles NaN cases properly)
		df_merged = df_merged[df_merged['Old'].fillna('') != df_merged['New'].fillna('')]

	# Export to file if output is specified
	if output is not None:
		if output.endswith('.xlsx'):
			df_merged.to_excel(output, index=False)
			print(f"Data exported to {output}")
		elif output.endswith('.csv'):
			df_merged.to_csv(output, index=False)
			print(f"Data exported to {output}")
		else:
			print(f"Error: Output file must have .xlsx or .csv extension")
			return

	# Print the full dataframe
	print(df_merged.to_string())
