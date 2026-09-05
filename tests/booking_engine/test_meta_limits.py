"""Meta's ceilings must never be widened by anything on our side.

Every test here is about the *direction* of a mistake. Under-sending is
recoverable; exceeding a platform limit costs quality rating, tier, and
eventually the sender.
"""
import pytest

from booking_engine.services.messaging import meta_limits as ml


# ------------------------------------------------------------- volume tiers

def test_known_tiers_map_to_metas_numbers():
    assert ml.tier_daily_conversations("TIER_250") == 250
    assert ml.tier_daily_conversations("TIER_1K") == 1_000
    assert ml.tier_daily_conversations("TIER_100K") == 100_000


def test_an_unknown_tier_falls_back_to_the_unverified_floor():
    """If Meta invents TIER_5K we must under-send, never over-send."""
    assert ml.tier_daily_conversations("TIER_5K") == ml.UNVERIFIED_TIER_CONVERSATIONS
    assert ml.tier_daily_conversations(None) == ml.UNVERIFIED_TIER_CONVERSATIONS
    assert ml.tier_daily_conversations("") == ml.UNVERIFIED_TIER_CONVERSATIONS


def test_tier_lookup_is_case_insensitive():
    assert ml.tier_daily_conversations("tier_1k") == 1_000


# ----------------------------------------------------------------- throughput

def test_coexistence_is_pinned_to_metas_slow_rate_whatever_else_is_reported():
    """Meta fixes a coexistence number at 20 mps for Business App compatibility.

    A 'HIGH' throughput level on such a number must not raise it.
    """
    assert ml.throughput_mps(platform_type="COEXISTENCE", throughput_level="HIGH") == 20


def test_unknown_throughput_falls_back_to_the_slowest_rate():
    assert ml.throughput_mps(platform_type=None, throughput_level=None) == 20
    assert ml.throughput_mps(platform_type="CLOUD_API", throughput_level="WARP") == 20


def test_a_standard_number_gets_metas_standard_rate():
    assert ml.throughput_mps(platform_type="CLOUD_API", throughput_level="STANDARD") == 80


# ------------------------------------------- the layering: ours only narrows

def test_our_cap_binds_when_it_is_lower_than_metas():
    """The normal case: a 50/day drip on a sender Meta would allow 1000."""
    sender = {"daily_cap": 50, "messaging_limit": "TIER_1K"}
    assert ml.effective_daily_cap(sender) == 50


def test_our_cap_cannot_authorise_more_than_meta_allows():
    """The safeguard. Someone sets daily_cap=5000 on a Tier-250 sender."""
    sender = {"daily_cap": 5000, "messaging_limit": "TIER_250"}
    assert ml.effective_daily_cap(sender) == 250


def test_an_unknown_tier_clamps_our_cap_to_the_unverified_floor():
    sender = {"daily_cap": 5000, "messaging_limit": None}
    assert ml.effective_daily_cap(sender) == ml.UNVERIFIED_TIER_CONVERSATIONS


def test_a_sender_with_no_cap_of_ours_sends_nothing():
    """Fails closed rather than inheriting Meta's ceiling as a default."""
    assert ml.effective_daily_cap({"messaging_limit": "TIER_1K"}) == 0
    assert ml.effective_daily_cap({"daily_cap": 0, "messaging_limit": "TIER_1K"}) == 0


def test_effective_cap_is_never_negative():
    assert ml.effective_daily_cap({"daily_cap": -10, "messaging_limit": "TIER_1K"}) == 0


# ----------------------------------------------------------------- send rate

def test_the_global_rate_is_clamped_below_the_slowest_per_number_throughput():
    """This invariant is why there is no second, per-number pacer.

    Below MAX_SENDS_PER_MINUTE no single number can be over-driven however the
    claimed batch happens to be distributed across shops.
    """
    assert ml.safe_sends_per_minute(10_000) == ml.MAX_SENDS_PER_MINUTE
    assert ml.MAX_SENDS_PER_MINUTE == ml.COEXISTENCE_MPS * 60


def test_a_sane_configured_rate_is_left_alone():
    assert ml.safe_sends_per_minute(60) == 60


def test_zero_still_means_no_pacing():
    assert ml.safe_sends_per_minute(0) == 0


# ---------------------------------------------------------------- onboarding

def test_onboarding_limit_reflects_access_verification():
    assert ml.onboarding_limit(False) == 10
    assert ml.onboarding_limit(True) == 200


# -------------------------------------------------------- blocked destinations

@pytest.mark.parametrize("phone", ["+12125550123", "+15551234567"])
def test_marketing_to_us_numbers_is_refused(phone):
    """Meta paused marketing delivery to +1 entirely on 2025-04-01."""
    assert ml.marketing_allowed(phone) is False


@pytest.mark.parametrize("phone", ["+393331112222", "+3725551234", "+447700900123"])
def test_marketing_elsewhere_is_allowed(phone):
    assert ml.marketing_allowed(phone) is True
