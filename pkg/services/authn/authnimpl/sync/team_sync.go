package sync

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"strconv"
	"strings"

	claims "github.com/grafana/authlib/types"

	"github.com/grafana/grafana/pkg/apimachinery/identity"
	"github.com/grafana/grafana/pkg/infra/log"
	"github.com/grafana/grafana/pkg/infra/tracing"
	"github.com/grafana/grafana/pkg/services/accesscontrol"
	"github.com/grafana/grafana/pkg/services/authn"
	"github.com/grafana/grafana/pkg/services/login"
	"github.com/grafana/grafana/pkg/services/org"
	"github.com/grafana/grafana/pkg/services/team"
	"github.com/grafana/grafana/pkg/setting"
)

const (
	teamMemberPermission = "Member"
	teamSearchPageSize   = 100
)

// TeamSyncMapping maps an external group to a team in a Grafana org.
type TeamSyncMapping struct {
	Group string
	// Org is either a numeric org ID or an org name.
	Org  string
	Team string
}

// ParseTeamSyncMappings parses `[auth.team_sync] mappings` entries formatted as
// `<external_group>:<org name or id>:<team name>`. Invalid entries are returned in
// invalid so the caller can report them.
func ParseTeamSyncMappings(entries []string) (mappings []TeamSyncMapping, invalid []string) {
	for _, entry := range entries {
		parts := splitUnescaped(entry, ':')
		if len(parts) != 3 || parts[0] == "" || parts[1] == "" || parts[2] == "" {
			invalid = append(invalid, entry)
			continue
		}
		mappings = append(mappings, TeamSyncMapping{Group: parts[0], Org: parts[1], Team: parts[2]})
	}
	return mappings, invalid
}

// splitUnescaped splits on sep, treating `\<sep>` as a literal sep. Any other
// backslash is kept so values such as `DOMAIN\group` stay intact.
func splitUnescaped(s string, sep byte) []string {
	var parts []string
	var current strings.Builder
	for i := 0; i < len(s); i++ {
		switch {
		case s[i] == '\\' && i+1 < len(s) && s[i+1] == sep:
			current.WriteByte(sep)
			i++
		case s[i] == sep:
			parts = append(parts, current.String())
			current.Reset()
		default:
			current.WriteByte(s[i])
		}
	}
	return append(parts, current.String())
}

func ProvideTeamSync(
	cfg *setting.Cfg, orgService org.Service, teamService team.Service,
	teamPermissions accesscontrol.TeamPermissionsService, tracer tracing.Tracer,
) *TeamSync {
	logger := log.New("team.sync")
	mappings, invalid := ParseTeamSyncMappings(cfg.TeamSyncMappings)
	for _, entry := range invalid {
		logger.Error("Skipping team sync mapping with invalid format, expected <group>:<org>:<team>", "mapping", entry)
	}

	return &TeamSync{
		cfg:             cfg,
		mappings:        mappings,
		orgService:      orgService,
		teamService:     teamService,
		teamPermissions: teamPermissions,
		log:             logger,
		tracer:          tracer,
	}
}

// TeamSync keeps the external team memberships of a user in line with the groups
// asserted by the identity provider.
type TeamSync struct {
	cfg             *setting.Cfg
	mappings        []TeamSyncMapping
	orgService      org.Service
	teamService     team.Service
	teamPermissions accesscontrol.TeamPermissionsService
	log             log.Logger
	tracer          tracing.Tracer
}

// SyncTeamsHook adds the user to the teams mapped from their external groups and
// removes externally synced memberships of mapped teams that the groups no longer
// justify. Memberships created manually and teams that are not part of the mappings
// are left alone. Failures are logged and never block the login.
//
// It must run after SyncOrgRolesHook: team membership requires org membership.
func (s *TeamSync) SyncTeamsHook(ctx context.Context, id *authn.Identity, _ *authn.Request) error {
	if !s.cfg.TeamSyncEnabled || len(s.mappings) == 0 || !id.ClientParams.SyncTeams {
		return nil
	}

	ctx, span := s.tracer.Start(ctx, "team.sync.SyncTeamsHook")
	defer span.End()

	if !id.IsIdentityType(claims.TypeUser) {
		return nil
	}

	// Local password logins carry no IdP groups, syncing them would drop every external membership.
	if !isExternalAuthModule(id.AuthenticatedBy) {
		return nil
	}

	logger := s.log.FromContext(ctx).New("id", id.ID, "login", id.Login)

	userID, err := id.GetInternalID()
	if err != nil {
		logger.Warn("Failed to sync teams, invalid ID for identity", "err", err)
		return nil
	}

	userOrgs, err := s.orgService.GetUserOrgList(ctx, &org.GetUserOrgListQuery{UserID: userID})
	if err != nil {
		logger.Error("Failed to sync teams, could not get the user's organizations", "err", err)
		return nil
	}
	memberOfOrg := make(map[int64]bool, len(userOrgs))
	for _, o := range userOrgs {
		memberOfOrg[o.OrgID] = true
	}

	for orgID, mappings := range s.mappingsByOrg(ctx) {
		if !memberOfOrg[orgID] {
			logger.Debug("Skipping team sync for organization the user is not a member of", "orgId", orgID)
			continue
		}
		s.syncOrg(ctx, logger.New("orgId", orgID), orgID, userID, id.ExternalGroups, mappings)
	}

	return nil
}

