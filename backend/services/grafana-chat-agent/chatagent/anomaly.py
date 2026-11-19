"""Anomaly detection for metric and log-volume series.

Pure standard library, so it is easy to test and has no native dependencies.

The detectors are robust statistics (median and MAD), not a trained model. They
need no history beyond the window you pass in, and they say why a point was
flagged. Four kinds of finding come out of one series:

- ``spike`` / ``dip``: a point (or a short run) far from the rolling median.
- ``level_shift``: the typical value moved and stayed there.
- ``flatline``: the value stopped changing in a series that normally moves.
- ``gap``: samples are missing for much longer than the sampling step.

Across many series, ``label_concentration`` finds the label values that the
anomalous series share, which is usually the root cause (one region, one rack).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Sequence

# Below this many samples a statistical statement is noise.
MIN_POINTS = 12
# Scales the MAD to match the standard deviation of a normal distribution.
MAD_TO_SIGMA = 1.4826
# A spike or step smaller than this share of the series' normal swing is not worth a person's attention,
# however clean it is statistically.
MIN_EFFECT_OF_SPREAD = 0.25
SPARK_CHARS = "▁▂▃▄▅▆▇█"

Point = tuple[float, float]


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _round(x: float, digits: int = 4) -> float:
    """Keep significant digits, so small values do not round to zero."""
    return float(f"{x:.{digits}g}") if math.isfinite(x) else x


def clean(points: Iterable[Point]) -> list[Point]:
    """Drop NaN and infinite samples and sort by time."""
    out = [(float(t), float(v)) for t, v in points if math.isfinite(float(v))]
    out.sort(key=lambda p: p[0])
    return out


def sparkline(values: Sequence[float], width: int = 24) -> str:
    """A tiny text chart of the shape of a series."""
    if not values:
        return ""
    if len(values) > width:
        # Average into `width` buckets so the shape survives downsampling.
        size = len(values) / width
        values = [statistics.fmean(values[int(i * size) : max(int((i + 1) * size), int(i * size) + 1)]) for i in range(width)]
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return SPARK_CHARS[0] * len(values)
    top = len(SPARK_CHARS) - 1
    return "".join(SPARK_CHARS[round((v - lo) / (hi - lo) * top)] for v in values)


def _mad(values: Sequence[float], center: float | None = None) -> float:
    if not values:
        return 0.0
    c = statistics.median(values) if center is None else center
    return statistics.median(abs(v - c) for v in values)


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    pos = (len(sorted_values) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _baseline(values: Sequence[float], window: int) -> list[float]:
    """The expected value at every point, from its neighbours only.

    Interior points use the median of the points on both sides, leaving the point itself
    out: a median that includes the point follows it, which shrinks the residuals and makes
    ordinary noise look like outliers. A symmetric window also has no bias on a steady slope.

    Near the edges one side is missing. A plain one-sided median lags a trend and flags the
    first and last points of any ramp, so those use the median of the nearest window plus a
    straight-line extrapolation of its slope.
    """
    n = len(values)
    half = window // 2
    out: list[float] = []
    for i in range(n):
        if half <= i < n - half:
            out.append(statistics.median([*values[i - half : i], *values[i + 1 : i + half + 1]]))
        elif i < half:
            out.append(_edge_baseline(values[i + 1 : i + 1 + window], i + 1, mirrored=True))
        else:
            out.append(_edge_baseline(values[max(0, i - window) : i], i - max(0, i - window), mirrored=False))
    return out


def _edge_baseline(win: Sequence[float], position: int, mirrored: bool) -> float:
    """Median of a one-sided window, extrapolated along its slope to the point being scored."""
    w = len(win)
    third = max(w // 3, 1)
    if w < 3:
        return statistics.median(win)
    if mirrored:
        near, far = list(win[:third]), list(win[-third:])  # near is closest to the point
    else:
        near, far = list(win[-third:]), list(win[:third])
    m_near, m_far = statistics.median(near), statistics.median(far)
    slope_per_sample = (m_near - m_far) / max(w - third, 1)  # change per sample moving toward the point
    # The point sits about `third / 2 + 0.5` samples beyond the middle of the near group.
    return m_near + slope_per_sample * (third / 2 + 0.5)


def _scale_floor(values: Sequence[float]) -> float:
    """The smallest spread worth reacting to.

    A flat series has a MAD of zero, so any change would score infinity. Floor the
    scale at a small share of the series' overall range, or of its magnitude when
    the range is also zero.
    """
    ordered = sorted(values)
    spread = _quantile(ordered, 0.95) - _quantile(ordered, 0.05)
    magnitude = abs(statistics.median(values))
    return max(spread * 0.02, magnitude * 0.001, 1e-9)


@dataclass
class Finding:
    kind: str
    start: float
    end: float
    score: float
    peak_ts: float
    peak_value: float
    baseline: float
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "start": iso(self.start),
            "end": iso(self.end),
            "duration_s": int(self.end - self.start),
            "peak_time": iso(self.peak_ts),
            "peak_value": _round(self.peak_value),
            "baseline": _round(self.baseline),
            "score": _round(self.score, 3),
            "detail": self.detail,
        }


@dataclass
class SeriesReport:
    points: int
    first: float
    last: float
    median: float
    findings: list[Finding] = field(default_factory=list)
    current_score: float = 0.0
    insufficient_data: bool = False
    # Regular repetition (a batch job switching on and off). Normal behaviour, reported but not flagged.
    periodic: list[dict] = field(default_factory=list)

    @property
    def score(self) -> float:
        return max((f.score for f in self.findings if f.kind != "gap"), default=0.0)


def analyze(points: Iterable[Point], sensitivity: float = 4.5, min_scale: float = 0.0, min_effect_ratio: float = 0.05) -> SeriesReport:
    """Find anomalies in one series.

    ``sensitivity`` is a score threshold in robust standard deviations: lower finds more. At 4.5 a
    noise-only series of a few hundred samples almost never trips it, and real faults score far higher.
    ``min_scale`` is the smallest
    noise level to assume, which matters for counts (a series of 0s and 1s has no spread, but
    a jump from 1 to 3 is not unusual). ``min_effect_ratio`` ignores changes smaller than that
    share of the typical value, and changes under a quarter of the series' normal swing.
    """
    data = clean(points)
    n = len(data)
    if n < MIN_POINTS:
        return SeriesReport(
            points=n,
            first=data[0][0] if data else 0,
            last=data[-1][1] if data else 0,
            median=statistics.median(v for _, v in data) if data else 0,
            insufficient_data=True,
        )

    ts = [t for t, _ in data]
    vs = [v for _, v in data]
    step = statistics.median(b - a for a, b in zip(ts, ts[1:])) or 1.0
    floor = max(_scale_floor(vs), min_scale)
    ordered = sorted(vs)
    spread = _quantile(ordered, 0.95) - _quantile(ordered, 0.05)
    # A change must matter relative to the value and to how much the series normally swings.
    min_effect = max(abs(statistics.median(vs)) * min_effect_ratio, MIN_EFFECT_OF_SPREAD * spread)
    report = SeriesReport(points=n, first=vs[0], last=vs[-1], median=statistics.median(vs))

    # Window of about a tenth of the series: odd, 7 to 31 samples. Bounded so cost stays flat.
    window = min(31, max(7, (n // 10) | 1))
    baseline = _baseline(vs, window)
    lag = find_period(vs)
    if lag:
        # A steady rhythm is normal. Compare each point with the same phase of the other cycles, so
        # the rhythm drops out and only departures from it remain.
        baseline, residuals = _seasonal(vs, lag, baseline)
        report.periodic.append({"kind": "cycle", "period_s": int(lag * step), "occurrences": round(n / lag, 1)})
    else:
        residuals = [v - b for v, b in zip(vs, baseline)]
    scale = max(MAD_TO_SIGMA * _mad(residuals, 0.0), floor)
    scores = [r / scale for r in residuals]

    if lag:
        # Persistent moves are found on the average over exactly one cycle, which has no rhythm left,
        # so a cycle is not "many shifts". Each average is placed at the middle of its cycle.
        prefix, _ = _prefix_sums(vs)
        level = [(prefix[i + 1] - prefix[i + 1 - lag]) / lag for i in range(lag - 1, n)]
        level_ts = [ts[i - lag // 2] for i in range(lag - 1, n)]
        shifts = _level_shifts(level_ts, level, lag, sensitivity, max(floor, 1e-9), min_effect)
    else:
        shifts = _level_shifts(ts, vs, window, sensitivity, floor, min_effect)
    runs = _runs(ts, vs, baseline, scores, sensitivity, step, min_effect)
    # A median baseline straddling a step reads the points beside it as dips and spikes. They are
    # the same event as the shift, and would outrank it, so they are folded into it.
    reach = (window // 2 + 1) * step
    runs = [r for r in runs if not any(abs(r.peak_ts - sh.peak_ts) <= reach for sh in shifts)]
    events, regular = _split_periodic(runs + shifts)
    report.periodic.extend(regular)
    # Up to three steps are described per series; more than that is a rhythm, not a single event.
    steps = sorted((f for f in events if f.kind == "level_shift"), key=lambda f: f.score, reverse=True)[:3]
    report.findings.extend([f for f in events if f.kind != "level_shift"] + steps)
    report.findings.extend(_flatlines(ts, vs, step, floor))
    report.findings.extend(_gaps(ts, vs, step))
    report.findings.sort(key=lambda f: f.score, reverse=True)
    report.current_score = scores[-1]
    return report


def _runs(ts, vs, baseline, scores, sensitivity: float, step: float, min_effect: float) -> list[Finding]:
    """Group consecutive over-threshold points into one spike or dip each."""
    out: list[Finding] = []
    i, n = 0, len(vs)

    def flagged(k: int) -> bool:
        return abs(scores[k]) >= sensitivity and abs(vs[k] - baseline[k]) >= min_effect

    while i < n:
        if not flagged(i):
            i += 1
            continue
        sign = 1 if scores[i] > 0 else -1
        j = i
        while j + 1 < n and flagged(j + 1) and (scores[j + 1] > 0) == (sign > 0):
            j += 1
        peak = max(range(i, j + 1), key=lambda k: abs(scores[k]))
        out.append(
            Finding(
                kind="spike" if sign > 0 else "dip",
                start=ts[i],
                end=ts[j] + step if j > i else ts[i],
                score=abs(scores[peak]),
                peak_ts=ts[peak],
                peak_value=vs[peak],
                baseline=baseline[peak],
                detail=f"{j - i + 1} sample(s) {'above' if sign > 0 else 'below'} the rolling median",
            )
        )
        i = j + 1
    return out


def _level_shifts(ts, vs, window: int, sensitivity: float, floor: float, min_effect: float) -> list[Finding]:
    """Find steps: the median before and after a point differ by more than either side's own noise."""
    n = len(vs)
    w = max(window, 6)
    if n < 2 * w:
        return []
    candidates: list[tuple[float, int, float, float]] = []
    for i in range(w, n - w + 1):
        left, right = vs[i - w : i], vs[i : i + w]
        ml, mr = statistics.median(left), statistics.median(right)
        noise = max(MAD_TO_SIGMA * max(_mad(left, ml), _mad(right, mr)), floor)
        z = abs(mr - ml) / noise
        if z >= sensitivity and abs(mr - ml) >= min_effect:
            candidates.append((z, i, ml, mr))
    # Strongest first; skip neighbours so one step is reported once.
    prefix, prefix_sq = _prefix_sums(vs)
    out: list[Finding] = []
    taken: list[int] = []
    for z, i, ml, mr in sorted(candidates, reverse=True):
        if any(abs(i - t) < w for t in taken):
            continue
        i = _locate_step(prefix, prefix_sq, i, w, n)
        taken.append(i)
        up = mr > ml
        out.append(
            Finding(
                kind="level_shift",
                start=ts[i],
                end=ts[-1],
                score=z,
                peak_ts=ts[i],
                peak_value=mr,
                baseline=ml,
                detail=f"typical value moved {'up' if up else 'down'} from about {_round(ml)} to {_round(mr)} and stayed there",
            )
        )
        if len(out) == 12:  # enough to recognise a cycle; the caller reports the strongest few
            break
    return out


