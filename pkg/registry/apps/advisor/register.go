package advisor

import (
	"context"
	"fmt"

	authlib "github.com/grafana/authlib/types"
	"github.com/prometheus/client_golang/prometheus"

	advisorv0alpha1 "github.com/grafana/grafana/apps/advisor/pkg/apis/advisor/v0alpha1"
	advisorapp "github.com/grafana/grafana/apps/advisor/pkg/app"
	"github.com/grafana/grafana/apps/advisor/pkg/app/checkregistry"
	appsdkapiserver "github.com/grafana/grafana/pkg/apiserver/appsdk"
	"github.com/grafana/grafana/pkg/infra/log"
	"github.com/grafana/grafana/pkg/router"
	"github.com/grafana/grafana/pkg/services/accesscontrol"
	"github.com/grafana/grafana/pkg/services/apiserver/appinstaller"
	grafanaauthorizer "github.com/grafana/grafana/pkg/services/apiserver/auth/authorizer"
	"github.com/grafana/grafana/pkg/services/org"
	"github.com/grafana/grafana/pkg/setting"
)

var (
	_ appsdkapiserver.AppInstaller    = (*AppInstaller)(nil)
	_ appinstaller.AuthorizerProvider = (*AppInstaller)(nil)
)

// remoteServiceEnabled reports whether an API group is served by its
// multi-tenant apiserver instead of this Grafana.
var remoteServiceEnabled = router.RemoteServiceEnabled

type AppInstaller struct {
	*advisorapp.AdvisorAppInstaller
}

// ProvideAppInstaller returns nil when advisor.grafana.app is served by the
// multi-tenant advisor, so the app, its check type registerer and its check
// scheduler don't run in this Grafana. The advisor access control roles are
// registered either way.
func ProvideAppInstaller(
	accessControlService accesscontrol.Service,
	accessClient authlib.AccessClient,
	checkRegistry checkregistry.CheckService,
	cfg *setting.Cfg,
	orgService org.Service,
	registerer prometheus.Registerer,
) (*AppInstaller, error) {
	if err := registerAccessControlRoles(accessControlService); err != nil {
		return nil, fmt.Errorf("registering access control roles: %w", err)
	}

	logger := log.New("app-registry")
	remote, err := remoteServiceEnabled(context.Background(), cfg, advisorv0alpha1.APIGroup)
	if err != nil {
		logger.Warn("Installing advisor.grafana.app locally: could not tell whether it is served remotely", "error", err)
	} else if remote {
		logger.Info("Skipping advisor.grafana.app apiserver: served by the multi-tenant advisor")
		return nil, nil
	}

	authorizer := grafanaauthorizer.NewResourceAuthorizer(accessClient)
	i, err := advisorapp.ProvideAppInstaller(authorizer, checkRegistry, cfg, orgService, registerer)
	if err != nil {
		return nil, err
	}
	return &AppInstaller{
		AdvisorAppInstaller: i,
	}, nil
}
