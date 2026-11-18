package resource

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"
	"testing/synctest"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
	"github.com/stretchr/testify/require"
	apierrors "k8s.io/apimachinery/pkg/api/errors"

	"github.com/grafana/grafana/pkg/storage/unified/resourcepb"
)

// clockKV reports a database clock offset from the local clock.
type clockKV struct {
	KV
	offset atomic.Int64
}

func (c *clockKV) UnixTimestamp(context.Context) (int64, error) {
	return time.Now().Add(time.Duration(c.offset.Load())).UnixMilli(), nil
}

func TestReadKVClock(t *testing.T) {
	store := &clockKV{}
	store.offset.Store(int64(time.Hour))

	before := time.Now().Add(time.Hour).Truncate(time.Millisecond)
	got, err := readKVClock(t.Context(), store)
	require.NoError(t, err)
	require.False(t, got.Before(before))
	require.Less(t, got.Sub(before), time.Second)
}

func TestClockOffset(t *testing.T) {
	before := time.Unix(1000, 0)
	after := before.Add(100 * time.Millisecond)
	for _, tc := range []struct {
		name string
		db   time.Time
		want time.Duration
	}{
		{name: "database read within the round trip", db: before.Add(50 * time.Millisecond)},
		{name: "behind by more than the round trip", db: after.Add(300 * time.Millisecond), want: -300 * time.Millisecond},
		// The database time is truncated, so it may be up to a millisecond later.
		{name: "ahead by more than the round trip", db: before.Add(-300 * time.Millisecond), want: 299 * time.Millisecond},
		{name: "truncation hides ahead skew below a millisecond", db: before.Add(-time.Millisecond)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			require.Equal(t, tc.want, clockOffset(before, after, tc.db))
		})
	}
}

func newTestClockSkewChecker(t *testing.T, cfg ClockSkewCheckConfig, read func(context.Context) (time.Time, error)) (*clockSkewChecker, *fakeLogger, *kvBackendMetrics) {
	t.Helper()
	logger := newFakeLogger()
	metrics := newKVBackendMetrics(prometheus.NewRegistry())
	return newClockSkewChecker(cfg, 500*time.Millisecond, read, logger, metrics), logger, metrics
}

// readingNow is a reading of offset taken at the current instant.
func readingNow(offset time.Duration) *clockReading {
	return &clockReading{offset: offset, wall: time.Now().Round(0), mono: time.Now()}
}

func TestClockSkewCheckerRecord(t *testing.T) {
	cfg := ClockSkewCheckConfig{Enabled: true, Interval: time.Second, WarnThreshold: 100 * time.Millisecond}

	t.Run("within tolerance", func(t *testing.T) {
		c, logger, metrics := newTestClockSkewChecker(t, cfg, nil)
		c.record(readingNow(-50 * time.Millisecond))

		require.NoError(t, c.checkWrite())
		require.Zero(t, logger.WarnLogs.Calls)
		require.Zero(t, logger.ErrorLogs.Calls)
		require.InDelta(t, -0.05, testutil.ToFloat64(metrics.ClockOffsetFromDatabase), 1e-9)
	})

	for _, offset := range []time.Duration{-200 * time.Millisecond, 200 * time.Millisecond} {
		t.Run("warns at "+offset.String(), func(t *testing.T) {
			c, logger, _ := newTestClockSkewChecker(t, cfg, nil)
			c.record(readingNow(offset))

			require.NoError(t, c.checkWrite())
			require.Equal(t, 1, logger.WarnLogs.Calls)
			require.Zero(t, logger.ErrorLogs.Calls)
		})
	}

	// Two instances each half the tolerated skew from the database, on opposite
	// sides, are the full tolerated skew apart.
	t.Run("accepts writes at half the tolerated skew", func(t *testing.T) {
		c, _, _ := newTestClockSkewChecker(t, cfg, nil)
		c.record(readingNow(250 * time.Millisecond))
		require.NoError(t, c.checkWrite())
	})

	for _, offset := range []time.Duration{-300 * time.Millisecond, 300 * time.Millisecond} {
		t.Run("rejects writes and recovers at "+offset.String(), func(t *testing.T) {
			c, logger, metrics := newTestClockSkewChecker(t, cfg, nil)
			c.record(readingNow(offset))

			err := c.checkWrite()
			require.True(t, apierrors.IsServiceUnavailable(err), "got %v", err)
			// Clients see no offsets or limits.
			require.ErrorContains(t, err, errClockNotSynchronized)
			require.NotContains(t, err.Error(), "300ms")
			require.Equal(t, 1, logger.ErrorLogs.Calls)
			require.Equal(t, 1.0, testutil.ToFloat64(metrics.ClockSkewRejectedWrites))

			c.record(readingNow(0))
			require.NoError(t, c.checkWrite())
			require.Equal(t, 1, logger.InfoLogs.Calls)
		})
	}

	t.Run("nil checker accepts writes", func(t *testing.T) {
		var c *clockSkewChecker
		require.NoError(t, c.checkWrite())
	})
}

