"""Link the gateway as a secondary device of your phone (no SMS/captcha).

Saves the QR code to link-qr.png: open Signal on the phone ->
Settings -> Linked devices -> + and scan it, then this script completes.

    SG_API_KEY=... python examples/link_account.py
"""

import base64
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from client.sdk import SignalGateway

with SignalGateway(os.getenv("SG_URL", "http://localhost:8000"),
                   api_key=os.getenv("SG_API_KEY", "")) as sg:
    start = sg.link_start()
    out = os.path.join(os.path.dirname(__file__), "link-qr.png")
    with open(out, "wb") as fh:
        fh.write(base64.b64decode(start["qr_png_base64"]))
    print(f"Scan {out} with the Signal app within ~60 seconds...")
    print("Linked:", sg.link_finish(start["device_link_uri"]))
    print("Accounts:", sg.accounts())
