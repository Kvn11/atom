from atom.limits import truncate_text


def test_short_text_unchanged():
    assert truncate_text("hello", max_chars=100, marker_template="[cut]") == "hello"


def test_keeps_head_and_tail_with_counts_marker():
    text = "A" * 50 + "B" * 50  # 100 chars
    out = truncate_text(text, max_chars=20, marker_template="[…{elided} of {total} elided…]")
    assert out.startswith("A" * 10)   # head = max_chars // 2
    assert out.endswith("B" * 10)     # tail = max_chars // 2
    assert "80 of 100 elided" in out


def test_zero_budget_returns_marker_only():
    out = truncate_text("Z" * 10, max_chars=0, marker_template="[gone]")
    assert out == "[gone]"


def test_extra_format_keys_ignored_when_unreferenced():
    out = truncate_text("Q" * 10, max_chars=4, marker_template="[{elided}]")
    assert out.startswith("QQ") and out.endswith("QQ") and "[6]" in out
