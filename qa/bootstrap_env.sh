#!/usr/bin/env bash
# Finds, or builds, a Python interpreter that can run the InvoiceGuard backend and the QA harness.
#
#   PY="$(bash qa/bootstrap_env.sh | tail -n 1)"
#
# The interpreter path is the LAST line of stdout; progress goes to stderr.
# Nothing is ever installed into the project folder: a throwaway virtualenv is created under
# $IG_QA_VENV (default: ~/.cache/invoiceguard-qa-venv) only when no usable interpreter exists.
# The project's own .venv is tried first but is often unusable from another OS (for example a
# macOS venv seen from a Linux sandbox), which is the usual reason the fallback is needed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${IG_QA_VENV:-$HOME/.cache/invoiceguard-qa-venv}"

# Python 3.11+ is required: the app calls datetime.UTC, which does not exist on 3.10.
# httpx is needed by Starlette's TestClient; requirements.txt only pulls it in indirectly, so it is named here.
NEED='import sys
assert sys.version_info >= (3, 11)
import fastapi, sqlalchemy, pydantic, multipart, dotenv, pdfplumber, httpx'

usable() { "$1" -c "$NEED" >/dev/null 2>&1; }

for candidate in "${IG_PYTHON:-}" "$ROOT/.venv/bin/python" "$ROOT/venv/bin/python" "$VENV/bin/python" python3 python; do
  [ -n "$candidate" ] || continue
  resolved="$(command -v "$candidate" 2>/dev/null || true)"
  [ -n "$resolved" ] || continue
  if usable "$resolved"; then
    echo "using existing interpreter: $resolved" >&2
    echo "$resolved"
    exit 0
  fi
done

echo "no usable interpreter found; building a throwaway virtualenv at $VENV" >&2
mkdir -p "$(dirname "$VENV")"

if command -v uv >/dev/null 2>&1; then
  uv venv --python 3.13 "$VENV" >&2
  uv pip install --python "$VENV/bin/python" -r "$ROOT/requirements.txt" httpx >&2
else
  base=""
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; assert sys.version_info >= (3, 11)' 2>/dev/null; then
      base="$c"; break
    fi
  done
  if [ -z "$base" ]; then
    echo "error: need Python 3.11+ (or uv) to build the test environment" >&2
    exit 1
  fi
  "$base" -m venv "$VENV" >&2
  "$VENV/bin/python" -m pip install --quiet --upgrade pip >&2
  "$VENV/bin/python" -m pip install --quiet -r "$ROOT/requirements.txt" httpx >&2
fi

if usable "$VENV/bin/python"; then
  echo "$VENV/bin/python"
else
  echo "error: the virtualenv was created but the backend still cannot be imported" >&2
  exit 1
fi
