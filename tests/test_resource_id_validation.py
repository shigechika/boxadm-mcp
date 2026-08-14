"""A folder id is interpolated into the request PATH, so it decides the endpoint.

``httpx.URL`` resolves ``..`` segments the way a browser does. An id of
``../users`` therefore does not produce a 404 against the folders endpoint — it
rewrites the request into ``GET /2.0/users``, a page of the enterprise user
directory, which is the exact enumeration ``get_user`` exists to refuse. A query
string smuggled into the same argument rides along.

These tests pin the refusal at the client, before any interpolation, because
every folder-taking tool shares it and a per-tool guard is one new tool away from
being forgotten.
"""

import httpx
import pytest

from boxadm_mcp.client import BoxError, BoxRequestError, _validate_resource_id

#: The rewrite itself, independent of this repo: what httpx builds from a
#: traversal id. Asserted so the tests below stay honest if httpx ever changes
#: its normalisation — a passing suite must not outlive the reason for the guard.
TRAVERSAL = "../users?filter_term=a&limit=1000&x="


def test_httpx_really_rewrites_the_endpoint():
    """The premise. Without this, the guard below is cargo cult."""
    rewritten = httpx.URL(f"https://api.box.com/2.0/folders/{TRAVERSAL}")
    assert rewritten.path == "/2.0/users"
    assert rewritten.params.get("filter_term") == "a"

    # And the suffix-less form is the worst one: no filter_term at all, which is
    # "no filter" rather than "no match" -- Box answers with a page of the directory.
    bare = httpx.URL("https://api.box.com/2.0/folders/../users")
    assert bare.path == "/2.0/users"
    assert not bare.params


@pytest.mark.parametrize(
    "bad",
    [
        TRAVERSAL,
        "../users",
        "..",
        "../../admin",
        "123/../users",
        "123?fields=x",
        "123#frag",
        "123 456",
        "abc",
        "",
        "   ",
        "1\n2",  # interior newline: an anchored `^...$` match accepts it, fullmatch does not
        "1" * 21,  # httpx raises InvalidURL, which is NOT an httpx.HTTPError
    ],
)
def test_refuses_anything_that_is_not_a_plain_decimal_id(bad):
    with pytest.raises(BoxRequestError):
        _validate_resource_id(bad, kind="folder")


@pytest.mark.parametrize("good", ["0", "1", "123456789012", "9" * 20])
def test_accepts_a_real_box_id(good):
    assert _validate_resource_id(good, kind="folder") == good


@pytest.mark.parametrize("padded", ["  42  ", "42\n", "\t42"])
def test_surrounding_whitespace_is_stripped_not_refused(padded):
    """A copy-pasted id often carries a space or a trailing newline.

    Stripping is safe because the STRIPPED value is what gets validated and what
    gets sent — so this is a convenience, not a way in. Pinned because the
    interior-newline case above is refused, and the difference between the two
    should be a decision rather than an accident.
    """
    assert _validate_resource_id(padded, kind="folder") == "42"


def test_anchored_match_would_have_been_weaker():
    """Why `fullmatch`, demonstrated rather than asserted in a comment."""
    import re

    anchored = re.compile(r"^[0-9]{1,20}$")
    assert anchored.match("1\n2\n") is None  # agrees here
    assert anchored.match("12\n") is not None  # `$` matches before a trailing newline
    with pytest.raises(BoxRequestError):
        _validate_resource_id("1\n2", kind="folder")


def test_refusal_is_a_boxerror_so_the_scan_tools_keep_their_contract():
    """Load-bearing, and the reason this is not a ValueError.

    ``_scan`` catches ``BoxError`` per folder and counts it into ``fetch_errors``;
    the tools call it with no ``try``. A ``ValueError`` would escape
    ``ThreadPoolExecutor.map`` into the tool and surface as a raw traceback,
    losing both the partial-coverage disclosure and the structured error shape.
    """
    assert issubclass(BoxRequestError, BoxError)
