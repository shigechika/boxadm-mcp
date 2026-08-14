"""Every registered tool must carry a smoke-test probe spec.

This is the CI half of the smoke test: the live run (scripts/smoke_test.py)
needs a reachable Box enterprise and a service account, but the *coverage*
question — did someone add a tool without deciding how we would know it works?
— is answerable offline, so it is enforced here on every push.
"""

import asyncio
import re
import sys
from pathlib import Path

from boxadm_mcp.server import mcp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import smoke_probes  # noqa: E402 - needs the sys.path line above
from smoke_harness import Probe  # noqa: E402

#: Literal shapes that would tie this public repository to one enterprise.
#: Named by shape rather than by value: spelling out the domain in order to
#: forbid it would put that domain here, which is what the check prevents.
#:
#: The IPv6 pattern covers both the fully written form and the compressed one.
#: A compressed match requires a hex group to the left of "::" so that a clock
#: time (12:34:56) and a Python slice (a[::2]) do not read as addresses, and
#: loopback/unspecified forms (::1, ::) are not matched at all — they identify
#: no site.
#: Names RFC 2606 / RFC 6761 reserve so they can never belong to anyone. A literal
#: under one of these cannot identify this (or any) enterprise, which is the only
#: thing the checks below exist to keep out of a public repository — so they are
#: exempt, by name. Nothing else is: the exemption is a short closed list, not a
#: pattern, precisely so it cannot quietly grow to cover a real domain.
RESERVED_DOMAINS = ("example.invalid", "example.com", "example.net", "example.org", "example.test")

ADDRESS_SHAPES = {
    "email address": r"[\w.+-]+@[\w-]+\.[\w.]+",
    "URL": r"https?://",
    # {1,} not {2,}: a bare two-label domain (example.com) is the common case
    # and was slipping through when this required a subdomain.
    "hostname": r"\b(?:[a-z0-9-]+\.){1,}(?:jp|com|org|net|edu|ac|co|io|dev)\b",
    "IPv4 address": r"\b\d{1,3}(?:\.\d{1,3}){3}\b",
    "IPv6 address": (
        r"(?i)\b[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){7}\b"
        r"|\b[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*::(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*)?"
    ),
}

#: Tool parameters whose value names a real account. A Box login is an email
#: address and would be caught by the shapes above, but the check is by key as
#: well: it states the rule rather than relying on one login happening to look
#: like one.
#:
#: ``root_folder_id`` is deliberately absent. Box numbers the root folder "0" in
#: every enterprise, so naming it identifies none.
#:
#: ``get_user``'s ``login`` is deliberately absent too, for a different reason:
#: its probe passes a made-up term no account can hold, precisely so the
#: not-found path — where a fuzzy hit would otherwise be reported as an answer —
#: is the one exercised. Banning the key would force that probe to discover a
#: real login instead, which is the opposite of what this file is protecting.
#: The address-shape scan below still refuses any real login written here.
IDENTIFIER_ARGS = {"created_by_logins"}

#: Parameters that bound how much work a tool does. A scheduled probe must pass
#: each one it is offered rather than inheriting a default sized for a human
#: asking once — these tools page the event stream and walk the folder tree.
BOUNDING_ARGS = {"since_hours", "limit", "max_events", "max_folders", "max_depth", "top"}

#: Tools that change state. The smoke test must never call these. Empty today:
#: every tool in this server reads. A future one that writes belongs here, and
#: the test below will then require its probe to be skipped.
STATE_CHANGING: set[str] = set()


def _registered_tool_names() -> set[str]:
    """Tool names from the live registry (no Box connection needed).

    ``asyncio.run`` rather than an async test: this suite has no async plugin,
    and the registry read is the only awaitable involved.
    """

    async def _names() -> set[str]:
        return {tool.name for tool in await mcp.list_tools()}

    return asyncio.run(_names())


def _server_source() -> str:
    # encoding pinned: the default is the locale's, which is cp1252 on the
    # Windows CI runner and cannot decode this source.
    return (Path(__file__).resolve().parent.parent / "boxadm_mcp" / "server.py").read_text(encoding="utf-8")


def test_every_registered_tool_has_a_probe():
    registered = _registered_tool_names()
    missing = sorted(registered - set(smoke_probes.PROBES))
    assert not missing, (
        f"Tool(s) registered with no smoke-test probe: {missing}. "
        "Add an entry to scripts/smoke_probes.py — arguments plus what a working "
        "answer looks like, or an explicit skip= reason."
    )


