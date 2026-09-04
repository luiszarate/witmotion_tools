#!/usr/bin/env bash
# Launch the WTVB01-BT50 visualiser, creating the virtualenv on first run.
#
#   ./run.sh                 # web UI, auto-detects the port
#   ./run.sh monitor         # terminal readout
#   ./run.sh record -d 60    # 60 s CSV capture
#   ./run.sh ports           # list serial ports
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
venv="$here/.venv"
python="${PYTHON:-python3}"

if [[ ! -x "$venv/bin/python" ]]; then
  echo "Creating virtualenv in $venv"
  "$python" -m venv "$venv"
  "$venv/bin/pip" install --quiet --upgrade pip
  "$venv/bin/pip" install --quiet -r "$here/requirements.txt"
fi

cd "$here"
exec "$venv/bin/python" -m wtvb01_monitor "$@"
