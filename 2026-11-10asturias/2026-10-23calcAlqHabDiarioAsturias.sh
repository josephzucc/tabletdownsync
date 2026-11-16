# =========
restotot=480;
source $functest >/dev/null;
gn;
d="${now}lista.txt";
com=$(ls *Comparativ* | tail -n 1);
while IFS=";" read ayto alqhabmes alqhabdia resto comentario;do
  if [[ "$ayto" == "AYTO" ]];then echo "${ayto};${alqhabmes};${alqhabdia};${resto};${comentario}" >$d;
  else
    [[ "$alqhabdia" == "" ]] && alqhabdia=$(echo "scale=2;(${alqhabmes}/30)" |  bc);
    [[ "$resto" == "" ]] && resto=$((480-alqhabmes))
    echo "${ayto};${alqhabmes};${alqhabdia};${resto};${comentario}" >>${d};
  fi;
done < <(cat $com);
mv $d $com;
rm $d 2>/dev/null
clear;
cat $com | csvlook -y 0 -d ';'
