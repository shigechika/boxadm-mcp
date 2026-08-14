"""Tests for the per-folder listing tool (list_folder_items).

Two classes of regression dominate here. First, attribution: Box records NO user
for a File Request upload, so the obvious fields (``created_by``, ``modified_by``,
``owned_by``) all fail to identify the submitter and only
``uploader_display_name`` works — a listing that filtered on the obvious ones
would return nothing and look correct doing it. Second, time: Box stamps items in
its own UTC offset while a caller asks in theirs, so any comparison done as text
is wrong by that difference and silent about it.
"""

import httpx
import pytest
import respx

from boxadm_mcp import server
from tests.conftest import TOKEN_URL

FOLDER = "12345"
FOLDER_URL = f"https://api.box.com/2.0/folders/{FOLDER}"
ITEMS_URL = f"{FOLDER_URL}/items"


def _call(tool):
    return getattr(tool, "fn", tool)


def _upload(fid, who, created, *, name=None, size=100):
    """A File Request upload: attributed ONLY by uploader_display_name."""
    return {
        "type": "file",
        "id": fid,
        "name": name or f"shot-{fid}.png",
        "uploader_display_name": who,
        "created_by": {"login": "", "name": "Anonymous User"},
        "created_at": created,
        "modified_at": created,
        "size": size,
        "shared_link": None,
    }


def _router(entries, *, total=None, folder_name="Enquiries"):
    r = respx.mock(assert_all_called=False)
    r.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))
    r.get(FOLDER_URL).mock(return_value=httpx.Response(200, json={"type": "folder", "id": FOLDER, "name": folder_name}))
    items = r.get(ITEMS_URL).mock(
        return_value=httpx.Response(200, json={"total_count": len(entries) if total is None else total, "entries": entries})
    )
    return r, items


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------
def test_uploader_display_name_is_what_identifies_the_submitter():
    """The finding the whole tool rests on.

    A File Request upload has ``created_by`` = "Anonymous User" and is owned by
    the application's service account. Filtering on either returns nobody, so the
    row's ``uploaded_by`` must come from ``uploader_display_name``.
    """
    r, _ = _router([_upload("1", "taro@example.com", "2026-08-13T22:33:26-07:00")])
    with r:
        out = _call(server.list_folder_items)(folder_id=FOLDER)
    assert out["items"][0]["uploaded_by"] == "taro@example.com"


def test_created_by_is_the_fallback_for_a_signed_in_upload():
    """The reverse case: a normally-uploaded file has no uploader_display_name."""
    entry = _upload("1", None, "2026-08-13T00:00:00+00:00")
    entry["created_by"] = {"login": "staff@example.com"}
    r, _ = _router([entry])
    with r:
        out = _call(server.list_folder_items)(folder_id=FOLDER)
    assert out["items"][0]["uploaded_by"] == "staff@example.com"


def test_uploaded_by_matches_case_insensitively_and_exactly():
    """Exact, like get_user's login: a prefix match would attribute one person's
    attachments to another whose address merely starts the same."""
    r, _ = _router(
        [
            _upload("1", "Taro@Example.com", "2026-08-13T00:00:00+00:00"),
            _upload("2", "taro2@example.com", "2026-08-12T00:00:00+00:00"),
        ]
    )
    with r:
        out = _call(server.list_folder_items)(folder_id=FOLDER, uploaded_by="taro@EXAMPLE.com")
    assert out["matched"] == 1
    assert [i["item_id"] for i in out["items"]] == ["1"]


def test_uploaded_by_is_not_parsed_as_an_email():
    """Observed live: one submitter value was not email-shaped. Matching it as an
    opaque string is what keeps that person findable."""
    r, _ = _router([_upload("1", "front desk", "2026-08-13T00:00:00+00:00")])
    with r:
        out = _call(server.list_folder_items)(folder_id=FOLDER, uploaded_by="Front Desk")
    assert out["matched"] == 1


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
def test_bounds_compare_instants_not_strings():
    """The timezone bug this tool must not have.

    The item is stamped 2026-08-13T22:33:26-07:00; the caller asks for everything
    since 2026-08-14T10:00:00+09:00. As TEXT the item sorts earlier and would be
    excluded — as time it is 4.5 hours NEWER and must be kept.
    """
    r, _ = _router([_upload("1", "taro@example.com", "2026-08-13T22:33:26-07:00")])
    with r:
        out = _call(server.list_folder_items)(folder_id=FOLDER, since="2026-08-14T10:00:00+09:00")
    assert out["matched"] == 1, "a lexicographic comparison would have dropped this"


