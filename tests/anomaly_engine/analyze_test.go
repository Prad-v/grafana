package anomaly_test

// Every series is synthetic with a known injected fault, so a failure here means the
// detector stopped finding something real or started inventing something.

import (
	"encoding/json"
	"fmt"
	"math"
	"strings"
	"testing"

	"github.com/kekaflow/anomaly-engine/anomaly"
)

func TestCleanNoisySeriesHasNoFindings(t *testing.T) {
	for seed := range uint64(40) {
		r := anomaly.Analyze(noisy(seed, nil), anomaly.Options{})
		noFindings(t, r, fmt.Sprintf("seed %d", seed))
	}
}

func TestSpikeIsFoundWithItsTimeAndSize(t *testing.T) {
	r := anomaly.Analyze(noisy(1, func(i int, v float64) float64 {
		if i == 120 {
			return v + 25
		}
		return v
	}), anomaly.Options{})
	spikes := ofKind(r, anomaly.KindSpike)
	if len(spikes) != 1 {
		t.Fatalf("want 1 spike, got %+v", r.Findings)
	}
	if spikes[0].PeakTS != t0+120*step {
		t.Errorf("peak at %v, want %v", spikes[0].PeakTS, t0+120*step)
	}
	if spikes[0].PeakValue <= 80 {
		t.Errorf("peak value %v, want > 80", spikes[0].PeakValue)
	}
	near(t, "baseline", spikes[0].Baseline, 60, 2)
}

func TestDipIsFound(t *testing.T) {
	r := anomaly.Analyze(noisy(1, func(i int, v float64) float64 {
		if i >= 90 && i < 93 {
			return v - 30
		}
		return v
	}), anomaly.Options{})
	dips := ofKind(r, anomaly.KindDip)
	if len(dips) != 1 {
		t.Fatalf("want 1 dip, got %+v", r.Findings)
	}
	if !strings.Contains(dips[0].Detail, "3 sample") {
		t.Errorf("detail %q does not say 3 samples", dips[0].Detail)
	}
}

func TestShortBurstIsOneFindingNotMany(t *testing.T) {
	r := anomaly.Analyze(noisy(1, func(i int, v float64) float64 {
		if i >= 100 && i < 104 {
			return v + 20
		}
		return v
	}), anomaly.Options{})
	if got := len(ofKind(r, anomaly.KindSpike)); got != 1 {
		t.Errorf("want 1 spike, got %d: %+v", got, r.Findings)
	}
}

func TestLevelShiftIsFoundOnce(t *testing.T) {
	r := anomaly.Analyze(noisy(1, func(i int, v float64) float64 {
		if i >= 100 {
			return v + 15
		}
		return v
	}), anomaly.Options{})
	shifts := ofKind(r, anomaly.KindLevelShift)
	if len(shifts) != 1 {
		t.Fatalf("want 1 level shift, got %+v", r.Findings)
	}
	near(t, "shift sample", (shifts[0].PeakTS-t0)/step, 100, 3)
	near(t, "before", shifts[0].Baseline, 60, 1.5)
	near(t, "after", shifts[0].PeakValue, 75, 1.5)
	if !strings.Contains(shifts[0].Detail, "up") {
		t.Errorf("detail %q does not say up", shifts[0].Detail)
	}
}

func TestDownwardLevelShiftSaysDown(t *testing.T) {
	r := anomaly.Analyze(noisy(1, func(i int, v float64) float64 {
		if i >= 100 {
			return v - 15
		}
		return v
	}), anomaly.Options{})
	shifts := ofKind(r, anomaly.KindLevelShift)
	if len(shifts) == 0 || !strings.Contains(shifts[0].Detail, "down") {
		t.Errorf("want a downward shift, got %+v", r.Findings)
	}
}

func TestFlatlineIsFoundInASeriesThatNormallyMoves(t *testing.T) {
	r := anomaly.Analyze(noisy(1, func(i int, v float64) float64 {
		if i >= 130 {
			return 61.5
		}
		return v
	}), anomaly.Options{})
	flat := ofKind(r, anomaly.KindFlatline)
	if len(flat) != 1 || flat[0].PeakValue != 61.5 {
		t.Errorf("want one flatline at 61.5, got %+v", r.Findings)
	}
}

func TestConstantSeriesIsNotAFlatline(t *testing.T) {
	pts := make([]anomaly.Point, 100)
	for i := range pts {
		pts[i] = anomaly.Point{T: t0 + float64(i*step), V: 100}
	}
	noFindings(t, anomaly.Analyze(pts, anomaly.Options{}), "constant series")
}

func withoutRange(pts []anomaly.Point, from, to int) []anomaly.Point {
	var out []anomaly.Point
	for i, p := range pts {
		if i < from || i >= to {
			out = append(out, p)
		}
	}
	return out
}

