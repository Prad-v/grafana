"""End-to-end check of OAuth org mapping and team sync against Keycloak.

Every user logs in through the real authorization code flow, then the resulting
org memberships, org roles and team memberships are read back from the Grafana
admin API. Standard library only, so it runs in a plain python image.
"""

import base64
import html
import http.cookiejar
import json
import os
import re
import unittest
import urllib.error
import urllib.parse
import urllib.request

GRAFANA = os.environ["GRAFANA_INTERNAL_URL"].rstrip("/")
GRAFANA_PUBLIC = os.environ["GRAFANA_PUBLIC_URL"].rstrip("/")
KEYCLOAK = os.environ["KEYCLOAK_INTERNAL_URL"].rstrip("/")
KEYCLOAK_PUBLIC = os.environ["KEYCLOAK_PUBLIC_URL"].rstrip("/")
REALM = os.environ["KEYCLOAK_REALM"]
PASSWORD = os.environ["SSO_TEST_PASSWORD"]
if not PASSWORD or not os.environ["KEYCLOAK_ADMIN_PASSWORD"]:
    raise RuntimeError("SSO_TEST_PASSWORD and KEYCLOAK_ADMIN_PASSWORD are empty, run this with `make sso-test`")
ADMIN_AUTH = base64.b64encode(
    f"{os.environ['GF_SECURITY_ADMIN_USER']}:{os.environ['GF_SECURITY_ADMIN_PASSWORD']}".encode()
).decode()

MAIN_ORG = "Main Org."

