#!/usr/bin/python3
"""Print the Homebrew formula for a released tag, with its real sha256.

Releasing goes:

    1. bump VERSION in reminders_ha_sync.py
    2. commit, then `git tag v0.2.0 && git push --tags`
    3. `make formula TAG=v0.2.0`
    4. paste the output over Formula/reminders-ha-sync.rb in the tap

Step 3 needs the tag to be on GitHub already -- the checksum is of the tarball
GitHub generates, so it cannot be computed from the working tree.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import sys
import tarfile
import urllib.error
import urllib.request

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORMULA_PATH = os.path.join(PROJECT_DIR, "Formula", "reminders-ha-sync.rb")
SCRIPT_PATH = os.path.join(PROJECT_DIR, "reminders_ha_sync.py")

DEFAULT_REPO = "sabbaken/apple-reminders-sync-to-home-assistant"


def script_version() -> str:
    with open(SCRIPT_PATH, encoding="utf-8") as fh:
        match = re.search(r'^VERSION = "([^"]+)"', fh.read(), re.M)
    if not match:
        raise SystemExit("could not find VERSION in %s" % SCRIPT_PATH)
    return match.group(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="the released tag, e.g. v0.2.0")
    parser.add_argument("--repo", default=os.environ.get("RHS_REPO", DEFAULT_REPO))
    args = parser.parse_args()

    tag = args.tag
    expected = script_version()
    if tag.lstrip("v") != expected:
        print(
            "warning: tag %s does not match VERSION %s in reminders_ha_sync.py"
            % (tag, expected),
            file=sys.stderr,
        )

    url = "https://github.com/%s/archive/refs/tags/%s.tar.gz" % (args.repo, tag)
    print("fetching %s" % url, file=sys.stderr)
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            payload = response.read()
            content_type = response.headers.get("Content-Type", "?")
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            "%s returned %d. Has the tag been pushed?" % (url, exc.code)
        ) from None
    except urllib.error.URLError as exc:
        raise SystemExit("could not fetch %s: %s" % (url, exc.reason)) from None

    # GitHub answers 200 with an HTML page for a tag or repo that does not
    # exist, so the status code proves nothing. Checksumming that page would
    # produce a formula that fails for every user, so check what arrived.
    if not payload.startswith(b"\x1f\x8b"):
        raise SystemExit(
            "%s did not return a tarball -- got %s, %d bytes.\n"
            "Either the tag is not pushed yet or the repository is private."
            % (url, content_type, len(payload))
        )

    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        members = archive.getnames()
        script = next(
            (m for m in members if m.endswith("/reminders_ha_sync.py")), None
        )
        if script is None:
            raise SystemExit(
                "%s is a tarball but has no reminders_ha_sync.py in it" % url
            )
        handle = archive.extractfile(script)
        released = re.search(
            r'^VERSION = "([^"]+)"', handle.read().decode("utf-8"), re.M
        )

    # Check the tag against what a user would actually download, not against the
    # working tree -- tagging before bumping VERSION is the easy mistake.
    if released and released.group(1) != tag.lstrip("v"):
        raise SystemExit(
            "tag %s contains VERSION %s. Bump VERSION, re-tag, and try again."
            % (tag, released.group(1))
        )

    digest = hashlib.sha256(payload).hexdigest()
    print("sha256 %s" % digest, file=sys.stderr)
    print(file=sys.stderr)

    with open(FORMULA_PATH, encoding="utf-8") as fh:
        formula = fh.read()

    formula, count = re.subn(r'^(\s*)url ".*"$', r'\1url "%s"' % url, formula, flags=re.M)
    if count != 1:
        raise SystemExit("expected exactly one url line in the formula, found %d" % count)
    formula, count = re.subn(
        r'^(\s*)sha256 "[0-9a-f]*"$', r'\1sha256 "%s"' % digest, formula, flags=re.M
    )
    if count != 1:
        raise SystemExit("expected exactly one sha256 line in the formula, found %d" % count)

    sys.stdout.write(formula)
    return 0


if __name__ == "__main__":
    sys.exit(main())
