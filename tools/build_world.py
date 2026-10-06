"""Build beaconview/static/world.json, the country outlines the Countries map draws, from Natural Earth's 1:110m
admin-0 countries (public domain, naturalearthdata.com).

    python tools/build_world.py ne_110m_admin_0_countries.geojson

Countries are keyed by ISO 3166-1 alpha-2 (Natural Earth's ISO_A2_EH), the codes GeoIP gives. Shapes are projected
here (Equal Earth) into ready SVG path data, so the page only draws. Antarctica is left out: no traffic, much area.
Places too small for this scale (Singapore, Hong Kong, Malta, ...) are not on the map; the table still lists them.
"""

import json
import math
import sys

WIDTH = 1000
PRECISION = 1                  # decimals in the path data
# not countries in ISO 3166; IP databases report these places under the country they lie in
FOLD = {"N. Cyprus": "CY", "Somaliland": "SO"}

A1, A2, A3, A4 = 1.340264, -0.081106, 0.000893, 0.003796
M = math.sqrt(3) / 2


def equal_earth(lon, lat):
    lam, phi = math.radians(lon), math.radians(lat)
    t = math.asin(M * math.sin(phi))
    t2, t6 = t * t, t ** 6
    x = lam * math.cos(t) / (M * (A1 + 3 * A2 * t2 + t6 * (7 * A3 + 9 * A4 * t2)))
    y = t * (A1 + A2 * t2 + t6 * (A3 + A4 * t2))
    return x, -y


def rings(geom):
    if geom["type"] == "Polygon":
        return geom["coordinates"]
    return [r for poly in geom["coordinates"] for r in poly]


def main(src, out="beaconview/static/world.json"):
    with open(src, encoding="utf-8") as f:
        features = json.load(f)["features"]
    shapes = {}
    for feat in features:
        p = feat["properties"]
        code = FOLD.get(p["NAME"], p["ISO_A2_EH"])
        if code in ("AQ", "-99"):
            continue
        shapes.setdefault(code, []).extend([[equal_earth(lon, lat) for lon, lat in ring] for ring in rings(feat["geometry"])])
    pts = [pt for rs in shapes.values() for r in rs for pt in r]
    x0, x1 = min(x for x, _ in pts), max(x for x, _ in pts)
    y0, y1 = min(y for _, y in pts), max(y for _, y in pts)
    scale = WIDTH / (x1 - x0)
    height = round((y1 - y0) * scale)
    q = 10 ** PRECISION

    def path(rs):
        parts = []
        for ring in rs:
            prev, cmds = None, []
            for x, y in ring:
                p = (round((x - x0) * scale * q), round((y - y0) * scale * q))
                if prev is None:
                    cmds.append(f"M{fmt(p[0])} {fmt(p[1])}")
                elif p != prev:
                    cmds.append(f"l{fmt(p[0] - prev[0])} {fmt(p[1] - prev[1])}")
                prev = p
            if len(cmds) > 2:
                parts.append("".join(cmds) + "z")
        return "".join(parts)

    def fmt(v):
        s = f"{v / q:.{PRECISION}f}".rstrip("0").rstrip(".")
        return s.replace("0.", ".", 1) if s.startswith(("0.", "-0.")) else s

    world = {"source": "Natural Earth 1:110m admin-0 countries (public domain), Equal Earth projection",
             "width": WIDTH, "height": height, "countries": {c: path(rs) for c, rs in sorted(shapes.items())}}
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(world, f, separators=(",", ":"))
        f.write("\n")
    print(f"{out}: {len(world['countries'])} countries, {WIDTH}x{height}")


if __name__ == "__main__":
    main(*sys.argv[1:])