def find_period(values: Sequence[float], min_cycles: float = 3.0, threshold: float = 0.5) -> int | None:
    """The length in samples of a repeating pattern, or None.

    Uses the autocorrelation of the mean-removed series. A cycle shows as a peak at its length
    and at multiples of it. Needs ``min_cycles`` repeats in the window so that "repeats" is a
    fact, not a hope. A ramp has high correlation at every lag but no peak, so it does not count.
    """
    n = len(values)
    mean = statistics.fmean(values)
    x = [v - mean for v in values]
    energy = sum(v * v for v in x)
    if energy < 1e-12:
        return None
    max_lag = int(n / min_cycles)
    acf = [1.0]
    for lag in range(1, max_lag + 2):
        # Rescale for the shrinking overlap, which otherwise hides long cycles.
        acf.append(sum(x[i] * x[i + lag] for i in range(n - lag)) / energy * n / (n - lag))
    for lag in range(3, max_lag + 1):
        if acf[lag] >= threshold and acf[lag] >= acf[lag - 1] and acf[lag] >= acf[lag + 1] and acf[lag] > acf[lag - 2]:
            # Take the fundamental: the shortest lag that is itself a clear peak.
            return lag
    return None


def _seasonal(values: Sequence[float], lag: int, fallback: Sequence[float]) -> tuple[list[float], list[float]]:
    """Expected value and deviation of every point, judged against the same phase of other cycles.

    The expected value is the median of that phase in up to four neighbouring cycles. The deviation
    is measured from the envelope of the same phase a couple of samples either side, not from the median:
    the edges of a cycle (a job switching on) land a sample earlier or later each time, and that
    jitter is not an anomaly. Where fewer than three reference points exist, the plain rolling
    baseline is used.
    """
    n = len(values)
    # Two edges that each wander a sample in opposite directions differ by two, so allow that much,
    # and more on a long cycle.
    slack = max(2, lag // 12)
    expected: list[float] = []
    deviation: list[float] = []
    for i in range(n):
        refs = [values[j] for k in (-2, -1, 1, 2) if 0 <= (j := i + k * lag) < n]
        near = [values[j] for k in (-2, -1, 1, 2) for d in range(-slack, slack + 1) if 0 <= (j := i + k * lag + d) < n]
        if len(refs) < 2 or len(near) < 3:
            expected.append(fallback[i])
            deviation.append(values[i] - fallback[i])
            continue
        expected.append(statistics.median(refs))
        lo, hi = min(near), max(near)
        deviation.append(values[i] - hi if values[i] > hi else values[i] - lo if values[i] < lo else 0.0)
    return expected, deviation


def _event_group(f: Finding) -> str | None:
    if f.kind in {"spike", "dip"}:
        return f.kind
    if f.kind == "level_shift":
        return "step_up" if f.peak_value > f.baseline else "step_down"
    return None


def _split_periodic(findings: list[Finding]) -> tuple[list[Finding], list[dict]]:
    """Separate events that recur at a steady interval from one-off events.

    Three or more events of the same kind at near-equal spacing are a cycle: a batch job, a
    checkpoint, a cron task. Calling each one an anomaly would bury the real ones.
    """
    groups: dict[str, list[Finding]] = {}
    for f in findings:
        key = _event_group(f)
        if key:
            groups.setdefault(key, []).append(f)
    cyclic: set[int] = set()
    periodic: list[dict] = []
    for key, members in groups.items():
        if len(members) < 3:
            continue
        times = sorted(f.peak_ts for f in members)
        gaps = [b - a for a, b in zip(times, times[1:])]
        typical = statistics.median(gaps)
        if typical <= 0:
            continue
        regular = sum(1 for g in gaps if abs(g - typical) <= 0.25 * typical)
        if regular >= max(2, 0.7 * len(gaps)):
            cyclic.update(id(f) for f in members)
            periodic.append({"kind": key, "period_s": int(typical), "occurrences": len(members)})
    return [f for f in findings if id(f) not in cyclic], periodic


def _prefix_sums(values: Sequence[float]) -> tuple[list[float], list[float]]:
    total = total_sq = 0.0
    prefix, prefix_sq = [0.0], [0.0]
    for v in values:
        total += v
        total_sq += v * v
        prefix.append(total)
        prefix_sq.append(total_sq)
    return prefix, prefix_sq


def _locate_step(prefix: list[float], prefix_sq: list[float], guess: int, w: int, n: int) -> int:
    """Where exactly does the level change? Many positions near a step look alike to a median
    comparison, so pick the split that makes both sides most constant."""

    def sse(a: int, b: int) -> float:
        count = b - a
        if count <= 0:
            return 0.0
        total = prefix[b] - prefix[a]
        return (prefix_sq[b] - prefix_sq[a]) - total * total / count

    lo, hi = max(w // 2, guess - w // 2), min(n - w // 2, guess + w // 2)
    best, best_cost = guess, math.inf
    for j in range(max(lo, 1), max(hi, lo + 1)):
        cost = sse(max(0, j - w), j) + sse(j, min(n, j + w))
        if cost < best_cost:
            best, best_cost = j, cost
    return best


def _flatlines(ts, vs, step: float, floor: float) -> list[Finding]:
    """A series that normally varies but is suddenly constant often means a stuck exporter."""
    n = len(vs)
    overall = _mad(vs) * MAD_TO_SIGMA
    if overall < floor * 2:
        return []  # constant the whole time: that is its normal, not an anomaly
    min_run = max(6, n // 8)
    out: list[Finding] = []
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(vs[j + 1] - vs[i]) <= 1e-12:
            j += 1
        if j - i + 1 >= min_run:
            out.append(
                Finding(
                    kind="flatline",
                    start=ts[i],
                    end=ts[j],
                    score=float(j - i + 1) / min_run * 5.0,
                    peak_ts=ts[j],
                    peak_value=vs[i],
                    baseline=statistics.median(vs),
                    detail=f"value stuck at {_round(vs[i])} for {j - i + 1} samples",
                )
            )
        i = j + 1
    return out


def _gaps(ts, vs, step: float) -> list[Finding]:
    out: list[Finding] = []
    for k in range(1, len(ts)):
        missing = (ts[k] - ts[k - 1]) / step
        if missing >= 4:
            out.append(
                Finding(
                    kind="gap",
                    start=ts[k - 1],
                    end=ts[k],
                    score=missing,
                    peak_ts=ts[k - 1],
                    peak_value=vs[k - 1],
                    baseline=vs[k - 1],
                    detail=f"no samples for about {int(missing)} steps",
                )
            )
    return out


def label_concentration(
    all_labels: Sequence[dict[str, str]],
    anomalous_labels: Sequence[dict[str, str]],
    min_share: float = 0.8,
) -> list[dict]:
    """Which label values do the anomalous series have in common?

    Reports ``label=value`` when at least ``min_share`` of the anomalous series carry
    it but the value is not simply everywhere. That is what turns "12 GPUs are hot"
    into "12 GPUs are hot, all in datacenter dub1".
    """
    total, bad = len(all_labels), len(anomalous_labels)
    if bad < 3 or total <= bad:
        return []
    out: list[dict] = []
    keys = {k for labels in all_labels for k in labels}
    for key in sorted(keys):
        counts_all: dict[str, int] = {}
        counts_bad: dict[str, int] = {}
        for labels in all_labels:
            if key in labels:
                counts_all[labels[key]] = counts_all.get(labels[key], 0) + 1
        for labels in anomalous_labels:
            if key in labels:
                counts_bad[labels[key]] = counts_bad.get(labels[key], 0) + 1
        if len(counts_all) < 2:
            continue  # the same everywhere says nothing
        for value, hit in counts_bad.items():
            share = hit / bad
            coverage = hit / counts_all[value]
            # Lift: how much likelier an anomalous series is to have this value than any series.
            # Without it a value that most of the fleet has (a driver version) looks like a cause.
            lift = share / (counts_all[value] / total)
            if share >= min_share and counts_all[value] < total and lift >= 1.5 and coverage >= 0.4:
                out.append(
                    {
                        "label": key,
                        "value": value,
                        "anomalous_series": hit,
                        "series_with_value": counts_all[value],
                        "share_of_anomalies": round(share, 2),
                        "coverage": round(coverage, 2),
                        "lift": round(lift, 1),
                    }
                )
    # Labels that explain the anomalies completely and tightly come first.
    out.sort(key=lambda d: (d["coverage"], d["share_of_anomalies"], -d["series_with_value"]), reverse=True)
    return out[:5]


def time_clusters(findings: Sequence[tuple[int, Finding]], window_s: float, min_series: int = 3) -> list[dict]:
    """Anomalies that start together across several series, such as an outage hitting many nodes.

    ``findings`` is ``(series_index, finding)``. Returns clusters seen in at least
    ``min_series`` different series, with the time they centre on.
    """
    events = sorted(((f.peak_ts, idx) for idx, f in findings if f.kind in {"spike", "dip", "level_shift"}))
    clusters: list[list[tuple[float, int]]] = []
    for ev in events:
        if clusters and ev[0] - clusters[-1][0][0] <= window_s:
            clusters[-1].append(ev)
        else:
            clusters.append([ev])
    out = []
    for cluster in clusters:
        series = {idx for _, idx in cluster}
        if len(series) >= min_series:
            out.append(
                {
                    "around": iso(statistics.median(t for t, _ in cluster)),
                    "series_affected": len(series),
                    "span_s": int(cluster[-1][0] - cluster[0][0]),
                }
            )
    out.sort(key=lambda c: c["series_affected"], reverse=True)
    return out[:3]
