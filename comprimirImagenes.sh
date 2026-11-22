#!/data/data/com.termux/files/usr/bin/bash
# =====================================================================
#  comprimir_capturas.sh - Comprime capturas de pantalla UNA A UNA
#  (Termux, Android sin root). Genera una imagen comprimida por cada
#  imagen original; no junta nada en un unico archivo.
#
#  Requisitos (una vez):  pkg install imagemagick
#                         termux-setup-storage
#  Uso:                   bash comprimir_capturas.sh            (busca las carpetas de capturas)
#                         bash comprimir_capturas.sh /ruta/carpeta
#
#  Flujo:
#    1) Analiza cada imagen: tamano, resolucion y formato.
#    2) Prueba la compresion con una muestra y estima el ahorro total
#       para 3 perfiles (suave, medio, fuerte).
#    3) Tu eliges perfil y comprime una por una en una carpeta aparte.
#    4) Al final puedes reemplazar los originales (solo si ahorra espacio).
# =====================================================================

R="/storage/emulated/0"
TMP="${TMPDIR:-${PREFIX:-/tmp}/tmp}/capt.$$"
mkdir -p "$TMP"
trap 'rm -rf "$TMP"' EXIT

c_r='\e[31m'; c_v='\e[32m'; c_a='\e[33m'; c_b='\e[36m'; c_n='\e[0m'
hum(){ numfmt --to=iec --suffix=B "${1:-0}" 2>/dev/null || echo "${1:-0}B"; }
confirmar(){ local x; read -r -p "$1 [s/N]: " x; [[ "$x" =~ ^[sS]$ ]]; }
titulo(){ echo; echo -e "${c_b}== $1 ==${c_n}"; }

# --- ImageMagick: 'magick' (v7) o 'convert' (v6) ---------------------
if command -v magick >/dev/null; then IM="magick"; ID="magick identify"
elif command -v convert >/dev/null; then IM="convert"; ID="identify"
else echo "Falta ImageMagick. Ejecuta: pkg install imagemagick"; exit 1; fi

# --- Carpetas de capturas ---------------------------------------------
DIRS=()
if [ -n "${1:-}" ]; then
  [ -d "$1" ] || { echo "No existe la carpeta: $1"; exit 1; }
  DIRS=("$1")
