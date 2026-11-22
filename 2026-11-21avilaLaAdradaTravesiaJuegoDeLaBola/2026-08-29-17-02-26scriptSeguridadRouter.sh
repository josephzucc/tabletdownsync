#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
#  wifi_info.sh — Diagnóstico de la conexión Wi-Fi actual
#  Pensado para Termux en Android (Huawei MatePad 11.5 incluido)
# ============================================================
#
# INSTALACIÓN (una sola vez):
#   1) Instala la app "Termux:API" desde F-Droid (la MISMA fuente
#      que tu Termux; mezclar F-Droid y AppGallery/Play rompe la
#      comunicación entre ambas apps).
#   2) Dentro de Termux:
#        pkg update && pkg install termux-api jq iproute2 -y
#   3) Ajustes de Android > Apps > Termux:API > Permisos:
#        - Ubicación: "Permitir siempre" (si no, Android oculta
#          el SSID/BSSID reales y devuelve valores falsos)
#        - Android 13+: activa también "Dispositivos cercanos"
#   4) Específico de Huawei/MatePad: en Ajustes > Batería > Inicio
#      de apps, pon Termux y Termux:API en modo MANUAL con las
#      3 opciones activadas (auto-inicio, inicio secundario,
#      ejecución en segundo plano). EMUI/Magic UI mata procesos en
#      segundo plano de forma agresiva y corta la comunicación
#      Termux <-> Termux:API si no se hace esto.
#
# USO:
#   bash wifi_info.sh
# ============================================================

set -uo pipefail
sep() { printf '\n──────── %s ────────\n' "$1"; }

for c in termux-wifi-connectioninfo jq ip getprop; do
  command -v "$c" >/dev/null 2>&1 || {
    echo "Falta el comando '$c'. Revisa el paso de instalación en la cabecera del script."
    exit 1
  }
done

sep "¿WIFI ACTIVADO?"
timeout 10 termux-wifi-enabled

sep "CONEXIÓN ACTUAL (termux-wifi-connectioninfo)"
CONN=$(timeout 15 termux-wifi-connectioninfo)
echo "$CONN" | jq .

SSID=$(echo "$CONN" | jq -r '.ssid // empty')
BSSID=$(echo "$CONN" | jq -r '.bssid // empty')

sep "SEGURIDAD DE LA RED (cruzando con el último escaneo)"
if [ -z "$SSID" ] || [ "$SSID" = "<unknown ssid>" ]; then
  echo "SSID no disponible: falta el permiso de Ubicación en Termux:API (ver instalación, paso 3)."
else
  SCAN=$(timeout 20 termux-wifi-scaninfo)
  MATCH=$(echo "$SCAN" | jq --arg s "$SSID" '[.[] | select(.ssid == $s)]' 2>/dev/null)
  N=$(echo "$MATCH" | jq 'length' 2>/dev/null || echo 0)
  if [ "$N" -gt 0 ]; then
    echo "$MATCH" | jq .
  else
    echo "'$SSID' no aparece todavía en el último escaneo (a veces tarda unos segundos en refrescar; vuelve a ejecutar el script)."
  fi
  HAS_CAPS=$(echo "$MATCH" | jq '[.[] | has("capabilities")] | any' 2>/dev/null || echo false)
  if [ "$HAS_CAPS" != "true" ]; then
    cat <<'EOF'

⚠ El tipo exacto de seguridad (WPA2-PSK, WPA3, WPA2-Enterprise...) NO
  viene en el JSON de termux-wifi-scaninfo en las versiones actuales
  de Termux:API. No es un fallo de este script: es una limitación
  documentada de la propia API (campo "capabilities" pedido y aún sin
  añadir — ver issue termux/termux-api #676 en GitHub). Sin root no
  hay forma fiable de leerlo por comandos en Android moderno.
  Dato 100% fiable en dos toques:
    Ajustes > Wi-Fi > mantén pulsada tu red > "Seguridad"
EOF
  fi
fi

sep "INTERFAZ E IP"
IFACE=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}')
IFACE=${IFACE:-wlan0}
echo "Interfaz detectada: $IFACE"
ip addr show "$IFACE" 2>/dev/null
echo ""
ip route show 2>/dev/null

sep "DHCP / DNS (getprop — disponibilidad depende del fabricante)"
for p in dhcp."$IFACE".ipaddress dhcp."$IFACE".gateway dhcp."$IFACE".mask \
         dhcp."$IFACE".dns1 dhcp."$IFACE".dns2; do
  v=$(getprop "$p" 2>/dev/null)
  [ -n "$v" ] && echo "$p = $v"
done

sep "RESUMEN"
FREQ=$(echo "$CONN" | jq -r '.frequency_mhz // empty')
case "$FREQ" in
  2*) BAND="2.4 GHz" ;;
  5*) BAND="5 GHz" ;;
  6*) BAND="6 GHz" ;;
  *)  BAND="?" ;;
esac
printf "SSID:       %s\n"  "${SSID:-desconocido}"
printf "BSSID:      %s\n"  "${BSSID:-desconocido}"
printf "Frecuencia: %s MHz (%s)\n" "${FREQ:-?}" "$BAND"
printf "RSSI:       %s dBm\n" "$(echo "$CONN" | jq -r '.rssi // "?"')"
printf "Velocidad:  %s Mbps\n" "$(echo "$CONN" | jq -r '.link_speed_mbps // "?"')"
printf "IP local:   %s\n" "$(echo "$CONN" | jq -r '.ip // "?"')"
echo ""
echo "Nota: el MAC que muestra 'ip addr' puede ser una MAC aleatoria"
echo "(privacidad de Android desde v10), no la MAC física real del chip."