@pytest.mark.parametrize("naive", ["2026-08-14", "2026-08-14T00:00:00"])
def test_a_naive_bound_is_refused_rather_than_guessed(naive):
    """Assuming a timezone would reintroduce the same error as a default."""
    r, items = _router([_upload("1", "a@example.com", "2026-08-13T00:00:00+00:00")])
    with r:
        out = _call(server.list_folder_items)(folder_id=FOLDER, since=naive)
        assert items.call_count == 0  # refused before the request
    assert "error" in out and "UTC offset" in out["error"]


def test_rows_are_sorted_newest_first_by_upload_time():
    """Sorted here rather than by Box: `sort=date` puts folders before files
    regardless of date, and measured against a live folder matched neither
    created_at nor modified_at order."""
    r, _ = _router(
        [
            _upload("old", "a@example.com", "2026-08-01T00:00:00+00:00"),
            _upload("new", "a@example.com", "2026-08-13T22:00:00-07:00"),
            _upload("mid", "a@example.com", "2026-08-10T00:00:00+00:00"),
        ]
    )
    with r:
        out = _call(server.list_folder_items)(folder_id=FOLDER)
    assert [i["item_id"] for i in out["items"]] == ["new", "mid", "old"]


def test_an_unparseable_timestamp_cannot_satisfy_a_bound():
    entry = _upload("1", "a@example.com", "not-a-timestamp")
    r, _ = _router([entry])
    with r:
        out = _call(server.list_folder_items)(folder_id=FOLDER, since="2020-01-01T00:00:00+00:00")
    assert out["matched"] == 0


# ---------------------------------------------------------------------------
# Bounds and disclosure
# ---------------------------------------------------------------------------
def test_limit_bounds_the_answer_not_the_search():
    """The regression that would make ``uploaded_by`` quietly useless.

    If ``limit`` bounded the FETCH, a submitter whose attachment is not among the
    newest N would come back as "no attachments" — a confident wrong answer on
    exactly the enquiry the tool exists for.
    """
    entries = [_upload(str(i), "noise@example.com", f"2026-08-{i:02d}T00:00:00+00:00") for i in range(2, 28)]
    entries.append(_upload("wanted", "taro@example.com", "2026-08-01T00:00:00+00:00"))  # oldest
    r, items = _router(entries)
    with r:
        out = _call(server.list_folder_items)(folder_id=FOLDER, uploaded_by="taro@example.com", limit=1)
        assert items.calls.last.request.url.params["limit"] == "1000"  # a page, not the caller's limit
    assert [i["item_id"] for i in out["items"]] == ["wanted"]


def test_limit_truncation_is_reported_separately_from_coverage():
    """Two different truths: the caller asked for fewer rows, versus the folder
    was bigger than one page. Conflating them hides the second."""
    entries = [_upload(str(i), "a@example.com", f"2026-08-{i:02d}T00:00:00+00:00") for i in range(1, 6)]
    r, _ = _router(entries)
    with r:
        out = _call(server.list_folder_items)(folder_id=FOLDER, limit=2)
    assert (out["returned"], out["matched"], out["capped"]) == (2, 5, False)
    assert "raise limit" in out["note"]


def test_a_folder_bigger_than_one_page_makes_a_miss_inconclusive():
    entries = [_upload(str(i), "a@example.com", f"2026-08-{i:02d}T00:00:00+00:00") for i in range(1, 4)]
    r, _ = _router(entries, total=5000)
    with r:
        out = _call(server.list_folder_items)(folder_id=FOLDER, uploaded_by="nobody@example.com")
    assert out["capped"] is True
    assert out["matched"] == 0
    assert "inconclusive" in out["note"]