def test_no_probe_targets_a_removed_tool():
    registered = _registered_tool_names()
    stale = sorted(set(smoke_probes.PROBES) - registered)
    assert not stale, f"Probe spec(s) for tools that are no longer registered: {stale}"


def test_state_changing_tools_are_skipped():
    """A smoke test that changes what it is measuring is worse than none."""
    registered = _registered_tool_names()
    for name in sorted(STATE_CHANGING & registered):
        probe = smoke_probes.PROBES[name]
        assert probe.skip, f"{name} changes state and must be skipped, not exercised"


def test_probes_are_probe_instances():
    for name, probe in smoke_probes.PROBES.items():
        assert isinstance(probe, Probe), f"{name} is not a Probe"


def test_expensive_tools_are_probed_within_explicit_bounds():
    """A scheduled probe must not inherit a scan tool's interactive defaults.

    These tools page the admin event stream and walk the folder tree; their own
    defaults (5000 events, 150 folders) are a reasonable answer for a human
    asking once and far too much work to repeat every day. Every bounding
    parameter a tool offers must therefore be passed explicitly — found here
    from the source, so a new one cannot be added without the same decision.
    """
    source = _server_source()
    for chunk in source.split("@mcp.tool()")[1:]:
        match = re.search(r"^def ([a-z_0-9]+)\(", chunk, re.MULTILINE)
        if not match:
            continue
        name = match.group(1)
        signature = chunk.split(") ->", 1)[0]
        declared = {arg for arg in BOUNDING_ARGS if re.search(rf"\b{arg}\s*:", signature)}
        if not declared:
            continue
        probe = smoke_probes.PROBES.get(name)
        assert probe is not None, f"{name} takes bounding arguments and has no probe spec"
        if probe.skip:
            continue
        unbounded = sorted(arg for arg in declared if not isinstance(probe.args.get(arg), int))
        assert not unbounded, (
            f"{name} accepts {unbounded} but its probe leaves them at the tool's "
            "own default. Proving the tool works needs a sample, not the whole "
            "enterprise."
        )


def test_every_exercised_probe_asserts_something():
    """A probe that asserts nothing reports a broken tool as OK."""
    offenders = [
        name
        for name, probe in smoke_probes.PROBES.items()
        if not probe.skip and not probe.must_match and not probe.min_chars and not probe.require_keys and not probe.min_values
    ]
    assert not offenders, (
        f"probes with nothing to assert: {offenders}. These tools answer with a "
        "dict, so name the envelope keys a working answer always carries "
        "(require_keys)."
    )


def test_address_shapes_catch_what_they_claim_to():
    """The guard below is only as good as these patterns, so pin them.

    IPv6 in particular is easy to get wrong in both directions: miss the
    compressed form, or swallow anything with two colons in it.
    """
    leaks = [
        "user@example.org",
        "https://api.example.ac.jp",
        "files.example.ac.jp",
        "example.com",  # a bare two-label domain is the common shape
        "example.io",
        "192.0.2.10",
        "2001:db8::1",
        "fe80::1",
        "2001:0db8:85a3:0000:0000:8a2e:0370:7334",
    ]
    for value in leaks:
        assert any(re.search(p, value) for p in ADDRESS_SHAPES.values()), f"missed: {value}"

    innocuous = [
        "12:34:56",  # a clock time
        "values[::2]",  # a Python slice
        "::1",  # loopback identifies no site
        '"max_folders": 25',  # a probe bound
        "root_folder_id",
    ]
    for value in innocuous:
        matched = [label for label, p in ADDRESS_SHAPES.items() if re.search(p, value)]
        assert not matched, f"false positive on {value!r}: {matched}"


def test_no_account_identifying_arguments_are_hardcoded():
    """Arguments that name a real account must be discovered, not written down.

    The shape scan below would catch a login, because a Box login is an email
    address — but the rule is about the parameter, not about the value
    happening to look like one, so it is stated as a parameter rule too.
    """
    source = _server_source()
    stale = sorted(k for k in IDENTIFIER_ARGS if not re.search(rf"\b{k}\s*:", source))
    assert not stale, (
        f"IDENTIFIER_ARGS names parameters no tool takes any more: {stale}. "
        "A renamed parameter silently empties this guard, so keep the set in "
        "step with the tool signatures."
    )

    offenders = [(name, key) for name, probe in smoke_probes.PROBES.items() for key in probe.args if key in IDENTIFIER_ARGS]
    assert not offenders, (
        f"account-identifying arguments hardcoded in smoke_probes.py: {offenders}. "
        "Leave them empty, or discover them at run time (args_factory); this "
        "repository is public."
    )

    # The check above reads the specs as data, which an args_factory sidesteps:
    # a factory returning {"created_by_logins": "someone@..."} would satisfy it
    # while committing the very literal it exists to prevent. So read the file
    # as text too and refuse one of these keys paired with a string literal
    # anywhere in it — a discovered value is an expression, never a quote.
    spec_source = (Path(__file__).resolve().parent.parent / "scripts" / "smoke_probes.py").read_text(encoding="utf-8")
    literals = sorted(key for key in IDENTIFIER_ARGS if re.search(rf'["\']{key}["\']\s*:\s*["\']', spec_source))
    assert not literals, (
        f"account-identifying arguments written as literals in smoke_probes.py: {literals}. "
        "Return them from a discovery call instead of writing the value down."
    )