# user -> ({org name: role}, {org name: sorted team names})
EXPECTED = {
    "alice": ({"TenantA": "Admin"}, {"TenantA": ["Ops"]}),
    "bob": ({"TenantA": "Editor"}, {"TenantA": ["Developers"]}),
    "carol": ({"TenantB": "Viewer"}, {"TenantB": ["Ops"]}),
    "dave": ({"TenantA": "Editor", "TenantB": "Viewer"}, {"TenantB": ["Ops"]}),
    "erin": ({MAIN_ORG: "Admin", "TenantA": "Admin", "TenantB": "Admin"}, {}),
    "frank": ({MAIN_ORG: "Viewer"}, {}),
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _internal(url):
    """Map the browser-facing URLs in redirects and forms to the compose network."""
    return url.replace(KEYCLOAK_PUBLIC, KEYCLOAK, 1).replace(GRAFANA_PUBLIC, GRAFANA, 1)


def _request(opener, method, url, data=None, headers=None):
    """Returns (status, headers, body) without following redirects or raising on 3xx/4xx."""
    req = urllib.request.Request(
        url,
        method=method,
        data=urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data,
        headers=headers or {},
    )
    try:
        resp = opener.open(req, timeout=30)
    except urllib.error.HTTPError as err:
        resp = err
    return resp.status if hasattr(resp, "status") else resp.code, resp.headers, resp.read().decode()


def _json(method, url, body=None, headers=None):
    opener = urllib.request.build_opener()
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    status, _, text = _request(opener, method, url, json.dumps(body).encode() if body is not None else None, hdrs)
    if status >= 400:
        raise AssertionError(f"{method} {url} -> {status}: {text}")
    return json.loads(text) if text else None


def grafana_admin(method, path, body=None, org_id=None):
    headers = {"Authorization": f"Basic {ADMIN_AUTH}"}
    if org_id is not None:
        headers["X-Grafana-Org-Id"] = str(org_id)
    return _json(method, GRAFANA + path, body, headers)


def oauth_login(username):
    """Runs the authorization code flow like a browser would. Returns the Grafana login name."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), _NoRedirect)

    status, headers, _ = _request(opener, "GET", GRAFANA + "/login/generic_oauth")
    assert status == 302, f"expected redirect to Keycloak, got {status}"

    status, _, page = _request(opener, "GET", _internal(headers["Location"]))
    assert status == 200, f"Keycloak login page returned {status}"
    action = re.search(r'<form[^>]+id="kc-form-login"[^>]+action="([^"]+)"', page)
    assert action, "Keycloak login form not found"

    status, headers, body = _request(
        opener,
        "POST",
        _internal(html.unescape(action.group(1))),
        {"username": username, "password": PASSWORD, "credentialId": ""},
    )
    assert status == 302, f"Keycloak rejected {username}: {status} {body[:200]}"

    callback = _internal(headers["Location"])
    assert callback.startswith(GRAFANA), f"unexpected redirect target {callback}"
    status, headers, _ = _request(opener, "GET", callback)
    assert status == 302, f"Grafana callback for {username} returned {status}"
    assert "/login" not in headers["Location"], f"Grafana refused {username}: {headers['Location']}"

    status, _, body = _request(opener, "GET", GRAFANA + "/api/user")
    assert status == 200, f"no Grafana session for {username}: {status}"
    return json.loads(body)["login"]


def grafana_state(username):
    """Org roles and team names of a user as Grafana stores them."""
    user = grafana_admin("GET", "/api/users/lookup?loginOrEmail=" + urllib.parse.quote(username))
    orgs = grafana_admin("GET", f"/api/users/{user['id']}/orgs")

    roles = {o["name"]: o["role"] for o in orgs}
    teams = {}
    for org in orgs:
        search = grafana_admin("GET", "/api/teams/search?perpage=100", org_id=org["orgId"])
        for team in search["teams"]:
            members = grafana_admin("GET", f"/api/teams/{team['id']}/members", org_id=org["orgId"])
            if any(m["login"] == username for m in members):
                teams.setdefault(org["name"], []).append(team["name"])
    return roles, {org: sorted(names) for org, names in teams.items()}


class KeycloakAdmin:
    """Just enough of the Keycloak admin API to change a user's group membership."""

    def __init__(self):
        token = _json_form(
            f"{KEYCLOAK}/realms/master/protocol/openid-connect/token",
            {
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": os.environ["KEYCLOAK_ADMIN"],
                "password": os.environ["KEYCLOAK_ADMIN_PASSWORD"],
            },
        )["access_token"]
        self.headers = {"Authorization": f"Bearer {token}"}

    def _user_id(self, username):
        users = _json("GET", f"{KEYCLOAK}/admin/realms/{REALM}/users?username={username}&exact=true", headers=self.headers)
        return users[0]["id"]

    def _group_id(self, name):
        groups = _json("GET", f"{KEYCLOAK}/admin/realms/{REALM}/groups?search={name}&exact=true", headers=self.headers)
        return groups[0]["id"]

    def leave_group(self, username, group):
        uid, gid = self._user_id(username), self._group_id(group)
        _json("DELETE", f"{KEYCLOAK}/admin/realms/{REALM}/users/{uid}/groups/{gid}", headers=self.headers)

    def join_group(self, username, group):
        uid, gid = self._user_id(username), self._group_id(group)
        _json("PUT", f"{KEYCLOAK}/admin/realms/{REALM}/users/{uid}/groups/{gid}", headers=self.headers)


def _json_form(url, form):
    status, _, text = _request(urllib.request.build_opener(), "POST", url, form)
    assert status == 200, f"POST {url} -> {status}: {text}"
    return json.loads(text)


class OrgMappingAndTeamSync(unittest.TestCase):
    def test_each_user_lands_in_the_right_orgs_and_teams(self):
        for username, (want_roles, want_teams) in EXPECTED.items():
            with self.subTest(user=username):
                self.assertEqual(oauth_login(username), username)
                roles, teams = grafana_state(username)
                self.assertEqual(roles, want_roles, "org memberships and roles")
                self.assertEqual(teams, want_teams, "team memberships")

    def test_tenants_stay_isolated(self):
        for username in ("alice", "bob"):
            oauth_login(username)
            self.assertNotIn("TenantB", grafana_state(username)[0])
        oauth_login("carol")
        roles, teams = grafana_state("carol")
        self.assertNotIn("TenantA", roles)
        self.assertNotIn("TenantA", teams)

    def test_login_is_idempotent(self):
        oauth_login("alice")
        first = grafana_state("alice")
        oauth_login("alice")
        self.assertEqual(grafana_state("alice"), first)

    def test_leaving_an_idp_group_removes_the_team_on_next_login(self):
        keycloak = KeycloakAdmin()
        oauth_login("alice")
        self.assertEqual(grafana_state("alice")[1], {"TenantA": ["Ops"]})

        keycloak.leave_group("alice", "tenant-a-ops")
        self.addCleanup(keycloak.join_group, "alice", "tenant-a-ops")
        oauth_login("alice")
        roles, teams = grafana_state("alice")
        self.assertEqual(roles, {"TenantA": "Admin"}, "org role must not change")
        self.assertEqual(teams, {}, "Ops membership must be removed")

        keycloak.join_group("alice", "tenant-a-ops")
        oauth_login("alice")
        self.assertEqual(grafana_state("alice")[1], {"TenantA": ["Ops"]}, "membership must come back")

    def test_manual_team_membership_survives_login(self):
        oauth_login("bob")
        bob_id = grafana_admin("GET", "/api/users/lookup?loginOrEmail=bob")["id"]
        org_id = next(o["orgId"] for o in grafana_admin("GET", f"/api/users/{bob_id}/orgs") if o["name"] == "TenantA")
        teams = grafana_admin("GET", "/api/teams/search?perpage=100", org_id=org_id)["teams"]
        ops = next(t for t in teams if t["name"] == "Ops")

        grafana_admin("POST", f"/api/teams/{ops['id']}/members", {"userId": bob_id}, org_id=org_id)
        self.addCleanup(grafana_admin, "DELETE", f"/api/teams/{ops['id']}/members/{bob_id}", None, org_id)

        oauth_login("bob")  # bob has no tenant-a-ops group
        self.assertEqual(grafana_state("bob")[1], {"TenantA": ["Developers", "Ops"]})


if __name__ == "__main__":
    unittest.main()
