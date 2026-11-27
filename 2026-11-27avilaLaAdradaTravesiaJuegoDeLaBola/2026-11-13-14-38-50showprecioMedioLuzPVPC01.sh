#cat 2026-11-13-14-32-44precioMedioLuzPVPC.txt | csvlook -I
source "$functest";
gn;
a=$(pwd);
#echo "Carpeta Actual $a";
f2=$(dirname "${BASH_SOURCE}");
cd "$f2";
#pwd;
echo "Now: ${now}";
dia=$(date +"%Y-%m-%d");
o2="*precioMedioLuzPVPC.txt";o=$(find . -maxdepth 1 -type f -iname "${o2}" 2>/dev/null | sort | tail -n 1);o=${o:2};
if [[ "$o" == "" ]];then echo "No encuentro el archivo ${o2}";else echo "BBDD Encontrada ${o}";fi;

export justbrief=0;

order="ORDER BY preciomedio DESC";
case $# in
  1)
     echo "Dia ${1}";
     dia=${1};
     ;;
  2)
     echo "Dia ${1}";
     echo "o ${2}";
     dia=${1};
     o=${2};
     ;;
  3)
     echo "Dia ${1}";
     echo "o ${2}";
     echo "order ${3}";
     dia="${1}";
     o="${2}";
     order="${3}";
     ;;
esac;

# | dia        | tipo  | periodo | preciomin | preciomax | preciomedio | mediodia | porc     | preciocoloq |
# | ---------- | ----- | ------- | --------- | --------- | ----------- | -------- | -------- | ----------- |

if (( justbrief == 0 ));then
  echo -e "| dia        | tipo  | periodo | preciomin | preciomax | preciomedio | mediodia | porc     | preciocoloq |\n| ---------- | ----- | ------- | --------- | --------- | ----------- | -------- | -------- | ----------- |";
  t=" 30 dias al mes por 24 horas, a 24 precios distintos por dia son 720 precios de luz por factura de 30 dias ";
  b=$(printf "%${#t}s" | tr ' ' '=';);echo -e "\n${b}\n${t}\n${b}\n";
  echo "El Precio Medio es la mitad entre el Precio Maximo y el Precio Minimo de toda la tabla";
fi;

dia7a=$(date -d -7days +"%Y-%m-%d");
medio=$(csvsql --tables t --query "SELECT (MAX(preciomedio)+MIN(preciomedio))/2 as medio FROM t" $o | tail -n +2);medio=$(printf "%.4f" $medio);
mediod=$(csvsql --tables t --query "SELECT (MAX(preciomedio)+MIN(preciomedio))/2 as medio FROM t WHERE dia == '${dia}'" $o | tail -n +2);mediod=$(printf "%.4f" $mediod);
alto=$(csvsql --tables t --query "SELECT MAX(preciomedio) as alto FROM t" $o | tail -n +2);alto=$(printf "%.4f" $alto);
altod=$(csvsql --tables t --query "SELECT MAX(preciomedio) as alto FROM t WHERE dia == '${dia}'" $o | tail -n +2);altod=$(printf "%.4f" $altod);
bajo=$(csvsql --tables t --query "SELECT MIN(preciomedio) as bajo FROM t" $o | tail -n +2);bajo=$(printf "%.4f" $bajo);
bajod=$(csvsql --tables t --query "SELECT MIN(preciomedio) as bajo FROM t WHERE dia == '${dia}'" $o | tail -n +2);bajod=$(printf "%.4f" $bajod);
limite1=$(csvsql --tables t --query "SELECT ((MAX(preciomedio)-MIN(preciomedio))/3)+MIN(preciomedio) as l1 FROM t" $o | tail -n +2);limite1=$(printf "%.4f" $limite1);
limite1d=$(csvsql --tables t --query "SELECT ((MAX(preciomedio)-MIN(preciomedio))/3)+MIN(preciomedio) as l1 FROM t WHERE dia == '${dia}'" $o | tail -n +2);limite1d=$(printf "%.4f" $limite1d);
limite2=$(csvsql --tables t --query "SELECT (((MAX(preciomedio)-MIN(preciomedio))/3)*2)+MIN(preciomedio) as l2 FROM t" $o | tail -n +2);limite2=$(printf "%.4f" $limite2);
limite2d=$(csvsql --tables t --query "SELECT (((MAX(preciomedio)-MIN(preciomedio))/3)*2)+MIN(preciomedio) as l2 FROM t WHERE dia == '${dia}'" $o | tail -n +2);limite2d=$(printf "%.4f" $limite2d);

if (( justbrief == 0 ));then echo -e "medio\t$medio\t$mediod\nalto\t$alto\t$altod\nbajo\t$bajo\t$bajod\nlimite1\t$limite1\t$limite1d\nlimite2\t$limite2\t$limite2d";fi;

