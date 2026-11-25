#!/usr/bin/env bash
# =======================================================================================================================
# cendoj-pedir-3.sh - Tercera tanda de busquedas (8/11/2026): incapacidad por covid persistente en tu nuevo TSJ
#                     (Castilla y Leon, Valladolid), discriminacion en periodo de prueba, abogados y sanidad.
# Uso (Termux, en la carpeta de cendoj.py):   bash cendoj-pedir-3.sh      Solo una:   bash cendoj-pedir-3.sh R01
# Resultado: ~/cendoj/busquedas/ (carpetas que acaban en _r01, _r02...). Registro: ~/cendoj/curl/registro.txt
# Subeme las carpetas _r01 ... _r14 y registro.txt. Prioridad: R01, R02, R03, R06, R07 (las demas pueden esperar).
# =======================================================================================================================

cd "$(dirname "$0")" || exit 1
PY=python3
[ -f cendoj.py ] || { echo "No encuentro cendoj.py en $(pwd). Copia este archivo a la carpeta de cendoj.py."; exit 1; }
BASE="${CENDOJ_DIR:-$HOME/cendoj}"
SAL="$BASE/curl"
LOG="$SAL/registro.txt"
mkdir -p "$SAL"
echo "=== cendoj-pedir-3.sh $(date '+%d/%m/%Y %H:%M') ===" >> "$LOG"
SOLO=" $* "
quiero() { [ "$SOLO" = "  " ] || [[ "$SOLO" == *" $1 "* ]]; }
buscar() {
    local id="$1"; shift
    quiero "$id" || return 0
    echo; echo "########## busqueda $id ##########"
    if $PY cendoj.py buscar "$@" --nombre "$id"; then echo "$id OK busqueda" >> "$LOG"
    else echo "$id FALLO busqueda: $*" >> "$LOG"; fi
}

# -----------------------------------------------------------------------------------------------------------------------
# 1. INCAPACIDAD PERMANENTE (la nueva solicitud ira al INSS de Avila; el recurso, al TSJ de Castilla y Leon)
# -----------------------------------------------------------------------------------------------------------------------
# R01. Covid persistente e incapacidad en el TSJ de Castilla y Leon (criterio de tu tribunal)
buscar R01 "covid persistente" "incapacidad permanente" -j social -o tsj -l "Castilla y León" -d 01/01/2023 \
       -m 40 --pdf --maxpdf 8
# R02. Incapacidad absoluta con deterioro cognitivo o niebla mental medida con test neuropsicologicos
buscar R02 "covid persistente" "absoluta" neuropsicológica cognitivo -j social -o tsj -d 01/01/2024 -m 40 \
       --pdf --maxpdf 6
# R03. Profesion habitual administrativa con atencion al publico y covid persistente (incapacidad total)
buscar R03 "covid persistente" "total" administrativo "atención al público" -j social -o tsj -d 01/01/2023 -m 30 \
       --pdf --maxpdf 5
# R04. Desplazamiento al trabajo como limitacion en la incapacidad (mismo criterio que STSJ M 6589/2023)
buscar R04 "incapacidad permanente" desplazamiento "transporte público" fatiga -j social -o tsj -d 01/01/2023 -m 30
# R05. La sentencia que no aparecio: TSJ de Castilla y Leon 3067/2024
buscar R05 -r "STSJ CL 3067/2024" -m 5 --pdf --maxpdf 1

# -----------------------------------------------------------------------------------------------------------------------
# 2. TRAGSATEC: DISCRIMINACION EN PERIODO DE PRUEBA Y AJUSTES RAZONABLES
# -----------------------------------------------------------------------------------------------------------------------
# R06. Cese en periodo de prueba de persona con discapacidad sin ajustes razonables (2025-2026)
buscar R06 "periodo de prueba" discapacidad "ajustes razonables" -j social -o ts,tsj -d 01/01/2025 -m 40 \
       --pdf --maxpdf 6
# R07. Ley 15/2022 y enfermedad: cese tras baja medica (indicios y carga de la prueba)
buscar R07 "Ley 15/2022" enfermedad "periodo de prueba" indicios -j social -o tsj -d 01/01/2024 -m 40 --pdf --maxpdf 5
# R08. Covid persistente como causa de discriminacion en el despido o el cese
buscar R08 "covid persistente" discriminación despido -j social -o ts,tsj -d 01/01/2023 -m 30 --pdf --maxpdf 5
# R09. Indemnizacion por no adaptar el puesto a un trabajador especialmente sensible (art. 25 LPRL)
buscar R09 "especialmente sensible" "adaptación del puesto" indemnización "daños" -j social -o tsj -d 01/01/2024 \
       -m 30 --pdf --maxpdf 4
# R10. Tutela de derechos fundamentales: cuantia del dano moral por discriminacion por discapacidad (2025-2026)
buscar R10 "daño moral" discriminación discapacidad "tutela de derechos fundamentales" LISOS -j social -o tsj \
       -d 01/01/2025 -m 30 --pdf --maxpdf 4

# -----------------------------------------------------------------------------------------------------------------------
# 3. DISCAPACIDAD, ABOGADOS Y SANIDAD
# -----------------------------------------------------------------------------------------------------------------------
# R11. Grado de discapacidad por covid persistente (para la revision pedida el 01/09/2026)
buscar R11 "covid persistente" "grado de discapacidad" -j social -o tsj -d 01/01/2023 -m 30 --pdf --maxpdf 4
# R12. Responsabilidad civil del abogado por perdida de oportunidad en un pleito laboral o de incapacidad
buscar R12 "responsabilidad civil" abogado "pérdida de oportunidad" "incapacidad permanente" -j civil -o ts,ap \
       -d 01/01/2020 -m 30 --pdf --maxpdf 4
# R13. Responsabilidad patrimonial de la sanidad por la atencion al covid persistente
buscar R13 "covid persistente" "responsabilidad patrimonial" -j contencioso -o tsj -d 01/01/2023 -m 30 --pdf --maxpdf 4
# R14. Separacion del servicio de un funcionario por actividad privada sin compatibilidad (solo para entender)
buscar R14 "separación del servicio" compatibilidad "actividad privada" funcionario -j contencioso -o tsj \
       -l "Madrid" -d 01/01/2020 -m 20

echo
echo "Terminado. Registro en $LOG"
grep -c FALLO "$LOG" >/dev/null && echo "Fallos anotados:" && grep FALLO "$LOG" | tail -30
