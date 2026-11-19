package anomaly

import (
	"math"
	"sort"
)

// FindPeriod returns the length in samples of a repeating pattern, or 0 when there is none.
//
// It uses the autocorrelation of the mean-removed series. A cycle shows as a peak at its length
// and at multiples of it. It needs minCycles repeats in the window so that "repeats" is a
// fact, not a hope. A ramp has high correlation at every lag but no peak, so it does not count.
func FindPeriod(values []float64) int {
	return findPeriod(values, 3.0, 0.5)
}

func findPeriod(values []float64, minCycles, threshold float64) int {
	n := len(values)
	if n == 0 {
		return 0
	}
	var mean float64
	for _, v := range values {
		mean += v
	}
	mean /= float64(n)
	x := make([]float64, n)
	var energy float64
	for i, v := range values {
		x[i] = v - mean
		energy += x[i] * x[i]
	}
	if energy < 1e-12 {
		return 0
	}
	maxLag := int(float64(n) / minCycles)
	acf := make([]float64, 1, maxLag+2)
	acf[0] = 1.0
	for lag := 1; lag <= maxLag+1 && lag < n; lag++ {
		var sum float64
		for i := 0; i < n-lag; i++ {
			sum += x[i] * x[i+lag]
		}
		// Rescale for the shrinking overlap, which otherwise hides long cycles.
		acf = append(acf, sum/energy*float64(n)/float64(n-lag))
	}
	for lag := 3; lag <= maxLag && lag+1 < len(acf); lag++ {
		if acf[lag] >= threshold && acf[lag] >= acf[lag-1] && acf[lag] >= acf[lag+1] && acf[lag] > acf[lag-2] {
			// Take the fundamental: the shortest lag that is itself a clear peak.
			return refinePeriod(acf, lag, maxLag)
		}
	}
	return 0
}

// refinePeriod corrects a period that is a sample or two off.
//
// When the edges of a cycle wander, the autocorrelation is nearly flat across the true
// period and its neighbours, so the first peak can sit one sample short. One sample of
// error per cycle adds up over multiples of the period, so the true period is the
// candidate whose multiples keep correlating. Ties keep the original, which preserves the
// shortest-peak rule.
func refinePeriod(acf []float64, lag, maxLag int) int {
	harmonics := func(l int) (sum float64, count int) {
		for m := l; m < len(acf); m += l {
			sum += acf[m]
			count++
		}
		return sum, count
	}
	best, bestScore := lag, math.Inf(-1)
	for _, cand := range [...]int{lag, lag - 1, lag + 1, lag - 2, lag + 2} {
		if cand < 3 || cand > maxLag {
			continue
		}
		sum, count := harmonics(cand)
		if count < 2 {
			return lag
		}
		if score := sum / float64(count); score > bestScore+1e-9 {
			best, bestScore = cand, score
		}
	}
	return best
}

func eventGroup(f Finding) string {
	switch f.Kind {
	case KindSpike, KindDip:
		return string(f.Kind)
	case KindLevelShift:
		if f.PeakValue > f.Baseline {
			return "step_up"
		}
		return "step_down"
	}
	return ""
}

// splitPeriodic separates events that recur at a steady interval from one-off events.
//
// Three or more events of the same kind at near-equal spacing are a cycle: a batch job, a
// checkpoint, a cron task. Calling each one an anomaly would bury the real ones.
func splitPeriodic(findings []Finding) (events []Finding, periodic []Periodic) {
	groups := map[string][]int{}
	var order []string
	for i, f := range findings {
		key := eventGroup(f)
		if key == "" {
			continue
		}
		if _, seen := groups[key]; !seen {
			order = append(order, key)
		}
		groups[key] = append(groups[key], i)
	}
	cyclic := map[int]bool{}
	for _, key := range order {
		members := groups[key]
		if len(members) < 3 {
			continue
		}
		times := make([]float64, len(members))
		for i, m := range members {
			times[i] = findings[m].PeakTS
		}
		sort.Float64s(times)
		gaps := make([]float64, len(times)-1)
		for i := range gaps {
			gaps[i] = times[i+1] - times[i]
		}
		typical := median(gaps)
		if typical <= 0 {
			continue
		}
		regular := 0
		for _, g := range gaps {
			if math.Abs(g-typical) <= 0.25*typical {
				regular++
			}
		}
		if float64(regular) >= math.Max(2, 0.7*float64(len(gaps))) {
			for _, m := range members {
				cyclic[m] = true
			}
			periodic = append(periodic, Periodic{Kind: key, PeriodS: int64(typical), Occurrences: float64(len(members))})
		}
	}
	for i, f := range findings {
		if !cyclic[i] {
			events = append(events, f)
		}
	}
	return events, periodic
}
