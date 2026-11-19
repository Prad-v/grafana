package anomaly_test

import (
	"math/rand/v2"
	"testing"

	"github.com/kekaflow/anomaly-engine/anomaly"
)

const (
	step = 60
	t0   = 1_700_000_000
)

// modifier changes one sample of a synthetic series.
type modifier func(i int, v float64) float64

func newRNG(seed uint64) *rand.Rand { return rand.New(rand.NewPCG(seed, seed^0x9e3779b97f4a7c15)) }

// series makes n samples one minute apart around base with gaussian noise. fn can modify a sample.
func series(n int, base, noise float64, seed uint64, fn modifier) []anomaly.Point {
	rng := newRNG(seed)
	out := make([]anomaly.Point, n)
	for i := range n {
		v := base + rng.NormFloat64()*noise
		if fn != nil {
			v = fn(i, v)
		}
		out[i] = anomaly.Point{T: t0 + float64(i*step), V: v}
	}
	return out
}

func noisy(seed uint64, fn modifier) []anomaly.Point { return series(180, 60, 0.8, seed, fn) }

// square is a batch job: `on` samples hot, the rest idle, repeating. Edges wander by a sample, as in real data.
func square(n, period, on int, seed uint64, fn modifier) []anomaly.Point {
	const hi, lo, noise = 64.0, 40.0, 0.4
	rng := newRNG(seed)
	out := make([]anomaly.Point, n)
	shift := 0
	for i := range n {
		if i%period == 0 {
			shift = []int{-1, 0, 0, 1}[rng.IntN(4)]
		}
		phase := ((i+shift)%period + period) % period
		v := lo
		if phase < on {
			v = hi
		}
		v += rng.NormFloat64() * noise
		if fn != nil {
			v = fn(i, v)
		}
		out[i] = anomaly.Point{T: t0 + float64(i*step), V: v}
	}
	return out
}

func values(points []anomaly.Point) []float64 {
	out := make([]float64, len(points))
	for i, p := range points {
		out[i] = p.V
	}
	return out
}

func ofKind(r anomaly.Report, k anomaly.Kind) []anomaly.Finding {
	var out []anomaly.Finding
	for _, f := range r.Findings {
		if f.Kind == k {
			out = append(out, f)
		}
	}
	return out
}

func kinds(r anomaly.Report) []anomaly.Kind {
	out := make([]anomaly.Kind, len(r.Findings))
	for i, f := range r.Findings {
		out[i] = f.Kind
	}
	return out
}

func near(t *testing.T, name string, got, want, delta float64) {
	t.Helper()
	if got < want-delta || got > want+delta {
		t.Errorf("%s = %v, want %v ± %v", name, got, want, delta)
	}
}

func noFindings(t *testing.T, r anomaly.Report, context string) {
	t.Helper()
	if len(r.Findings) != 0 {
		t.Errorf("%s: want no findings, got %+v", context, r.Findings)
	}
}
