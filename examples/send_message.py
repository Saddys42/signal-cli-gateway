"""Send one message. Usage:

    SG_API_KEY=... python examples/send_message.py +39333... +39333... "ciao!"
      argv: <from-account> <to-number> <text>
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from client.sdk import SignalGateway

account, recipient, text = sys.argv[1], sys.argv[2], " ".join(sys.argv[3:])

with SignalGateway(os.getenv("SG_URL", "http://localhost:8000"),
                   api_key=os.getenv("SG_API_KEY", "")) as sg:
    print("Accounts:", sg.accounts())
    print("Result:", sg.send(account, text, recipients=[recipient]))
