# opencode over Tailscale, without agy-remote in the middle.
#
# agy-remote used to front opencode as a second backend. It no longer does:
# `opencode serve` already hosts a mobile-tuned web app at `/`, so the only
# thing left to solve is getting to it from a phone, safely. That is one
# `tailscale serve` away.
#
# opencode prints "OPENCODE_SERVER_PASSWORD is not set; server is unsecured" and
# means it -- anything that reaches the port can drive an agent with shell
# access. So the server stays on 127.0.0.1 (its default) and the tailnet proxy
# is the only door. Set OPENCODE_SERVER_PASSWORD too, and never `tailscale
# funnel` this: that is the public internet.
#
# Source it from ~/.zshrc:
#
#   source /path/to/agy-remote/contrib/opencode-tailscale.zsh
#
#   ocs            # opencode on :4096, tailnet HTTPS on :8443
#   ocs 5000 9443  # pick your own opencode port / tailnet port
#   ocs-stop       # tear both down

ocs() {
  local oc_port="${1:-4096}"
  local ts_port="${2:-8443}"

  if lsof -nP -iTCP:"$oc_port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "opencode already listening on $oc_port, reusing it"
  else
    opencode serve --port "$oc_port" >"${TMPDIR:-/tmp}/opencode-$oc_port.log" 2>&1 &
    local tries=0
    while ! curl -sf -m 1 "http://127.0.0.1:$oc_port/session" >/dev/null 2>&1; do
      (( ++tries > 40 )) && {
        echo "opencode did not come up on $oc_port — see ${TMPDIR:-/tmp}/opencode-$oc_port.log"
        return 1
      }
      sleep 0.25
    done
    echo "opencode serve → 127.0.0.1:$oc_port"
  fi

  tailscale serve --bg --https "$ts_port" "$oc_port" >/dev/null || {
    echo "tailscale serve failed (is tailscale up?)"
    return 1
  }

  local host
  host=$(tailscale status --json | python3 -c 'import sys,json; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
  local url="https://${host}:${ts_port}/"

  echo
  echo "  $url"
  echo
  if command -v qrencode >/dev/null 2>&1; then
    qrencode -t ANSIUTF8 "$url"
  elif python3 -c 'import qrcode' >/dev/null 2>&1; then
    python3 - "$url" <<'PY'
import sys, qrcode
qr = qrcode.QRCode(border=1)
qr.add_data(sys.argv[1])
qr.make(fit=True)
qr.print_ascii(invert=True)
PY
  else
    echo "  (install qrencode for a scannable code: brew install qrencode)"
  fi
  echo "  stop with: ocs-stop ${oc_port} ${ts_port}"
}

ocs-stop() {
  local oc_port="${1:-4096}"
  local ts_port="${2:-8443}"
  tailscale serve --https "$ts_port" off >/dev/null 2>&1 || tailscale serve reset >/dev/null 2>&1
  pkill -f "opencode serve --port $oc_port" 2>/dev/null
  echo "stopped opencode on $oc_port and the tailnet proxy on $ts_port"
}