if (( justbrief == 0 ));then
  t=" Dia Desde 7 dias antes ${dia7a} ";
  b=$(printf "%${#t}s" | tr ' ' '=';);echo -e "\n${b}\n${t}\n${b}\n";

  t1=$(mktemp);
  csvsql --tables t --query "SELECT ( CASE strftime('%w', dia) WHEN '0' THEN 'Sonntag' WHEN '1' THEN 'Montag' WHEN '2' THEN 'Dienstag' WHEN '3' THEN 'Mittwoch' WHEN '4' THEN 'Donnerstag' WHEN '5' THEN 'Freitag' WHEN '6' THEN 'Samstag' END) || ' ' || dia AS dia,tipo,periodo,preciomedio,PRINTF('%.2f',${medio}) as mediodia,PRINTF('%8s',PRINTF('%.2f',(((preciomedio/${medio})-1)*100)) || ' %') AS porc,CASE WHEN preciomedio > ${limite1} AND preciomedio < ${limite2} THEN 'NORMAL' WHEN preciomedio >= ${limite2} THEN 'VIP' ELSE 'ECONOMICO' END AS preciocoloq FROM t WHERE dia > '${dia7a}' ${order}" $o | csvlook -I >>${t1};
  # 2. Get total number of lines
  TOTAL=$(wc -l < ${t1})
  # 3. Extract the records
  head -n 4 ${t1}  # Header + 2 records
  sed -n "$((TOTAL/2)),$((TOTAL/2+1))p" ${t1};
  tail -n 2 ${t1};
  rm "$t1";
fi;

horaactual=$(date +"%H");horaini=$((10#$horaactual));horafin=$((10#$horaactual+5));

hs=$(csvsql --tables t --query "SELECT periodo FROM (SELECT dia,tipo,periodo,preciomedio,PRINTF('%.2f',${medio}) as mediodia,PRINTF('%8s',PRINTF('%.2f',(((preciomedio/${medio})-1)*100)) || ' %') AS porc, CASE WHEN preciomedio > ${limite1} AND preciomedio < ${limite2} THEN 'NORMAL' WHEN preciomedio >= ${limite2} THEN 'VIP' ELSE 'ECONOMICO' END AS preciocoloq, CASE WHEN preciomedio > ${limite1d} AND preciomedio < ${limite2d} THEN 'NORMAL' WHEN preciomedio >= ${limite2d} THEN 'VIP' ELSE 'ECONOMICO' END AS preciocoloqd FROM t WHERE dia == '${dia}') as t WHERE preciocoloqd == 'VIP'" $o | tail -n +2 | awk -F- '{ if (NR == 1) {start=$1;end=$2} else if ($1 == end) {end=$2} else {printf "de %s a %s, ", start, end;start=$1;end=$2;} } END {printf "de %s a %s\n", start, end }');
t="${dia} Horas a evitar gastar mucha luz ${hs} ";b=$(printf "%${#t}s" | tr ' ' '=';);echo -e "\n${b}\n${t}\n${b}\n" | tee "${dia}GastoLuzMensaje.txt";

if (( justbrief == 0 ));then
  t=" Dia Actual ${today} ";b=$(printf "%${#t}s" | tr ' ' '=';);echo -e "\n${b}\n${t}\n${b}\n";
  csvsql --tables t --query "SELECT dia,tipo,periodo,preciomedio,PRINTF('%.2f',${medio}) as mediodia,PRINTF('%8s',PRINTF('%.2f',(((preciomedio/${medio})-1)*100)) || ' %') AS porc, CASE WHEN preciomedio > ${limite1} AND preciomedio < ${limite2} THEN 'NORMAL' WHEN preciomedio >= ${limite2} THEN 'VIP' ELSE 'ECONOMICO' END AS preciocoloq, CASE WHEN preciomedio > ${limite1d} AND preciomedio < ${limite2d} THEN 'NORMAL' WHEN preciomedio >= ${limite2d} THEN 'VIP' ELSE 'ECONOMICO' END AS preciocoloqd FROM t WHERE dia == '${dia}'" $o | csvlook -I;
fi;

if (( justbrief == 0 ));then
  t=" 3 horas antes y 3 horas despues ";b=$(printf "%${#t}s" | tr ' ' '=';);echo -e "\n${b}\n${t}\n${b}\n";
  csvsql --tables t --query "SELECT dia,periodo,preciomedio,PRINTF('%.2f',${medio}) as mediodia,PRINTF('%8s', PRINTF('%.2f',((preciomedio/${medio})-1)*100) || ' %') AS porc FROM t WHERE CAST(TRIM(SUBSTR(periodo, 1, INSTR(periodo, '-') - 1)) AS INTEGER) BETWEEN ${horaini} AND ${horafin} AND dia == '${dia}'" $o | csvlook -I
fi;

if (( justbrief == 0 ));then
  t=" Tabla precios Claros ";b=$(printf "%${#t}s" | tr ' ' '=';);echo -e "\n${b}\n${t}\n${b}\n";
  gn;d="${now}tablaprecios.txt";
  csvsql --tables t --query "SELECT dia,periodo,CASE WHEN preciomedio > ${limite1} AND preciomedio < ${limite2} THEN 'NORMAL' WHEN preciomedio >= ${limite2} THEN 'VIP' ELSE 'ECONOMICO' END AS preciocoloq FROM t WHERE dia == '${dia}'" $o | csvlook -I >>${d}
  cat $d
fi;

#t=" Conversion a PDF ";
#b=$(printf "%${#t}s" | tr ' ' '=';);echo -e "\n${b}\n${t}\n${b}\n";
#o=$(ls *tabla* | sort | tail -n 1 );
#python3 ../txt2pdf.py $o;

cd "$a";
