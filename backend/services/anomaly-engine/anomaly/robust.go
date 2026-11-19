package anomaly

import (
	"fmt"
	"math"
	"sort"
)

// Robust is the default [Detector]: rolling-median baselines, MAD-scaled scores, a cycle check
// that stops a steady rhythm from being reported, and separate finders for steps, flatlines and gaps.
type Robust struct{}

// Name implements [Detector].
func (Robust) Name() string { return "robust" }

// Detect implements [Detector].
func (Robust) Detect(points []Point, opts Options) Report { return Analyze(points, opts) }

// Analyze finds anomalies in one series. A zero [Options] means the defaults.
func Analyze(points []Point, opts Options) Report {
	opts = opts.withDefaults()
	data := Clean(points)
	n := len(data)
	if n < MinPoints {
		r := Report{Points: n, InsufficientData: true}
		if n > 0 {
			vs := make([]float64, n)
			for i, p := range data {
				vs[i] = p.V
			}
			r.First, r.Last, r.Median = vs[0], vs[n-1], median(vs)
		}
		return r
	}

	ts := make([]float64, n)
	vs := make([]float64, n)
	for i, p := range data {
		ts[i], vs[i] = p.T, p.V
	}
	steps := make([]float64, n-1)
	for i := range steps {
		steps[i] = ts[i+1] - ts[i]
	}
	step := median(steps)
	if step == 0 {
		step = 1.0
	}
	floor := math.Max(scaleFloor(vs), opts.MinScale)
	ordered := append([]float64(nil), vs...)
	sort.Float64s(ordered)
	spread := quantile(ordered, 0.95) - quantile(ordered, 0.05)
	typical := median(vs)
	// A change must matter relative to the value and to how much the series normally swings.
	minEffect := math.Max(math.Abs(typical)*opts.MinEffectRatio, minEffectOfSpread*spread)
	report := Report{Points: n, First: vs[0], Last: vs[n-1], Median: typical}

	// Window of about a tenth of the series: odd, 7 to 31 samples. Bounded so cost stays flat.
	window := minInt(31, maxInt(7, (n/10)|1))
	base := baseline(vs, window)
	lag := FindPeriod(vs)
	var residuals []float64
	if lag > 0 {
		// A steady rhythm is normal. Compare each point with the same phase of the other cycles, so
		// the rhythm drops out and only departures from it remain.
		base, residuals = seasonal(vs, lag, base)
		report.Periodic = append(report.Periodic, Periodic{
			Kind:        "cycle",
			PeriodS:     int64(float64(lag) * step),
			Occurrences: math.Round(float64(n)/float64(lag)*10) / 10,
		})
	} else {
		residuals = make([]float64, n)
		for i := range vs {
			residuals[i] = vs[i] - base[i]
		}
	}
	scale := math.Max(madToSigma*mad(residuals, 0), floor)
	scores := make([]float64, n)
	for i, r := range residuals {
		scores[i] = r / scale
	}

	var shifts []Finding
	if lag > 0 {
		// Persistent moves are found on the average over exactly one cycle, which has no rhythm left,
		// so a cycle is not "many shifts". Each average is placed at the middle of its cycle.
		prefix, _ := prefixSums(vs)
		level := make([]float64, 0, n-lag+1)
		levelTS := make([]float64, 0, n-lag+1)
		for i := lag - 1; i < n; i++ {
			level = append(level, (prefix[i+1]-prefix[i+1-lag])/float64(lag))
			levelTS = append(levelTS, ts[i-lag/2])
		}
		shifts = levelShifts(levelTS, level, lag, opts.Sensitivity, math.Max(floor, 1e-9), minEffect)
	} else {
		shifts = levelShifts(ts, vs, window, opts.Sensitivity, floor, minEffect)
	}
	runs := findRuns(ts, vs, base, scores, opts.Sensitivity, step, minEffect)
	// A median baseline straddling a step reads the points beside it as dips and spikes. They are
	// the same event as the shift, and would outrank it, so they are folded into it.
	reach := float64(window/2+1) * step
	kept := runs[:0]
	for _, r := range runs {
		near := false
		for _, sh := range shifts {
			if math.Abs(r.PeakTS-sh.PeakTS) <= reach {
				near = true
				break
			}
		}
		if !near {
			kept = append(kept, r)
		}
	}
	events, regular := splitPeriodic(append(append([]Finding(nil), kept...), shifts...))
	report.Periodic = append(report.Periodic, regular...)

	// Up to three steps are described per series; more than that is a rhythm, not a single event.
	var stepsFound []Finding
	for _, f := range events {
		if f.Kind == KindLevelShift {
			stepsFound = append(stepsFound, f)
		} else {
			report.Findings = append(report.Findings, f)
		}
	}
	sort.SliceStable(stepsFound, func(i, j int) bool { return stepsFound[i].Score > stepsFound[j].Score })
	if len(stepsFound) > 3 {
		stepsFound = stepsFound[:3]
	}
	report.Findings = append(report.Findings, stepsFound...)
	report.Findings = append(report.Findings, flatlines(ts, vs, floor)...)
	report.Findings = append(report.Findings, gaps(ts, vs, step)...)
	sort.SliceStable(report.Findings, func(i, j int) bool { return report.Findings[i].Score > report.Findings[j].Score })
	report.CurrentScore = scores[n-1]

	if opts.Trace {
		report.Scale = scale
		report.Times, report.Expected, report.Scores = ts, base, scores
	}
	return report
}

