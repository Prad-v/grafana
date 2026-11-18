package resource

import (
	"context"
	"sync/atomic"
	"time"

	"github.com/grafana/grafana-app-sdk/logging"
	"github.com/prometheus/client_golang/prometheus"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
)

const (
	clockSkewCheckTimeout = 5 * time.Second

	errClockNotSynchronized = "writes are temporarily unavailable because the server clock is not synchronized; contact your administrator"
)

// ClockSkewCheckConfig configures the check that rejects writes while this
// process's clock is too far from the database clock.
//
// Write leases compare expiry times written by one instance against the clock
// of another, and resource versions come from the clock of the instance that
// handles the write. Both assume the instances sharing a database agree on the
// time within KVBackendOptions.MaxClockSkew.
type ClockSkewCheckConfig struct {
	// Enabled is set by the wiring, which knows whether instances share the database.
	Enabled bool
	// Interval between checks. Zero disables the check.
	Interval time.Duration
	// WarnThreshold logs a warning while the clock is at least this far from
	// the database clock. Zero disables the warning.
	WarnThreshold time.Duration
}

func readKVClock(ctx context.Context, store KV) (time.Time, error) {
	millis, err := store.UnixTimestamp(ctx)
	if err != nil {
		return time.Time{}, err
	}
	return time.UnixMilli(millis), nil
}

// clockOffset returns how far the local clock is ahead of the database
// (negative when behind). Zero means the clocks may agree.
func clockOffset(before, after, db time.Time) time.Duration {
	if behind := db.Sub(after); behind > 0 {
		return -behind
	}
	if ahead := before.Sub(db.Add(time.Millisecond)); ahead > 0 {
		return ahead
	}
	return 0
}

// clockReading is the offset from the database measured at one instant, kept
// with the local wall and monotonic clocks at that instant.
type clockReading struct {
	offset time.Duration
	// wall has no monotonic reading, so Sub on it compares wall clocks.
	wall time.Time
	mono time.Time
}

// estimate returns the current offset from the database. A step of the wall
// clock since the reading moves wall time but not monotonic time, so the
// difference between the two is how far the clock jumped.
//
// The monotonic clock also stops while the host is suspended, so a resumed
// host looks like it jumped ahead until the next check measures it again.
func (r *clockReading) estimate(wall time.Time) time.Duration {
	return r.offset + wall.Sub(r.wall) - time.Since(r.mono)
}

type clockSkewChecker struct {
	cfg            ClockSkewCheckConfig
	maxOffset      time.Duration
	readDBClock    func(context.Context) (time.Time, error)
	wallNow        func() time.Time
	log            logging.Logger
	offsetGauge    prometheus.Gauge
	rejectedWrites prometheus.Counter

	// last is nil until the first successful check.
	last atomic.Pointer[clockReading]
	// rejecting only tracks the state for logging transitions; writes are
	// decided from last on every call.
	rejecting atomic.Bool
	// recheck asks run to measure again without waiting for the next tick.
	recheck chan struct{}
}

func newClockSkewChecker(cfg ClockSkewCheckConfig, maxClockSkew time.Duration, readDBClock func(context.Context) (time.Time, error), log logging.Logger, metrics *kvBackendMetrics) *clockSkewChecker {
	return &clockSkewChecker{
		cfg:         cfg,
		maxOffset:   maxClockSkew / 2,
		readDBClock: readDBClock,
		// Round(0) drops the monotonic reading, which Sub would otherwise
		// prefer over the wall clock.
		wallNow:        func() time.Time { return time.Now().Round(0) },
		log:            log,
		offsetGauge:    metrics.ClockOffsetFromDatabase,
		rejectedWrites: metrics.ClockSkewRejectedWrites,
		recheck:        make(chan struct{}, 1),
	}
}

func (c *clockSkewChecker) run(ctx context.Context) {
	ticker := time.NewTicker(c.cfg.Interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			c.check(ctx)
		case <-c.recheck:
			c.check(ctx)
		}
	}
}

func (c *clockSkewChecker) requestCheck() {
	select {
	case c.recheck <- struct{}{}:
	default:
	}
}

func (c *clockSkewChecker) check(ctx context.Context) {
	readCtx, cancel := context.WithTimeout(ctx, clockSkewCheckTimeout)
	defer cancel()

	before := c.wallNow()
	dbNow, err := c.readDBClock(readCtx)
	after := c.wallNow()
	mono := time.Now()
	if err != nil {
		if ctx.Err() == nil {
			// Keep the last result: writes fail anyway while the database is unreachable.
			c.log.Warn("Failed to read the database clock for the clock skew check", "error", err)
		}
		return
	}
	c.record(&clockReading{offset: clockOffset(before, after, dbNow), wall: after, mono: mono})
}

func (c *clockSkewChecker) record(reading *clockReading) {
	c.last.Store(reading)
	offset := reading.offset
	c.offsetGauge.Set(offset.Seconds())

	magnitude, direction := offsetMagnitude(offset)
	tooFar := magnitude > c.maxOffset
	wasRejecting := c.rejecting.Swap(tooFar)
	switch {
	case tooFar:
		c.log.Error("Clock is too far from the database clock, rejecting writes",
			"offset", magnitude.String(), "direction", direction, "maxOffset", c.maxOffset.String())
	case c.cfg.WarnThreshold > 0 && magnitude >= c.cfg.WarnThreshold:
		c.log.Warn("Clock is drifting from the database clock, synchronize it to avoid rejected writes",
			"offset", magnitude.String(), "direction", direction, "warnThreshold", c.cfg.WarnThreshold.String(), "maxOffset", c.maxOffset.String())
	}
	if wasRejecting && !tooFar {
		c.log.Info("Clock is back within the allowed offset from the database clock, accepting writes",
			"offset", magnitude.String(), "direction", direction)
	}
}

func offsetMagnitude(offset time.Duration) (time.Duration, string) {
	if offset < 0 {
		return -offset, "behind"
	}
	return offset, "ahead"
}

// checkWrite returns an error while the clock is too far from the database clock.
// It extrapolates from the last check, so a clock that jumps between checks
// stops writes right away instead of on the next tick.
// The message reaches API clients, so the offset and limits stay in the checker's logs.
func (c *clockSkewChecker) checkWrite() error {
	if c == nil {
		return nil
	}
	last := c.last.Load()
	if last == nil {
		return nil
	}
	magnitude, direction := offsetMagnitude(last.estimate(c.wallNow()))
	tooFar := magnitude > c.maxOffset
	if c.rejecting.CompareAndSwap(!tooFar, tooFar) {
		if tooFar {
			c.log.Error("Clock moved since the last clock skew check, rejecting writes",
				"offset", magnitude.String(), "direction", direction, "maxOffset", c.maxOffset.String())
		} else {
			c.log.Info("Clock moved back within the allowed offset from the database clock, accepting writes",
				"offset", magnitude.String(), "direction", direction)
		}
		// Confirm against the database instead of trusting the estimate until the next tick.
		c.requestCheck()
	}
	if !tooFar {
		return nil
	}
	c.rejectedWrites.Inc()
	return apierrors.NewServiceUnavailable(errClockNotSynchronized)
}