def _is_reserved(literal: str) -> bool:
    """True when ``literal`` sits in a name the RFCs reserve for documentation.

    Matched on a label boundary, not as a bare suffix: ``endswith("example.com")``
    also accepts ``corp-example.com``, which is a perfectly real domain — so the
    exemption meant for documentation names would have waved a live enterprise
    address straight past the guard below.
    """
    host = literal.rsplit("@", 1)[-1].rstrip("/").lower()
    return any(host == d or host.endswith("." + d) for d in RESERVED_DOMAINS)


def test_no_enterprise_specific_literals_in_specs():
    """This repository is public: probes must not name the enterprise.

    The complement of the check above: it bans the parameters that carry an
    account name, this one bans anything address-shaped anywhere in the file —
    a login, a URL, a hostname, an IP. The patterns are deliberately generic:
    spelling out the enterprise's own domain in order to forbid it would put
    that domain in a public repository, which is the very thing this test
    exists to prevent.
    """
    source = (Path(__file__).resolve().parent.parent / "scripts" / "smoke_probes.py").read_text(encoding="utf-8")
    hits = {}
    for label, pattern in ADDRESS_SHAPES.items():
        found = [m for m in re.findall(pattern, source) if not _is_reserved(m)]
        if found:
            hits[label] = sorted(set(found))
    assert not hits, (
        f"address-like literals in smoke_probes.py: {hits}. Discover such arguments at run time (args_factory) rather than hardcoding them."
    )


def test_reserved_domain_exemption_matches_on_a_label_boundary():
    """A real domain that merely ends in a reserved name must stay banned.

    ``corp-example.com`` and ``notexample.invalid`` are ordinary registrable
    domains. A suffix test accepts both, which turns the documentation-name
    exemption into a hole big enough for a live address.
    """
    for reserved in ("user@example.com", "files.example.invalid", "example.test", "sub.example.org"):
        assert _is_reserved(reserved), f"should be exempt: {reserved}"
    for real in ("employee@corp-example.com", "notexample.invalid", "myexample.test", "example.community"):
        assert not _is_reserved(real), f"must NOT be exempt: {real}"


def test_reserved_domain_exemption_rejects_a_real_domain_under_an_example_label():
    """The specific hole the shape-based version of the guard above had.

    ``example.corp-acme.com`` is registrable and real, yet starts with ``example.``;
    admitting it to RESERVED_DOMAINS would exempt that whole host from the
    public-repository address scan. Pinned so the guard cannot regress to a shape test.
    """
    rfc2606 = {"example.com", "example.net", "example.org"}

    def accepted(name: str) -> bool:
        return name in rfc2606 or name.rsplit(".", 1)[-1] in ("invalid", "test")

    for reserved in RESERVED_DOMAINS:
        assert accepted(reserved), reserved
    for real in ("example.corp-acme.com", "example.co.jp", "example.acme.net", "exampled.com"):
        assert not accepted(real), f"must NOT be admissible: {real}"


def test_reserved_domain_exemption_covers_only_reserved_names():
    """Guards the exemption itself from growing to cover a real domain.

    The check above stops rejecting a literal once it sits under a RESERVED_DOMAINS
    entry, so that list is the one place where adding a line would silently let a
    real address into a public repository. Every entry must be a name the RFCs
    reserve, tested as a closed set rather than by shape: a ``startswith("example.")``
    rule also accepts ``example.corp-acme.com``, an ordinary registrable domain,
    which would exempt every address at that host and all of its subdomains.
    """
    rfc2606 = {"example.com", "example.net", "example.org"}
    for name in RESERVED_DOMAINS:
        assert name in rfc2606 or name.rsplit(".", 1)[-1] in ("invalid", "test"), name