// findRuns groups consecutive over-threshold points into one spike or dip each.
func findRuns(ts, vs, base, scores []float64, sensitivity, step, minEffect float64) []Finding {
	var out []Finding
	n := len(vs)
	flagged := func(k int) bool {
		return math.Abs(scores[k]) >= sensitivity && math.Abs(vs[k]-base[k]) >= minEffect
	}
	for i := 0; i < n; {
		if !flagged(i) {
			i++
			continue
		}
		up := scores[i] > 0
		j := i
		for j+1 < n && flagged(j+1) && (scores[j+1] > 0) == up {
			j++
		}
		peak := i
		for k := i; k <= j; k++ {
			if math.Abs(scores[k]) > math.Abs(scores[peak]) {
				peak = k
			}
		}
		end := ts[i]
		if j > i {
			end = ts[j] + step
		}
		kind, side := KindDip, "below"
		if up {
			kind, side = KindSpike, "above"
		}
		out = append(out, Finding{
			Kind:      kind,
			Start:     ts[i],
			End:       end,
			Score:     math.Abs(scores[peak]),
			PeakTS:    ts[peak],
			PeakValue: vs[peak],
			Baseline:  base[peak],
			Detail:    fmt.Sprintf("%d sample(s) %s the rolling median", j-i+1, side),
		})
		i = j + 1
	}
	return out
}

type shiftCandidate struct {
	z      float64
	i      int
	ml, mr float64
}

