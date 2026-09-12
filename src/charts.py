import html


def clip(text, limit):
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def compact(value):
    value = float(value)
    sign = "-" if value < 0 else ""
    value = abs(value)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= limit:
            return "%s%.1f%s" % (sign, value / limit, suffix)
    return "%s%.0f" % (sign, value)


def signed_compact(value):
    return ("+" if value >= 0 else "") + compact(value)


def exact(value):
    return "{:,}".format(int(round(value)))


def percent(value, digits=1):
    return ("%." + str(digits) + "f%%") % value


def nice_ticks(maximum, count=4):
    if maximum <= 0:
        return [0.0, 1.0]
    raw = maximum / count
    magnitude = 10 ** int(len("%d" % int(raw)) - 1) if raw >= 1 else 1
    for multiple in (1, 2, 2.5, 5, 10):
        step = multiple * magnitude
        if step * count >= maximum:
            break
    ticks = []
    value = 0.0
    while value <= step * count + 1e-9:
        ticks.append(value)
        value += step
    return ticks


def esc(text):
    return html.escape(str(text), quote=True)


PLOT_WIDTH = 940


PLOT_HEIGHT = 320


MARGIN = {"left": 84, "right": 28, "top": 18, "bottom": 58}


BAR_CAP = 24


CORNER = 4


GAP = 2


def _plot_box(height=PLOT_HEIGHT):
    return (
        MARGIN["left"],
        MARGIN["top"],
        PLOT_WIDTH - MARGIN["left"] - MARGIN["right"],
        height - MARGIN["top"] - MARGIN["bottom"],
    )


def _bar_path(x, y, width, height, radius):
    radius = max(0.0, min(radius, width / 2, height))
    return "M%.2f %.2f v%.2f a%.2f %.2f 0 0 1 %.2f %.2f h%.2f a%.2f %.2f 0 0 1 %.2f %.2f v%.2f z" % (
        x,
        y + height,
        -(height - radius),
        radius,
        radius,
        radius,
        -radius,
        width - 2 * radius,
        radius,
        radius,
        radius,
        radius,
        height - radius,
    )


def _horizontal_bar(x, y, width, height, radius, flip):
    radius = max(0.0, min(radius, width / 2, height / 2))
    if not flip:
        return "M%.2f %.2f h%.2f a%.2f %.2f 0 0 1 %.2f %.2f v%.2f a%.2f %.2f 0 0 1 %.2f %.2f h%.2f z" % (
            x, y, width - radius, radius, radius, radius, radius,
            height - 2 * radius, radius, radius, -radius, radius, -(width - radius),
        )
    return "M%.2f %.2f h%.2f a%.2f %.2f 0 0 0 %.2f %.2f v%.2f a%.2f %.2f 0 0 0 %.2f %.2f h%.2f z" % (
        x, y, -(width - radius), radius, radius, -radius, radius,
        height - 2 * radius, radius, radius, radius, radius, width - radius,
    )


def _grid_and_ticks(ticks, top_tick, left, top, plot_height):
    parts = []
    for tick in ticks:
        offset = 0.0 if top_tick == 0 else tick / top_tick * plot_height
        y = top + plot_height - offset
        parts.append(
            '<line class="grid" x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f"/>'
            % (left, y, PLOT_WIDTH - MARGIN["right"], y)
        )
        parts.append(
            '<text class="tick" x="%.2f" y="%.2f" text-anchor="end">%s</text>'
            % (left - 10, y + 4, esc(compact(tick)))
        )
    return "".join(parts)


