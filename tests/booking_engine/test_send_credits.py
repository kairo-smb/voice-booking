from booking_engine.services.messaging.send_credits import send_credits


def test_one_italian_sms_segment():
    # $0.093 × 2 × 1000 credits/USD = 186
    assert send_credits(0.093) == 186


def test_two_segments():
    assert send_credits(0.186) == 372


def test_free_stays_free():
    # A WhatsApp service message inside the 24h window costs Twilio nothing.
    # rawToUserCredits() in the webapp floors at 1; that would quietly invert
    # the economics of the whole free-form model, so this must return 0.
    assert send_credits(0.0) == 0


def test_negative_and_nonsense_are_free_not_charged():
    assert send_credits(-1.0) == 0
    assert send_credits(float("nan")) == 0


def test_rounds_up_so_a_send_is_never_free_by_rounding():
    assert send_credits(0.0001) == 1   # 0.0001 × 2 × 1000 = 0.2 → ceil 1
