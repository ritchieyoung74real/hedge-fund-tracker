#!/bin/bash
# Double-click this file on a Mac to refresh the data and open the dashboard.
cd "$(dirname "$0")"
echo "Updating hedge fund data (the first run can take 10-15 minutes)..."
python3 tracker.py
open docs/index.html
echo "Done. You can close this window."
