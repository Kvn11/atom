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
    # distinct input names -> distinct stored names even with identical client filenames
    assert stored_name("a", "data.csv") == "a.csv"
    assert stored_name("b", "data.csv") == "b.csv"


def test_stored_name_sanitizes_and_falls_back():
    assert stored_name("my input!", "x.txt") == "my-input.txt"
    assert stored_name("", "x.txt") == "upload.txt"


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
