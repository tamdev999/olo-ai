#!/usr/bin/env bash
set -euo pipefail
PYTHON=/root/olo_linux_venv/bin/python3
$PYTHON -m pip install \
    "google-auth==2.37.0" \
    "google-auth-oauthlib==1.2.1" \
    "google-api-python-client==2.154.0" \
    2>&1 | tail -3
echo "Install done: $?"
$PYTHON -c "import google.oauth2.credentials; print('google-auth OK')"
$PYTHON -c "import googleapiclient.discovery; print('google-api-client OK')"