func TestGapIsFound(t *testing.T) {
	r := anomaly.Analyze(withoutRange(noisy(1, nil), 80, 100), anomaly.Options{})
	gaps := ofKind(r, anomaly.KindGap)
	if len(gaps) != 1 {
		t.Fatalf("want 1 gap, got %+v", r.Findings)
	}
	if gaps[0].Score < 19 {
		t.Errorf("gap score %v, want >= 19", gaps[0].Score)
	}
}

func TestGapsDoNotCountTowardTheSeriesScore(t *testing.T) {
	r := anomaly.Analyze(withoutRange(noisy(1, nil), 80, 100), anomaly.Options{})
	if r.Score() != 0 {
		t.Errorf("score %v, want 0", r.Score())
	}
}

func TestSmoothDailyCurveIsNotALevelShift(t *testing.T) {
	wave := make([]anomaly.Point, 360)
	for i := range wave {
		wave[i] = anomaly.Point{T: t0 + float64(i*step), V: 50 + 30*math.Sin(float64(i)/180*2*math.Pi)}
	}
	r := anomaly.Analyze(wave, anomaly.Options{})
	for _, k := range kinds(r) {
		if k == anomaly.KindLevelShift || k == anomaly.KindSpike {
			t.Errorf("a smooth wave produced %v: %+v", k, r.Findings)
		}
	}
}

func TestTinyWigglesOnASteadyValueAreIgnored(t *testing.T) {
	// 0.1% changes are statistically clean here but not worth a person's attention.
	pts := make([]anomaly.Point, 180)
	for i := range pts {
		v := 1000.0 + float64(i%2)*0.01
		if i == 100 {
			v += 0.5
		}
		pts[i] = anomaly.Point{T: t0 + float64(i*step), V: v}
	}
	r := anomaly.Analyze(pts, anomaly.Options{})
	if len(ofKind(r, anomaly.KindSpike))+len(ofKind(r, anomaly.KindDip)) != 0 {
		t.Errorf("want no spikes or dips, got %+v", r.Findings)
	}
}

func TestSensitivityChangesWhatIsFound(t *testing.T) {
	// A bump of about 5 sigma: clear at a loose threshold, ignored at a strict one. The minimum
	// effect is pinned low so that only the threshold decides.
	data := noisy(1, func(i int, v float64) float64 {
		if i == 100 {
			return 64.5 // absolute, so the draw for this sample cannot cancel the bump
		}
		return v
	})
	if k := kinds(anomaly.Analyze(data, anomaly.Options{Sensitivity: 8, MinEffectRatio: 0.01})); len(k) != 0 {
		t.Errorf("sensitivity 8 found %v", k)
	}
	// At 3 sigma ordinary noise also trips now and then, so look for the injected point itself.
	loose := anomaly.Analyze(data, anomaly.Options{Sensitivity: 3, MinEffectRatio: 0.01})
	found := false
	for _, f := range ofKind(loose, anomaly.KindSpike) {
		found = found || f.PeakTS == t0+100*step
	}
	if !found {
		t.Errorf("sensitivity 3 did not flag the injected spike: %+v", loose.Findings)
	}
}

func TestCurrentAnomalyIsFlaggedWhenTheLastPointIsBad(t *testing.T) {
	bad := anomaly.Analyze(noisy(1, func(i int, v float64) float64 {
		if i == 179 {
			return v + 30
		}
		return v
	}), anomaly.Options{})
	if bad.CurrentScore <= anomaly.DefaultSensitivity {
		t.Errorf("current score %v, want > %v", bad.CurrentScore, anomaly.DefaultSensitivity)
	}
	if good := anomaly.Analyze(noisy(1, nil), anomaly.Options{}); good.CurrentScore >= anomaly.DefaultSensitivity {
		t.Errorf("clean current score %v, want < %v", good.CurrentScore, anomaly.DefaultSensitivity)
	}
}

func TestTooFewPointsReportsInsufficientData(t *testing.T) {
	r := anomaly.Analyze(series(5, 60, 0.8, 1, nil), anomaly.Options{})
	if !r.InsufficientData || len(r.Findings) != 0 {
		t.Errorf("want insufficient data and no findings, got %+v", r)
	}
	if empty := anomaly.Analyze(nil, anomaly.Options{}); !empty.InsufficientData {
		t.Errorf("no points: want insufficient data, got %+v", empty)
	}
}

func TestNaNAndInfinityAreIgnored(t *testing.T) {
	data := noisy(1, nil)
	data[10].V = math.NaN()
	data[11].V = math.Inf(1)
	noFindings(t, anomaly.Analyze(data, anomaly.Options{}), "NaN and Inf")
}

func TestUnsortedInputIsHandled(t *testing.T) {
	data := noisy(1, func(i int, v float64) float64 {
		if i == 120 {
			return v + 25
		}
		return v
	})
	newRNG(3).Shuffle(len(data), func(i, j int) { data[i], data[j] = data[j], data[i] })
	if got := len(ofKind(anomaly.Analyze(data, anomaly.Options{}), anomaly.KindSpike)); got != 1 {
		t.Errorf("want 1 spike, got %d", got)
	}
}

