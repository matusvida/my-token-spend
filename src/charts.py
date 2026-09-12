import html
from collections import Counter, defaultdict
from datetime import datetime, timezone

import text


def clip(text, limit):
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


MIN_AFFIX = 10

MIN_REMAINDER = 4


def _shared_prefix(labels):
    first = labels[0]
    count = 0
    for index, char in enumerate(first):
        if all(len(label) > index and label[index] == char for label in labels):
            count = index + 1
        else:
            break
    return first[:count].rsplit(" ", 1)[0] + " " if " " in first[:count] else ""


def _shared_suffix(labels):
    first = labels[0]
    count = 0
    for index in range(1, len(first) + 1):
        char = first[-index]
        if all(len(label) >= index and label[-index] == char for label in labels):
            count = index
        else:
            break
    tail = first[len(first) - count :]
    return " " + tail.split(" ", 1)[1] if " " in tail else ""


def shorten_labels(labels, limit):
    if len(set(labels)) < 2:
        return [clip(label, limit) for label in labels]
    prefix = _shared_prefix(labels)
    if len(prefix) < MIN_AFFIX or any(len(label) - len(prefix) < MIN_REMAINDER for label in labels):
        prefix = ""
    trimmed = [label[len(prefix) :] for label in labels]
    suffix = _shared_suffix(trimmed)
    if len(suffix) < MIN_AFFIX or any(len(label) - len(suffix) < MIN_REMAINDER for label in trimmed):
        suffix = ""
    trimmed = [label[: len(label) - len(suffix)] if suffix else label for label in trimmed]
    lead = "..." if prefix else ""
    trail = "..." if suffix else ""
    room = limit - len(lead) - len(trail)
    return [lead + clip(label, room) + trail for label in trimmed]


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


def ticks_within(maximum, limit, counts=(4, 5, 6, 7, 8, 9, 10)):
    best = None
    for count in counts:
        ticks = nice_ticks(maximum, count)
        if ticks[-1] <= limit:
            return ticks
        if best is None or ticks[-1] < best[-1]:
            best = ticks
    return best


def esc(text):
    return html.escape(str(text), quote=True)


PLOT_WIDTH = 940


PLOT_HEIGHT = 320


MARGIN = {"left": 84, "right": 28, "top": 18, "bottom": 58}


BAR_CAP = 24


LABEL_CHARS = 46


CORNER = 4


GAP = 2


INSIDE_LABEL_MIN = 56.0


VALUE_LABEL_ASCENT = 12.0

VALUE_LABEL_LIFT = 14.0

VALUE_LABEL_DROP = 20.0


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


def _column_ticks(categories, reference=None):
    return nice_ticks(max([sum(item["parts"]) for item in categories] + [reference or 0.0]))


def column_heights(categories, index, reference=None, height=PLOT_HEIGHT):
    _, _, _, plot_height = _plot_box(height)
    top_tick = _column_ticks(categories, reference)[-1]
    if not top_tick:
        return [0.0 for _ in categories]
    return [item["parts"][index] / top_tick * plot_height for item in categories]


