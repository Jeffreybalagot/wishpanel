"""Docker HEALTHCHECK: exit 0 if the local-only /health answers 200, else 1."""

import os
import sys
import urllib.request

port = os.environ.get("PORT", "8080")
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=4) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except Exception as exc:
    print(f"healthcheck failed: {exc}", file=sys.stderr)
    sys.exit(1)
