package teamsync_test

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"testing"

	claims "github.com/grafana/authlib/types"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/grafana/grafana/pkg/infra/tracing"
	"github.com/grafana/grafana/pkg/services/accesscontrol"
	"github.com/grafana/grafana/pkg/services/authn"
	authnsync "github.com/grafana/grafana/pkg/services/authn/authnimpl/sync"
	"github.com/grafana/grafana/pkg/services/login"
	"github.com/grafana/grafana/pkg/services/org"
	"github.com/grafana/grafana/pkg/services/org/orgtest"
	"github.com/grafana/grafana/pkg/services/team"
	"github.com/grafana/grafana/pkg/setting"
)

const userID = int64(7)

type memberKey struct{ orgID, teamID int64 }

// fakeTeams is an in-memory team service for a single user.
type fakeTeams struct {
	team.Service

	teams     map[int64][]*team.TeamDTO // by org
	members   map[memberKey]bool        // value: external
	nextID    int64
	createErr error
	failList  bool
	created   []string
}

func newFakeTeams() *fakeTeams {
	return &fakeTeams{teams: map[int64][]*team.TeamDTO{}, members: map[memberKey]bool{}, nextID: 100}
}

func (f *fakeTeams) addTeam(orgID int64, name string) int64 {
	f.nextID++
	f.teams[orgID] = append(f.teams[orgID], &team.TeamDTO{ID: f.nextID, OrgID: orgID, Name: name})
	return f.nextID
}

func (f *fakeTeams) SearchTeams(_ context.Context, q *team.SearchTeamsQuery) (team.SearchTeamQueryResult, error) {
	var res []*team.TeamDTO
	for _, t := range f.teams[q.OrgID] {
		if q.Name == "" || strings.EqualFold(t.Name, q.Name) {
			res = append(res, t)
		}
	}
	return team.SearchTeamQueryResult{Teams: res}, nil
}

func (f *fakeTeams) CreateTeam(_ context.Context, cmd *team.CreateTeamCommand) (team.Team, error) {
	if f.createErr != nil {
		return team.Team{}, f.createErr
	}
	f.created = append(f.created, fmt.Sprintf("%d/%s", cmd.OrgID, cmd.Name))
	return team.Team{ID: f.addTeam(cmd.OrgID, cmd.Name), OrgID: cmd.OrgID, Name: cmd.Name}, nil
}

func (f *fakeTeams) GetUserTeamMemberships(_ context.Context, orgID, _ int64, external, _ bool) ([]*team.TeamMemberDTO, error) {
	if f.failList {
		return nil, errors.New("boom")
	}
	var res []*team.TeamMemberDTO
	for k, isExternal := range f.members {
		if k.orgID == orgID && (!external || isExternal) {
			res = append(res, &team.TeamMemberDTO{OrgID: orgID, TeamID: k.teamID, External: isExternal})
		}
	}
	return res, nil
}

// fakePermissions applies membership changes to fakeTeams and records them.
type fakePermissions struct {
	accesscontrol.TeamPermissionsService

	teams *fakeTeams
	calls []string
	err   error
}

func (f *fakePermissions) SetUserPermission(_ context.Context, orgID int64, user accesscontrol.User, resourceID, permission string) (*accesscontrol.ResourcePermission, error) {
	f.calls = append(f.calls, fmt.Sprintf("org=%d team=%s perm=%q external=%t user=%d", orgID, resourceID, permission, user.IsExternal, user.ID))
	if f.err != nil {
		return nil, f.err
	}
	teamID, _ := strconv.ParseInt(resourceID, 10, 64)
	if permission == "" {
		delete(f.teams.members, memberKey{orgID, teamID})
	} else {
		f.teams.members[memberKey{orgID, teamID}] = user.IsExternal
	}
	return &accesscontrol.ResourcePermission{}, nil
}

type harness struct {
	cfg   *setting.Cfg
	teams *fakeTeams
	perms *fakePermissions
	orgs  *orgtest.FakeOrgService
}

