addlog2 () { while read -r line;do echo "${now}:${pid} ${line}" >>${log};done }
addlog () { while read -r line;do txtadd="${now}: ";qadd=${#txtadd};resta=$(( 121 - qadd ));qfor=${#line};echo "${txtadd}${line:0: ${resta}}";done }
t="meteoaemet.log";i=0;j=30;
while (( i <= j ));do 
  fecha=$(date -d -${i}days +"%Y-%m-%d");
  t2="${fecha}${t}";
  echo "Buscando ${t2}...";
  f=$(find . -maxdepth 1 -type f -iname "${t2}" 2>/dev/null | sort | tail -n 1);
  f=${f:2};
  if [[ "${f}" != "" ]];then if [[ "${f:0:10}" != "${today}" ]];then export log="${today}${t}";mv "${f}" "${logmeteo}";else log="${t2}";fi;break;fi;
  i=$((i+1));
done;
if (( i > j ));then echo "Despues de ${j} pasadas no se ha encontrado ${today}${t}..";
else echo "ENCONTRADO ${t2}";
fi;
