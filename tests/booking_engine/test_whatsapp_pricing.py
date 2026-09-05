from booking_engine.services.messaging import whatsapp_pricing as wp


def test_a_service_message_is_free_again():
    """Going direct to Meta removed the only non-zero component.

    Under Twilio a free-form reply inside the 24h window still cost the flat
    platform fee, which is what docs/messaging-design.md §5.1 originally got
    wrong and the 2026-08-22 entry corrected. As a Meta Tech Provider there is
    no such fee, so §5.1's "$0 total" is true after all.
    """
    assert wp.estimate_usd("service") == 0.0


def test_marketing_is_metas_italian_list_price_and_nothing_else():
    assert wp.estimate_usd("marketing") == wp.META_USD_IT["marketing"]


def test_marketing_costs_more_than_a_reminder():
    """If this ever inverts, the UI is telling owners to pick the wrong channel."""
    assert wp.estimate_usd("marketing") > wp.estimate_usd("utility")


def test_price_list_covers_what_the_owner_can_actually_trigger():
    assert {row["kind"] for row in wp.price_list()} == {
        "marketing", "utility", "service"
    }


def test_price_list_quotes_no_credits():
    """The salon pays Meta directly; Kairo debits nothing for a WhatsApp send.

    A `credits` key here would be the visible half of double-charging — the
    salon's own card is already on its own WABA.
    """
    for row in wp.price_list():
        assert "credits" not in row
