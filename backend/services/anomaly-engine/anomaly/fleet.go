package anomaly

import (
	"math"
	"sort"
)

// LabelConcentration reports which label values the anomalous series have in common.
//
// It reports label=value when at least minShare of the anomalous series carry it but the
// value is not simply everywhere. That is what turns "12 GPUs are hot" into "12 GPUs are
// hot, all in datacenter dub1". Pass 0.8 for minShare unless you have a reason not to.
func LabelConcentration(all, anomalous []map[string]string, minShare float64) []LabelPattern {
	total, bad := len(all), len(anomalous)
	if bad < 3 || total <= bad {
		return nil
	}
	keySet := map[string]struct{}{}
	for _, labels := range all {
		for k := range labels {
			keySet[k] = struct{}{}
		}
	}
	keys := make([]string, 0, len(keySet))
	for k := range keySet {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	var out []LabelPattern
	for _, key := range keys {
		countsAll := map[string]int{}
		countsBad := map[string]int{}
		var badOrder []string
		for _, labels := range all {
			if v, ok := labels[key]; ok {
				countsAll[v]++
			}
		}
		for _, labels := range anomalous {
			if v, ok := labels[key]; ok {
				if _, seen := countsBad[v]; !seen {
					badOrder = append(badOrder, v)
				}
				countsBad[v]++
			}
		}
		if len(countsAll) < 2 {
			continue // the same everywhere says nothing
		}
		for _, value := range badOrder {
			hit := countsBad[value]
			share := float64(hit) / float64(bad)
			coverage := float64(hit) / float64(countsAll[value])
			// Lift: how much likelier an anomalous series is to have this value than any series.
			// Without it a value that most of the fleet has (a driver version) looks like a cause.
			lift := share / (float64(countsAll[value]) / float64(total))
			if share >= minShare && countsAll[value] < total && lift >= 1.5 && coverage >= 0.4 {
				out = append(out, LabelPattern{
					Label:            key,
					Value:            value,
					AnomalousSeries:  hit,
					SeriesWithValue:  countsAll[value],
					ShareOfAnomalies: math.Round(share*100) / 100,
					Coverage:         math.Round(coverage*100) / 100,
					Lift:             math.Round(lift*10) / 10,
				})
			}
		}
	}
	// Labels that explain the anomalies completely and tightly come first.
	sort.SliceStable(out, func(i, j int) bool {
		a, b := out[i], out[j]
		if a.Coverage != b.Coverage {
			return a.Coverage > b.Coverage
		}
		if a.ShareOfAnomalies != b.ShareOfAnomalies {
			return a.ShareOfAnomalies > b.ShareOfAnomalies
		}
		return a.SeriesWithValue < b.SeriesWithValue
	})
	if len(out) > 5 {
		out = out[:5]
	}
	return out
}

// LabelPattern is a label value shared by most of the anomalous series.
type LabelPattern struct {
	Label            string  `json:"label"`
	Value            string  `json:"value"`
	AnomalousSeries  int     `json:"anomalous_series"`
	SeriesWithValue  int     `json:"series_with_value"`
	ShareOfAnomalies float64 `json:"share_of_anomalies"`
	Coverage         float64 `json:"coverage"`
	Lift             float64 `json:"lift"`
}

// SeriesFinding ties a finding to the index of the series it came from.
type SeriesFinding struct {
	Series  int
	Finding Finding
}

// TimeCluster is a moment when several series changed together.
type TimeCluster struct {
	Around         string `json:"around"`
	SeriesAffected int    `json:"series_affected"`
	SpanS          int64  `json:"span_s"`
}

// TimeClusters finds anomalies that start together across several series, such as an outage
// hitting many nodes. It returns clusters seen in at least minSeries different series
// (3 is a sensible default), with the time they centre on.
func TimeClusters(findings []SeriesFinding, windowS float64, minSeries int) []TimeCluster {
	type event struct {
		ts     float64
		series int
	}
	var events []event
	for _, sf := range findings {
		switch sf.Finding.Kind {
		case KindSpike, KindDip, KindLevelShift:
			events = append(events, event{sf.Finding.PeakTS, sf.Series})
		}
	}
	sort.Slice(events, func(i, j int) bool {
		if events[i].ts != events[j].ts {
			return events[i].ts < events[j].ts
		}
		return events[i].series < events[j].series
	})
	var clusters [][]event
	for _, ev := range events {
		if last := len(clusters) - 1; last >= 0 && ev.ts-clusters[last][0].ts <= windowS {
			clusters[last] = append(clusters[last], ev)
		} else {
			clusters = append(clusters, []event{ev})
		}
	}
	var out []TimeCluster
	for _, c := range clusters {
		seen := map[int]struct{}{}
		times := make([]float64, len(c))
		for i, ev := range c {
			seen[ev.series] = struct{}{}
			times[i] = ev.ts
		}
		if len(seen) >= minSeries {
			out = append(out, TimeCluster{Around: ISO(median(times)), SeriesAffected: len(seen), SpanS: int64(c[len(c)-1].ts - c[0].ts)})
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].SeriesAffected > out[j].SeriesAffected })
	if len(out) > 3 {
		out = out[:3]
	}
	return out
}
