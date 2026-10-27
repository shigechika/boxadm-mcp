"""Configuration: organization domain allowlist and external-actor detection.

The whole point of boxadm-mcp is to surface *external* file flow, so the one piece
of org-specific knowledge it needs is "which email domains are us". Everything
else (who shares, what leaks) is derived from Box events relative to this list.

No organization-specific value is hardcoded: set ``BOX_ALLOWED_DOMAINS``
(comma-separated) to your own domains. Left unset, the allowlist is empty and
every address is treated as external — a safe default for a leakage-detection
tool (nothing is silently trusted as internal until you configure it).
"""

import os

DEFAULT_ALLOWED_DOMAINS: tuple[str, ...] = ()


def allowed_domains() -> list[str]:
    """Return the internal (org) email domains, lower-cased.

    Reads BOX_ALLOWED_DOMAINS (comma-separated); empty/unset yields no domains,
    so is_external() treats every address as external until configured.
    """
    raw = os.environ.get("BOX_ALLOWED_DOMAINS")
    if not raw:
        return list(DEFAULT_ALLOWED_DOMAINS)
    return [d.strip().lower() for d in raw.split(",") if d.strip()]


def is_external(email: str | None, domains: list[str] | None = None) -> bool:
    """True when an email address is outside the organization.

    A blank/missing/malformed address is treated as external — an unknown actor
    is the cautious assumption for a leakage check. Subdomains of an allowed
    domain (e.g. ``sub.example.com`` when ``example.com`` is allowed) count as
    internal.
    """
    if not email or "@" not in email:
        return True
    doms = domains if domains is not None else allowed_domains()
    dom = email.rsplit("@", 1)[1].strip().lower()
    return not any(dom == d or dom.endswith("." + d) for d in doms)


READ_FOLDERS_ENV = "BOXADM_READ_FOLDER_IDS"


def read_folder_ids() -> tuple[tuple[str, ...], str | None]:
    """Folders whose files ``read_file`` may read, from ``BOXADM_READ_FOLDER_IDS``.

    Returns ``(ids, None)``, or ``((), reason)`` when the variable is set but
    unusable. Unset or blank yields ``((), None)``: the tool is simply off. Every
    entry must be a decimal Box folder id other than ``"0"`` (the account's whole
    root), and one bad entry rejects the whole list rather than being skipped, so
    a typo cannot quietly narrow or widen what the tool may read.
    """
    raw = os.environ.get(READ_FOLDERS_ENV, "")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return (), None
    for p in parts:
        if not p.isdecimal() or len(p) > 20:
            return (), f"{READ_FOLDERS_ENV} holds a value that is not a decimal Box folder id"
        if p == "0":
            return (), f"{READ_FOLDERS_ENV} must not include 0 (the whole root)"
    return tuple(dict.fromkeys(parts)), None
