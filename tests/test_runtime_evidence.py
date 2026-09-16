"""Evidence must accept Git newline conversion, but still reject source drift."""
import pytest
from scripts.capture_runtime import source_hashes as hashes, compare_sources as compare


def test_exact_and_newline_only_sources_are_distinct():
    lf = {"api.py": hashes(b"value = 1\nprint(value)\n")}
    crlf = {"api.py": hashes(b"value = 1\r\nprint(value)\r\n")}
    assert compare(lf, lf) == []
    assert compare(crlf, lf) == ["api.py"]
    assert compare(lf, crlf) == ["api.py"]
    assert lf["api.py"]["sha256"] != crlf["api.py"]["sha256"]


@pytest.mark.parametrize("changed", [b"value = 2\n", b"value=1\n", b"value = 1", b"\xef\xbb\xbfvalue = 1\n"])
def test_content_whitespace_bom_and_final_newline_changes_fail(changed):
    with pytest.raises(RuntimeError, match="does not match checkout"):
        compare({"api.py": hashes(b"value = 1\n")}, {"api.py": hashes(changed)})


@pytest.mark.parametrize("checkout,deployed", [({}, {"extra.py": hashes(b"pass\n")}),
                                                ({"missing.py": hashes(b"pass\n")}, {})])
def test_added_or_missing_image_source_fails(checkout, deployed):
    with pytest.raises(RuntimeError, match="file set"):
        compare(checkout, deployed)