func TestClockSkewCheckerAcceptsWritesBeforeTheFirstCheck(t *testing.T) {
	c, _, _ := newTestClockSkewChecker(t, ClockSkewCheckConfig{Enabled: true, Interval: time.Second}, nil)
	require.NoError(t, c.checkWrite())
}

// jumpingChecker returns a checker whose wall clock is offset by jump from the
// real one, against a database clock that is not.
func jumpingChecker(t *testing.T, jump *atomic.Int64) (*clockSkewChecker, *fakeLogger) {
	t.Helper()
	cfg := ClockSkewCheckConfig{Enabled: true, Interval: time.Hour}
	c, logger, _ := newTestClockSkewChecker(t, cfg, func(context.Context) (time.Time, error) {
		return time.Now(), nil
	})
	c.wallNow = func() time.Time { return time.Now().Round(0).Add(time.Duration(jump.Load())) }
	return c, logger
}

func TestClockSkewCheckerDetectsJumpsBetweenChecks(t *testing.T) {
	for _, jumpBy := range []time.Duration{79 * 24 * time.Hour, -time.Hour} {
		t.Run(jumpBy.String(), func(t *testing.T) {
			var jump atomic.Int64
			c, logger := jumpingChecker(t, &jump)
			c.check(t.Context())
			require.NoError(t, c.checkWrite())

			jump.Store(int64(jumpBy))
			err := c.checkWrite()
			require.True(t, apierrors.IsServiceUnavailable(err), "got %v", err)
			require.Equal(t, 1, logger.ErrorLogs.Calls)
			require.Len(t, c.recheck, 1, "a jump asks for a check against the database")

			// Later writes keep failing without logging the transition again.
			require.Error(t, c.checkWrite())
			require.Equal(t, 1, logger.ErrorLogs.Calls)

			jump.Store(0)
			require.NoError(t, c.checkWrite())
			require.Equal(t, 1, logger.InfoLogs.Calls)
		})
	}
}

func TestClockSkewCheckerAcceptsAJumpThatFixesTheClock(t *testing.T) {
	var jump atomic.Int64
	jump.Store(int64(time.Hour))
	c, _ := jumpingChecker(t, &jump)
	c.check(t.Context())
	require.Error(t, c.checkWrite())

	// Time synchronization steps the clock back to the database's.
	jump.Store(0)
	require.NoError(t, c.checkWrite())
}

func TestClockSkewCheckerRechecksAfterAJump(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		var jump atomic.Int64
		var reads atomic.Int32
		c, _ := jumpingChecker(t, &jump)
		read := c.readDBClock
		c.readDBClock = func(ctx context.Context) (time.Time, error) {
			reads.Add(1)
			return read(ctx)
		}
		c.check(t.Context())

		ctx, cancel := context.WithCancel(t.Context())
		defer cancel()
		go c.run(ctx)

		jump.Store(int64(time.Hour))
		require.Error(t, c.checkWrite())
		synctest.Wait()
		// The interval is an hour, so this read came from the jump.
		require.Equal(t, int32(2), reads.Load())
		// The lower bound allows for the database truncating to the millisecond.
		require.InDelta(t, time.Hour, c.last.Load().offset, float64(time.Millisecond))
	})
}

