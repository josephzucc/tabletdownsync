#set -u
#set -x
#export PS4='+ ${BASH_SOURCE}:${LINENO}: ';
#set -e

source "$functest";
gn;
a=$(pwd);
echo $now;
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd);
cd "$SCRIPT_DIR";
d="${now}ebiblista.txt";
m1=0;m2=0;m3=0;paso=1;
dre | while read -r line;do
  txt="${line}";
  case $paso in
    1) if [[ "${txt:0:12}" == "=== RESERVAS" ]];then paso=2;elif [[ "${txt:0:13}" == "=== PRESTAMOS" ]];then paso=5;fi;;
    2) if [[ "${txt:0:6}" == "Cuenta" ]]; then paso=3;fi;;
    3) if [[ "${txt:0:6}" == "------" ]];then paso=4;fi;;
    4) if [[ "${txt:0:6}" == "Total:" ]] || [[ "$txt" == "" ]];then break;
         else
           acc="${txt:0:12}";acc=$(echo "$acc" | sed "s/[[:space:]]*$//g" );
           item="${txt:14:34}";item=$(echo "$item" | sed "s/[[:space:]]*$//g" );
           estado="${txt:50:11}";estado=$(echo "$estado" | sed "s/[[:space:]]*$//g" );
           pos="${txt:62:6}";pos=$(echo "$pos" | sed "s/[[:space:]]*$//g" );
           x="${txt:68:18}";x=$(echo "$x" | sed "s/[[:space:]]*$//g" );
           if [[ "${estado}" == "DISPONIBLE" ]];then
             y="${txt:95:10}";y=$(echo "$y" | sed "s/[[:space:]]*$//g" );
             y2=$(echo $y | sed "s/m//g" );
             # crear bbdd con items que se tiene que ir actualizando, y ahi poner campo ultimo aviso, sigte aviso
             # avisar con termux-dialog confirm -t "ALERTA" -i "Ejemplar El Pais a pedir en prestamo en 15 minutos"
             sig=$(date -d +${y}inutes +"%H:%M");estado="ANTES ${sig}";
           else y="${txt:86:9}";y=$(echo "$y" | sed "s/[[:space:]]*$//g" );
           fi;
            # Analizamos 6h 45m y lo convertimos a minutos como 456m
           q=${#y};m=0;i=0;nro="";while (( i < q ));do case ${y: ${i}:1} in [0-9]) nro="${nro}${y: ${i}:1}";; "h") m=$(((60*nro)+m));nro="";; "m" ) m=$((m+nro));; esac;i=$((i+1));done;
           pedirantes="${txt:95:10}";pedirantes=$(echo "${pedirantes}" | sed "s/[[:space:]]*$//g" );
           #echo -e "\t${txt:69:10}";
           #echo "${line}";
           echo "reservas;${acc};${item};${estado};${pos};${x};${y};${m};${pedirantes}" >>$d;
       fi;;
    5) if [[ "${txt:0:6}" == "Cuenta" ]]; then paso=6;fi;;
    6) if [[ "${txt:0:6}" == "------" ]]; then paso=7;fi;;
    7) if [[ "${txt:0:6}" == "Total:" ]] || [[ "$txt" == "" ]];then paso=1;
         else
           item="${txt:0:14}";item=$(echo "$item" | sed "s/[[:space:]]*$//g" );
           x="${txt:14:36}";x=$(echo "$x" | sed "s/[[:space:]]*$//g" );
           z="${txt:68:10}";z=$(echo "$z" | sed "s/[[:space:]]*$//g" );
           # Analizamos 8d 6h 45m y lo convertimos a minutos como 456m
           q=${#z};m=0;dias=0;i=0;nro="";while (( i < q ));do case ${z: ${i}:1} in [0-9]) nro="${nro}${z: ${i}:1}";; "d") m=$(((nro*24*60)+m));dias="${nro}";nro="";; "h") m=$(((60*nro)+m));nro="";; "m" ) m=$((m+nro));; esac;i=$((i+1));done;
           if (( dias > 0 ));then y=$(printf 'DEVOL %4sd' "${dias}");
           else y="DEVOL ${txt:50:17}";y=$(echo "$y" | sed "s/[[:space:]]*$//g" | sed "s/\/2026//g" );
           fi;
           echo "prestamos;${item};${x};${y};${z};;;${m};" >>$d;
       fi;;
  esac;
done;
#cat $d
#csvsql -d ';' --tables t --no-header-row --query "SELECT a,b,c,d,e,h FROM t" $d | csvlook -d ',';
csvsql -d ';' --tables t --no-header-row --query "SELECT a,b,c,d,e,h FROM t ORDER BY h ASC" $d | csvlook -d ',';
cols="tipo,cuenta,desc,esp1,esp2,esp3";
rm $d;
cd "$a";
