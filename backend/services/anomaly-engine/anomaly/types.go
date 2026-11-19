// Package anomaly finds anomalies in time series.
//
// It has no dependency on Grafana or on any metrics backend: it takes samples in
// and returns findings out, so the same detectors serve a data source plugin, an
// alert rule and a command line tool. Only the standard library is used.
//
// The default detector uses robust statistics (median and MAD), not a trained
// model. It needs no history beyond the window it is given, and every finding
// says why it was raised. Four kinds of finding come out of one series:
//
//   - spike / dip: a point, or a short run, far from the rolling median.
//   - level_shift: the typical value moved and stayed there.
//   - flatline: the value stopped changing in a series that normally moves.
//   - gap: samples are missing for much longer than the sampling step.
//
// Across many series, [LabelConcentration] finds the label values the anomalous
// series share, which is usually the root cause (one region, one rack).
package anomaly

import (
	"encoding/json"
	"math"
	"strconv"
	"time"
)

// Kind names a type of finding.
type Kind string

const (
	KindSpike      Kind = "spike"
	KindDip        Kind = "dip"
	KindLevelShift Kind = "level_shift"
	KindFlatline   Kind = "flatline"
	KindGap        Kind = "gap"
)

const (
	// MinPoints is the fewest samples a statistical statement needs; below it the result is noise.
	MinPoints = 12
	// DefaultSensitivity is the score threshold in robust standard deviations. At 4.5 a
	// noise-only series of a few hundred samples almost never trips it, and real faults score far higher.
	DefaultSensitivity = 4.5
	// DefaultMinEffectRatio ignores changes smaller than this share of the typical value.
	DefaultMinEffectRatio = 0.05

	// madToSigma scales the MAD to match the standard deviation of a normal distribution.
	madToSigma = 1.4826
	// minEffectOfSpread: a spike or step smaller than this share of the series' normal swing is not
	// worth a person's attention, however clean it is statistically.
	minEffectOfSpread = 0.25
)

// Point is one sample: unix seconds and a value.
type Point struct {
	T float64
	V float64
}

// Options tune a detector. The zero value means "use the defaults".
type Options struct {
	// Sensitivity is the score threshold in robust standard deviations: lower finds more.
	Sensitivity float64
	// MinScale is the smallest noise level to assume. It matters for counts: a series of 0s and 1s
	// has no spread, but a jump from 1 to 3 is not unusual.
	MinScale float64
	// MinEffectRatio ignores changes smaller than this share of the typical value, and changes under
	// a quarter of the series' normal swing.
	MinEffectRatio float64
	// Trace asks for the per-sample expected value and score, which a chart needs to draw a band.
	Trace bool
}

// DefaultOptions returns the options used when none are given.
func DefaultOptions() Options {
	return Options{Sensitivity: DefaultSensitivity, MinEffectRatio: DefaultMinEffectRatio}
}

func (o Options) withDefaults() Options {
	if o == (Options{}) {
		return DefaultOptions()
	}
	if o.Sensitivity <= 0 {
		o.Sensitivity = DefaultSensitivity
	}
	if o.MinEffectRatio < 0 {
		o.MinEffectRatio = DefaultMinEffectRatio
	}
	if o.MinScale < 0 {
		o.MinScale = 0
	}
	return o
}

// Finding is one anomaly in a series.
type Finding struct {
	Kind      Kind
	Start     float64
	End       float64
	Score     float64
	PeakTS    float64
	PeakValue float64
	Baseline  float64
	Detail    string
}

// MarshalJSON writes the shape the HTTP API and the chat agent share: ISO timestamps, rounded numbers.
func (f Finding) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Kind      Kind    `json:"kind"`
		Start     string  `json:"start"`
		End       string  `json:"end"`
		DurationS int64   `json:"duration_s"`
		PeakTime  string  `json:"peak_time"`
		PeakValue float64 `json:"peak_value"`
		Baseline  float64 `json:"baseline"`
		Score     float64 `json:"score"`
		Detail    string  `json:"detail"`
	}{f.Kind, ISO(f.Start), ISO(f.End), int64(f.End - f.Start), ISO(f.PeakTS), Round(f.PeakValue, 4), Round(f.Baseline, 4), Round(f.Score, 3), f.Detail})
}

// Periodic describes regular repetition (a batch job switching on and off). It is normal
// behaviour: reported, but not flagged.
type Periodic struct {
	Kind        string  `json:"kind"`
	PeriodS     int64   `json:"period_s"`
	Occurrences float64 `json:"occurrences"`
}

// Report is the result for one series.
type Report struct {
	Points           int
	First            float64
	Last             float64
	Median           float64
	Findings         []Finding
	CurrentScore     float64
	InsufficientData bool
	Periodic         []Periodic

	// Scale is the noise level (robust standard deviation) the scores are measured in. The band
	// around the expected value is Sensitivity*Scale wide. Set when Options.Trace is true.
	Scale float64
	// Times, Expected and Scores are aligned with each other, one entry per usable sample.
	// Set when Options.Trace is true.
	Times    []float64
	Expected []float64
	Scores   []float64
}

// Score is the strongest finding that is not a gap: gaps are about missing data, not about the value.
func (r Report) Score() float64 {
	best := 0.0
	for _, f := range r.Findings {
		if f.Kind != KindGap && f.Score > best {
			best = f.Score
		}
	}
	return best
}

// Detector finds anomalies in one series. Implementations must be safe for concurrent use.
type Detector interface {
	Name() string
	Detect(points []Point, opts Options) Report
}

// ISO formats unix seconds as a UTC RFC 3339 timestamp.
func ISO(ts float64) string {
	sec := math.Floor(ts)
	return time.Unix(int64(sec), 0).UTC().Format("2006-01-02T15:04:05Z")
}

// Round keeps significant digits, so small values do not round to zero.
func Round(x float64, digits int) float64 {
	if math.IsNaN(x) || math.IsInf(x, 0) {
		return x
	}
	r, err := strconv.ParseFloat(strconv.FormatFloat(x, 'g', digits, 64), 64)
	if err != nil {
		return x
	}
	return r
}

func num(x float64) string {
	return strconv.FormatFloat(Round(x, 4), 'g', -1, 64)
}
