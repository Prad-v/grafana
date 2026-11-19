package anomaly

// baseline is the expected value at every point, from its neighbours only.
//
// Interior points use the median of the points on both sides, leaving the point itself
// out: a median that includes the point follows it, which shrinks the residuals and makes
// ordinary noise look like outliers. A symmetric window also has no bias on a steady slope.
//
// Near the edges one side is missing. A plain one-sided median lags a trend and flags the
// first and last points of any ramp, so those use the median of the nearest window plus a
// straight-line extrapolation of its slope.
func baseline(values []float64, window int) []float64 {
	n := len(values)
	half := window / 2
	out := make([]float64, n)
	buf := make([]float64, 0, window)
	for i := range n {
		switch {
		case i >= half && i < n-half:
			buf = append(buf[:0], values[i-half:i]...)
			buf = append(buf, values[i+1:i+half+1]...)
			out[i] = median(buf)
		case i < half:
			out[i] = edgeBaseline(slice(values, i+1, i+1+window), true)
		default:
			out[i] = edgeBaseline(slice(values, maxInt(0, i-window), i), false)
		}
	}
	return out
}

// edgeBaseline is the median of a one-sided window, extrapolated along its slope to the point being scored.
func edgeBaseline(win []float64, mirrored bool) float64 {
	w := len(win)
	if w == 0 {
		return 0
	}
	if w < 3 {
		return median(win)
	}
	third := maxInt(w/3, 1)
	var near, far []float64
	if mirrored {
		near, far = win[:third], win[w-third:] // near is closest to the point
	} else {
		near, far = win[w-third:], win[:third]
	}
	mNear, mFar := median(near), median(far)
	slopePerSample := (mNear - mFar) / float64(maxInt(w-third, 1)) // change per sample moving toward the point
	// The point sits about `third / 2 + 0.5` samples beyond the middle of the near group.
	return mNear + slopePerSample*(float64(third)/2+0.5)
}

// seasonal gives the expected value and deviation of every point, judged against the same phase of other cycles.
//
// The expected value is the median of that phase in up to four neighbouring cycles. The deviation
// is measured from the envelope of the same phase a couple of samples either side, not from the median:
// the edges of a cycle (a job switching on) land a sample earlier or later each time, and that
// jitter is not an anomaly. Where fewer than three reference points exist, the plain rolling
// baseline is used.
func seasonal(values []float64, lag int, fallback []float64) (expected, deviation []float64) {
	n := len(values)
	// Two edges that each wander a sample in opposite directions differ by two, so allow that much,
	// and more on a long cycle.
	slack := maxInt(2, lag/12)
	expected = make([]float64, n)
	deviation = make([]float64, n)
	refs := make([]float64, 0, 4)
	near := make([]float64, 0, 4*(2*slack+1))
	for i := range n {
		refs, near = refs[:0], near[:0]
		for _, k := range [...]int{-2, -1, 1, 2} {
			if j := i + k*lag; j >= 0 && j < n {
				refs = append(refs, values[j])
			}
			for d := -slack; d <= slack; d++ {
				if j := i + k*lag + d; j >= 0 && j < n {
					near = append(near, values[j])
				}
			}
		}
		if len(refs) < 2 || len(near) < 3 {
			expected[i] = fallback[i]
			deviation[i] = values[i] - fallback[i]
			continue
		}
		expected[i] = median(refs)
		lo, hi := near[0], near[0]
		for _, v := range near {
			lo, hi = min(lo, v), max(hi, v)
		}
		switch {
		case values[i] > hi:
			deviation[i] = values[i] - hi
		case values[i] < lo:
			deviation[i] = values[i] - lo
		}
	}
	return expected, deviation
}
