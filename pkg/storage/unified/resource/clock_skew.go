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

type clockSkewChecker struct {
	cfg            ClockSkewCheckConfig
	maxOffset      time.Duration
	readDBClock    func(context.Context) (time.Time, error)
	log            logging.Logger
	offsetGauge    prometheus.Gauge
	rejectedWrites prometheus.Counter

	rejecting atomic.Bool
}

func newClockSkewChecker(cfg ClockSkewCheckConfig, maxClockSkew time.Duration, readDBClock func(context.Context) (time.Time, error), log logging.Logger, metrics *kvBackendMetrics) *clockSkewChecker {
	return &clockSkewChecker{
		cfg:            cfg,
		maxOffset:      maxClockSkew / 2,
		readDBClock:    readDBClock,
		log:            log,
		offsetGauge:    metrics.ClockOffsetFromDatabase,
		rejectedWrites: metrics.ClockSkewRejectedWrites,
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
		}
	}
}

func (c *clockSkewChecker) check(ctx context.Context) {
	readCtx, cancel := context.WithTimeout(ctx, clockSkewCheckTimeout)
	defer cancel()

	before := time.Now()
	dbNow, err := c.readDBClock(readCtx)
	after := time.Now()
	if err != nil {
		if ctx.Err() == nil {
			// Keep the last result: writes fail anyway while the database is unreachable.
			c.log.Warn("Failed to read the database clock for the clock skew check", "error", err)
		}
		return
	}
	c.record(clockOffset(before, after, dbNow))
}

func (c *clockSkewChecker) record(offset time.Duration) {
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
// The message reaches API clients, so the offset and limits stay in the checker's logs.
func (c *clockSkewChecker) checkWrite() error {
	if c == nil || !c.rejecting.Load() {
		return nil
	}
	c.rejectedWrites.Inc()
	return apierrors.NewServiceUnavailable(errClockNotSynchronized)
}
