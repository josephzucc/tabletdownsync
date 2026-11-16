echo "verificar si existe bono para avila. Si no existe no se puede buscar";

export a=$(pwd);
export path=$(dirname "${0}");
cd "$path";
if (( $# > 0 ));then echo "1er parametro: segundos de espera entre peticiones";w1="${1}";else w1=0;fi;
export d=$(ls ${today}*plazasLibres*.txt | sort | tail -n 1);
source $functest;
export m=20;
export t=$((m*60));
export v=$(ls *verifult10d.sh);
export out="";
while [[ $out == "" ]];do
  gn;
  echo -e "${now}: Waiting ${m} minutes..." >>$d;
  printf '\r\033[K%s Waiting %s minutes...' "${now}" "$m";
  sleep $t;
  wait;
  (bash "$v" $w1) & pid=$!;
  seconds=0;
  while kill -0 "$pid" 2>/dev/null; do
    printf '\rElapsed: %ds' "$seconds";
    sleep 1;
    ((seconds++));
  done;
  wait "$pid";
  status=$?;
  printf '\r\033[KFinished after %ds (exit code %d)' "$seconds" "$status";
  gn;
  echo "${now}: Finished bash execution after ${seconds} with ${status} status" >>$d;
  read -t 5 -p "${now}: Salir ? " out;
  echo;
done;
cd "$a";
