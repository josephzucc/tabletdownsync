a=$(pwd);
path=$(dirname "${0}");
cd "$path";
maxdias=10;
if (( $# > 0 ));then echo "1er parametro: segundos de espera entre peticiones";w1="${1}";else w1=0;fi;
source ../functest.sh >/dev/null;
gn >/dev/null;
i=0;
d="";
while [[ "$d" == "" ]];do
  fecha=$(date -d -${i}days +"%Y-%m-%d");
  busq="${fecha}*plazasLibres*.txt";
  echo "Buscando ${busq}....";
  d=$(find . -maxdepth 1 -type f -iname "${busq}" 2>/dev/null | sort | tail -n 1);
  i=$((i+1));
done
if (( i > 1 ));then
  echo -e "Cambio plazas libres ${now}${d:21}....";mv -- "$d" "${now}${d:21}";
  else echo -e "Plazas Libres en ${d:2}";
fi;
echo "Buscando plazas libres por ${maxdias} dias...";
i=0;
while (( i < maxdias ));do
  fecha=$(date -d +${i}days +"%d/%m/%Y");
  echo "Buscando tickets ${fecha}..."
  p=$(python3 samar.py comprobar avila laadrada ${fecha} 17:30 --callado | grep -E '^(LIBRE|LLENA|NO CONSTA|SIN SERVICIO|FUERA DE PLAZO):' | cut -c '1-90');
  sleep $w1;wait;
  echo "${now}: $p" >>$d;
  i=$((i+1));
done;
cat $d | tail -n 30;
cd "$a";
