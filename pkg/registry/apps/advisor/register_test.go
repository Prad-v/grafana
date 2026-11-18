package advisor

import (
	"context"
	"errors"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/stretchr/testify/require"

	advisorv0alpha1 "github.com/grafana/grafana/apps/advisor/pkg/apis/advisor/v0alpha1"
	"github.com/grafana/grafana/pkg/services/accesscontrol"
	"github.com/grafana/grafana/pkg/services/accesscontrol/actest"
	"github.com/grafana/grafana/pkg/setting"
)

type recordingACService struct {
	actest.FakeService
	declared *[]accesscontrol.RoleRegistration
}

func (s recordingACService) DeclareFixedRoles(registrations ...accesscontrol.RoleRegistration) error {
	*s.declared = append(*s.declared, registrations...)
	return nil
}

func stubRemoteServiceEnabled(t *testing.T, remote bool, err error) {
	t.Helper()
	orig := remoteServiceEnabled
	t.Cleanup(func() { remoteServiceEnabled = orig })
	remoteServiceEnabled = func(_ context.Context, _ *setting.Cfg, group string) (bool, error) {
		require.Equal(t, advisorv0alpha1.APIGroup, group)
		return remote, err
	}
}

func TestProvideAppInstaller(t *testing.T) {
	t.Run("skips the app when advisor.grafana.app is served remotely", func(t *testing.T) {
		stubRemoteServiceEnabled(t, true, nil)
		var declared []accesscontrol.RoleRegistration

		i, err := ProvideAppInstaller(recordingACService{declared: &declared}, nil, nil, setting.NewCfg(), nil, prometheus.NewRegistry())

		require.NoError(t, err)
		require.Nil(t, i)
		require.NotEmpty(t, declared, "access control roles are still registered")
	})

	for name, tc := range map[string]struct {
		remote bool
		err    error
	}{
		"installs the app when advisor.grafana.app is served locally":  {remote: false},
		"installs the app when the remote services file can't be read": {err: errors.New("boom")},
	} {
		t.Run(name, func(t *testing.T) {
			stubRemoteServiceEnabled(t, tc.remote, tc.err)
			var declared []accesscontrol.RoleRegistration

			i, err := ProvideAppInstaller(recordingACService{declared: &declared}, nil, nil, setting.NewCfg(), nil, prometheus.NewRegistry())

			require.NoError(t, err)
			require.NotNil(t, i)
			require.NotEmpty(t, declared)
		})
	}
}
