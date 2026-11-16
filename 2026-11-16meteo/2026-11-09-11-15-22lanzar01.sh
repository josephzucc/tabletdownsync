a=$(pwd);
echo "Actual $a";
s="${BASH_SOURCE}";
r=$(realpath "$s");
path=$(dirname "$r");
cd "$path";
export m=60;
addlog2 () { while read -r line;do echo "${now}:${pid} ${line}" >>${log};done }
addlog () { while read -r line;do txtadd="${now}: ";qadd=${#txtadd};resta=$(( 121 - qadd ));qfor=${#line};echo "${txtadd}${line:0: ${resta}}" >>${log};done }
t="meteoaemet.log";i=0;j=30;
while (( i <= j ));do
  fecha=$(date -d -${i}days +"%Y-%m-%d");
  t2="${fecha}${t}";
  echo "Buscando ${t2}...";
  f=$(find . -maxdepth 1 -type f -iname "${t2}" 2>/dev/null | sort | tail -n 1);
  f=${f:2};
  if [[ "${f}" != "" ]];then if [[ "${f:0:10}" != "${today}" ]];then export log="${today}${t}";mv "${f}" "${log}";else log="${t2}";fi;break;fi;
  i=$((i+1));
done;
if (( i > j ));then echo "Despues de ${j} pasadas no se ha encontrado ${today}${t}..";log="${today}${t}";
else echo "ENCONTRADO ${log}";
fi;
echo "log es ${log}";
export pid=$$;
export s=$((m*60));
export bajameteo="${today}baja.txt";
export now=$(date +"%m-%d-%H-%M");
echo "${now}:${pid}: $bajameteo" >>$log;
echo "Archivo de salida $bajameteo";
echo "${now}: Espera ${m} minutos";
(
 i=1;j=10;
  while (( i < j ));do
    now=$(date +"%m-%d-%H-%M");
    python3 aemet.py 2>&1 | addlog;
    python3 meteo.py 2>&1 | addlog;
    python3 meteo.py estado 2>&1 | addlog;
    export sig=$(date -d +${m}minutes +"%m-%d-%H-%M");
    echo "${now} ${i} de ${j} de ${m}m siguiente ${sig}";
    echo "${now} ${i} de ${j} de ${m}m siguiente ${sig}" | addlog;
    sleep $s;wait;
    if [[ -f "$bajameteo" ]];then echo "${i} de ${j} archivo $bajameteo" | addlog;rm $bajameteo;break;fi;
    i=$((i+1));
  done;
  gn;
  echo "${now} i ${i} saliendo..." | addlog;
) &
cd "$a";