def svg_stacked_columns(categories, series, reference=None, height=PLOT_HEIGHT):
    left, top, width, plot_height = _plot_box(height)
    maximum = max([sum(item["parts"]) for item in categories] + [reference or 0.0])
    ticks = nice_ticks(maximum)
    top_tick = ticks[-1]

    def scaled(value):
        return 0.0 if top_tick == 0 else value / top_tick * plot_height

    band = width / max(1, len(categories))
    bar_width = min(BAR_CAP, band * 0.5)
    parts = ['<svg viewBox="0 0 %d %d" class="chart" role="img">' % (PLOT_WIDTH, height)]
    parts.append(_grid_and_ticks(ticks, top_tick, left, top, plot_height))
    for index, item in enumerate(categories):
        x = left + band * index + (band - bar_width) / 2
        cursor = top + plot_height
        highest = len(item["parts"]) - 1
        for depth, value in enumerate(item["parts"]):
            bar_height = scaled(value)
            if bar_height <= 0:
                continue
            y = cursor - bar_height
            visible = bar_height - (GAP if depth < highest else 0)
            parts.append(
                '<path class="mark" d="%s" fill="var(%s)" tabindex="0" data-tip="%s"/>'
                % (
                    _bar_path(x, y, bar_width, max(visible, 1.0), CORNER if depth == highest else 0),
                    series[depth]["color"],
                    esc(item["tips"][depth]),
                )
            )
            cursor = y
        centre = left + band * index + band / 2
        parts.append(
            '<text class="%s" x="%.2f" y="%.2f" text-anchor="middle">%s</text>'
            % (
                "axis-label strong" if item.get("emphasis") else "axis-label",
                centre,
                top + plot_height + 22,
                esc(item["label"]),
            )
        )
        if item.get("sublabel"):
            parts.append(
                '<text class="axis-sublabel" x="%.2f" y="%.2f" text-anchor="middle">%s</text>'
                % (centre, top + plot_height + 40, esc(item["sublabel"]))
            )
    parts.append(
        '<line class="baseline" x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f"/>'
        % (left, top + plot_height, PLOT_WIDTH - MARGIN["right"], top + plot_height)
    )
    if reference:
        y = top + plot_height - scaled(reference)
        parts.append(
            '<line class="reference" x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f"/>'
            % (left, y, PLOT_WIDTH - MARGIN["right"], y)
        )
        parts.append(
            '<text class="reference-label" x="%.2f" y="%.2f" text-anchor="end">ceiling %s</text>'
            % (PLOT_WIDTH - MARGIN["right"], y - 8, esc(compact(reference)))
        )
    parts.append("</svg>")
    return "".join(parts)


def svg_ranked_bars(rows, color="--series-1", label_width=280, row_height=32):
    height = MARGIN["top"] + row_height * max(1, len(rows)) + 16
    maximum = max([abs(row["value"]) for row in rows] + [1.0])
    track = PLOT_WIDTH - label_width - 140
    parts = ['<svg viewBox="0 0 %d %d" class="chart" role="img">' % (PLOT_WIDTH, height)]
    for index, row in enumerate(rows):
        y = MARGIN["top"] + index * row_height
        bar_height = min(BAR_CAP, row_height - 10)
        length = max(2.0, abs(row["value"]) / maximum * track)
        parts.append(
            '<text class="row-label" x="%d" y="%.2f" text-anchor="end">%s</text>'
            % (label_width - 12, y + bar_height / 2 + 4, esc(row["label"]))
        )
        parts.append(
            '<path class="mark" d="%s" fill="var(%s)" tabindex="0" data-tip="%s"/>'
            % (_horizontal_bar(label_width, y, length, bar_height, CORNER, False), color, esc(row["tip"]))
        )
        parts.append(
            '<text class="value-label" x="%.2f" y="%.2f">%s</text>'
            % (label_width + length + 10, y + bar_height / 2 + 4, esc(compact(row["value"])))
        )
    parts.append("</svg>")
    return "".join(parts)


