echo -e "readEmails - (R) Jose Zuccoli 2026"
if [[ "$1" == "" ]];then echo "Parametro 1 vacio: Asignamos 5 correos";set -- "5";
else echo "parametro1 pasado: $1";fi
echo -e "ejecutando python3 leerCorreo.py -acc todas -last $1..."
python3 leerCorreo.py -acc todas -last $1 -ver