// mappingsByOrg resolves the org of every mapping and groups the mappings by org ID.
func (s *TeamSync) mappingsByOrg(ctx context.Context) map[int64][]TeamSyncMapping {
	result := map[int64][]TeamSyncMapping{}
	resolved := map[string]int64{}
	failed := map[string]bool{}

	for _, m := range s.mappings {
		if failed[m.Org] {
			continue
		}

		orgID, ok := resolved[m.Org]
		if !ok {
			var err error
			orgID, err = s.resolveOrg(ctx, m.Org)
			if err != nil {
				s.log.FromContext(ctx).Warn("Skipping team sync mappings, could not resolve organization", "org", m.Org, "err", err)
				failed[m.Org] = true
				continue
			}
			resolved[m.Org] = orgID
		}

		result[orgID] = append(result[orgID], m)
	}

	return result
}

func (s *TeamSync) resolveOrg(ctx context.Context, ref string) (int64, error) {
	if orgID, err := strconv.ParseInt(ref, 10, 64); err == nil {
		return orgID, nil
	}

	res, err := s.orgService.GetByName(ctx, &org.GetOrgByNameQuery{Name: ref})
	if err != nil {
		return 0, err
	}
	if res == nil {
		return 0, org.ErrOrgNotFound
	}
	return res.ID, nil
}

func (s *TeamSync) syncOrg(
	ctx context.Context, logger log.Logger, orgID, userID int64, externalGroups []string, mappings []TeamSyncMapping,
) {
	ctx, requester := identity.WithServiceIdentity(ctx, orgID)

	all, err := s.teamService.GetUserTeamMemberships(ctx, orgID, userID, false, true)
	if err != nil {
		logger.Error("Failed to sync teams, could not get current team memberships", "err", err)
		return
	}
	external, err := s.teamService.GetUserTeamMemberships(ctx, orgID, userID, true, true)
	if err != nil {
		logger.Error("Failed to sync teams, could not get current external team memberships", "err", err)
		return
	}
	isMember := teamIDSet(all)
	isExternalMember := teamIDSet(external)

	desired := map[string]bool{}
	var teamNames []string
	for _, m := range mappings {
		if !slices.Contains(teamNames, m.Team) {
			teamNames = append(teamNames, m.Team)
		}
		if slices.Contains(externalGroups, m.Group) {
			desired[m.Team] = true
		}
	}
	slices.Sort(teamNames)

	for _, name := range teamNames {
		teamLogger := logger.New("team", name)

		teamID, found, err := s.findTeam(ctx, requester, orgID, name)
		if err != nil {
			teamLogger.Error("Failed to look up team", "err", err)
			continue
		}

		if !desired[name] {
			if found && isExternalMember[teamID] {
				s.setMembership(ctx, teamLogger, orgID, userID, teamID, "")
			}
			continue
		}

		if !found {
			if !s.cfg.TeamSyncAutoCreateTeams {
				teamLogger.Warn("Team referenced by team sync does not exist, enable auto_create_teams or create it")
				continue
			}
			if teamID, err = s.createTeam(ctx, requester, orgID, name); err != nil {
				teamLogger.Error("Failed to create team", "err", err)
				continue
			}
		}

		// Existing members keep their current permission, a manual team admin must not be downgraded.
		if !isMember[teamID] {
			s.setMembership(ctx, teamLogger, orgID, userID, teamID, teamMemberPermission)
		}
	}
}

// setMembership adds the user as an external team member, or removes them when permission is empty.
func (s *TeamSync) setMembership(ctx context.Context, logger log.Logger, orgID, userID, teamID int64, permission string) {
	user := accesscontrol.User{ID: userID, IsExternal: true}
	_, err := s.teamPermissions.SetUserPermission(ctx, orgID, user, strconv.FormatInt(teamID, 10), permission)
	if err != nil {
		// The membership may have been removed concurrently or by another login.
		if permission == "" && errors.Is(err, team.ErrTeamMemberNotFound) {
			return
		}
		logger.Error("Failed to update team membership", "teamId", teamID, "permission", permission, "err", err)
		return
	}

	if permission == "" {
		logger.Debug("Removed user from team", "teamId", teamID)
	} else {
		logger.Debug("Added user to team", "teamId", teamID)
	}
}

func (s *TeamSync) findTeam(ctx context.Context, requester identity.Requester, orgID int64, name string) (int64, bool, error) {
	res, err := s.teamService.SearchTeams(ctx, &team.SearchTeamsQuery{
		OrgID:        orgID,
		Name:         name,
		SignedInUser: requester,
		Limit:        teamSearchPageSize,
		Page:         1,
	})
	if err != nil {
		return 0, false, fmt.Errorf("search teams: %w", err)
	}

	for _, t := range res.Teams {
		if strings.EqualFold(t.Name, name) {
			return t.ID, true, nil
		}
	}
	return 0, false, nil
}

func (s *TeamSync) createTeam(ctx context.Context, requester identity.Requester, orgID int64, name string) (int64, error) {
	created, err := s.teamService.CreateTeam(ctx, &team.CreateTeamCommand{OrgID: orgID, Name: name})
	if err == nil {
		return created.ID, nil
	}

	// Another login may have created the team in the meantime.
	if errors.Is(err, team.ErrTeamNameTaken) {
		if id, found, findErr := s.findTeam(ctx, requester, orgID, name); findErr == nil && found {
			return id, nil
		}
	}
	return 0, err
}

func teamIDSet(members []*team.TeamMemberDTO) map[int64]bool {
	set := make(map[int64]bool, len(members))
	for _, m := range members {
		set[m.TeamID] = true
	}
	return set
}

// isExternalAuthModule reports whether the identity was authenticated by an
// external identity provider that can assert group membership.
func isExternalAuthModule(module string) bool {
	if strings.HasPrefix(module, "oauth_") {
		return true
	}

	switch module {
	case login.JWTModule, login.LDAPAuthModule, login.AuthProxyAuthModule, login.SAMLAuthModule:
		return true
	}
	return false
}