func TestCountsNeedMinScaleOrEveryBlipIsAnAnomaly(t *testing.T) {
	rng := newRNG(4)
	choices := []float64{0, 0, 1, 1, 1, 2}
	counts := make([]anomaly.Point, 180)
	for i := range counts {
		counts[i] = anomaly.Point{T: t0 + float64(i*step), V: choices[rng.IntN(len(choices))]}
	}
	counts[120].V = 3
	r := anomaly.Analyze(counts, anomaly.Options{MinScale: 1, MinEffectRatio: 0.05})
	if got := len(ofKind(r, anomaly.KindSpike)) + len(ofKind(r, anomaly.KindLevelShift)); got != 0 {
		t.Errorf("want no spikes or shifts, got %+v", r.Findings)
	}
}

func TestLogBurstIsStillFoundWithMinScale(t *testing.T) {
	rng := newRNG(4)
	choices := []float64{0, 1, 1, 2}
	counts := make([]anomaly.Point, 180)
	for i := range counts {
		counts[i] = anomaly.Point{T: t0 + float64(i*step), V: choices[rng.IntN(len(choices))]}
	}
	for i := 140; i < 146; i++ {
		counts[i].V = 40
	}
	r := anomaly.Analyze(counts, anomaly.Options{MinScale: 1, MinEffectRatio: 0.05})
	if len(ofKind(r, anomaly.KindSpike)) == 0 {
		t.Errorf("want a spike, got %+v", r.Findings)
	}
}

func TestFindingsSerialiseToTheSharedJSONShape(t *testing.T) {
	r := anomaly.Analyze(noisy(1, func(i int, v float64) float64 {
		if i == 120 {
			return v + 25
		}
		return v
	}), anomaly.Options{})
	raw, err := json.Marshal(r.Findings)
	if err != nil {
		t.Fatal(err)
	}
	var decoded []map[string]any
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"kind", "start", "end", "duration_s", "peak_time", "peak_value", "baseline", "score", "detail"} {
		if _, ok := decoded[0][key]; !ok {
			t.Errorf("JSON is missing %q: %s", key, raw)
		}
	}
	if !strings.Contains(string(raw), "2023-") {
		t.Errorf("want ISO timestamps, got %s", raw)
	}
}

func TestZeroOptionsMeanDefaults(t *testing.T) {
	data := noisy(1, func(i int, v float64) float64 {
		if i == 120 {
			return v + 25
		}
		return v
	})
	zero := anomaly.Analyze(data, anomaly.Options{})
	explicit := anomaly.Analyze(data, anomaly.DefaultOptions())
	if len(zero.Findings) != len(explicit.Findings) || zero.CurrentScore != explicit.CurrentScore {
		t.Errorf("zero options differ from defaults: %+v vs %+v", zero.Findings, explicit.Findings)
	}
}

func TestRobustImplementsDetector(t *testing.T) {
	var d anomaly.Detector = anomaly.Robust{}
	if d.Name() != "robust" {
		t.Errorf("name %q", d.Name())
	}
	if r := d.Detect(noisy(1, nil), anomaly.Options{}); len(r.Findings) != 0 {
		t.Errorf("clean series: %+v", r.Findings)
	}
}

func TestTraceGivesAlignedExpectedValuesForABand(t *testing.T) {
	r := anomaly.Analyze(noisy(1, func(i int, v float64) float64 {
		if i == 120 {
			return v + 25
		}
		return v
	}), anomaly.Options{Sensitivity: 4.5, MinEffectRatio: 0.05, Trace: true})
	if len(r.Times) != 180 || len(r.Expected) != 180 || len(r.Scores) != 180 {
		t.Fatalf("trace lengths %d/%d/%d, want 180", len(r.Times), len(r.Expected), len(r.Scores))
	}
	if r.Scale <= 0 {
		t.Errorf("scale %v, want > 0", r.Scale)
	}
	near(t, "expected at the spike", r.Expected[120], 60, 2)
	if r.Scores[120] < anomaly.DefaultSensitivity {
		t.Errorf("score at the spike %v, want >= %v", r.Scores[120], anomaly.DefaultSensitivity)
	}
	if without := anomaly.Analyze(noisy(1, nil), anomaly.Options{}); without.Times != nil {
		t.Error("trace data returned without Options.Trace")
	}
}

func TestDetectorsAreSafeForConcurrentUse(t *testing.T) {
	data := noisy(1, nil)
	done := make(chan struct{})
	for range 8 {
		go func() {
			defer func() { done <- struct{}{} }()
			anomaly.Analyze(data, anomaly.Options{})
		}()
	}
	for range 8 {
		<-done
	}
}
