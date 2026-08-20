"""Probe specs for this server's tools — the Box-specific half of the smoke test.

Every registered tool needs an entry here (the harness fails on a tool with no
spec), so adding a tool forces a decision: how would we know it works?

Three constraints shape everything below.

**Read-only.** Every tool here reads; none of them changes anything in Box. A
future tool that does must be skipped by name, and the test suite enforces
that.

**No enterprise-specific values in this file.** This repository is public, so a
probe may not name a real account, folder, domain or item. Two literals identify
nothing and are therefore allowed: folder id ``"0"`` — Box's root folder, the
same in every enterprise — and ``ABSENT_LOGIN``, a made-up term no account can
hold, which is what the per-user lookup is probed with.

**Bounded.** These tools page through the event stream and walk the folder
tree; their defaults (5000 events, 150 folders) are sized for a human asking
once. Every probe passes a small explicit cap instead, so a scheduled run costs
a known, small number of API calls.

Assertions are envelope-first. A quiet enterprise is a real and desirable
observation — no external collaborators is the goal, not a malfunction — so a
probe asserts the accounting fields the tool always returns (``count``,
``folders_scanned``, ``window_hours``) rather than requiring a non-empty
result. What must never appear is the ``{"error": ...}`` shape these tools
return in place of raising.
"""

from typing import Any

from smoke_harness import Probe

#: Box's root folder. The same id in every enterprise, so naming it here
#: reveals nothing while still exercising the tree walk.
ROOT_FOLDER = "0"

#: Bounds shared by the three folder-tree scanners. Depth 1 with a small folder
#: cap is enough to prove the walk, the external-domain classification and the
#: capping logic all run; measuring the whole tree is a separate, deliberate
#: operation.
SCAN_BOUNDS: dict[str, Any] = {"root_folder_id": ROOT_FOLDER, "max_folders": 25, "max_depth": 1}

#: The scanners always report how far they got, so a probe can assert the
#: accounting without requiring the enterprise to be misconfigured.
SCAN_KEYS = ("folders_scanned", "capped", "fetch_errors")

#: A login that no account can hold, used to exercise the per-user lookup without
#: naming anybody: it exercises the exact-match filter and the not-found path,
#: which is where a fuzzy hit would otherwise be reported as an answer.
#:
#: Email-SHAPED on purpose. ``get_user`` refuses a term that is not, before the
#: term ever reaches Box, so a bare word would make this probe assert the
#: not-found shape against a validation error and fail on every live run. The
#: domain is reserved by RFC 2606, so no enterprise can hold this login — which
#: is also what keeps it clear of the test that scans this file for address
#: literals.
ABSENT_LOGIN = "boxadm-mcp-smoke-probe-no-such-account@example.invalid"

#: A scan swallows a per-folder API failure into ``fetch_errors`` rather than
#: raising, so a revoked scope produces an answer whose envelope is complete
#: and whose contents are empty. Requiring the counter to be *present* misses
#: that entirely; requiring it to be zero is what separates "nothing to report"
#: from "could not look".
#:
#: ``capped`` is deliberately not forbidden: these probes cap the walk on
#: purpose, so it is true on every healthy run.
NO_FETCH_ERRORS = (r'"fetch_errors": [1-9]',)