def test_an_empty_match_in_a_fully_read_folder_is_a_negative_answer():
    r, _ = _router([_upload("1", "a@example.com", "2026-08-01T00:00:00+00:00")])
    with r:
        out = _call(server.list_folder_items)(folder_id=FOLDER, uploaded_by="nobody@example.com")
    assert out["capped"] is False
    assert "negative answer" in out["note"]


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------
def test_links_use_the_generic_host_so_no_enterprise_subdomain_is_needed():
    """Verified with GET (HEAD answers 405): app.box.com/file/{id} and
    /folder/{id} both 302 to login with the right redirect_url."""
    r, _ = _router([_upload("77", "a@example.com", "2026-08-01T00:00:00+00:00")])
    with r:
        out = _call(server.list_folder_items)(folder_id=FOLDER)
    assert out["items"][0]["item_url"] == "https://app.box.com/file/77"
    assert out["folder_url"] == f"https://app.box.com/folder/{FOLDER}"


def test_a_folder_row_reports_no_byte_size():
    """Box's `size` on a folder is a rolled-up total — a different unit of
    meaning from a file's byte count, and summing the two would be wrong."""
    r, _ = _router([{"type": "folder", "id": "9", "name": "Resolved", "size": 999, "created_at": "2026-08-01T00:00:00+00:00"}])
    with r:
        out = _call(server.list_folder_items)(folder_id=FOLDER)
    row = out["items"][0]
    assert row["size_bytes"] is None
    assert row["item_url"] == "https://app.box.com/folder/9"


def test_the_row_carries_only_reviewed_keys():
    """Built key by key, so a field Box adds later cannot ride into an answer."""
    entry = _upload("1", "a@example.com", "2026-08-01T00:00:00+00:00")
    entry["path_collection"] = {"entries": [{"id": "0", "name": "All Files"}]}
    entry["sha1"] = "deadbeef"
    r, _ = _router([entry])
    with r:
        out = _call(server.list_folder_items)(folder_id=FOLDER)
    assert set(out["items"][0]) == {
        "item_type",
        "item_id",
        "name",
        "uploaded_by",
        "created_at",
        "modified_at",
        "size_bytes",
        "shared_link_access",
        "item_url",
    }


def test_a_malformed_folder_id_is_refused_before_any_request():
    r, items = _router([])
    with r:
        out = _call(server.list_folder_items)(folder_id="../users")
        assert items.call_count == 0
    assert "error" in out


def test_the_error_shape_carries_no_counts():
    """A failed listing must never be readable as an empty folder."""
    r = respx.mock(assert_all_called=False)
    r.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))
    r.get(FOLDER_URL).mock(return_value=httpx.Response(403))
    with r:
        out = _call(server.list_folder_items)(folder_id=FOLDER)
    assert "error" in out
    for absent in ("items", "returned", "matched", "total_in_folder", "capped"):
        assert absent not in out


def test_a_web_link_gets_a_direct_link_too():
    """A folder can hold three item types and all three have a Box web path.

    Verified with GET: /file/{id}, /folder/{id} and /web_link/{id} all 302 to
    login with the right redirect_url. The underscore matters -- /weblink/{id}
    is a 404 -- so the set is closed rather than derived from Box's `type`.
    """
    r, _ = _router([{"type": "web_link", "id": "55", "name": "portal", "created_at": "2026-08-01T00:00:00+00:00"}])
    with r:
        out = _call(server.list_folder_items)(folder_id=FOLDER)
    assert out["items"][0]["item_url"] == "https://app.box.com/web_link/55"


def test_an_unknown_item_type_gets_no_guessed_link():
    """A URL built from a type Box invents later would 404 while looking valid."""
    r, _ = _router([{"type": "something_new", "id": "9", "name": "x", "created_at": "2026-08-01T00:00:00+00:00"}])
    with r:
        out = _call(server.list_folder_items)(folder_id=FOLDER)
    assert out["items"][0]["item_url"] is None


def test_uploader_matching_folds_case_beyond_ascii():
    """`lower()` does not fold every case pair.

    "Straße".lower() is "straße"; a caller typing "STRASSE" lowers to "strasse",
    so an ASCII-only fold would report a real submitter as having no attachments.
    """
    r, _ = _router([_upload("1", "Straße", "2026-08-01T00:00:00+00:00")])
    with r:
        out = _call(server.list_folder_items)(folder_id=FOLDER, uploaded_by="STRASSE")
    assert out["matched"] == 1


def test_the_root_folder_convention_is_documented():
    """R1F2: a calling model that cannot learn `"0"` means the root has to guess,
    and every guess (a name, a URL) is refused."""
    doc = _call(server.list_folder_items).__doc__
    assert '"0"' in doc and "root" in doc
