"""These invariants are the difference between a scheduling tool and a
surveillance tool. They are tested like load-bearing code because they are."""

import pytest

from bridge.privacy import (
    MIN_POOL_SIZE,
    PrivacyViolation,
    aggregation_floor,
    assert_no_pii,
    may_report_crew_load,
    pseudonymize,
)

SALT = "TURNAROUND_PSEUDONYM_SALT"


@pytest.fixture
def salted(monkeypatch):
    monkeypatch.setenv(SALT, "a-real-deployment-salt")


class TestPseudonymisation:
    def test_is_stable_and_short(self, salted):
        assert pseudonymize("person-1") == pseudonymize("person-1")
        assert len(pseudonymize("person-1")) == 8

    def test_distinguishes_people(self, salted):
        assert pseudonymize("person-1") != pseudonymize("person-2")

    def test_salt_changes_the_mapping(self, monkeypatch):
        monkeypatch.setenv(SALT, "salt-a")
        a = pseudonymize("person-1")
        monkeypatch.setenv(SALT, "salt-b")
        assert pseudonymize("person-1") != a

    def test_refuses_to_run_with_the_placeholder_salt(self, monkeypatch):
        monkeypatch.setenv(SALT, "change-me")
        with pytest.raises(PrivacyViolation, match="placeholder"):
            pseudonymize("person-1")

    def test_refuses_to_run_with_no_salt(self, monkeypatch):
        monkeypatch.delenv(SALT, raising=False)
        with pytest.raises(PrivacyViolation):
            pseudonymize("person-1")


class TestPiiGuard:
    def test_passes_clean_attributes(self):
        assert_no_pii({"production.artist": "a7f3c2d1", "production.department": "comp"})

    def test_blocks_email(self):
        with pytest.raises(PrivacyViolation, match="email"):
            assert_no_pii({"production.artist": "jane.doe@studio.com"})

    def test_blocks_raw_source_uuid(self):
        with pytest.raises(PrivacyViolation, match="raw source id"):
            assert_no_pii({"production.artist": "3f2504e0-4f89-11d3-9a0c-0305e82c3301"})

    @pytest.mark.parametrize(
        "key", ["email", "first_name", "fullName", "person_id", "user_id", "artist.last_name"]
    )
    def test_blocks_identifying_keys(self, key):
        with pytest.raises(PrivacyViolation, match="identify a person"):
            assert_no_pii({key: "anything"})


class TestAggregationFloor:
    def test_small_pools_are_dropped_entirely(self):
        kept = aggregation_floor({"comp": ["a", "b", "c"], "di": ["d", "e"]})
        assert kept == {"comp": 3}
        # Deliberately not merged into an "other" bucket: a supervisor could
        # infer membership of a two-person residue.
        assert "di" not in kept

    def test_duplicates_do_not_inflate_headcount(self):
        assert aggregation_floor({"comp": ["a", "a", "a", "b"]}) == {}

    def test_gate_matches_the_floor(self):
        assert not may_report_crew_load(["a", "b"])
        assert may_report_crew_load(["a", "b", "c"])
        assert MIN_POOL_SIZE == 3
