#!/bin/sh
# Copy this file into Roms/APPS and run it from the APPS menu, with WiFi on.
# It downloads Z3pad from GitHub and installs it next to itself.
#
# Prefer not to use WiFi? Skip this script entirely and copy the Z3pad folder
# and Z3pad.sh into Roms/APPS by hand. See the README.
set -e
progdir=$(cd "$(dirname "$0")" && pwd)
exec >"$progdir/Z3pad-install-logfile.txt" 2>&1

export GIT_TERMINAL_PROMPT=0
export GIT_HTTP_LOW_SPEED_LIMIT=1000
export GIT_HTTP_LOW_SPEED_TIME=60

REPO=Z3R0C1PH3R/Z3pad
BRANCH=main

ok=0
fail() {
    if [ "$ok" -eq 1 ]; then
        return 0
    fi
    echo "ERROR"
    # Try to say so on screen, from whichever copy of display.py exists.
    (cd /temp/Z3pad 2>/dev/null || cd "$progdir/Z3pad" 2>/dev/null) && \
        python3 -c "import display; display.draw_text('ERROR, CHECK LOGS')" || true
    cd / || true
    rm -rf /temp
    exit 1
}
trap fail EXIT

retry() {
    tries=$1
    shift
    n=1
    while [ "$n" -le "$tries" ]; do
        if "$@"; then
            return 0
        fi
        echo "attempt $n/$tries failed: $*"
        n=$((n + 1))
        sleep 5
    done
    return 1
}

msg() {
    (cd /temp/Z3pad && python3 -c "import display; display.draw_text('''$1''')") || true
}

echo "Z3pad install started $(date)"
echo "kernel $(uname -r), installing into $progdir"

echo "-- requirements --"
python3 -c "import sys; assert sys.version_info >= (3, 6), sys.version" || \
    { echo "python3 is too old or missing"; exit 1; }
test -c /dev/fb0 || echo "WARNING: no /dev/fb0, the on-screen menu will not draw"
if python3 -c "import dbus; from gi.repository import GLib" >/dev/null 2>&1; then
    echo "dbus and gi present, Bluetooth gamepad mode available"
else
    echo "dbus/gi missing: Bluetooth mode needs 'apt install python3-dbus python3-gi'"
    echo "USB gamepad mode will still work"
fi

# Prefer IPv4: these handhelds often have AAAA records but no working IPv6.
if [ -f /etc/gai.conf ]; then
    grep -q '^precedence ::ffff:0:0/96' /etc/gai.conf 2>/dev/null || \
        echo 'precedence ::ffff:0:0/96  100' >> /etc/gai.conf
else
    echo 'precedence ::ffff:0:0/96  100' > /etc/gai.conf
fi

# If GitHub does not resolve (common on busy/captive WiFi), try public DNS.
if ! getent hosts github.com >/dev/null 2>&1; then
    echo "github.com did not resolve, trying public DNS"
    iface=$(ip route 2>/dev/null | awk '/default/ {print $5; exit}')
    if command -v resolvectl >/dev/null 2>&1 && [ -n "$iface" ]; then
        resolvectl dns "$iface" 1.1.1.1 8.8.8.8 9.9.9.9 || true
    fi
fi

clone_github() {
    rm -rf /temp
    git clone --depth 1 --branch "$BRANCH" "https://github.com/$REPO.git" /temp
}

fetch_tarball() {
    rm -rf /temp /tmp/z3pad.tgz /tmp/z3pad-extract
    mkdir -p /tmp/z3pad-extract
    wget --timeout=60 --tries=8 --retry-connrefused \
        -O /tmp/z3pad.tgz \
        "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH"
    tar -xzf /tmp/z3pad.tgz -C /tmp/z3pad-extract
    mv "/tmp/z3pad-extract/$(basename "$REPO")-$BRANCH" /temp
    rm -rf /tmp/z3pad.tgz /tmp/z3pad-extract
}

echo "-- downloading Z3pad --"
if ! (command -v git >/dev/null 2>&1 && retry 3 clone_github); then
    echo "git clone unavailable or failed, trying GitHub tarball"
    retry 3 fetch_tarball
fi
test -f /temp/Z3pad/z3pad.py
test -f /temp/Z3pad/font32.bin
test -f /temp/Z3pad.sh

msg "Installing Z3pad..."

# Keep any button map the user already calibrated.
if [ -f "$progdir/Z3pad/padmap.json" ]; then
    cp "$progdir/Z3pad/padmap.json" /temp/Z3pad/padmap.json
    echo "kept existing padmap.json"
fi

cp -r /temp/Z3pad /temp/Z3pad.sh "$progdir/"
chmod a+x "$progdir/Z3pad.sh"

echo "-- what this kernel supports --"
(cd "$progdir/Z3pad" && python3 hiddiag.py) || \
    echo "diagnostic reported no usable transport, see above"

ok=1
# Z3apps installs several apps back to back, so it sets Z3APPS_NO_REBOOT and
# restarts once itself at the end. On its own this script still restarts.
if [ -n "$Z3APPS_NO_REBOOT" ]; then last="Done"; else last="Rebooting..."; fi
msg "Installing Z3pad...
Done
Install Successful
$last"
cd /
rm -rf /temp
sleep 5
[ -n "$Z3APPS_NO_REBOOT" ] || reboot
