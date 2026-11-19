package anomaly_test

// A steady rhythm is normal. It must not drown out a real problem inside it.

import (
	"fmt"
	"math"
	"testing"

	"github.com/kekaflow/anomaly-engine/anomaly"
)

func plainSquare(seed uint64, fn modifier) []anomaly.Point { return square(240, 24, 16, seed, fn) }

func TestThePeriodOfASquareWaveIsFound(t *testing.T) {
	if got := anomaly.FindPeriod(values(plainSquare(1, nil))); got != 24 {
		t.Errorf("period %d, want 24", got)
	}
}

func TestThePeriodSurvivesEdgeJitter(t *testing.T) {
	// With wandering edges the autocorrelation is nearly flat across 23, 24 and 25. Picking the
	// first peak lands one sample short about one time in ten, and that error compounds when a
	// point is compared with cycles two periods back.
	for seed := range uint64(100) {
		if got := anomaly.FindPeriod(values(plainSquare(seed, nil))); got != 24 {
			t.Errorf("seed %d: period %d, want 24", seed, got)
		}
	}
}

func TestACyclingSeriesHasNoAnomaliesAndReportsItsPeriod(t *testing.T) {
	for seed := range uint64(100) {
		r := anomaly.Analyze(plainSquare(seed, nil), anomaly.Options{})
		noFindings(t, r, fmt.Sprintf("cycling seed %d", seed))
		if len(r.Periodic) == 0 || r.Periodic[0].Kind != "cycle" {
			t.Errorf("seed %d: want a cycle, got %+v", seed, r.Periodic)
			continue
		}
		// The autocorrelation peak can land one sample off when the edges wander.
		near(t, "period_s", float64(r.Periodic[0].PeriodS), 24*step, step)
	}
}

func TestEdgeJitterIsNotAnAnomaly(t *testing.T) {
	noFindings(t, anomaly.Analyze(plainSquare(7, nil), anomaly.Options{}), "jittering edges")
}

func TestAFaultInsideACyclingSeriesIsStillFound(t *testing.T) {
	// The job is idle (40) at samples 188..190, but something heats it to 70.
	r := anomaly.Analyze(plainSquare(1, func(i int, v float64) float64 {
		if i >= 20+24*7 && i < 23+24*7 {
			return 70 + (v - 40)
		}
		return v
	}), anomaly.Options{})
	if len(ofKind(r, anomaly.KindSpike)) == 0 {
		t.Errorf("want a spike, got %+v", r.Findings)
	}
	if len(r.Periodic) == 0 {
		t.Error("it should still know the series cycles")
	}
}

func TestAShiftInsideACyclingSeriesIsFoundOnce(t *testing.T) {
	// A step is only visible on the average over a whole cycle, so it is smeared across one
	// cycle: it is placed within a quarter cycle of the truth, and now and then (about 1 in 100
	// at this size) it does not clear the threshold. The test states both limits.
	const runs = 100
	found := 0
	for seed := range uint64(runs) {
		r := anomaly.Analyze(square(360, 24, 16, seed, func(i int, v float64) float64 {
			if i >= 200 {
				return v + 14
			}
			return v
		}), anomaly.Options{})
		shifts := ofKind(r, anomaly.KindLevelShift)
		if len(shifts) > 1 {
			t.Errorf("seed %d: one step reported %d times: %+v", seed, len(shifts), shifts)
		}
		if len(shifts) == 1 {
			found++
			near(t, fmt.Sprintf("seed %d shift sample", seed), (shifts[0].PeakTS-t0)/step, 200, 6)
		}
	}
	if found < 95 {
		t.Errorf("found the step in %d of %d series, want at least 95", found, runs)
	}
}

func TestNoiseAloneHasNoPeriod(t *testing.T) {
	for seed := range uint64(30) {
		if got := anomaly.FindPeriod(values(noisy(seed, nil))); got != 0 {
			t.Errorf("seed %d: period %d, want none", seed, got)
		}
	}
}

func TestARampHasNoPeriod(t *testing.T) {
	ramp := make([]float64, 200)
	wobbly := make([]float64, 200)
	for i := range ramp {
		ramp[i] = float64(i)
		wobbly[i] = 50 + 0.3*float64(i) + math.Sin(float64(i))*0.1
	}
	if got := anomaly.FindPeriod(ramp); got != 0 {
		t.Errorf("ramp: period %d", got)
	}
	if got := anomaly.FindPeriod(wobbly); got != 0 {
		t.Errorf("wobbly ramp: period %d", got)
	}
}

func TestFewerThanThreeCyclesIsNotAPeriod(t *testing.T) {
	if got := anomaly.FindPeriod(values(square(60, 24, 16, 1, nil))); got != 0 {
		t.Errorf("period %d, want none", got)
	}
}

func TestAConstantSeriesHasNoPeriod(t *testing.T) {
	flat := make([]float64, 100)
	for i := range flat {
		flat[i] = 5
	}
	if got := anomaly.FindPeriod(flat); got != 0 {
		t.Errorf("period %d, want none", got)
	}
	if got := anomaly.FindPeriod(nil); got != 0 {
		t.Errorf("empty: period %d, want none", got)
	}
}

func TestRegularSpikesAreARhythmNotThreeAnomalies(t *testing.T) {
	// A cron job: a spike every 20 minutes. Sparse spikes have weak autocorrelation, so this
	// is caught by the regular-spacing check instead.
	r := anomaly.Analyze(series(240, 60, 0.8, 1, func(i int, v float64) float64 {
		if i%20 == 5 {
			return v + 20
		}
		return v
	}), anomaly.Options{})
	if got := ofKind(r, anomaly.KindSpike); len(got) != 0 {
		t.Errorf("want no spikes, got %+v", got)
	}
	if len(r.Periodic) == 0 {
		t.Error("want the rhythm reported")
	}
}

func TestIrregularSpikesAreAllReported(t *testing.T) {
	hits := map[int]bool{30: true, 71: true, 190: true}
	r := anomaly.Analyze(series(240, 60, 0.8, 1, func(i int, v float64) float64 {
		if hits[i] {
			return v + 20
		}
		return v
	}), anomaly.Options{})
	if got := len(ofKind(r, anomaly.KindSpike)); got != 3 {
		t.Errorf("want 3 spikes, got %d: %+v", got, r.Findings)
	}
	if len(r.Periodic) != 0 {
		t.Errorf("want no rhythm, got %+v", r.Periodic)
	}
}
