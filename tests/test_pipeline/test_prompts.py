"""Host-owned role payloads for ModelRuntime."""

from hyperresearch.pipeline.prompts import role_payload


def test_width_payload_requires_host_actions():
    text = role_payload("width", "What is an EarthNode?")
    assert "You have no tools" in text
    assert "actions" in text
    assert "fetch" in text
    assert "Do not write the research report" in text


def test_decompose_payload_forbids_report_field():
    text = role_payload("decompose", "q", declared_tier="light")
    assert "required_section_headings" in text
    assert 'Do not include a "report" field' in text


def test_draft_payload_forbids_invented_src_note():
    text = role_payload("draft", "q", extra="Evidence notes: none")
    assert "src-note" in text
    assert "markdown" in text.lower()
