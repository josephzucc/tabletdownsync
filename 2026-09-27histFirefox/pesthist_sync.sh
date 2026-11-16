#!/data/data/com.termux/files/usr/bin/bash
# ============================================================================
#  pesthist_sync.sh  -  Descarga el historial de Firefox (Android) desde
#  Firefox Sync para buscarlo con la extensión PestHist.     v1.5
#
#  Uso:
#    bash pesthist_sync.sh instalar        instala proot y ffsclient
#    bash pesthist_sync.sh login           inicia sesión en tu cuenta Mozilla
#    bash pesthist_sync.sh historial [N]   descarga el historial (N = últimos
#                                          N días; sin N = todo)
#    bash pesthist_sync.sh pestanas-json F guarda en F las pestañas de Sync
#                                          (lo usa pesthist.py)
#    bash pesthist_sync.sh estado          comprueba la sesión
#    bash pesthist_sync.sh salir           borra la sesión guardada
#
#  El archivo se guarda en la carpeta donde ejecutas el comando y se copia
#  a Descargas (si ejecutaste termux-setup-storage) para cargarlo en PestHist.
# ============================================================================
set -u

BIN="$PREFIX/bin/ffsclient-bin"
REL="https://github.com/Mikescher/firefox-sync-client/releases/latest/download"
SRC="https://github.com/Mikescher/firefox-sync-client.git"
DESCARGAS="$HOME/storage/downloads"
ZI="$HOME/.local/share/pesthist/zoneinfo.zip"   # zonas horarias para ffsclient
ZI_URL="https://raw.githubusercontent.com/golang/go/master/lib/time/zoneinfo.zip"

error() { echo "ERROR: $*" >&2; exit 1; }

# ffsclient es un programa Linux: en Android no encuentra por sí solo el DNS,
# los certificados ni las zonas horarias, así que usa los de Termux.
ffs() {
  [ -x "$BIN" ] || error "ffsclient no está instalado. Ejecuta: bash $0 instalar"
  zonas
  # Termux no define $USER y Android no tiene /etc/passwd: se le da uno.
  USER="${USER:-$(id -un 2>/dev/null || echo termux)}" HOME="$HOME" \
  SSL_CERT_FILE="$PREFIX/etc/tls/cert.pem" ZONEINFO="$ZI" \
    proot -b "$PREFIX/etc/resolv.conf:/etc/resolv.conf" "$BIN" "$@"
}

# Termux no trae zonas horarias en formato Linux: se descarga el archivo
# oficial de Go (zoneinfo.zip, ~400 KB) una sola vez.
zonas() {
  [ -s "$ZI" ] && return 0
  echo ">> Descargando zonas horarias (una sola vez)..."
  mkdir -p "$(dirname "$ZI")"
  curl -fsSL -o "$ZI" "$ZI_URL" || { rm -f "$ZI"; error "no se pudo descargar $ZI_URL"; }
}

instalar() {
  echo ">> Instalando paquetes de Termux (proot, curl, certificados)..."
  pkg install -y proot curl ca-certificates resolv-conf || error "falló pkg install"

  case "$(uname -m)" in
    aarch64|arm64) ARCH="arm64" ;;
    x86_64)        ARCH="amd64" ;;
    *)             ARCH="" ;;
  esac

  if [ -n "$ARCH" ]; then
    echo ">> Descargando ffsclient ($ARCH)..."
    curl -fL -o "$BIN" "$REL/ffsclient_linux-$ARCH-static" || error "no se pudo descargar ffsclient"
  else
    echo ">> Tu procesador ($(uname -m)) no tiene binario publicado: se compila (tarda unos minutos)..."
    pkg install -y golang git || error "falló pkg install golang git"
    TMP="$(mktemp -d)"
    git clone --depth 1 "$SRC" "$TMP/ffs" || error "falló git clone"
    (cd "$TMP/ffs" && CGO_ENABLED=0 GOOS=linux go build -o "$BIN" ./cmd/ffsclient) || error "falló la compilación"
    rm -rf "$TMP"
  fi
  chmod +x "$BIN"
  echo ">> Versión instalada:"
  ffs --version
  echo
  echo "Listo. Ahora ejecuta:  bash $0 login"
  [ -d "$DESCARGAS" ] || echo "Consejo: ejecuta 'termux-setup-storage' para que el archivo se copie a Descargas."
}

login() {
  read -r -p "Email de tu cuenta Mozilla (Firefox Sync): " EMAIL
  read -r -s -p "Contraseña: " PASS; echo
  [ -n "$EMAIL" ] && [ -n "$PASS" ] || error "faltan email o contraseña"
  echo ">> Iniciando sesión. Si Mozilla te envía un código por email (o usas"
  echo "   una app de doble factor), el programa te lo pedirá a continuación."
  ffs login "$EMAIL" "$PASS" --device-name="Termux PestHist" --device-type="mobile"
  unset PASS
}

historial() {
  DIAS="${1:-}"
  STAMP="$(date +%Y%m%d_%H%M)"
  OUT="$(pwd)/historial_firefox_${STAMP}.json"
  ARGS=(history list --format json --ignore-schema-errors --sort newest --output "$OUT")
  if [ -n "$DIAS" ]; then
    [[ "$DIAS" =~ ^[0-9]+$ ]] || error "N días debe ser un número"
    DESDE="$(date -u -d "-$DIAS days" +%Y-%m-%dT%H:%M:%SZ)"
    ARGS+=(--after "$DESDE")
    echo ">> Descargando historial de los últimos $DIAS días..."
  else
    echo ">> Descargando todo el historial guardado en Firefox Sync..."
  fi
  ffs "${ARGS[@]}" || error "falló la descarga (¿sesión caducada? prueba: bash $0 login)"
  [ -s "$OUT" ] || error "no se generó el archivo"
  N="$(grep -c '"uri"' "$OUT" || true)"
  echo ">> Guardado: $OUT  ($N páginas)"
  if [ -d "$DESCARGAS" ]; then
    cp "$OUT" "$DESCARGAS/" && echo ">> Copiado a Descargas: $DESCARGAS/$(basename "$OUT")"
  fi
  echo
  echo "En Firefox: PestHist -> Historial -> 'Cargar archivo(s)...' -> elige ese archivo."
}

case "${1:-}" in
  instalar)  instalar ;;
  login)     login ;;
  historial) historial "${2:-}" ;;
  pestanas-json)
             [ -n "${2:-}" ] || error "falta el archivo de salida"
             ffs list tabs --decoded --format json --output "$2" ;;
  estado)    ffs check-session ;;
  salir)     rm -f "$HOME/.config/firefox-sync-client.secret" && echo "Sesión borrada." ;;
  *) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
