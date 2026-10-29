#!/usr/bin/env bash
set -euo pipefail

server_url="${1:-}"
agent_token="${2:-}"
if [[ -z "$server_url" || -z "$agent_token" ]]; then
  echo "Uso: install-device-agent.sh URL_SERVIDOR TOKEN" >&2
  exit 2
fi

install_dir="${XDG_DATA_HOME:-$HOME/.local/share}/escritorio-remoto-agent"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
env_file="${XDG_CONFIG_HOME:-$HOME/.config}/escritorio-remoto-agent.env"
python_bin="python3"
command -v "$python_bin" >/dev/null || { echo "Falta python3." >&2; exit 1; }
mkdir -p "$install_dir" "$unit_dir"

curl -fsSL "$server_url/api/device-agent/download/bundle" -o "$install_dir/device-agent.pyz"
printf 'RDWEB_AGENT_SERVER=%s\nRDWEB_AGENT_TOKEN=%s\n' "$server_url" "$agent_token" >"$env_file"
chmod 600 "$env_file"

cat >"$unit_dir/escritorio-remoto-agent.service" <<EOF
[Unit]
Description=Agente local de Escritorio Remoto
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$env_file
ExecStart=$python_bin $install_dir/device-agent.pyz
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable escritorio-remoto-agent.service
systemctl --user restart escritorio-remoto-agent.service
echo "Agente instalado y activo."
