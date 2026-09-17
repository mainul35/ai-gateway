#!/usr/bin/env python3
"""Creates Cloudflare Access applications that protect the gateway's web UI.

The API endpoints (/v1), the health check and our own SSO routes stay open, because API clients
authenticate with gateway API keys and Access would reject them. Everything else -- the dashboard,
user management, settings, admin API and /docs -- requires your identity at Cloudflare's edge.

Credentials are read from the environment, never passed on the command line:
    CF_API_TOKEN    API token with "Access: Apps and Policies: Edit" on the account
    CF_ACCOUNT_ID   Cloudflare account id
    GATEWAY_DOMAIN  e.g. ai-gateway.mainul35.dev
    ACCESS_EMAILS   comma separated emails allowed in (e.g. you@example.com)

Run it again after changing ACCESS_EMAILS; existing applications are updated in place.

    python3 scripts/cloudflare_access.py [--dry-run]
"""
import json
import os
import sys
import urllib.error
import urllib.request

API_ROOT = "https://api.cloudflare.com/client/v4"
SESSION_DURATION = "24h"


def api(method, path, token, payload=None):
    request = urllib.request.Request(
        f"{API_ROOT}{path}", method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.load(response)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        raise SystemExit(f"Cloudflare API {method} {path} failed ({e.code}): {detail}")
    if not body.get("success", False):
        raise SystemExit(f"Cloudflare API {method} {path} returned errors: {body.get('errors')}")
    return body.get("result")


def desired_applications(domain, emails):
    everyone = [{"everyone": {}}]
    allowed = [{"email": {"email": address}} for address in emails]
    # Cloudflare matches the most specific path first, so these bypasses win over the host-wide rule
    return [
        {"name": "Model Gateway API (open)", "domain": f"{domain}/v1",
         "policies": [{"name": "Open to API clients", "decision": "bypass", "include": everyone}]},
        {"name": "Model Gateway health (open)", "domain": f"{domain}/health",
         "policies": [{"name": "Open for monitoring", "decision": "bypass", "include": everyone}]},
        {"name": "Model Gateway SSO routes (open)", "domain": f"{domain}/auth",
         "policies": [{"name": "Open for the gateway's own sign-in", "decision": "bypass", "include": everyone}]},
        {"name": "Model Gateway UI", "domain": domain,
         "policies": [{"name": "Allowed people", "decision": "allow", "include": allowed}]},
    ]


def main():
    dry_run = "--dry-run" in sys.argv
    token = os.getenv("CF_API_TOKEN")
    account = os.getenv("CF_ACCOUNT_ID")
    domain = os.getenv("GATEWAY_DOMAIN")
    emails = [e.strip() for e in (os.getenv("ACCESS_EMAILS") or "").split(",") if e.strip()]

    missing = [name for name, value in
               (("CF_API_TOKEN", token), ("CF_ACCOUNT_ID", account), ("GATEWAY_DOMAIN", domain),
                ("ACCESS_EMAILS", emails)) if not value]
    if missing:
        raise SystemExit(f"Missing: {', '.join(missing)}")

    wanted = desired_applications(domain, emails)
    if dry_run:
        for app in wanted:
            decisions = ", ".join(f"{p['decision']} ({len(p['include'])} rule)" for p in app["policies"])
            print(f"  {app['domain']:<45} {decisions}")
        return

    existing = {app["name"]: app for app in api("GET", f"/accounts/{account}/access/apps", token) or []}

    for app in wanted:
        body = {"name": app["name"], "domain": app["domain"], "type": "self_hosted",
                "session_duration": SESSION_DURATION, "app_launcher_visible": False}
        current = existing.get(app["name"])
        if current:
            result = api("PUT", f"/accounts/{account}/access/apps/{current['id']}", token, body)
            action = "updated"
        else:
            result = api("POST", f"/accounts/{account}/access/apps", token, body)
            action = "created"
        app_id = result["id"]

        # Replace the policies so re-running matches exactly what this script describes
        for policy in api("GET", f"/accounts/{account}/access/apps/{app_id}/policies", token) or []:
            api("DELETE", f"/accounts/{account}/access/apps/{app_id}/policies/{policy['id']}", token)
        for index, policy in enumerate(app["policies"], start=1):
            api("POST", f"/accounts/{account}/access/apps/{app_id}/policies", token,
                {"name": policy["name"], "decision": policy["decision"],
                 "include": policy["include"], "precedence": index})
        print(f"  {action}: {app['domain']} -> {app['policies'][0]['decision']}")

    print("\nDone. Check an incognito window: the UI should ask for identity, /v1 and /health should not.")


if __name__ == "__main__":
    main()
