import os
import pytest

from booking_engine.clients.twilio_regulatory import get_regulation_sid

pytestmark = pytest.mark.skipif(
    not os.getenv("TWILIO_ACCOUNT_SID"), reason="TWILIO_ACCOUNT_SID not set"
)

EXPECTED = "RN26dca8d0e541a6c8fce4abd46e518506"


@pytest.mark.asyncio
async def test_estonia_mobile_regulation_still_matches_the_design():
    """If this fails, Estonia changed its rules. Update design §2.1 before shipping."""
    sid = await get_regulation_sid(
        iso_country="EE", number_type="mobile",
        account_sid=os.environ["TWILIO_ACCOUNT_SID"],
        auth_token=os.environ["TWILIO_AUTH_TOKEN"],
    )
    assert sid == EXPECTED
