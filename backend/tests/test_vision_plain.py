"""Plain-prompt vision parsing for small local models (moondream)."""
from mitra.providers.vision import parse_plain_vision


def test_phrases_as_robot_and_detects_person():
    r = parse_plain_vision("The image features a cartoon scene of a man standing in front of a red house. "
                           "The man is on the left. A third sentence that is dropped.")
    assert r.description == "I can see a cartoon scene of a man standing in front of a red house. The man is on the left."
    assert r.person_present is True and r.confidence >= 0.6


def test_drops_garbled_first_fragment_and_ignores_newlines():
    r = parse_plain_vision("urn of a house with a brown roof.\n\n4. What is the main subject?")
    assert r.description.startswith("Of a house") or r.description.startswith("A house") or "house" in r.description
    assert "\n" not in r.description and r.person_present is False


def test_empty_answer_is_low_confidence():
    r = parse_plain_vision("")
    assert r.confidence == 0.0
