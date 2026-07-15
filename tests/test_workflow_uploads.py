"""Pure upload helpers: deterministic naming + size/type checks."""
from __future__ import annotations

import pytest

from atom.workflow.uploads import (
    UploadTooLarge, UploadTypeNotAllowed,
    check_extension, check_size, safe_extension, stored_name, virtual_upload_path,
)


def test_safe_extension_cases():
    assert safe_extension("report.PDF") == ".pdf"
    assert safe_extension("archive.tar.gz") == ".gz"
    assert safe_extension("noext") == ""
    assert safe_extension("") == ""
    assert safe_extension("../../evil.sh") == ".sh"      # only the basename's suffix is taken


def test_stored_name_is_deterministic_and_collision_free():
    assert stored_name("doc", "q3-results.pdf") == "doc.pdf"
    assert stored_name("a", "data.csv") == "a.csv"
    assert stored_name("b", "data.csv") == "b.csv"
    # names that differ only in sanitized-away characters must NOT collide on disk
    assert stored_name("a b", "x.txt") != stored_name("a-b", "x.txt")
    assert stored_name("a!b", "x.txt") != stored_name("a-b", "x.txt")
    assert stored_name("a b", "x.txt") != stored_name("a!b", "x.txt")
    # deterministic
    assert stored_name("a b", "x.txt") == stored_name("a b", "x.txt")


def test_stored_name_sanitizes_and_disambiguates_lossy_names():
    # a clean identifier stays clean (no hash suffix)
    assert stored_name("document", "x.txt") == "document.txt"
    assert stored_name("a-b", "x.txt") == "a-b.txt"
    # a lossy name is sanitized AND gets a deterministic hash suffix so it can't collide
    a = stored_name("my input!", "x.txt")
    assert a.startswith("my-input-") and a.endswith(".txt")
    assert a == stored_name("my input!", "x.txt")     # deterministic
    # empty/degenerate falls back to an 'upload' stem
    assert stored_name("", "x.txt").startswith("upload")


def test_virtual_upload_path():
    assert virtual_upload_path("doc", "q3.pdf") == "/mnt/user-data/uploads/doc.pdf"


def test_check_size():
    check_size(50, 50)          # equal is OK
    check_size(999, 0)          # 0 = no limit
    with pytest.raises(UploadTooLarge):
        check_size(51, 50)


def test_check_extension():
    check_extension("x.txt", [])            # empty allowlist = allow any
    check_extension("x.PDF", ["pdf"])       # case-insensitive
    with pytest.raises(UploadTypeNotAllowed):
        check_extension("x.txt", ["pdf"])
