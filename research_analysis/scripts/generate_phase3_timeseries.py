#!/usr/bin/env python3
"""Generate the Article 2 pH and EC time-series figure as an EPS file."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from xml.etree.ElementTree import iterparse
from zipfile import ZipFile


NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def column_index(reference: str) -> int:
    value = 0
    for char in re.match(r"[A-Z]+", reference).group():
        value = value * 26 + ord(char) - 64
    return value - 1


def shared_strings(archive: ZipFile) -> list[str]:
    values: list[str] = []
    if "xl/sharedStrings.xml" not in archive.namelist():
        return values
    with archive.open("xl/sharedStrings.xml") as source:
        for _, element in iterparse(source, events=("end",)):
            if element.tag == NS + "si":
                values.append("".join(node.text or "" for node in element.iter(NS + "t")))
                element.clear()
    return values


def worksheet_records(archive: ZipFile, sheet: int, strings: list[str]):
    with archive.open(f"xl/worksheets/sheet{sheet}.xml") as source:
        header = None
        for _, element in iterparse(source, events=("end",)):
            if element.tag != NS + "row":
                continue
            cells = {}
            for cell in element.findall(NS + "c"):
                index = column_index(cell.attrib["r"])
                node = cell.find(NS + "v")
                raw = node.text if node is not None else None
                cell_type = cell.attrib.get("t")
                if cell_type == "s" and raw is not None:
                    cells[index] = strings[int(raw)]
                elif cell_type == "inlineStr":
                    inline = cell.find(NS + "is")
                    cells[index] = (
                        "".join(node.text or "" for node in inline.iter(NS + "t"))
                        if inline is not None
                        else None
                    )
                else:
                    cells[index] = raw
            row = [cells.get(i) for i in range(max(cells, default=-1) + 1)]
            element.clear()
            if header is None:
                header = row
                continue
            row += [None] * (len(header) - len(row))
            yield dict(zip(header, row))


def escape_ps(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def hourly_medians(observations):
    """Match display resolution to a single-column, 35-day overview."""
    buckets = {}
    for timestamp, ph, ec in observations:
        key = timestamp.replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(key, ([], []))
        buckets[key][0].append(ph)
        buckets[key][1].append(ec)
    return [
        (timestamp, median(values[0]), median(values[1]))
        for timestamp, values in sorted(buckets.items())
    ]


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: generate_phase3_timeseries.py INPUT.xlsx OUTPUT.eps")

    workbook = Path(sys.argv[1])
    output = Path(sys.argv[2])
    observations = []
    actions = []

    with ZipFile(workbook) as archive:
        strings = shared_strings(archive)
        for row in worksheet_records(archive, 3, strings):
            metadata = json.loads(row["decision_metadata"])
            trigger = metadata.get("triggered_by")
            timestamp = datetime.fromisoformat(row["timestamp"])
            if trigger not in {"batch_scheduler", "maintenance_mode"}:
                observations.append((timestamp, float(row["ph"]), float(row["ec"])))
            if row["pump_activated"] != "none":
                actions.append(
                    (
                        timestamp,
                        row["pump_activated"],
                        float(row["ph"]),
                        float(row["ec"]),
                    )
                )

    display_observations = hourly_medians(observations)
    first = observations[0][0]
    last = observations[-1][0]
    span = (last - first).total_seconds()
    page_w, page_h = 600, 360
    left, right = 72, 570
    panel_h = 125
    ph_bottom, ec_bottom = 210, 65
    plot_w = right - left

    def xcoord(value: datetime) -> float:
        return left + ((value - first).total_seconds() / span) * plot_w

    def ycoord(value: float, lower: float, upper: float, bottom: float) -> float:
        return bottom + ((value - lower) / (upper - lower)) * panel_h

    lines = [
        "%!PS-Adobe-3.0 EPSF-3.0",
        f"%%BoundingBox: 0 0 {page_w} {page_h}",
        "1 setlinejoin 1 setlinecap",
        "/Helvetica findfont 14 scalefont setfont",
    ]

    def band(bottom: float, lower: float, upper: float, target_low: float, target_high: float):
        y0 = ycoord(target_low, lower, upper, bottom)
        y1 = ycoord(target_high, lower, upper, bottom)
        lines.extend([
            "0.92 0.96 0.92 setrgbcolor",
            f"{left:.2f} {y0:.2f} {plot_w:.2f} {y1-y0:.2f} rectfill",
            "0.28 0.55 0.31 setrgbcolor",
            "[4 3] 0 setdash 0.45 setlinewidth",
            f"newpath {left:.2f} {y0:.2f} moveto {right:.2f} {y0:.2f} lineto stroke",
            f"newpath {left:.2f} {y1:.2f} moveto {right:.2f} {y1:.2f} lineto stroke",
            "[] 0 setdash",
        ])

    # Use the same axis limits for bands, traces, and ticks.
    band(ph_bottom, 5.4, 6.7, 5.5, 6.5)
    band(ec_bottom, 1.0, 2.25, 1.2, 2.0)
    lines.append("0 setgray")

    lines.extend(["0.84 setgray", "0.3 setlinewidth"])
    for day in range(0, 36, 7):
        x = xcoord(first + timedelta(days=day))
        for bottom in (ph_bottom, ec_bottom):
            lines.append(
                f"newpath {x:.2f} {bottom:.2f} moveto "
                f"{x:.2f} {bottom+panel_h:.2f} lineto stroke"
            )
    for value in (5.5, 6.0, 6.5):
        y = ycoord(value, 5.4, 6.7, ph_bottom)
        lines.append(f"newpath {left} {y:.2f} moveto {right} {y:.2f} lineto stroke")
    for value in (1.0, 1.5, 2.0):
        y = ycoord(value, 1.0, 2.25, ec_bottom)
        lines.append(f"newpath {left} {y:.2f} moveto {right} {y:.2f} lineto stroke")

    def trace(column: int, lower: float, upper: float, bottom: float, color: str):
        # The plot connects hourly medians across gaps; metrics exclude long gaps.
        lines.extend([color, "1.05 setlinewidth", "newpath"])
        for index, row in enumerate(display_observations):
            command = "moveto" if index == 0 else "lineto"
            lines.append(f"{xcoord(row[0]):.2f} {ycoord(row[column], lower, upper, bottom):.2f} {command}")
        lines.append("stroke")

    trace(1, 5.4, 6.7, ph_bottom, "0.05 0.32 0.62 setrgbcolor")
    trace(2, 1.0, 2.25, ec_bottom, "0.88 0.40 0.05 setrgbcolor")

    def triangle(x: float, y: float, size: float, upward: bool):
        direction = 1 if upward else -1
        lines.append(
            f"newpath {x:.2f} {y+direction*size:.2f} moveto "
            f"{x-size:.2f} {y-direction*size:.2f} lineto "
            f"{x+size:.2f} {y-direction*size:.2f} lineto closepath fill"
        )

    def diamond(x: float, y: float, size: float):
        lines.append(
            f"newpath {x:.2f} {y+size:.2f} moveto "
            f"{x-size:.2f} {y:.2f} lineto {x:.2f} {y-size:.2f} lineto "
            f"{x+size:.2f} {y:.2f} lineto closepath fill"
        )

    # Mark dosing times at the panel edges to keep the readings visible.
    for timestamp, pump, ph, ec in actions:
        x = xcoord(timestamp)
        if pump == "ph_down":
            lines.append("0.72 0.08 0.12 setrgbcolor")
            triangle(x, ph_bottom + panel_h - 5, 2.4, False)
        elif pump == "ec_up":
            lines.append("0.00 0.48 0.52 setrgbcolor")
            triangle(x, ec_bottom + 5, 2.4, True)
        elif pump == "ec_down":
            lines.append("0.18 0.18 0.18 setrgbcolor")
            diamond(x, ec_bottom + panel_h - 6, 3.2)

    lines.extend(["0 setgray", "0.7 setlinewidth"])
    for bottom in (ph_bottom, ec_bottom):
        lines.append(f"newpath {left} {bottom} moveto {right} {bottom} lineto {right} {bottom+panel_h} lineto {left} {bottom+panel_h} lineto closepath stroke")

    lines.extend(["/Helvetica findfont 13 scalefont setfont"])
    for value in (5.5, 6.0, 6.5):
        y = ycoord(value, 5.4, 6.7, ph_bottom)
        lines.append(f"newpath {left-4} {y:.2f} moveto {left} {y:.2f} lineto stroke")
        lines.append(f"{left-31} {y-3:.2f} moveto ({value:.1f}) show")
    for value in (1.0, 1.5, 2.0):
        y = ycoord(value, 1.0, 2.25, ec_bottom)
        lines.append(f"newpath {left-4} {y:.2f} moveto {left} {y:.2f} lineto stroke")
        lines.append(f"{left-31} {y-3:.2f} moveto ({value:.1f}) show")

    for day in range(0, 36, 7):
        timestamp = first + timedelta(days=day)
        x = xcoord(timestamp)
        lines.append(f"newpath {x:.2f} {ec_bottom-4} moveto {x:.2f} {ec_bottom} lineto stroke")
        label = timestamp.strftime("%b %d")
        lines.append(f"{x-19:.2f} {ec_bottom-20} moveto ({escape_ps(label)}) show")

    lines.extend([
        "/Helvetica-Bold findfont 15 scalefont setfont",
        f"gsave 18 {ph_bottom+50} translate 90 rotate 0 0 moveto (pH) show grestore",
        f"gsave 18 {ec_bottom+24} translate 90 rotate 0 0 moveto ({escape_ps('EC (mS/cm)')}) show grestore",
        "/Helvetica findfont 9 scalefont setfont",
        "0.72 0.08 0.12 setrgbcolor",
        f"newpath {left+18} {ph_bottom+panel_h-16} moveto {left+13} {ph_bottom+panel_h-8} lineto {left+23} {ph_bottom+panel_h-8} lineto closepath fill",
        "0 setgray",
        f"{left+29} {ph_bottom+panel_h-15} moveto (pH Down) show",
        "0.72 0.08 0.12 setrgbcolor",
        "0.00 0.48 0.52 setrgbcolor",
        f"newpath {left+18} {ec_bottom+panel_h-8} moveto {left+13} {ec_bottom+panel_h-16} lineto {left+23} {ec_bottom+panel_h-16} lineto closepath fill",
        "0 setgray",
        f"{left+29} {ec_bottom+panel_h-15} moveto (EC Up) show",
        "0.18 setgray",
        f"newpath {left+114} {ec_bottom+panel_h-8} moveto {left+110} {ec_bottom+panel_h-12} lineto {left+114} {ec_bottom+panel_h-16} lineto {left+118} {ec_bottom+panel_h-12} lineto closepath fill",
        "0 setgray",
        f"{left+125} {ec_bottom+panel_h-15} moveto (EC Down) show",
        "showpage",
        "%%EOF",
    ])
    output.write_text("\n".join(lines), encoding="ascii")


if __name__ == "__main__":
    main()