PROBES: dict[str, Probe] = {
    # -- server / backend health ------------------------------------------
    # events_accessible is the one that matters: a valid token does not prove
    # the admin_logs scope is granted, and every analytics tool below needs it.
    "health_check": Probe(
        require_keys=("status", "service", "auth", "events_accessible"),
        must_match=(r'"auth": "ok"', r'"events_accessible": true', r'"status": "(healthy|degraded)"'),
        allow_empty=True,
    ),
    # -- raw event passthrough ---------------------------------------------
    # A one-page fetch: enough to prove the event scope answers, cheap enough
    # to repeat daily. An empty window is possible on a quiet day, so the
    # assertion is the envelope rather than the count.
    "recent_admin_events": Probe(
        args={"since_hours": 24, "limit": 25},
        require_keys=("count", "events"),
        rows_key="events",
        allow_empty=True,
    ),
    # -- access analytics ---------------------------------------------------
    "external_access_events": Probe(
        args={"since_hours": 24, "max_events": 500, "top": 5},
        require_keys=("window_hours", "events_scanned", "capped", "external_access_count"),
        rows_key="top_external_accessors",
        allow_empty=True,
    ),
    # -- exposure scans ------------------------------------------------------
    "external_collaborators": Probe(
        args=SCAN_BOUNDS,
        require_keys=(*SCAN_KEYS, "count", "external_collaborators"),
        rows_key="external_collaborators",
        allow_empty=True,
        must_not_match=NO_FETCH_ERRORS,
    ),
    "public_shared_links": Probe(
        args=SCAN_BOUNDS,
        require_keys=(*SCAN_KEYS, "count", "public_shared_links"),
        rows_key="public_shared_links",
        allow_empty=True,
        must_not_match=NO_FETCH_ERRORS,
    ),
    "top_external_sharers": Probe(
        args={**SCAN_BOUNDS, "top": 5},
        require_keys=(*SCAN_KEYS, "top_external_sharers"),
        rows_key="top_external_sharers",
        allow_empty=True,
        must_not_match=NO_FETCH_ERRORS,
    ),
    # -- per-account lookup --------------------------------------------------
    # Read-only, one request, and deliberately asks about an account that cannot
    # exist: a live enterprise cannot be probed with a real login here, and the
    # negative answer is the assertion that matters. "found": false proves the
    # user directory answered AND that the exact-match filter refused whatever
    # the prefix search returned — a tool reporting the first hit would say true.
    # allow_empty because the answer is an envelope with no list in it at all:
    # without it the harness demands list-shaped rows and fails the healthy
    # not-found response (which is what this probe is designed to receive).
    "get_user": Probe(
        args={"login": ABSENT_LOGIN},
        require_keys=("requested_login", "found", "capped", "note"),
        must_match=(r'"found": false',),
        allow_empty=True,
    ),
    # -- one folder's contents -----------------------------------------------
    # Folder "0" is the caller's own root, already the allowed literal here: it
    # names no enterprise and every account has one. Bounded explicitly (`limit`)
    # because the tool fetches a full page regardless, and asserts the disclosure
    # keys rather than any row -- a root with nothing in it is a legitimate answer
    # on a fresh tenant, so requiring an item would make the probe flaky rather
    # than strict.
    "list_folder_items": Probe(
        args={"folder_id": ROOT_FOLDER, "limit": 5},
        require_keys=("folder_id", "items", "returned", "matched", "capped", "note"),
        # The PATH form only. The address-shape test scans this file and rejects
        # a URL literal down to the bare scheme, so the host is asserted in the
        # unit tests instead (test_links_use_the_generic_host_...). The path is
        # the half that would regress here anyway.
        must_match=(r'"folder_url": ".*/folder/0"',),
        rows_key="items",
        allow_empty=True,
    ),
    # -- morning patrol ------------------------------------------------------
    # The brief runs the access analytics and the exposure scan back to back,
    # so it is the slowest tool here and the one whose composition can silently
    # lose a section — hence asserting both halves by name.
    "daily_brief": Probe(
        args={"since_hours": 24, "max_events": 500, "max_folders": 25, "max_depth": 1, "top": 5},
        require_keys=(
            "window_hours",
            "access",
            "exposure",
            "exposure.folders_scanned",
            # The partial-coverage fields are this server's central contract:
            # a capped scan or a fetch error that reads as a complete audit is
            # exactly what they exist to prevent, so a brief that drops them
            # must not pass.
            "access.capped",
            "exposure.capped",
            "exposure.fetch_errors",
        ),
        # Present is not enough: the brief reuses the same scan, so a revoked
        # scope gives it a full-looking envelope over an empty audit.
        must_not_match=NO_FETCH_ERRORS,
        allow_empty=True,
        timeout=600,
    ),
}
