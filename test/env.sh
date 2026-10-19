# Local test environment. Usage (bash or zsh):   source test/env.sh
#
# Expects OSS CAD Suite in ~/tools/oss-cad-suite (or set OSS_CAD_SUITE) and
# the Python packages in .venv:
#   python3 -m venv .venv && .venv/bin/pip install -r test/requirements.txt
_here="${BASH_SOURCE[0]:-$0}"
REFEREE_ROOT="$(cd "$(dirname "$_here")/.." && pwd)"
export OSS_CAD_SUITE="${OSS_CAD_SUITE:-$HOME/tools/oss-cad-suite}"
export PATH="$REFEREE_ROOT/.venv/bin:$REFEREE_ROOT/test/shims:$OSS_CAD_SUITE/bin:$PATH"
unset _here