def svg_diverging_bars(rows, label_width=330, row_height=36):
    height = MARGIN["top"] + row_height * max(1, len(rows)) + 24
    maximum = max([abs(row["delta"]) for row in rows] + [1.0])
    arm = (PLOT_WIDTH - label_width - 150) / 2
    centre = label_width + 30 + arm
    parts = ['<svg viewBox="0 0 %d %d" class="chart" role="img">' % (PLOT_WIDTH, height)]
    parts.append(
        '<line class="baseline" x1="%.2f" y1="%d" x2="%.2f" y2="%.2f"/>'
        % (centre, MARGIN["top"] - 8, centre, MARGIN["top"] + row_height * len(rows) + 4)
    )
    for index, row in enumerate(rows):
        y = MARGIN["top"] + index * row_height
        bar_height = min(BAR_CAP, row_height - 12)
        length = max(2.0, abs(row["delta"]) / maximum * arm)
        increased = row["delta"] > 0
        parts.append(
            '<text class="row-label" x="%d" y="%.2f" text-anchor="end">%s</text>'
            % (label_width, y + bar_height / 2 + 4, esc(row["phrase"]))
        )
        parts.append(
            '<path class="mark" d="%s" fill="var(%s)" tabindex="0" data-tip="%s"/>'
            % (
                _horizontal_bar(centre, y, length, bar_height, CORNER, not increased),
                "--delta-up" if increased else "--delta-down",
                esc(row["tip"]),
            )
        )
        parts.append(
            '<text class="value-label" x="%.2f" y="%.2f" text-anchor="%s">%s</text>'
            % (
                centre + length + 10 if increased else centre - length - 10,
                y + bar_height / 2 + 4,
                "start" if increased else "end",
                esc(signed_compact(row["delta"])),
            )
        )
    parts.append("</svg>")
    return "".join(parts)


def svg_lines(points, series, height=300):
    left, top, width, plot_height = _plot_box(height)
    maximum = max([max(serie["values"]) for serie in series] + [1.0])
    ticks = nice_ticks(maximum)
    top_tick = ticks[-1]
    step = width / max(1, len(points) - 1) if len(points) > 1 else width

    def x_at(index):
        return left + step * index

    def y_at(value):
        return top + plot_height - (value / top_tick * plot_height if top_tick else 0.0)

    parts = ['<svg viewBox="0 0 %d %d" class="chart" role="img">' % (PLOT_WIDTH, height)]
    parts.append(_grid_and_ticks(ticks, top_tick, left, top, plot_height))
    for serie in series:
        coordinates = " ".join("%.2f,%.2f" % (x_at(i), y_at(v)) for i, v in enumerate(serie["values"]))
        parts.append(
            '<polyline class="line" points="%s" stroke="var(%s)"/>' % (coordinates, serie["color"])
        )
        last = len(serie["values"]) - 1
        parts.append(
            '<circle class="end-dot" cx="%.2f" cy="%.2f" r="5" fill="var(%s)"/>'
            % (x_at(last), y_at(serie["values"][last]), serie["color"])
        )
        parts.append(
            '<text class="value-label" x="%.2f" y="%.2f" text-anchor="end">%s</text>'
            % (x_at(last), y_at(serie["values"][last]) - 14, esc(compact(serie["values"][last])))
        )
    for index, point in enumerate(points):
        tip = "%s\n%s" % (
            point,
            "\n".join("%s: %s" % (serie["name"], exact(serie["values"][index])) for serie in series),
        )
        parts.append(
            '<rect class="hit" x="%.2f" y="%.2f" width="%.2f" height="%.2f" tabindex="0" data-tip="%s"/>'
            % (x_at(index) - step / 2, top, step, plot_height, esc(tip))
        )
        parts.append(
            '<text class="axis-label" x="%.2f" y="%.2f" text-anchor="middle">%s</text>'
            % (x_at(index), top + plot_height + 22, esc(point))
        )
    parts.append(
        '<line class="baseline" x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f"/>'
        % (left, top + plot_height, PLOT_WIDTH - MARGIN["right"], top + plot_height)
    )
    parts.append("</svg>")
    return "".join(parts)


def legend(series):
    items = "".join(
        '<span class="legend-item"><span class="swatch" style="background:var(%s)"></span>%s</span>'
        % (serie["color"], esc(serie["name"]))
        for serie in series
    )
    return '<div class="legend">%s</div>' % items


def meter(fraction, label):
    filled = max(0.0, min(1.0, fraction)) * 100
    return (
        '<div class="meter" role="img" aria-label="%s"><div class="meter-track">'
        '<div class="meter-fill" style="width:%.2f%%"></div></div>'
        '<div class="meter-caption">%s</div></div>' % (esc(label), filled, esc(label))
    )


def table(headers, rows):
    head = "".join("<th>%s</th>" % esc(header) for header in headers)
    body = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % esc(cell) for cell in row) for row in rows)
    return "<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (head, body)


def table_view(headers, rows, summary="Table view"):
    return '<details class="table-view"><summary>%s</summary>%s</details>' % (
        esc(summary),
        table(headers, rows),
    )
