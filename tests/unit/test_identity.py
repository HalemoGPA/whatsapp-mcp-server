"""Identity resolution: LID <-> phone number, and naming.

The property that matters is in the module docstring: resolution may only ever
make attribution MORE accurate. An unknown identifier must echo back unchanged
rather than resolve to the wrong person, because a confidently wrong name is
worse than no name.
"""
from __future__ import annotations

import pytest

NAMED_PN = "201234567890"
NAMED_LID = "10000000000001"
UNNAMED_PN = "201234567891"
UNNAMED_LID = "10000000000002"
PUSH_ONLY_PN = "201234567892"
BUSINESS_PN = "201234567893"


class TestBare:
    """_bare() reduces any identifier shape to its digits."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("201234567890@s.whatsapp.net", "201234567890"),
            ("10000000000001@lid", "10000000000001"),
            ("201234567890", "201234567890"),
            ("+20 123 456 7890", "201234567890"),
            ("120363000000000000@g.us", "120363000000000000"),
            ("", ""),
            ("@s.whatsapp.net", ""),
        ],
    )
    def test_reduces_to_digits(self, identity, raw, expected):
        assert identity._bare(raw) == expected


class TestResolve:
    def test_lid_resolves_to_phone(self, identity):
        r = identity.resolve(f"{NAMED_LID}@lid")
        assert r["phone"] == NAMED_PN
        assert r["lid"] == NAMED_LID
        assert r["resolved"] is True

    def test_phone_resolves_to_lid(self, identity):
        r = identity.resolve(f"{NAMED_PN}@s.whatsapp.net")
        assert r["lid"] == NAMED_LID
        assert r["phone"] == NAMED_PN

    def test_both_directions_agree(self, identity):
        """The same person, reached from either identity, is one person."""
        from_lid = identity.resolve(NAMED_LID)
        from_pn = identity.resolve(NAMED_PN)
        assert from_lid["phone"] == from_pn["phone"]
        assert from_lid["lid"] == from_pn["lid"]
        assert set(from_lid["senders"]) == set(from_pn["senders"])

    def test_saved_name_beats_push_name(self, identity):
        """You saved them as 'Alex Doe'; they call themselves 'al3x'."""
        r = identity.resolve(NAMED_PN)
        assert r["saved_name"] == "Alex Doe"
        assert r["push_name"] == "al3x"
        assert r["name"] == "Alex Doe"
        assert r["display"] == "Alex Doe (201234567890)"

    def test_push_name_used_when_never_saved(self, identity):
        r = identity.resolve(PUSH_ONLY_PN)
        assert r["saved_name"] is None
        assert r["name"] == "Jordan"
        assert r["resolved"] is True

    def test_business_name_counts_as_saved(self, identity):
        assert identity.resolve(BUSINESS_PN)["name"] == "Cairo Coffee"

    def test_name_found_via_the_other_identity(self, identity):
        """The contacts row is keyed on the phone; asking by LID still names them."""
        assert identity.resolve(NAMED_LID)["name"] == "Alex Doe"


class TestNeverWrong:
    """Best-effort resolution: unknown input echoes back, never guesses."""

    def test_unknown_identifier_echoes_back(self, identity):
        r = identity.resolve("209999999999")
        assert r["resolved"] is False
        assert r["phone"] == "209999999999"
        assert r["lid"] is None
        assert r["name"] == "209999999999"

    def test_empty_identifier_is_not_resolved(self, identity):
        r = identity.resolve("")
        assert r["resolved"] is False
        assert r["senders"] == []

    def test_mapped_but_unnamed_has_no_name_invented(self, identity):
        """A LID we can map but nobody we can name: numbers, not a guess."""
        r = identity.resolve(UNNAMED_LID)
        assert r["phone"] == UNNAMED_PN
        assert r["saved_name"] is None
        assert r["push_name"] is None
        assert r["name"] == UNNAMED_PN

    def test_display_never_shows_a_bare_none(self, identity):
        for ident in (NAMED_PN, UNNAMED_LID, "209999999999", ""):
            assert "None" not in identity.resolve(ident)["display"]

    def test_missing_store_degrades_to_echo(self, monkeypatch, tmp_path):
        """No store on disk must not raise; it resolves to itself."""
        import importlib

        monkeypatch.setenv("WHATSMEOW_DB_PATH", str(tmp_path / "absent.db"))
        mod = importlib.reload(importlib.import_module("identity"))
        r = mod.resolve(NAMED_PN)
        assert r["resolved"] is False
        assert r["phone"] == NAMED_PN


class TestSendersFor:
    """senders_for() drives the cross-identity message query."""

    def test_returns_both_identities(self, identity):
        s = identity.senders_for(NAMED_PN)
        assert set(s) == {NAMED_PN, NAMED_LID}

    def test_same_set_from_either_side(self, identity):
        assert set(identity.senders_for(NAMED_LID)) == set(identity.senders_for(NAMED_PN))

    def test_unmapped_person_still_queryable(self, identity):
        """Half a history is better than none: fall back to the bare input."""
        assert identity.senders_for("209999999999") == ["209999999999"]


class TestFindByName:
    def test_matches_saved_name_case_insensitively(self, identity):
        assert [m["phone"] for m in identity.find_by_name("alex doe")] == [NAMED_PN]

    def test_matches_push_name(self, identity):
        assert [m["phone"] for m in identity.find_by_name("jordan")] == [PUSH_ONLY_PN]

    def test_substring_matches(self, identity):
        assert identity.find_by_name("coffee")[0]["name"] == "Cairo Coffee"

    def test_no_match_is_empty_not_an_error(self, identity):
        assert identity.find_by_name("nobody here") == []

    def test_empty_query_returns_nothing(self, identity):
        """An empty query must not mean 'everyone'."""
        assert identity.find_by_name("") == []
        assert identity.find_by_name("   ") == []

    def test_respects_limit(self, identity):
        assert len(identity.find_by_name("a", limit=1)) <= 1

    def test_one_person_appears_once(self, identity):
        """pn and lid rows for the same human must collapse to a single hit."""
        hits = identity.find_by_name("alex")
        assert len({h["phone"] for h in hits}) == len(hits)


class TestLabel:
    def test_labels_a_known_sender(self, identity):
        assert identity.label(NAMED_LID) == "Alex Doe (201234567890)"

    def test_falls_back_to_the_raw_value(self, identity):
        assert identity.label("209999999999") == "209999999999"

    def test_empty_input_is_empty_output(self, identity):
        assert identity.label("") == ""
