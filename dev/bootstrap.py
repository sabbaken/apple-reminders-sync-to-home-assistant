#!/usr/bin/env python3
"""Bring the dev Home Assistant instance to a state the sync script can use.

Idempotent: run it as often as you like. It walks HA's onboarding API to create
the dev user, stores the resulting refresh token in dev/secrets.json, creates a
Local To-do list per test list, and writes dev/config.json for the sync script.

Access tokens minted from a refresh token live 30 minutes, which is why every
Makefile target that talks to HA runs this first -- it re-mints on each run.
A permanent token is not needed here; for a real instance you create a
long-lived one in the HA profile UI instead.

    ./dev/bootstrap.py                 onboard + create lists + write config
    ./dev/bootstrap.py --print-token   just print a fresh access token
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEV_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(DEV_DIR)

# A loopback *IP* on purpose: HA's IndieAuth treats loopback client_ids as local
# and skips trying to fetch the client_id URL to discover redirect_uris, which
# is what would happen with "localhost" and would fail here.
BASE_URL = os.environ.get("RHS_DEV_HA_URL", "http://127.0.0.1:8124")
CLIENT_ID = BASE_URL + "/"

DEV_USER = {"name": "Dev", "username": "dev", "password": "devdevdev"}

# One ASCII list, one Cyrillic one -- the Cyrillic list is what catches
# URL-encoding and entity-id slug problems that ASCII names hide. Note that HA
# transliterates when it slugifies, so a Cyrillic name must not transliterate
# into an existing one ("RHS Тест" -> rhs_test, colliding with "RHS Test").
TEST_LISTS = [
    {"reminders": "RHS Test", "ha_name": "RHS Test"},
    {"reminders": "RHS Покупки", "ha_name": "RHS Покупки"},
]

SECRETS_PATH = os.path.join(DEV_DIR, "secrets.json")
CONFIG_PATH = os.path.join(DEV_DIR, "config.json")


class HttpError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__("HTTP %d: %s" % (status, body[:400]))
        self.status = status
        self.body = body


def request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    json_body: object = None,
    form_body: dict | None = None,
    timeout: float = 30.0,
) -> object:
    url = BASE_URL + path
    data = None
    headers = {"Accept": "application/json"}

    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif form_body is not None:
        data = urllib.parse.urlencode(form_body).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    if token:
        headers["Authorization"] = "Bearer " + token

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise HttpError(exc.code, exc.read().decode("utf-8", "replace")) from None

    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def core_api_up() -> bool:
    """True once the core REST API is serving, onboarded or not.

    Unauthenticated it answers 401, which is proof enough that HA is up; while
    it is still booting the route does not exist yet and answers 404.
    """
    try:
        request("GET", "/api/", timeout=5)
    except HttpError as exc:
        return exc.status != 404
    except OSError:
        return False
    return True


def wait_for_ha(timeout: float = 300.0) -> None:
    """Block until HA is serving, i.e. it finished booting."""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            request("GET", "/api/onboarding", timeout=5)
            return
        except HttpError as exc:
            # Any HTTP status means the server is up; onboarding may still be
            # loading, so keep polling on 404 -- except that HA *removes* the
            # onboarding views once onboarding is complete, so a 404 also means
            # a fully booted instance. The core API tells the two apart.
            if exc.status != 404 or core_api_up():
                return
            last = str(exc)
        except OSError as exc:
            last = str(exc)
        time.sleep(2)
    raise SystemExit("home assistant did not come up at %s (%s)" % (BASE_URL, last))


def onboarding_steps() -> dict | None:
    """Map step -> done, or None once HA has taken the onboarding views away."""
    try:
        steps = request("GET", "/api/onboarding")
    except HttpError as exc:
        if exc.status == 404:
            return None
        raise
    return {s["step"]: s["done"] for s in steps}


def exchange(grant: dict) -> dict:
    return request("POST", "/auth/token", form_body=dict(grant, client_id=CLIENT_ID))


def load_secrets() -> dict:
    if not os.path.exists(SECRETS_PATH):
        return {}
    with open(SECRETS_PATH) as fh:
        return json.load(fh)


def save_secrets(secrets: dict) -> None:
    with open(SECRETS_PATH, "w") as fh:
        json.dump(secrets, fh, indent=2)
    os.chmod(SECRETS_PATH, 0o600)


def onboard() -> str:
    """Return a refresh token for the dev user, creating it if needed."""
    secrets = load_secrets()
    steps = onboarding_steps()

    # No steps at all means the views are gone, which HA does only after
    # onboarding finishes -- the same situation as the user step being done.
    if steps is None or steps.get("user"):
        if not secrets.get("refresh_token"):
            raise SystemExit(
                "HA is already onboarded but %s has no refresh token.\n"
                "Run `make ha-reset && make ha-up` to start from scratch."
                % os.path.relpath(SECRETS_PATH, PROJECT_DIR)
            )
        return secrets["refresh_token"]

    result = request(
        "POST",
        "/api/onboarding/users",
        json_body=dict(DEV_USER, client_id=CLIENT_ID, language="en"),
    )
    tokens = exchange({"grant_type": "authorization_code", "code": result["auth_code"]})
    secrets["refresh_token"] = tokens["refresh_token"]
    save_secrets(secrets)
    print("created dev user %s / %s" % (DEV_USER["username"], DEV_USER["password"]))

    access = tokens["access_token"]
    # Finish the remaining steps so the UI does not drop into the onboarding
    # wizard when you open it. None of them are needed for the REST API.
    for step, body in (
        ("core_config", {}),
        ("analytics", {}),
        ("integration", {"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID}),
    ):
        try:
            request("POST", "/api/onboarding/" + step, token=access, json_body=body)
        except HttpError as exc:
            print("note: onboarding step %s: %s" % (step, exc))

    return secrets["refresh_token"]


def mint_access_token(refresh_token: str) -> str:
    tokens = exchange({"grant_type": "refresh_token", "refresh_token": refresh_token})
    return tokens["access_token"]


def todo_entities(token: str) -> dict:
    """Map friendly_name -> entity_id for every todo entity."""
    states = request("GET", "/api/states", token=token)
    return {
        s["attributes"].get("friendly_name", s["entity_id"]): s["entity_id"]
        for s in states
        if s["entity_id"].startswith("todo.")
    }


def create_local_todo(token: str, name: str) -> None:
    flow = request(
        "POST",
        "/api/config/config_entries/flow",
        token=token,
        json_body={"handler": "local_todo", "show_advanced_options": False},
    )
    result = request(
        "POST",
        "/api/config/config_entries/flow/" + flow["flow_id"],
        token=token,
        json_body={"todo_list_name": name},
    )
    if result.get("type") != "create_entry":
        raise SystemExit("could not create list %r: %s" % (name, json.dumps(result)))


def ensure_lists(token: str) -> list:
    """Create a Local To-do list per test list; return sync-config list pairs."""
    existing = todo_entities(token)
    missing = [spec for spec in TEST_LISTS if spec["ha_name"] not in existing]

    for spec in missing:
        create_local_todo(token, spec["ha_name"])
        print("created HA todo list %r" % spec["ha_name"])

    if missing:
        # The entity registry needs a moment to surface the new entities.
        deadline = time.time() + 30
        while time.time() < deadline:
            existing = todo_entities(token)
            if all(spec["ha_name"] in existing for spec in TEST_LISTS):
                break
            time.sleep(1)

    pairs = []
    for spec in TEST_LISTS:
        entity_id = existing.get(spec["ha_name"])
        if not entity_id:
            raise SystemExit("HA todo entity for %r never appeared" % spec["ha_name"])
        pairs.append({"reminders": spec["reminders"], "ha": entity_id})
    return pairs


def write_dev_config(token: str, pairs: list) -> None:
    config = {
        "home_assistant": {"url": BASE_URL, "token": token},
        "lists": pairs,
        "conflict_winner": "reminders",
        "due_from_ha": "revert",
        "log_level": "debug",
    }
    with open(CONFIG_PATH, "w") as fh:
        json.dump(config, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.chmod(CONFIG_PATH, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--print-token",
        action="store_true",
        help="print a fresh access token and exit",
    )
    args = parser.parse_args()

    wait_for_ha()
    refresh_token = onboard()
    token = mint_access_token(refresh_token)

    if args.print_token:
        print(token)
        return 0

    pairs = ensure_lists(token)
    write_dev_config(token, pairs)

    print("dev HA ready at %s" % BASE_URL)
    print("  login:  %s / %s" % (DEV_USER["username"], DEV_USER["password"]))
    print("  config: %s" % os.path.relpath(CONFIG_PATH, PROJECT_DIR))
    for pair in pairs:
        print("  %-12s <-> %s" % (pair["reminders"], pair["ha"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