// levelShifts finds steps: the median before and after a point differ by more than either side's own noise.
func levelShifts(ts, vs []float64, window int, sensitivity, floor, minEffect float64) []Finding {
	n := len(vs)
	w := maxInt(window, 6)
	if n < 2*w {
		return nil
	}
	var candidates []shiftCandidate
	for i := w; i < n-w+1; i++ {
		left, right := vs[i-w:i], vs[i:i+w]
		ml, mr := median(left), median(right)
		noise := math.Max(madToSigma*math.Max(mad(left, ml), mad(right, mr)), floor)
		z := math.Abs(mr-ml) / noise
		if z >= sensitivity && math.Abs(mr-ml) >= minEffect {
			candidates = append(candidates, shiftCandidate{z, i, ml, mr})
		}
	}
	// Strongest first; skip neighbours so one step is reported once.
	sort.Slice(candidates, func(a, b int) bool {
		x, y := candidates[a], candidates[b]
		if x.z != y.z {
			return x.z > y.z
		}
		if x.i != y.i {
			return x.i > y.i
		}
		if x.ml != y.ml {
			return x.ml > y.ml
		}
		return x.mr > y.mr
	})
	prefix, prefixSq := prefixSums(vs)
	var out []Finding
	var taken []int
	for _, c := range candidates {
		near := false
		for _, t := range taken {
			if abs(c.i-t) < w {
				near = true
				break
			}
		}
		if near {
			continue
		}
		i := locateStep(prefix, prefixSq, c.i, w, n)
		taken = append(taken, i)
		dir := "down"
		if c.mr > c.ml {
			dir = "up"
		}
		out = append(out, Finding{
			Kind:      KindLevelShift,
			Start:     ts[i],
			End:       ts[len(ts)-1],
			Score:     c.z,
			PeakTS:    ts[i],
			PeakValue: c.mr,
			Baseline:  c.ml,
			Detail:    fmt.Sprintf("typical value moved %s from about %s to %s and stayed there", dir, num(c.ml), num(c.mr)),
		})
		if len(out) == 12 { // enough to recognise a cycle; the caller reports the strongest few
			break
		}
	}
	return out
}

// locateStep answers where exactly the level changes. Many positions near a step look alike to a
// median comparison, so it picks the split that makes both sides most constant.
func locateStep(prefix, prefixSq []float64, guess, w, n int) int {
	sse := func(a, b int) float64 {
		count := b - a
		if count <= 0 {
			return 0
		}
		total := prefix[b] - prefix[a]
		return (prefixSq[b] - prefixSq[a]) - total*total/float64(count)
	}
	lo, hi := maxInt(w/2, guess-w/2), minInt(n-w/2, guess+w/2)
	best, bestCost := guess, math.Inf(1)
	for j := maxInt(lo, 1); j < maxInt(hi, lo+1); j++ {
		cost := sse(maxInt(0, j-w), j) + sse(j, minInt(n, j+w))
		if cost < bestCost {
			best, bestCost = j, cost
		}
	}
	return best
}

// flatlines: a series that normally varies but is suddenly constant often means a stuck exporter.
func flatlines(ts, vs []float64, floor float64) []Finding {
	n := len(vs)
	overall := mad(vs, median(vs)) * madToSigma
	if overall < floor*2 {
		return nil // constant the whole time: that is its normal, not an anomaly
	}
	minRun := maxInt(6, n/8)
	var out []Finding
	for i := 0; i < n; {
		j := i
		for j+1 < n && math.Abs(vs[j+1]-vs[i]) <= 1e-12 {
			j++
		}
		if j-i+1 >= minRun {
			out = append(out, Finding{
				Kind:      KindFlatline,
				Start:     ts[i],
				End:       ts[j],
				Score:     float64(j-i+1) / float64(minRun) * 5.0,
				PeakTS:    ts[j],
				PeakValue: vs[i],
				Baseline:  median(vs),
				Detail:    fmt.Sprintf("value stuck at %s for %d samples", num(vs[i]), j-i+1),
			})
		}
		i = j + 1
	}
	return out
}

func gaps(ts, vs []float64, step float64) []Finding {
	var out []Finding
	for k := 1; k < len(ts); k++ {
		missing := (ts[k] - ts[k-1]) / step
		if missing >= 4 {
			out = append(out, Finding{
				Kind:      KindGap,
				Start:     ts[k-1],
				End:       ts[k],
				Score:     missing,
				PeakTS:    ts[k-1],
				PeakValue: vs[k-1],
				Baseline:  vs[k-1],
				Detail:    fmt.Sprintf("no samples for about %d steps", int(missing)),
			})
		}
	}
	return out
}

func abs(x int) int {
	if x < 0 {
		return -x
	}
	return x
}