func newHarness(mappings ...string) *harness {
	cfg := setting.NewCfg()
	cfg.TeamSyncEnabled = true
	cfg.TeamSyncMappings = mappings

	teams := newFakeTeams()
	return &harness{
		cfg:   cfg,
		teams: teams,
		perms: &fakePermissions{teams: teams},
		orgs: &orgtest.FakeOrgService{
			ExpectedUserOrgDTO: []*org.UserOrgDTO{{OrgID: 1}, {OrgID: 2}},
			ExpectedOrgs:       []*org.OrgDTO{{ID: 1, Name: "Main Org."}, {ID: 2, Name: "TenantA"}, {ID: 3, Name: "TenantB"}},
		},
	}
}

func (h *harness) run(t *testing.T, id *authn.Identity) {
	t.Helper()
	s := authnsync.ProvideTeamSync(h.cfg, h.orgs, h.teams, h.perms, tracing.InitializeTracerForTest())
	require.NoError(t, s.SyncTeamsHook(context.Background(), id, nil))
}

func oauthIdentity(groups ...string) *authn.Identity {
	return &authn.Identity{
		ID:              strconv.FormatInt(userID, 10),
		Type:            claims.TypeUser,
		AuthenticatedBy: login.GenericOAuthModule,
		ExternalGroups:  groups,
		ClientParams:    authn.ClientParams{SyncTeams: true},
	}
}

func TestParseTeamSyncMappings(t *testing.T) {
	mappings, invalid := authnsync.ParseTeamSyncMappings([]string{
		"admins:TenantA:Ops",
		`DOMAIN\grp:2:Dev`,
		`a\:b:TenantA:Team\:X`,
		"missing-team:TenantA",
		"too:many:parts:here",
		"::",
		"empty-org::Team",
	})

	assert.Equal(t, []authnsync.TeamSyncMapping{
		{Group: "admins", Org: "TenantA", Team: "Ops"},
		{Group: `DOMAIN\grp`, Org: "2", Team: "Dev"},
		{Group: "a:b", Org: "TenantA", Team: "Team:X"},
	}, mappings)
	assert.Equal(t, []string{"missing-team:TenantA", "too:many:parts:here", "::", "empty-org::Team"}, invalid)
}

func TestSyncTeamsHook_AddsMembershipForMatchingGroup(t *testing.T) {
	h := newHarness("ops:TenantA:Ops")
	ops := h.teams.addTeam(2, "Ops")

	h.run(t, oauthIdentity("ops", "other"))

	assert.Equal(t, []string{`org=2 team=` + strconv.FormatInt(ops, 10) + ` perm="Member" external=true user=7`}, h.perms.calls)
	assert.True(t, h.teams.members[memberKey{2, ops}], "membership must be flagged external")
}

func TestSyncTeamsHook_ResolvesOrgByNumericID(t *testing.T) {
	h := newHarness("ops:2:Ops")
	ops := h.teams.addTeam(2, "Ops")

	h.run(t, oauthIdentity("ops"))

	assert.Contains(t, h.teams.members, memberKey{2, ops})
}

func TestSyncTeamsHook_TeamNameMatchIsCaseInsensitive(t *testing.T) {
	h := newHarness("ops:TenantA:ops")
	ops := h.teams.addTeam(2, "OPS")

	h.run(t, oauthIdentity("ops"))

	assert.Contains(t, h.teams.members, memberKey{2, ops})
}

func TestSyncTeamsHook_SyncsMultipleOrgsAndTeams(t *testing.T) {
	h := newHarness("ops:TenantA:Ops", "dev:TenantA:Dev", "viewers:Main Org.:Readers")
	ops := h.teams.addTeam(2, "Ops")
	dev := h.teams.addTeam(2, "Dev")
	readers := h.teams.addTeam(1, "Readers")

	h.run(t, oauthIdentity("ops", "dev", "viewers"))

	assert.Contains(t, h.teams.members, memberKey{2, ops})
	assert.Contains(t, h.teams.members, memberKey{2, dev})
	assert.Contains(t, h.teams.members, memberKey{1, readers})
}

