package anomaly_test

import (
	"fmt"
	"testing"

	"github.com/kekaflow/anomaly-engine/anomaly"
)

func fleet() []map[string]string {
	var out []map[string]string
	for _, r := range []string{"us", "eu", "ap"} {
		for d := 1; d <= 2; d++ {
			for n := 1; n <= 2; n++ {
				for g := 0; g <= 1; g++ {
					out = append(out, map[string]string{
						"region":     r,
						"datacenter": fmt.Sprintf("%s-dc%d", r, d),
						"node":       fmt.Sprintf("%s-dc%d-n%d", r, d, n),
						"gpu":        fmt.Sprint(g),
					})
				}
			}
		}
	}
	return out
}

func TestLabelConcentrationFindsTheDatacenterAllAnomaliesShare(t *testing.T) {
	all := fleet()
	var bad []map[string]string
	for _, l := range all {
		if l["datacenter"] == "eu-dc1" {
			bad = append(bad, l)
		}
	}
	found := anomaly.LabelConcentration(all, bad, 0.8)
	if len(found) == 0 || found[0].Label != "datacenter" || found[0].Value != "eu-dc1" || found[0].Coverage != 1.0 {
		t.Fatalf("want datacenter=eu-dc1 first with full coverage, got %+v", found)
	}
	// The region is also shared, but less tightly, so it comes after.
	hasRegion := false
	for _, f := range found {
		hasRegion = hasRegion || (f.Label == "region" && f.Value == "eu")
	}
	if !hasRegion {
		t.Errorf("want region=eu among the patterns, got %+v", found)
	}
}

func TestScatteredAnomaliesHaveNoPattern(t *testing.T) {
	all := fleet()
	bad := []map[string]string{all[0], all[9], all[17], all[22]}
	if got := anomaly.LabelConcentration(all, bad, 0.8); len(got) != 0 {
		t.Errorf("want none, got %+v", got)
	}
}

func TestEverythingAnomalousHasNoPattern(t *testing.T) {
	all := fleet()
	if got := anomaly.LabelConcentration(all, all, 0.8); len(got) != 0 {
		t.Errorf("want none, got %+v", got)
	}
}

func TestTooFewAnomalousSeriesToSaySomething(t *testing.T) {
	all := fleet()
	if got := anomaly.LabelConcentration(all, all[:2], 0.8); len(got) != 0 {
		t.Errorf("want none, got %+v", got)
	}
}

func TestALabelThatIsTheSameEverywhereIsIgnored(t *testing.T) {
	var all []map[string]string
	for i := range 10 {
		all = append(all, map[string]string{"job": "dcgm", "node": fmt.Sprintf("n%d", i)})
	}
	if got := anomaly.LabelConcentration(all, all[:5], 0.8); len(got) != 0 {
		t.Errorf("want none, got %+v", got)
	}
}

func finding(ts float64, kind anomaly.Kind) anomaly.Finding {
	return anomaly.Finding{Kind: kind, Start: ts, End: ts, Score: 5, PeakTS: ts, PeakValue: 1}
}

func TestSeriesThatChangeTogetherFormACluster(t *testing.T) {
	var in []anomaly.SeriesFinding
	for i := range 5 {
		in = append(in, anomaly.SeriesFinding{Series: i, Finding: finding(t0+1000+float64(i*30), anomaly.KindSpike)})
	}
	in = append(in, anomaly.SeriesFinding{Series: 9, Finding: finding(t0+50000, anomaly.KindSpike)})
	clusters := anomaly.TimeClusters(in, 600, 3)
	if len(clusters) != 1 || clusters[0].SeriesAffected != 5 {
		t.Errorf("want one cluster of 5 series, got %+v", clusters)
	}
}

func TestTwoSeriesAreNotACluster(t *testing.T) {
	in := []anomaly.SeriesFinding{
		{Series: 0, Finding: finding(t0, anomaly.KindSpike)},
		{Series: 1, Finding: finding(t0+10, anomaly.KindSpike)},
	}
	if got := anomaly.TimeClusters(in, 600, 3); len(got) != 0 {
		t.Errorf("want none, got %+v", got)
	}
}

func TestTheSameSeriesTwiceCountsOnce(t *testing.T) {
	in := []anomaly.SeriesFinding{
		{Series: 0, Finding: finding(t0, anomaly.KindSpike)},
		{Series: 0, Finding: finding(t0+10, anomaly.KindSpike)},
		{Series: 1, Finding: finding(t0+20, anomaly.KindSpike)},
	}
	if got := anomaly.TimeClusters(in, 600, 3); len(got) != 0 {
		t.Errorf("want none, got %+v", got)
	}
}

func TestGapsAndFlatlinesAreNotClustered(t *testing.T) {
	var in []anomaly.SeriesFinding
	for i := range 5 {
		in = append(in, anomaly.SeriesFinding{Series: i, Finding: finding(t0+float64(i), anomaly.KindGap)})
	}
	if got := anomaly.TimeClusters(in, 600, 3); len(got) != 0 {
		t.Errorf("want none, got %+v", got)
	}
}
