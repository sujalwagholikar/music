#!/usr/bin/env bash
set -euo pipefail

# Vercel builds run in Linux. We download Deno during the build so the
# executable is included in the function bundle without storing a 100+ MB
# binary in GitHub.
DENO_VERSION="${SUJALCONNECT_DENO_VERSION:-2.7.13}"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64)
    ASSET="deno-x86_64-unknown-linux-gnu.zip"
    ;;
  aarch64|arm64)
    ASSET="deno-aarch64-unknown-linux-gnu.zip"
    ;;
  *)
    echo "Unsupported Linux architecture for bundled Deno: $ARCH" >&2
    exit 1
    ;;
esac

URL="https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/${ASSET}"
DEST_DIR="vendor"
DEST="${DEST_DIR}/deno"
TMP="/tmp/deno-${DENO_VERSION}.zip"

mkdir -p "$DEST_DIR"
rm -f "$DEST"

echo "[SujalConnect] Downloading Deno ${DENO_VERSION} (${ARCH})..."
if command -v curl >/dev/null 2>&1; then
  curl -fL --retry 4 --retry-all-errors --connect-timeout 15 --max-time 180 "$URL" -o "$TMP"
elif command -v wget >/dev/null 2>&1; then
  wget -q --tries=4 --timeout=15 -O "$TMP" "$URL"
else
  echo "Neither curl nor wget is available in the Vercel build image." >&2
  exit 1
fi

python3 - "$TMP" "$DEST" <<'PY'
from pathlib import Path
import sys
import zipfile

archive = Path(sys.argv[1])
dest = Path(sys.argv[2])
with zipfile.ZipFile(archive) as zf:
    try:
        member = next(n for n in zf.namelist() if n.rstrip("/").endswith("/deno") or n == "deno")
    except StopIteration:
        raise SystemExit("Deno archive did not contain an executable named 'deno'")
    with zf.open(member) as src, dest.open("wb") as out:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
PY
chmod 755 "$DEST"
rm -f "$TMP"

"$DEST" --version

echo "[SujalConnect] Deno installed at $DEST"
