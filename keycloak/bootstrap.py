"""Create the tenant organizations Grafana maps OAuth groups to.

Idempotent. Prints the orgs it created. Configuration comes from the environment:
GRAFANA_URL, GF_SECURITY_ADMIN_USER, GF_SECURITY_ADMIN_PASSWORD, TENANT_ORGS.
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

GRAFANA_URL = os.environ["GRAFANA_URL"].rstrip("/")
AUTH = base64.b64encode(
    f"{os.environ['GF_SECURITY_ADMIN_USER']}:{os.environ['GF_SECURITY_ADMIN_PASSWORD']}".encode()
).decode()


def call(method, path, body=None):
    req = urllib.request.Request(
        GRAFANA_URL + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Basic {AUTH}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def main():
    for name in [o.strip() for o in os.environ["TENANT_ORGS"].split(",") if o.strip()]:
        try:
            org = call("GET", "/api/orgs/name/" + urllib.parse.quote(name))
            print(f"org {name!r} exists (id={org['id']})")
        except urllib.error.HTTPError as err:
            if err.code != 404:
                raise
            org = call("POST", "/api/orgs", {"name": name})
            print(f"org {name!r} created (id={org['orgId']})")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - report any failure to the make target
        print(f"bootstrap failed: {exc}", file=sys.stderr)
        sys.exit(1)
