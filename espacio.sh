#!/data/data/com.termux/files/usr/bin/bash
# =====================================================================
#  espacio.sh - Analiza y libera espacio en Android SIN ROOT (Termux)
#  Probado para tablets Huawei (EMUI/HarmonyOS) y Android en general.
#
#  Requisito previo (solo una vez):   termux-setup-storage
#  Uso:                                bash espacio.sh
#
#  Nada se borra sin que tu lo confirmes. Nunca toca fuera del
#  almacenamiento interno ni el directorio de Termux.
# =====================================================================

R="/storage/emulated/0"
TMP="${TMPDIR:-$PREFIX/tmp}/espacio.$$"
mkdir -p "$TMP"
trap 'rm -rf "$TMP"' EXIT

c_r='\e[31m'; c_v='\e[32m'; c_a='\e[33m'; c_b='\e[36m'; c_n='\e[0m'

hum(){ numfmt --to=iec --suffix=B "${1:-0}" 2>/dev/null || echo "${1:-0}"; }
pausa(){ echo; read -r -p "Pulsa Enter para volver al menu..." _; }
confirmar(){ local x; read -r -p "$1 [s/N]: " x; [[ "$x" =~ ^[sS]$ ]]; }
titulo(){ echo; echo -e "${c_b}== $1 ==${c_n}"; }

comprobar(){
  if ! ls "$R" >/dev/null 2>&1; then
    echo -e "${c_r}Termux no tiene permiso de almacenamiento.${c_n}"
    echo "Ejecuta:  termux-setup-storage   (acepta el permiso) y vuelve a lanzar."
    exit 1
  fi
  local p
  for p in du find sort numfmt md5sum awk stat realpath uniq; do
    command -v "$p" >/dev/null || { echo "Falta '$p'. Ejecuta: pkg install coreutils findutils gawk"; exit 1; }
  done
}