func TestSyncTeamsHook_RemovesExternalMembershipWhenGroupIsGone(t *testing.T) {
	h := newHarness("ops:TenantA:Ops", "dev:TenantA:Dev")
	ops := h.teams.addTeam(2, "Ops")
	dev := h.teams.addTeam(2, "Dev")
	h.teams.members[memberKey{2, ops}] = true
	h.teams.members[memberKey{2, dev}] = true

	h.run(t, oauthIdentity("dev"))

	assert.NotContains(t, h.teams.members, memberKey{2, ops})
	assert.Contains(t, h.teams.members, memberKey{2, dev})
	require.Len(t, h.perms.calls, 1)
	assert.Contains(t, h.perms.calls[0], `perm=""`)
}

func TestSyncTeamsHook_RemovesAllMappedMembershipsWhenIdPReturnsNoGroups(t *testing.T) {
	h := newHarness("ops:TenantA:Ops")
	ops := h.teams.addTeam(2, "Ops")
	h.teams.members[memberKey{2, ops}] = true

	h.run(t, oauthIdentity())

	assert.Empty(t, h.teams.members)
}

func TestSyncTeamsHook_KeepsManualMemberships(t *testing.T) {
	h := newHarness("ops:TenantA:Ops")
	ops := h.teams.addTeam(2, "Ops")
	h.teams.members[memberKey{2, ops}] = false // added by an admin

	h.run(t, oauthIdentity()) // group no longer present
	assert.Contains(t, h.teams.members, memberKey{2, ops}, "manual membership must not be removed")

	h.run(t, oauthIdentity("ops")) // group present
	assert.False(t, h.teams.members[memberKey{2, ops}], "manual membership must not be converted to external")
	assert.Empty(t, h.perms.calls, "existing members must not be rewritten (would reset admin permission)")
}

func TestSyncTeamsHook_LeavesUnmappedTeamsAlone(t *testing.T) {
	h := newHarness("ops:TenantA:Ops")
	h.teams.addTeam(2, "Ops")
	other := h.teams.addTeam(2, "Unmapped")
	h.teams.members[memberKey{2, other}] = true

	h.run(t, oauthIdentity())

	assert.Contains(t, h.teams.members, memberKey{2, other})
}

func TestSyncTeamsHook_IsIdempotent(t *testing.T) {
	h := newHarness("ops:TenantA:Ops")
	h.teams.addTeam(2, "Ops")

	h.run(t, oauthIdentity("ops"))
	h.run(t, oauthIdentity("ops"))

	assert.Len(t, h.perms.calls, 1)
}

func TestSyncTeamsHook_SkipsOrgsTheUserIsNotMemberOf(t *testing.T) {
	h := newHarness("ops:TenantB:Ops")
	h.teams.addTeam(3, "Ops")

	h.run(t, oauthIdentity("ops"))

	assert.Empty(t, h.perms.calls)
}

func TestSyncTeamsHook_MissingTeam(t *testing.T) {
	t.Run("is skipped by default", func(t *testing.T) {
		h := newHarness("ops:TenantA:Ops")

		h.run(t, oauthIdentity("ops"))

		assert.Empty(t, h.teams.created)
		assert.Empty(t, h.perms.calls)
	})

	t.Run("is created with auto_create_teams", func(t *testing.T) {
		h := newHarness("ops:TenantA:Ops")
		h.cfg.TeamSyncAutoCreateTeams = true

		h.run(t, oauthIdentity("ops"))

		assert.Equal(t, []string{"2/Ops"}, h.teams.created)
		require.Len(t, h.perms.calls, 1)
		assert.Contains(t, h.perms.calls[0], "org=2")
	})

	t.Run("is not created when the user does not have the group", func(t *testing.T) {
		h := newHarness("ops:TenantA:Ops")
		h.cfg.TeamSyncAutoCreateTeams = true

		h.run(t, oauthIdentity("someone-else"))

		assert.Empty(t, h.teams.created)
	})

	t.Run("is skipped when creation fails", func(t *testing.T) {
		h := newHarness("ops:TenantA:Ops")
		h.cfg.TeamSyncAutoCreateTeams = true
		h.teams.createErr = team.ErrTeamNameTaken

		h.run(t, oauthIdentity("ops"))

		assert.Empty(t, h.perms.calls, "no team could be resolved, nothing to write")
	})
}