else
  ls "$R" >/dev/null 2>&1 || { echo "Sin permiso de almacenamiento. Ejecuta: termux-setup-storage"; exit 1; }
  for d in "$R/Pictures/Screenshots" "$R/DCIM/Screenshots" "$R/Screenshots" "$R/Pictures/Screenshot"; do
    [ -d "$d" ] && DIRS+=("$d")
  done
  [ ${#DIRS[@]} -eq 0 ] && { echo "No encuentro la carpeta de capturas. Pasala como parametro."; exit 1; }
fi

# --- Perfiles de compresion -------------------------------------------
#  nombre | calidad | lado mayor maximo (px) | formato
#  Las capturas de la MatePad 11.5 son de 2800x1840: bajar a 1920 sigue
#  siendo perfectamente legible.
PERF_N=("Suave"  "Medio"  "Fuerte")
PERF_Q=(85       75       65)
PERF_M=(2800     1920     1440)
PERF_F=(jpg      jpg      webp)
PERF_D=("mantiene resolucion, apenas se nota"
        "recomendado: legible y mucho mas ligero"
        "maximo ahorro, formato WebP (lo abre la galeria)")

comprimir(){  # $1 origen  $2 destino  $3 calidad  $4 lado max  $5 formato
  local src="$1" dst="$2" q="$3" m="$4" f="$5"
  if [ "$f" = "webp" ]; then
    $IM "$src" -auto-orient -strip -resize "${m}x${m}>" -quality "$q" \
        -define webp:method=6 "$dst" 2>/dev/null </dev/null
  else
    $IM "$src" -auto-orient -strip -resize "${m}x${m}>" -background white -flatten \
        -sampling-factor 4:2:0 -interlace JPEG -quality "$q" "$dst" 2>/dev/null </dev/null
  fi
}

# =====================================================================
# 1) ANALISIS IMAGEN POR IMAGEN
# =====================================================================
titulo "1) Analizando imagenes"
LISTA="$TMP/lista"   # tamano<TAB>ancho<TAB>alto<TAB>formato<TAB>ruta
: > "$LISTA"
n=0
for d in "${DIRS[@]}"; do
  echo "Carpeta: ${d#$R/}"
  while IFS= read -r -d '' f; do
    n=$((n+1))
    s=$(stat -c %s -- "$f")
    dim=$($ID -ping -format '%w %h %m\n' "$f[0]" 2>/dev/null </dev/null | head -n 1)
    read -r w h fm <<< "$dim"
    [[ "$w" =~ ^[0-9]+$ ]] || continue
    printf '%s\t%s\t%s\t%s\t%s\n' "$s" "$w" "$h" "$fm" "$f" >> "$LISTA"
    printf "\r  Leidas: %d" "$n"
  done < <(find "$d" -maxdepth 1 -type f \
             \( -iname '*.png' -o -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.webp' \) -print0)
  echo
done

TOTAL_N=$(wc -l < "$LISTA")
[ "$TOTAL_N" -eq 0 ] && { echo "No hay imagenes."; exit 0; }
TOTAL_B=$(awk -F'\t' '{t+=$1} END{printf "%.0f", t}' "$LISTA")

echo
echo "  Imagenes:        $TOTAL_N"
echo "  Espacio total:   $(hum "$TOTAL_B")"
echo "  Tamano medio:    $(hum $((TOTAL_B / TOTAL_N)))"
echo
echo "  Por formato:"
awk -F'\t' '{s[$4]+=$1; c[$4]++} END{for(k in s) printf "%.0f\t%s\t%d\n", s[k], k, c[k]}' "$LISTA" \
  | sort -rn | while IFS=$'\t' read -r s k c; do
      printf "    %-6s %5d imagenes  %9s\n" "$k" "$c" "$(hum "$s")"; done
echo
echo "  Por tamano:"
awk -F'\t' '{m=$1/1048576; if(m<0.5)a++; else if(m<1)b++; else if(m<2)c++; else if(m<5)d++; else e++}
  END{printf "    < 0,5 MB: %d\n    0,5-1 MB: %d\n    1-2 MB:   %d\n    2-5 MB:   %d\n    > 5 MB:   %d\n", a,b,c,d,e}' "$LISTA"
echo
echo "  Las 10 mas pesadas:"
sort -rn "$LISTA" | head -n 10 | while IFS=$'\t' read -r s w h fm p; do
  printf "    %9s  %4sx%-4s %-4s  %s\n" "$(hum "$s")" "$w" "$h" "$fm" "$(basename "$p")"; done

# =====================================================================
# 2) PRUEBA CON MUESTRA Y PROPUESTA GENERAL
# =====================================================================
titulo "2) Probando compresion con una muestra"
MUESTRA=20
[ "$TOTAL_N" -lt "$MUESTRA" ] && MUESTRA=$TOTAL_N
# muestra repartida entre pequenas y grandes
sort -n "$LISTA" | awk -v n="$MUESTRA" -v t="$TOTAL_N" 'NR==1||int((NR-1)*n/t)!=int((NR-2)*n/t)' \
  | head -n "$MUESTRA" > "$TMP/muestra"

declare -a EST
for i in 0 1 2; do
  orig=0; comp=0
  while IFS=$'\t' read -r s w h fm p; do
    out="$TMP/prueba.${PERF_F[$i]}"
    comprimir "$p" "$out" "${PERF_Q[$i]}" "${PERF_M[$i]}" "${PERF_F[$i]}" || continue
    c=$(stat -c %s "$out"); [ "$c" -gt "$s" ] && c=$s   # si no mejora, se conserva el original
    orig=$((orig+s)); comp=$((comp+c))
  done < "$TMP/muestra"
  [ "$orig" -eq 0 ] && orig=1
  EST[$i]=$(awk -v t="$TOTAL_B" -v o="$orig" -v c="$comp" 'BEGIN{printf "%.0f", t*c/o}')
done

echo
printf "  %-3s %-7s %-8s %-9s %-6s %11s %11s\n" "" "Perfil" "Calidad" "Lado max" "Formato" "Quedaria" "Ahorro"
for i in 0 1 2; do
  ah=$((TOTAL_B - EST[$i]))
  pct=$(awk -v a="$ah" -v t="$TOTAL_B" 'BEGIN{printf "%.0f", 100*a/t}')
  printf "  %-3s %-7s %-8s %-9s %-6s %11s %8s (%s%%)\n" "$((i+1)))" "${PERF_N[$i]}" "${PERF_Q[$i]}" \
    "${PERF_M[$i]}px" "${PERF_F[$i]}" "$(hum "${EST[$i]}")" "$(hum "$ah")" "$pct"
  echo "        -> ${PERF_D[$i]}"
done
echo "  4)  Personalizado"
echo
echo -e "  ${c_a}Estimacion basada en $MUESTRA imagenes de muestra.${c_n}"

read -r -p "Elige perfil [2]: " op; op=${op:-2}
case "$op" in
  1|2|3) i=$((op-1)); Q=${PERF_Q[$i]}; M=${PERF_M[$i]}; F=${PERF_F[$i]} ;;
  4) read -r -p "  Calidad (40-95) [75]: " Q; Q=${Q:-75}
     read -r -p "  Lado mayor maximo en px [1920]: " M; M=${M:-1920}
     read -r -p "  Formato jpg/webp [jpg]: " F; F=${F:-jpg}
     [[ "$Q" =~ ^[0-9]+$ && "$M" =~ ^[0-9]+$ ]] || { echo "Valores no validos"; exit 1; }
     [[ "$F" = "jpg" || "$F" = "webp" ]] || F=jpg ;;
  *) echo "Opcion no valida"; exit 1 ;;
esac

# =====================================================================
# 3) COMPRESION UNA A UNA
# =====================================================================
titulo "3) Comprimiendo una a una (calidad $Q, max ${M}px, $F)"
LOG="$TMP/resultado"   # original<TAB>comprimida<TAB>tam_orig<TAB>tam_comp
: > "$LOG"
k=0; ahorro=0; saltadas=0; errores=0
while IFS=$'\t' read -r s w h fm p; do
  k=$((k+1))
  dir=$(dirname "$p"); base=$(basename "$p"); nom="${base%.*}"
  outdir="$dir/_comprimidas"; mkdir -p "$outdir"
  out="$outdir/$nom.$F"
  [ -e "$out" ] && out="$outdir/${nom}_${k}.$F"   # evita pisar si hay foto.png y foto.jpg
  if ! comprimir "$p" "$out" "$Q" "$M" "$F" || [ ! -s "$out" ]; then
    errores=$((errores+1)); rm -f "$out"
    printf "[%d/%d] ${c_r}ERROR${c_n}  %s\n" "$k" "$TOTAL_N" "$base"; continue
  fi
  c=$(stat -c %s "$out")
  if [ "$c" -ge "$s" ]; then           # no compensa: se descarta
    rm -f "$out"; saltadas=$((saltadas+1))
    printf "[%d/%d] %-40.40s %9s -> ya optimizada, se deja igual\n" "$k" "$TOTAL_N" "$base" "$(hum "$s")"
    continue
  fi
  touch -r "$p" "$out"                 # conserva la fecha para que la galeria no la desordene
  ahorro=$((ahorro + s - c))
  printf '%s\t%s\t%s\t%s\n' "$p" "$out" "$s" "$c" >> "$LOG"
  printf "[%d/%d] %-40.40s %9s -> %9s (-%s%%)\n" "$k" "$TOTAL_N" "$base" "$(hum "$s")" "$(hum "$c")" \
    "$(awk -v a="$s" -v b="$c" 'BEGIN{printf "%.0f", 100*(a-b)/a}')"
done < "$LISTA"

hechas=$(wc -l < "$LOG")
titulo "Resumen"
echo "  Comprimidas:            $hechas"
echo "  Ya optimizadas (igual): $saltadas"
echo "  Errores:                $errores"
echo -e "  ${c_v}Ahorro posible:         $(hum "$ahorro")${c_n}"
echo "  Las comprimidas estan en la subcarpeta '_comprimidas' de cada carpeta."
echo "  Revisa algunas en la galeria antes de seguir."
[ "$hechas" -eq 0 ] && exit 0

# =====================================================================
# 4) REEMPLAZAR ORIGINALES (opcional)
# =====================================================================
echo
echo "Opciones:"
echo "  1) Reemplazar los originales por las comprimidas (libera $(hum "$ahorro"))"
echo "  2) Dejar ambas (NO libera espacio, ocupa mas hasta que borres algo)"
echo "  3) Borrar las comprimidas y dejar todo como estaba"
read -r -p "Opcion [2]: " fin; fin=${fin:-2}

case "$fin" in
  1)
    confirmar "Se BORRARAN $hechas originales (sin papelera). ¿Seguro?" || { echo "Cancelado. Quedan en _comprimidas."; exit 0; }
    ok=0
    while IFS=$'\t' read -r p out s c; do
      dest="$(dirname "$p")/$(basename "$out")"
      # si cambia la extension y ya existe otro archivo con ese nombre, no se pisa
      if [ "$dest" != "$p" ] && [ -e "$dest" ]; then
        echo "  Existe ya $(basename "$dest"), se deja en _comprimidas"; continue
      fi
      $ID -ping "$out" >/dev/null 2>&1 </dev/null || { echo "  Comprimida no valida, se conserva original: $(basename "$p")"; continue; }
      rm -f -- "$p" && mv -- "$out" "$dest" && ok=$((ok+1))
    done < "$LOG"
    for d in "${DIRS[@]}"; do rmdir "$d/_comprimidas" 2>/dev/null; done
    echo -e "${c_v}Reemplazadas: $ok${c_n}"
    if command -v termux-media-scan >/dev/null; then
      for d in "${DIRS[@]}"; do termux-media-scan -r "$d" >/dev/null 2>&1; done
      echo "Galeria actualizada."
    else
      echo "Si la galeria muestra huecos, reinicia la tablet o instala: pkg install termux-api"
    fi
    ;;
  3)
    while IFS=$'\t' read -r p out s c; do rm -f -- "$out"; done < "$LOG"
    for d in "${DIRS[@]}"; do rmdir "$d/_comprimidas" 2>/dev/null; done
    echo "Comprimidas eliminadas. Todo como estaba."
    ;;
  *) echo "Se dejan ambas versiones." ;;
esac
echo
df -h "$R" 2>/dev/null | awk 'NR==2{print "Espacio libre ahora: "$4}'
