#!/usr/bin/env bash
# Install the WTVB01-BT50 logger as a systemd service on Raspberry Pi OS.
#
#   sudo ./install.sh
#
# Idempotent: safe to re-run to upgrade. An existing
# /etc/wtvb01-logger/config.toml is never overwritten.
set -euo pipefail

PREFIX=/opt/wtvb01-logger
CONFIG_DIR=/etc/wtvb01-logger
SERVICE_USER=wtvb01
UNIT=wtvb01-logger.service
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/.." && pwd)"

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

echo "==> paquetes del sistema"
apt-get update -qq
apt-get install -y -qq python3-venv python3-dev bluez

echo "==> usuario de servicio: $SERVICE_USER"
id -u "$SERVICE_USER" &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
usermod -aG dialout,bluetooth "$SERVICE_USER"

echo "==> código en $PREFIX"
mkdir -p "$PREFIX"
cp -r "$repo/core" "$repo/pi" "$PREFIX/"
[[ -f "$repo/pi/README.md" ]] && cp "$repo/pi/README.md" "$PREFIX/README.md"

echo "==> entorno virtual"
python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet "$PREFIX/core[ble]" "$PREFIX/pi"

echo "==> configuración en $CONFIG_DIR"
mkdir -p "$CONFIG_DIR"
if [[ -f "$CONFIG_DIR/config.toml" ]]; then
  echo "    config.toml ya existe, se conserva"
  cp "$repo/pi/config.example.toml" "$CONFIG_DIR/config.example.toml"
else
  cp "$repo/pi/config.example.toml" "$CONFIG_DIR/config.toml"
  echo "    creado desde el ejemplo — EDÍTALO antes de arrancar"
fi
chown -R root:"$SERVICE_USER" "$CONFIG_DIR"
chmod 750 "$CONFIG_DIR"

echo "==> servicio systemd (instalado, SIN autoarranque)"
install -m 644 "$repo/pi/systemd/$UNIT" "/etc/systemd/system/$UNIT"
systemctl daemon-reload
# Deliberately not enabled: the operator decides when the logger runs. See
# the note printed at the end for how to turn autostart on later.

echo "==> enlace del comando"
ln -sf "$PREFIX/venv/bin/wtvb01-logger" /usr/local/bin/wtvb01-logger

# A config preserved from an older install can point the service somewhere the
# CLI no longer looks. Checking it here beats debugging "no service listening".
echo "==> comprobando la configuración como el usuario del servicio"
if ! sudo -u "$SERVICE_USER" "$PREFIX/venv/bin/wtvb01-logger" \
       -c "$CONFIG_DIR/config.toml" validate; then
  echo
  echo "  AVISO: el servicio NO podrá arrancar con esta configuración."
  echo "         Corrígela con: sudoedit $CONFIG_DIR/config.toml"
  echo
fi

# The unit hides /home from the service (ProtectHome=yes). Writing CSVs there
# needs the directory exposed back in, and owned so the service can write and
# you can read. A drop-in keeps the shipped unit generic.
output_dir="$(grep -oP '(?<=^output_dir = ")[^"]+' "$CONFIG_DIR/config.toml" 2>/dev/null || true)"
dropin_dir="/etc/systemd/system/$UNIT.d"
dropin="$dropin_dir/output-home.conf"
if [[ "$output_dir" == /home/* ]]; then
  echo "==> los CSV van a $output_dir (dentro de /home): exponiéndolo al servicio"
  mkdir -p "$output_dir"
  # Propietario el servicio, grupo el tuyo, setgid para que puedas leer lo escrito.
  chown "$SERVICE_USER":"${SUDO_USER:-$SERVICE_USER}" "$output_dir"
  chmod 2775 "$output_dir"
  mkdir -p "$dropin_dir"
  cat > "$dropin" <<DROPIN
# Generado por install.sh porque output_dir está dentro de /home.
# ProtectHome=tmpfs oculta /home entero; BindPaths vuelve a exponer solo el
# directorio de logs, así el servicio no ve el resto de tu home.
[Service]
ProtectHome=tmpfs
BindPaths=$output_dir
DROPIN
  systemctl daemon-reload
elif [[ -f "$dropin" ]]; then
  echo "==> output_dir ya no está en /home: retirando el drop-in"
  rm -f "$dropin"
  rmdir --ignore-fail-on-non-empty "$dropin_dir" 2>/dev/null || true
  systemctl daemon-reload
fi

socket_in_config="$(grep -oP '(?<=^control_socket = ")[^"]+' "$CONFIG_DIR/config.toml" 2>/dev/null || true)"
if [[ -n "$socket_in_config" && "$socket_in_config" != /run/wtvb01-logger/* ]]; then
  cat <<WARN

  AVISO: tu configuración usa control_socket = "$socket_in_config".
         La unidad de systemd crea /run/wtvb01-logger/ (RuntimeDirectory), así
         que el socket debe estar dentro de ese directorio o el servicio no
         podrá crearlo. Recomendado:
             control_socket = "/run/wtvb01-logger/control.sock"

WARN
fi

# So the admin can run control commands without sudo.
admin="${SUDO_USER:-}"
if [[ -n "$admin" ]] && ! id -nG "$admin" | grep -qw "$SERVICE_USER"; then
  usermod -aG "$SERVICE_USER" "$admin"
  echo "    '$admin' añadido al grupo $SERVICE_USER (vuelve a iniciar sesión para que aplique)"
fi

cat <<'NEXT'

Instalado. El servicio NO arranca solo y NO se conecta solo a los sensores.

Siguientes pasos:

  1. sudoedit /etc/wtvb01-logger/config.toml
  2. wtvb01-logger -c /etc/wtvb01-logger/config.toml validate
  3. sudo systemctl start wtvb01-logger    # levanta el servicio, inactivo
  4. wtvb01-logger connect                 # ahora sí toma los sensores
  5. wtvb01-logger status

Para soltar los sensores sin parar el servicio:
  wtvb01-logger disconnect

Para descubrir sensores:
  wtvb01-logger ports        # por cable
  wtvb01-logger ble-scan     # por Bluetooth

Si más adelante quieres que arranque y grabe solo al encender la Raspberry:
  sudo systemctl enable wtvb01-logger
  y pon connect_on_start = true en /etc/wtvb01-logger/config.toml
NEXT