func TestSyncTeamsHook_DoesNothingWhen(t *testing.T) {
	tests := map[string]func(h *harness, id *authn.Identity){
		"disabled":              func(h *harness, _ *authn.Identity) { h.cfg.TeamSyncEnabled = false },
		"identity opts out":     func(_ *harness, id *authn.Identity) { id.ClientParams.SyncTeams = false },
		"not a user":            func(_ *harness, id *authn.Identity) { id.Type = claims.TypeServiceAccount },
		"local password login":  func(_ *harness, id *authn.Identity) { id.AuthenticatedBy = login.PasswordAuthModule },
		"no valid mappings":     func(h *harness, _ *authn.Identity) { h.cfg.TeamSyncMappings = []string{"invalid"} },
		"invalid internal user": func(_ *harness, id *authn.Identity) { id.ID = "not-a-number" },
	}

	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			h := newHarness("ops:TenantA:Ops")
			ops := h.teams.addTeam(2, "Ops")
			h.teams.members[memberKey{2, ops}] = true // would be removed if the hook ran
			id := oauthIdentity()
			mutate(h, id)

			h.run(t, id)

			assert.Empty(t, h.perms.calls)
			assert.Contains(t, h.teams.members, memberKey{2, ops})
		})
	}
}

func TestSyncTeamsHook_RunsForExternalAuthModules(t *testing.T) {
	for _, module := range []string{login.GenericOAuthModule, login.AzureADAuthModule, login.GithubAuthModule, login.JWTModule, login.LDAPAuthModule, login.AuthProxyAuthModule} {
		t.Run(module, func(t *testing.T) {
			h := newHarness("ops:TenantA:Ops")
			ops := h.teams.addTeam(2, "Ops")
			id := oauthIdentity("ops")
			id.AuthenticatedBy = module

			h.run(t, id)

			assert.Contains(t, h.teams.members, memberKey{2, ops})
		})
	}
}

func TestSyncTeamsHook_NeverFailsTheLogin(t *testing.T) {
	t.Run("membership write fails", func(t *testing.T) {
		h := newHarness("ops:TenantA:Ops", "dev:Main Org.:Dev")
		h.teams.addTeam(2, "Ops")
		dev := h.teams.addTeam(1, "Dev")
		h.perms.err = errors.New("db down")

		h.run(t, oauthIdentity("ops", "dev"))

		assert.Len(t, h.perms.calls, 2, "a failure in one team must not stop the others")
		assert.NotContains(t, h.teams.members, memberKey{1, dev})
	})

	t.Run("membership listing fails", func(t *testing.T) {
		h := newHarness("ops:TenantA:Ops")
		h.teams.addTeam(2, "Ops")
		h.teams.failList = true

		h.run(t, oauthIdentity("ops"))

		assert.Empty(t, h.perms.calls)
	})

	t.Run("org list fails", func(t *testing.T) {
		h := newHarness("ops:TenantA:Ops")
		h.teams.addTeam(2, "Ops")
		h.orgs.ExpectedError = errors.New("db down")

		h.run(t, oauthIdentity("ops"))

		assert.Empty(t, h.perms.calls)
	})

	t.Run("member already removed", func(t *testing.T) {
		h := newHarness("ops:TenantA:Ops")
		ops := h.teams.addTeam(2, "Ops")
		h.teams.members[memberKey{2, ops}] = true
		h.perms.err = team.ErrTeamMemberNotFound

		h.run(t, oauthIdentity())

		assert.Len(t, h.perms.calls, 1)
	})
}