func TestClockSkewCheckerKeepsLastResultOnReadError(t *testing.T) {
	var fail atomic.Bool
	cfg := ClockSkewCheckConfig{Enabled: true, Interval: time.Second}
	c, logger, _ := newTestClockSkewChecker(t, cfg, func(context.Context) (time.Time, error) {
		if fail.Load() {
			return time.Time{}, errors.New("database unavailable")
		}
		return time.Now().Add(time.Minute), nil
	})

	c.check(t.Context())
	require.Error(t, c.checkWrite())

	fail.Store(true)
	c.check(t.Context())
	require.Error(t, c.checkWrite())
	require.Equal(t, 1, logger.WarnLogs.Calls)
}

func TestClockSkewCheckerRun(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		var offset atomic.Int64
		offset.Store(int64(time.Minute))
		cfg := ClockSkewCheckConfig{Enabled: true, Interval: 10 * time.Second}
		c, _, _ := newTestClockSkewChecker(t, cfg, func(context.Context) (time.Time, error) {
			return time.Now().Add(time.Duration(offset.Load())), nil
		})

		ctx, cancel := context.WithCancel(t.Context())
		defer cancel()
		go c.run(ctx)

		time.Sleep(cfg.Interval)
		synctest.Wait()
		require.Error(t, c.checkWrite())

		offset.Store(0)
		time.Sleep(cfg.Interval)
		synctest.Wait()
		require.NoError(t, c.checkWrite())
	})
}

func TestKVBackendClockSkewCheck(t *testing.T) {
	newBackend := func(t *testing.T, enabled bool, maxClockSkew, dbOffset time.Duration) *kvStorageBackend {
		store := &clockKV{}
		store.offset.Store(int64(dbOffset))
		return setupTestStorageBackend(t, func(opts *KVBackendOptions) {
			store.KV = opts.KvStore
			opts.KvStore = store
			opts.MaxClockSkew = maxClockSkew
			opts.ClockSkewCheck = ClockSkewCheckConfig{Enabled: enabled, Interval: time.Hour, WarnThreshold: 100 * time.Millisecond}
		})
	}

	for _, dbOffset := range []time.Duration{-time.Minute, time.Minute} {
		t.Run("rejects writes when the clock is too far off by "+dbOffset.String(), func(t *testing.T) {
			backend := newBackend(t, true, 0, dbOffset)
			_, err := writeEvent(t, t.Context(), backend, "item", resourcepb.WatchEvent_ADDED)
			require.True(t, apierrors.IsServiceUnavailable(err), "got %v", err)
		})
	}

	t.Run("accepts writes when the clock is in sync", func(t *testing.T) {
		backend := newBackend(t, true, 0, 0)
		_, err := writeEvent(t, t.Context(), backend, "item", resourcepb.WatchEvent_ADDED)
		require.NoError(t, err)
	})

	t.Run("uses the configured skew", func(t *testing.T) {
		backend := newBackend(t, true, 5*time.Second, 2*time.Second)
		require.Equal(t, 2500*time.Millisecond, backend.clockSkew.maxOffset)
		_, err := writeEvent(t, t.Context(), backend, "item", resourcepb.WatchEvent_ADDED)
		require.NoError(t, err)
	})

	t.Run("does not check when disabled", func(t *testing.T) {
		backend := newBackend(t, false, 0, time.Minute)
		require.Nil(t, backend.clockSkew)
		_, err := writeEvent(t, t.Context(), backend, "item", resourcepb.WatchEvent_ADDED)
		require.NoError(t, err)
	})
}
