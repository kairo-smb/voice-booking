"""Print the SIP dial target for a manual softphone/pjsua test call against QA.

The `X-Shop-Id` custom header only gets attached automatically when Twilio's
<Dial><Sip> noun does it for you (it translates a `?X-Shop-Id=...` query
string into a real SIP header on the INVITE it sends OpenAI). A raw softphone
dialing OpenAI directly has no such translation layer, so this prints the
bare dial URI and the header to add separately (e.g. via pjsua's
`--add-header`, if your build supports it — check `pjsua --help | grep -i
header`).

Usage:
    set -a; source .env; set +a
    python scripts/print_sip_test_uri.py <shop_id>
"""
from __future__ import annotations

import os
import sys


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: print_sip_test_uri.py <shop_id>")
    shop_id = sys.argv[1]
    project_id = os.environ["OPENAI_SIP_PROJECT_ID"]
    print(f"Dial:          sip:{project_id}@sip.api.openai.com;transport=tls")
    print(f"Custom header: X-Shop-Id: {shop_id}")
    print("(add the header yourself — a raw client doesn't get Twilio's automatic translation)")


if __name__ == "__main__":
    main()