def svg_stacked_columns(categories, series, reference=None, height=PLOT_HEIGHT):
    left, top, width, plot_height = _plot_box(height)
    ticks = _column_ticks(categories, reference)
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
            % (
                _horizontal_bar(label_width, y, length, bar_height, CORNER, False),
                row.get("color") or color,
                esc(row["tip"]),
            )
        )
        inside = length >= INSIDE_LABEL_MIN
        parts.append(
            '<text class="value-label%s" x="%.2f" y="%.2f">%s</text>'
            % (
                " inside" if inside else "",
                label_width + (10 if inside else length + 10),
                y + bar_height / 2 + 4,
                esc(compact(row["value"])),
            )
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


def table_view(headers, rows, summary="Numbers"):
    return '<details class="table-view"><summary>%s</summary>%s</details>' % (
        esc(summary),
        table(headers, rows),
    )


CATEGORICAL = (
    "--series-1",
    "--series-2",
    "--series-3",
    "--series-4",
    "--series-5",
    "--series-6",
    "--series-7",
    "--series-8",
)
OTHER = "--series-other"


def color_map(names):
    return {name: CATEGORICAL[index] if index < len(CATEGORICAL) else OTHER for index, name in enumerate(names)}


def _seconds(value):
    text = str(value or "")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _clock(value):
    return str(value or "")[11:16]


def _svg(height, body):
    return '<svg viewBox="0 0 %d %d" class="chart" role="img">%s</svg>' % (PLOT_WIDTH, height, body)


CONTEXT_CLASSES = 5

OTHER_CONTEXT = "other or unattributed"


def _context_colors(chart):
    return color_map([entry["tool"] for entry in chart["by_tool"][:CONTEXT_CLASSES]])


def context_legend(chart):
    colors = _context_colors(chart)
    drawn = {point[3] for point in chart["series"] if point[1] > 0}
    entries = [{"name": name, "color": colors[name]} for name in colors if name in drawn]
    if any(name not in colors for name in drawn):
        entries.append({"name": OTHER_CONTEXT, "color": OTHER})
    return entries


def svg_context_series(chart, height=280):
    left, top, width, plot_height = _plot_box(height)
    series = chart["series"]
    if not series:
        return ""
    colors = _context_colors(chart)
    threshold = chart.get("threshold") or 0
    ticks = nice_ticks(max([point[1] for point in series] + [threshold]))
    top_tick = ticks[-1]
    band = width / max(1, len(series))
    bar_width = max(1.0, band - 1.0)
    parts = [_grid_and_ticks(ticks, top_tick, left, top, plot_height)]
    for index, point in enumerate(series):
        value = point[1]
        bar_height = value / top_tick * plot_height if top_tick else 0.0
        if bar_height <= 0:
            continue
        leader = point[3] or "unattributed"
        parts.append(
            '<rect class="mark" x="%.2f" y="%.2f" width="%.2f" height="%.2f" fill="var(%s)" data-tip="%s"/>'
            % (
                left + band * index,
                top + plot_height - bar_height,
                bar_width,
                bar_height,
                colors.get(point[3], OTHER),
                esc(
                    "%s\ncontext held: %s tokens\ngrew by %s, led by %s"
                    % (_clock(point[0]), exact(value), exact(point[2]), leader)
                ),
            )
        )
    if threshold and top_tick:
        y = top + plot_height - threshold / top_tick * plot_height
        parts.append(
            '<line class="reference" x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f"/>'
            % (left, y, PLOT_WIDTH - MARGIN["right"], y)
        )
        parts.append(
            '<text class="reference-label" x="%.2f" y="%.2f" text-anchor="end">threshold %s</text>'
            % (PLOT_WIDTH - MARGIN["right"], y - 8, esc(compact(threshold)))
        )
    stamps = [point[0] for point in series]
    for moment in chart.get("compactions") or []:
        index = 0
        for position, stamp in enumerate(stamps):
            if stamp >= moment:
                index = position
                break
        else:
            index = len(stamps) - 1
        x = left + band * index
        parts.append(
            '<line class="marker" x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" data-tip="%s"/>'
            % (x, top, x, top + plot_height, esc("compaction at %s" % _clock(moment)))
        )
    parts.append(
        '<line class="baseline" x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f"/>'
        % (left, top + plot_height, PLOT_WIDTH - MARGIN["right"], top + plot_height)
    )
    last = len(series) - 1
    for position, anchor in ((0, "start"), (last // 2, "middle"), (last, "end")):
        parts.append(
            '<text class="axis-label" x="%.2f" y="%.2f" text-anchor="%s">%s</text>'
            % (
                left + band * position + bar_width / 2,
                top + plot_height + 22,
                anchor,
                esc("turn %d (%s)" % (position + 1, _clock(series[position][0]))),
            )
        )
    return _svg(height, "".join(parts))


MIN_BAR = 3.0

MIN_MEDIAN_BAR = 12.0

DAY_SECONDS = 86400.0

TICK_MINUTES = (5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 2880)


def _timeline_extent(chart, label_width=300):
    runs = chart["runs"]
    starts = [_seconds(run["first_ts"]) or 0.0 for run in runs]
    ends = [_seconds(run["last_ts"]) or 0.0 for run in runs]
    origin, last = min(starts), max(ends)
    return starts, ends, origin, max(1.0, last - origin), PLOT_WIDTH - label_width - 80


def timeline_mode(chart, label_width=300):
    if not chart["runs"]:
        return "clock"
    starts, ends, _, span, track = _timeline_extent(chart, label_width)
    widths = sorted((end - start) / span * track for start, end in zip(starts, ends))
    middle = len(widths) // 2
    median = widths[middle] if len(widths) % 2 else (widths[middle - 1] + widths[middle]) / 2
    return "clock" if median >= MIN_MEDIAN_BAR else "turns"


def _hour_ticks(origin, span):
    minutes = span / 60.0
    step = next((value for value in TICK_MINUTES if minutes / value <= 8), TICK_MINUTES[-1])
    seconds = step * 60.0
    first = origin - (origin % seconds) + seconds
    ticks = []
    moment = first
    while moment <= origin + span:
        ticks.append(moment)
        moment += seconds
    return ticks


def _day_ticks(origin, span):
    first = origin - (origin % DAY_SECONDS) + DAY_SECONDS
    ticks = []
    moment = first
    while moment <= origin + span:
        ticks.append(moment)
        moment += DAY_SECONDS
    return ticks


def _stamp_text(seconds, same_day):
    moment = datetime.fromtimestamp(seconds, timezone.utc)
    return moment.strftime("%H:%M") if same_day else moment.strftime("%m-%d %H:%M")


def distinct_labels(labels, limit, stamps=None):
    short = shorten_labels(labels, limit)
    groups = defaultdict(list)
    for index, label in enumerate(short):
        groups[label].append(index)
    for indexes in groups.values():
        if len(indexes) < 2:
            continue
        within = shorten_labels([labels[index] for index in indexes], limit)
        if len(set(within)) == len(indexes):
            for index, label in zip(indexes, within):
                short[index] = label
        elif stamps:
            for index in indexes:
                short[index] = "%s %s" % (short[index], _clock(stamps[index]))
    return short


def run_labels(runs):
    return distinct_labels(
        [run["label"] for run in runs], LABEL_CHARS, [run["first_ts"] for run in runs]
    )


def _runs_by_turns(chart, label_width, row_height):
    ordered = sorted(chart["runs"], key=lambda run: (-run["turns"], run["first_ts"]))
    short = run_labels(ordered)
    rows = [
        {
            "label": label,
            "value": run["turns"],
            "color": "--series-1" if run["named"] else OTHER,
            "tip": "%s\n%s to %s\n%d turns, %s weighted\nagent type %s"
            % (
                run["label"],
                _clock(run["first_ts"]),
                _clock(run["last_ts"]),
                run["turns"],
                exact(run["weighted"]),
                run["agent"],
            ),
        }
        for run, label in zip(ordered, short)
    ]
    return svg_ranked_bars(rows, label_width=label_width, row_height=row_height + 6)


def svg_run_timeline(chart, label_width=300, row_height=26):
    runs = chart["runs"]
    if not runs:
        return ""
    if timeline_mode(chart, label_width) == "turns":
        return _runs_by_turns(chart, label_width, row_height)
    height = MARGIN["top"] + row_height * len(runs) + 44
    starts, ends, origin, span, track = _timeline_extent(chart, label_width)
    busiest = max(run["turns"] for run in runs)
    short = run_labels(runs)
    parts = []
    for index, run in enumerate(runs):
        thickness = 6.0 + 12.0 * (run["turns"] / busiest if busiest else 0.0)
        y = MARGIN["top"] + index * row_height + (row_height - thickness) / 2
        x = label_width + (starts[index] - origin) / span * track
        length = max(MIN_BAR, (ends[index] - starts[index]) / span * track)
        parts.append(
            '<text class="row-label" x="%d" y="%.2f" text-anchor="end">%s</text>'
            % (label_width - 12, y + thickness / 2 + 4, esc(short[index]))
        )
        parts.append(
            '<path class="mark" d="%s" fill="var(%s)" tabindex="0" data-tip="%s"/>'
            % (
                _horizontal_bar(x, y, length, thickness, CORNER, False),
                "--series-1" if run["named"] else OTHER,
                esc(
                    "%s\n%s to %s\n%d turns, %s weighted\nagent type %s"
                    % (
                        run["label"],
                        _clock(run["first_ts"]),
                        _clock(run["last_ts"]),
                        run["turns"],
                        exact(run["weighted"]),
                        run["agent"],
                    )
                ),
            )
        )
    baseline = MARGIN["top"] + row_height * len(runs) + 6
    parts.append(
        '<line class="baseline" x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f"/>'
        % (label_width, baseline, label_width + track, baseline)
    )
    same_day = str(min(run["first_ts"] for run in runs))[:10] == str(max(run["last_ts"] for run in runs))[:10]
    for moment in _day_ticks(origin, span):
        x = label_width + (moment - origin) / span * track
        parts.append(
            '<line class="grid day" x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f"/>'
            % (x, MARGIN["top"] - 10, x, baseline)
        )
    for moment in _hour_ticks(origin, span):
        x = label_width + (moment - origin) / span * track
        parts.append(
            '<line class="grid" x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f"/>'
            % (x, MARGIN["top"] - 4, x, baseline)
        )
        parts.append(
            '<text class="axis-label" x="%.2f" y="%.2f" text-anchor="middle">%s</text>'
            % (x, baseline + 18, esc(_stamp_text(moment, same_day)))
        )
    return _svg(height, "".join(parts))


def svg_scatter(chart, height=320):
    left, top, width, plot_height = _plot_box(height)
    points = chart["points"]
    if not points:
        return ""
    colors = color_map(chart["models"])
    box = chart["box"]
    y_ticks = nice_ticks(max(chart.get("thinking_max") or 0, box["thinking"], 1))
    top_tick = y_ticks[-1]
    x_ticks = nice_ticks(max(chart.get("axis_max") or 0, box["output"], 1))
    right_tick = x_ticks[-1]

    def x_at(value):
        return left + (min(value, right_tick) / right_tick * width if right_tick else 0.0)

    def y_at(value):
        return top + plot_height - (min(value, top_tick) / top_tick * plot_height if top_tick else 0.0)

    parts = [_grid_and_ticks(y_ticks, top_tick, left, top, plot_height)]
    parts.append(
        '<rect class="region" x="%.2f" y="%.2f" width="%.2f" height="%.2f" data-tip="%s"/>'
        % (
            left,
            y_at(box["thinking"]),
            max(2.0, x_at(box["output"]) - left),
            max(2.0, top + plot_height - y_at(box["thinking"])),
            esc(
                "counted here: at most %d output tokens, %d thinking tokens and %d tool call"
                % (box["output"], box["thinking"], box["tools"])
            ),
        )
    )
    frequency = Counter(point["model"] for point in points)
    for point in sorted(points, key=lambda item: -frequency[item["model"]]):
        parts.append(
            '<circle class="dot" cx="%.2f" cy="%.2f" r="4" fill="var(%s)" data-tip="%s"/>'
            % (
                x_at(point["output"]),
                y_at(point["thinking"]),
                colors.get(point["model"], OTHER),
                esc(
                    "%s\n%s, %s output tokens, %s thinking"
                    % (
                        point["model"],
                        text.plural(point["tools"], "tool call"),
                        exact(point["output"]),
                        exact(point["thinking"]),
                    )
                ),
            )
        )
    parts.append(
        '<line class="baseline" x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f"/>'
        % (left, top + plot_height, PLOT_WIDTH - MARGIN["right"], top + plot_height)
    )
    for tick in x_ticks:
        parts.append(
            '<text class="axis-label" x="%.2f" y="%.2f" text-anchor="middle">%s</text>'
            % (x_at(tick), top + plot_height + 22, esc(compact(tick)))
        )
    parts.append(
        '<text class="axis-label" x="%.2f" y="%.2f" text-anchor="middle">output tokens, thinking up the side</text>'
        % (left + width / 2, top + plot_height + 44)
    )
    parts.append(
        '<text class="region-label" x="%.2f" y="%.2f" text-anchor="end">counted: %d output, %d thinking, %d tool call</text>'
        % (PLOT_WIDTH - MARGIN["right"], top + 12, box["output"], box["thinking"], box["tools"])
    )
    return _svg(height, "".join(parts))


def svg_burn(labels, cumulative, ceiling, reset_label, ceiling_label="ceiling", height=300):
    left, top, width, plot_height = _plot_box(height)
    reached = max(cumulative + [ceiling or 0.0, 1.0])
    ticks = ticks_within(reached, reached * 1.1)
    top_tick = ticks[-1]
    step = width / max(1, len(labels) - 1) if len(labels) > 1 else width

    def x_at(index):
        return left + step * index

    def y_at(value):
        return top + plot_height - (value / top_tick * plot_height if top_tick else 0.0)

    parts = [_grid_and_ticks(ticks, top_tick, left, top, plot_height)]
    if ceiling:
        y = y_at(ceiling)
        parts.append(
            '<line class="reference" x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f"/>'
            % (left, y, PLOT_WIDTH - MARGIN["right"], y)
        )
        parts.append(
            '<text class="reference-label" x="%.2f" y="%.2f" text-anchor="start">%s %s</text>'
            % (left + 6, y - 8, esc(ceiling_label), esc(compact(ceiling)))
        )
    coordinates = " ".join("%.2f,%.2f" % (x_at(index), y_at(value)) for index, value in enumerate(cumulative))
    parts.append('<polyline class="line" points="%s" stroke="var(--series-1)"/>' % coordinates)
    if cumulative:
        end_y = y_at(cumulative[-1])
        label_y = end_y - VALUE_LABEL_LIFT
        if label_y < VALUE_LABEL_ASCENT:
            label_y = end_y + VALUE_LABEL_DROP
        parts.append(
            '<circle class="end-dot" cx="%.2f" cy="%.2f" r="5" fill="var(--series-1)"/>'
            % (x_at(len(cumulative) - 1), end_y)
        )
        parts.append(
            '<text class="value-label" x="%.2f" y="%.2f" text-anchor="end">%s</text>'
            % (x_at(len(cumulative) - 1), label_y, esc(compact(cumulative[-1])))
        )
    edge = PLOT_WIDTH - MARGIN["right"]
    parts.append('<line class="marker" x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f"/>' % (edge, top, edge, top + plot_height))
    parts.append(
        '<text class="reference-label" x="%.2f" y="%.2f" text-anchor="end">%s</text>'
        % (edge - 6, top + plot_height - 8, esc(reset_label))
    )
    for index, label in enumerate(labels):
        parts.append(
            '<rect class="hit" x="%.2f" y="%.2f" width="%.2f" height="%.2f" tabindex="0" data-tip="%s"/>'
            % (
                x_at(index) - step / 2,
                top,
                step,
                plot_height,
                esc("%s\n%s weighted spent so far" % (label, exact(cumulative[index]))),
            )
        )
        parts.append(
            '<text class="axis-label" x="%.2f" y="%.2f" text-anchor="middle">%s</text>'
            % (x_at(index), top + plot_height + 22, esc(label))
        )
    parts.append(
        '<line class="baseline" x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f"/>'
        % (left, top + plot_height, PLOT_WIDTH - MARGIN["right"], top + plot_height)
    )
    return _svg(height, "".join(parts))
