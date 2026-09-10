#!/usr/bin/env python3
"""Directory grants: the one capability here that can grow its own privileges.

Every other capability is fixed at config time.  This one changes while the
assistant runs, so a bug does not leak what the model was allowed to see -- it
leaks whatever the model can *talk someone into* approving next.  The rules
below are deliberately hostile to the model and friendly to the human, and that
ordering is the whole design.

THE MODEL CANNOT APPROVE ANYTHING
    request() only records a pending request.  There is no code path from a tool
    call to a grant: approve() is reachable only from the page, over an HTTP
    route the model has no tool for.  A tool that could grant would make every
    other control in this file decorative.

APPROVAL IS OF A REALPATH, NOT OF A STRING
    The model asks with text, and text can be "~/projects" or
    "~/projects/../.ssh" or a symlink into a root.  What is stored, and what the
    deny-list is checked against, is os.path.realpath() of the request -- so the
    thing a human approved is the thing that gets opened.

WHAT A HUMAN SEES BEFORE CLICKING
    describe() reports the file count and any credential-looking names, so the
    blast radius is legible *before* the grant rather than after.  That is the
    entire point of the card.

VOICE ASKS, THE PAGE CONFIRMS
    The assistant may speak the question; the grant needs a click.  The
    microphone is not an authenticator -- anyone in the room, a podcast, a TV
    can say "yes" -- and this page is reachable over the LAN.  Voice-only
    approval is a deliberate future flag, not an oversight here.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

# No click can approve these.  They are checked against the realpath, so a
# symlink or a ".." cannot walk into them from an allowed neighbour.
#
# /home and /mnt are deliberately absent: refusing them would refuse every
# ordinary project directory.  Home itself is refused by name in refusal(), and
# the credential component list is what stops ~/.ssh and friends.
NEVER_PREFIXES = ("/etc", "/proc", "/sys", "/dev", "/root", "/boot", "/run", "/usr", "/bin",
                  "/sbin", "/lib", "/lib64", "/opt", "/var/lib", "/var/log")
NEVER_COMPONENTS = {".ssh", ".aws", ".gnupg", ".azure", ".config", ".docker", ".kube", ".netrc",
                    ".git-credentials", "credentials", "secrets", ".mozilla", ".thunderbird"}

# Names that make a human want to read twice before approving.
CREDENTIAL_HINTS = ("key", "secret", "token", "password", "passwd", "credential", "id_rsa",
                    "id_ed25519", ".env", "apikey", "api_key", "bearer", "private")

MAX_WALK = 20000          # describe() must not become a denial of service
MAX_HINTS = 8


class ApprovalError(Exception):
    pass


def canonical(path) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))


def _home() -> str:
    return os.path.realpath(os.path.expanduser("~"))


def refusal(path) -> str:
    """Why this path may never be granted, or "" if it is not forbidden."""
    real = canonical(path)
    if real == "/":
        return "the whole filesystem cannot be approved"
    if real == _home():
        return f"an entire home directory ({real}) cannot be approved"
    for prefix in NEVER_PREFIXES:
        if real == prefix or real.startswith(prefix.rstrip("/") + "/"):
            return f"{real} is inside {prefix}, which cannot be approved"
    for component in [part for part in real.split(os.sep) if part]:
        if component.lower() in NEVER_COMPONENTS:
            return f"{real} contains {component!r}, which cannot be approved"
    return ""


def describe(path) -> dict:
    """What granting this would actually expose.  Bounded, and says so if cut short."""
    real = canonical(path)
    out = {"realpath": real, "files": 0, "truncated": False, "hints": [], "error": ""}
    if not os.path.isdir(real):
        out["error"] = "not a directory"
        return out
    for dirpath, dirnames, filenames in os.walk(real):
        # Do not follow links out of the tree: the count describes this
        # directory, and a link to / would report the whole filesystem as its
        # own blast radius.
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            out["files"] += 1
            lowered = name.lower()
            if out["files"] <= 4000 and any(hint in lowered for hint in CREDENTIAL_HINTS):
                if len(out["hints"]) < MAX_HINTS:
                    out["hints"].append(os.path.relpath(os.path.join(dirpath, name), real))
            if out["files"] >= MAX_WALK:
                out["truncated"] = True
                return out
    return out


class Store:
    """var/approvals.json: pending requests and granted roots."""

    def __init__(self, path):
        self.path = Path(path)

    # -- persistence -------------------------------------------------------
    def _read(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {"pending": [], "granted": []}
        except ValueError as error:
            raise ApprovalError(f"the approvals file is unreadable: {error}") from error
        if not isinstance(data, dict) or not isinstance(data.get("pending"), list) \
                or not isinstance(data.get("granted"), list):
            raise ApprovalError("the approvals file has an unexpected shape")
        return data

    def _write(self, data: dict) -> None:
        # Write-then-rename.  A crash mid-write would otherwise leave a
        # truncated approvals file and lose every grant the assistant was given.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)

    # -- the model-facing half: it may only ever *ask* ---------------------
    def request(self, path, reason: str = "") -> dict:
        real = canonical(path)
        blocked = refusal(real)
        if blocked:
            raise ApprovalError(blocked)
        if not os.path.isdir(real):
            raise ApprovalError(f"{real} is not a directory")
        data = self._read()
        if any(entry.get("realpath") == real for entry in data["granted"]):
            return {"already_granted": True, "realpath": real}
        existing = next((entry for entry in data["pending"] if entry.get("realpath") == real), None)
        if existing is None:
            existing = {"id": uuid.uuid4().hex[:12], "realpath": real,
                        "asked_at": int(time.time()), "reason": str(reason or "")[:300],
                        **describe(real)}
            data["pending"].append(existing)
            self._write(data)
        return {"already_granted": False, **existing}

    # -- the human-facing half: reachable only from the page ---------------
    def approve(self, request_id: str) -> dict:
        data = self._read()
        pending = next((entry for entry in data["pending"] if entry.get("id") == request_id), None)
        if pending is None:
            raise ApprovalError("that request no longer exists")
        # Re-check at approval time, not just at request time: the request was
        # made earlier, and a path can have been moved or re-linked since.
        blocked = refusal(pending["realpath"])
        if blocked:
            data["pending"] = [e for e in data["pending"] if e.get("id") != request_id]
            self._write(data)
            raise ApprovalError(blocked)
        real = canonical(pending["realpath"])
        if not os.path.isdir(real):
            raise ApprovalError(f"{real} is not a directory any more")
        data["pending"] = [entry for entry in data["pending"] if entry.get("id") != request_id]
        if not any(entry.get("realpath") == real for entry in data["granted"]):
            data["granted"].append({"realpath": real, "granted_at": int(time.time()),
                                    "granted_via": "page",
                                    "files": describe(real)["files"]})
            self._write(data)
        return {"granted": real}

    def decline(self, request_id: str) -> dict:
        """Drop a pending request without granting it.

        Without this, the only way to make an unwanted request disappear is to
        approve it -- a user interface that pressures a person into the one
        action this whole file exists to make hard.  Declining is cheap and
        reversible: the assistant can simply ask again.
        """
        data = self._read()
        before = len(data["pending"])
        data["pending"] = [entry for entry in data["pending"] if entry.get("id") != request_id]
        if len(data["pending"]) == before:
            raise ApprovalError("that request no longer exists")
        self._write(data)
        return {"declined": True}

    def revoke(self, path) -> dict:
        real = canonical(path)
        data = self._read()
        before = len(data["granted"])
        data["granted"] = [entry for entry in data["granted"] if entry.get("realpath") != real]
        if len(data["granted"]) == before:
            raise ApprovalError(f"{real} was not granted")
        self._write(data)
        return {"revoked": real}

    # -- reads -------------------------------------------------------------
    def pending(self) -> list:
        return list(self._read()["pending"])

    def granted(self) -> list:
        return list(self._read()["granted"])

    def granted_roots(self) -> list:
        """Realpaths to hand the file server.  Directories that vanished are
        dropped from the answer rather than crashing a read-only server."""
        return [entry["realpath"] for entry in self.granted()
                if isinstance(entry.get("realpath"), str) and os.path.isdir(entry["realpath"])]