# Borrado con red de seguridad: solo dentro del almacenamiento interno o Termux
borrar(){
  local p rp
  for p in "$@"; do
    rp=$(realpath -- "$p" 2>/dev/null) || { echo "No existe: $p"; continue; }
    case "$rp" in
      "$R"|"$R/Android"|"$R/DCIM"|"$R/Download"|"$R/Pictures"|"$HOME"|"$PREFIX")
        echo -e "${c_a}Protegido, no se borra:${c_n} $rp" ;;
      "$R"/*|"$HOME"/*|"$PREFIX/tmp"/*)
        rm -rf -- "$rp" && echo -e "${c_v}Borrado:${c_n} ${rp#$R/}" ;;
      *) echo -e "${c_r}Fuera de zona permitida:${c_n} $rp" ;;
    esac
  done
}

# Recibe un fichero "tamano<TAB>ruta", lo numera y permite borrar por numero
mostrar_y_borrar(){
  local f="$1" i=0 s p n sel total
  local -a lista
  [ -s "$f" ] || { echo "Nada encontrado."; return; }
  while IFS=$'\t' read -r s p; do
    i=$((i+1)); lista[$i]="$p"
    printf "%3d) %9s  %s\n" "$i" "$(hum "$s")" "${p#$R/}"
  done < "$f"
  total=$(awk -F'\t' '{t+=$1} END{printf "%.0f", t}' "$f")
  echo -e "     ${c_a}Total listado: $(hum "$total")${c_n}"
  echo
  read -r -p "Numeros a borrar (ej: 1 3 7), 'todos', o Enter para no borrar: " sel
  [ -z "$sel" ] && return
  [ "$sel" = "todos" ] && sel=$(seq 1 "$i")
  echo "Vas a borrar:"
  for n in $sel; do
    [[ "$n" =~ ^[0-9]+$ ]] && [ -n "${lista[$n]:-}" ] && echo "   - ${lista[$n]#$R/}"
  done
  confirmar "¿Borrar DEFINITIVAMENTE? (no hay papelera)" || { echo "Cancelado."; return; }
  for n in $sel; do
    [[ "$n" =~ ^[0-9]+$ ]] && [ -n "${lista[$n]:-}" ] && borrar "${lista[$n]}"
  done
}

# ---------------------------------------------------------------------
resumen(){
  titulo "Espacio del almacenamiento interno"
  df -h "$R" 2>/dev/null | awk 'NR<=2'
  titulo "Espacio usado por Termux"
  du -sh "$HOME" "$PREFIX" 2>/dev/null
}

carpetas(){
  local dir="${1:-$R}"
  titulo "Carpetas mas pesadas en ${dir#$R/} (2 niveles)"
  echo "Calculando..."
  du -h --max-depth=2 "$dir" 2>/dev/null | sort -rh | head -n 30
}

explorar(){
  local sub
  read -r -p "Carpeta a explorar (ej: DCIM, Download, Android/media): " sub
  [ -d "$R/$sub" ] || { echo "No existe: $sub"; return; }
  carpetas "$R/$sub"
}

grandes(){
  local mb
  read -r -p "Tamano minimo en MB [100]: " mb; mb=${mb:-100}
  [[ "$mb" =~ ^[0-9]+$ ]] || mb=100
  titulo "Archivos mayores de ${mb} MB"
  echo "Buscando (puede tardar)..."
  find "$R" "$HOME" -type f -size +"${mb}"M -printf '%s\t%p\n' 2>/dev/null \
    | sort -rn | head -n 60 > "$TMP/grandes"
  mostrar_y_borrar "$TMP/grandes"
}

tipos(){
  titulo "Espacio por tipo de archivo"
  echo "Calculando..."
  find "$R" -type f -printf '%s\t%f\n' 2>/dev/null | awk -F'\t' '
  { n=tolower($2); t="Otros"
    if      (n ~ /\.(mp4|mkv|avi|mov|3gp|webm|ts)$/)            t="Video"
    else if (n ~ /\.(jpg|jpeg|png|heic|heif|gif|webp|dng|raw)$/) t="Imagen"
    else if (n ~ /\.(mp3|m4a|opus|ogg|wav|flac|aac|amr)$/)       t="Audio"
    else if (n ~ /\.(apk|xapk|apks)$/)                           t="APK"
    else if (n ~ /\.(zip|rar|7z|tar|gz|xz|bz2)$/)                t="Comprimido"
    else if (n ~ /\.(pdf|docx?|xlsx?|pptx?|txt|odt|ods|epub)$/)  t="Documento"
    else if (n ~ /\.(iso|img|bin|vhd)$/)                         t="Imagen disco"
    s[t]+=$1; c[t]++ }
  END { for (t in s) printf "%.0f\t%s\t%d\n", s[t], t, c[t] }' \
  | sort -rn | while IFS=$'\t' read -r s t c; do
      printf "  %-13s %9s   (%d archivos)\n" "$t" "$(hum "$s")" "$c"
    done
}

conocidas(){
  titulo "Carpetas tipicas que engordan"
  local d
  for d in \
    "Android/media/com.whatsapp/WhatsApp/Media" "WhatsApp/Media" \
    "Android/media/com.whatsapp.w4b" "Android/media/org.telegram.messenger" "Telegram" \
    "DCIM/Camera" "DCIM/Screenshots" "Pictures/Screenshots" \
    "DCIM/.thumbnails" "Pictures/.thumbnails" "Pictures/.Gallery2" \
    "Download" "Movies" "Music" "Documents" "Huawei" "HuaweiBackup" "Backup"; do
    [ -d "$R/$d" ] && printf "  %8s  %s\n" "$(du -sh "$R/$d" 2>/dev/null | cut -f1)" "$d"
  done
  titulo "Carpetas ocultas en la raiz"
  du -sh "$R"/.[!.]* 2>/dev/null | sort -rh | head -n 15
  echo
  echo "Nota: Android/data y Android/obb no son accesibles sin root en Android 11+."
}

duplicados(){
  titulo "Archivos duplicados (> 1 MB)"
  echo "Paso 1/2: agrupando por tamano..."
  find "$R" -type f -size +1M -not -path "$R/Android/data/*" -not -path "$R/Android/obb/*" \
       -printf '%s\t%p\n' 2>/dev/null > "$TMP/todos"
  awk -F'\t' 'NR==FNR{c[$1]++; next} c[$1]>1{print $2}' "$TMP/todos" "$TMP/todos" > "$TMP/cand0"
  echo "Paso 2/2: comparando contenido de $(wc -l < "$TMP/cand0") archivos (puede tardar)..."
  while IFS= read -r p; do md5sum -- "$p" 2>/dev/null; done < "$TMP/cand0" | sort > "$TMP/md5"
  uniq -w32 --all-repeated=separate "$TMP/md5" > "$TMP/dups"
  [ -s "$TMP/dups" ] || { echo "No hay duplicados."; return; }
  # De cada grupo se conserva la primera copia; se listan solo las sobrantes
  awk 'NF==0{g=0; next} { if (g++) print substr($0,35) }' "$TMP/dups" \
    | while IFS= read -r p; do printf '%s\t%s\n' "$(stat -c %s -- "$p")" "$p"; done \
    | sort -rn | head -n 100 > "$TMP/cand"
  echo -e "${c_a}Se listan solo las COPIAS SOBRANTES (siempre queda una de cada grupo).${c_n}"
  echo "Para ver cada grupo completo:  less $TMP/dups  (mientras el script siga abierto)"
  echo
  mostrar_y_borrar "$TMP/cand"
}

limpieza(){
  titulo "Limpieza segura"
  local tam n d

  # 1. Caches de Termux
  tam=$(du -sb "$HOME/.cache" "$PREFIX/var/cache/apt" 2>/dev/null | awk '{t+=$1} END{printf "%.0f", t}')
  echo "1) Caches de Termux (apt/pip/npm): $(hum "$tam")"
  if confirmar "   ¿Limpiar?"; then
    apt clean >/dev/null 2>&1
    rm -rf "$HOME/.cache/"* 2>/dev/null
    command -v pip >/dev/null && pip cache purge >/dev/null 2>&1
    command -v npm >/dev/null && npm cache clean --force >/dev/null 2>&1
    echo -e "   ${c_v}Hecho.${c_n}"
  fi

  # 2. Miniaturas (se regeneran solas)
  for d in "$R/DCIM/.thumbnails" "$R/Pictures/.thumbnails" "$R/Movies/.thumbnails" "$R/Music/.thumbnails"; do
    [ -d "$d" ] || continue
    echo; echo "2) Miniaturas ${d#$R/}: $(du -sh "$d" 2>/dev/null | cut -f1) (se regeneran solas)"
    confirmar "   ¿Vaciar?" && rm -rf -- "$d"/* 2>/dev/null && echo -e "   ${c_v}Hecho.${c_n}"
  done

  # 3. Instaladores APK sueltos
  find "$R" -type f \( -iname '*.apk' -o -iname '*.xapk' -o -iname '*.apks' \) \
       -not -path "$R/Android/*" -printf '%s\t%p\n' 2>/dev/null | sort -rn > "$TMP/apk"
  if [ -s "$TMP/apk" ]; then
    echo; echo "3) Instaladores APK (si la app ya esta instalada, sobran):"
    mostrar_y_borrar "$TMP/apk"
  fi

  # 4. Descargas antiguas
  find "$R/Download" -type f -mtime +90 -printf '%s\t%p\n' 2>/dev/null | sort -rn | head -n 60 > "$TMP/viejos"
  if [ -s "$TMP/viejos" ]; then
    echo; echo "4) Archivos en Download con mas de 90 dias:"
    mostrar_y_borrar "$TMP/viejos"
  fi

  # 5. Temporales y logs
  find "$R" -type f \( -iname '*.tmp' -o -iname '*.temp' -o -iname '*.log' -o -name 'Thumbs.db' -o -name '.DS_Store' \) \
       -not -path "$R/Android/*" -printf '%s\t%p\n' 2>/dev/null | sort -rn | head -n 100 > "$TMP/tmpf"
  if [ -s "$TMP/tmpf" ]; then
    echo; echo "5) Temporales y logs:"
    mostrar_y_borrar "$TMP/tmpf"
  fi

  # 6. Carpetas vacias
  n=$(find "$R" -mindepth 2 -type d -empty -not -path "$R/Android/*" 2>/dev/null | wc -l)
  if [ "$n" -gt 0 ]; then
    echo
    if confirmar "6) Hay $n carpetas vacias. ¿Eliminarlas?"; then
      find "$R" -mindepth 2 -type d -empty -not -path "$R/Android/*" -delete 2>/dev/null
      echo -e "   ${c_v}Hecho.${c_n}"
    fi
  fi

  echo; resumen
}

informe(){
  local f="$R/Download/informe_espacio_$(date +%Y%m%d_%H%M).txt"
  echo "Generando informe (tarda un poco)..."
  {
    echo "INFORME DE ESPACIO - $(date '+%d/%m/%Y %H:%M')"
    resumen
    carpetas "$R"
    tipos
    conocidas
    titulo "Top 40 archivos mas grandes"
    find "$R" -type f -size +20M -printf '%s\t%p\n' 2>/dev/null | sort -rn | head -n 40 \
      | while IFS=$'\t' read -r s p; do printf "  %9s  %s\n" "$(hum "$s")" "${p#$R/}"; done
  } 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' > "$f"
  echo -e "${c_v}Informe guardado en:${c_n} Download/$(basename "$f")"
}

# ---------------------------------------------------------------------
comprobar
while true; do
  clear
  echo -e "${c_b}=========== LIBERAR ESPACIO (sin root) ===========${c_n}"
  df -h "$R" 2>/dev/null | awk 'NR==2{print "  Total: "$2"   Usado: "$3"   Libre: "$4"   ("$5" lleno)"}'
  echo "--------------------------------------------------"
  echo "  1) Resumen de espacio"
  echo "  2) Carpetas que mas ocupan"
  echo "  3) Explorar una carpeta concreta"
  echo "  4) Archivos mas grandes (y borrarlos)"
  echo "  5) Espacio por tipo (video, fotos, APK...)"
  echo "  6) Carpetas tipicas (WhatsApp, Telegram, camara...)"
  echo "  7) Buscar duplicados (y borrarlos)"
  echo "  8) Limpieza segura guiada"
  echo "  9) Guardar informe .txt en Download"
  echo "  0) Salir"
  echo "--------------------------------------------------"
  read -r -p "Opcion: " op
  case "$op" in
    1) resumen ;;   2) carpetas ;;  3) explorar ;;
    4) grandes ;;   5) tipos ;;     6) conocidas ;;
    7) duplicados ;; 8) limpieza ;; 9) informe ;;
    0) exit 0 ;;
    *) echo "Opcion no valida" ;;
  esac
  pausa
done
