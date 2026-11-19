package anomaly

import (
	"math"
	"sort"
)

// Clean drops NaN and infinite samples and sorts by time.
func Clean(points []Point) []Point {
	out := make([]Point, 0, len(points))
	for _, p := range points {
		if math.IsNaN(p.V) || math.IsInf(p.V, 0) || math.IsNaN(p.T) || math.IsInf(p.T, 0) {
			continue
		}
		out = append(out, p)
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].T < out[j].T })
	return out
}

func median(values []float64) float64 {
	n := len(values)
	if n == 0 {
		return 0
	}
	s := make([]float64, n)
	copy(s, values)
	sort.Float64s(s)
	if n%2 == 1 {
		return s[n/2]
	}
	return (s[n/2-1] + s[n/2]) / 2
}

// mad is the median absolute deviation from center.
func mad(values []float64, center float64) float64 {
	if len(values) == 0 {
		return 0
	}
	dev := make([]float64, len(values))
	for i, v := range values {
		dev[i] = math.Abs(v - center)
	}
	return median(dev)
}

func quantile(sorted []float64, q float64) float64 {
	if len(sorted) == 0 {
		return 0
	}
	pos := float64(len(sorted)-1) * q
	lo, hi := math.Floor(pos), math.Ceil(pos)
	return sorted[int(lo)] + (sorted[int(hi)]-sorted[int(lo)])*(pos-lo)
}

// scaleFloor is the smallest spread worth reacting to.
//
// A flat series has a MAD of zero, so any change would score infinity. Floor the
// scale at a small share of the series' overall range, or of its magnitude when
// the range is also zero.
func scaleFloor(values []float64) float64 {
	ordered := append([]float64(nil), values...)
	sort.Float64s(ordered)
	spread := quantile(ordered, 0.95) - quantile(ordered, 0.05)
	magnitude := math.Abs(median(values))
	return math.Max(math.Max(spread*0.02, magnitude*0.001), 1e-9)
}

func prefixSums(values []float64) (prefix, prefixSq []float64) {
	prefix = make([]float64, 1, len(values)+1)
	prefixSq = make([]float64, 1, len(values)+1)
	var total, totalSq float64
	for _, v := range values {
		total += v
		totalSq += v * v
		prefix = append(prefix, total)
		prefixSq = append(prefixSq, totalSq)
	}
	return prefix, prefixSq
}

// slice clamps like a Python slice: out-of-range bounds shrink the result instead of panicking.
func slice(values []float64, from, to int) []float64 {
	if from < 0 {
		from = 0
	}
	if to > len(values) {
		to = len(values)
	}
	if from >= to {
		return nil
	}
	return values[from:to]
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}
