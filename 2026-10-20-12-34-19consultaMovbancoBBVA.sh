f="${BASH_SOURCE[0]}";
echo -e "\n\n\t$(basename ${f})\n\tfunciones:\n\tgmv:\t\t\t\t\tlista movimientos BBVA.\n\tgap:\t\t\t\t\tlista movimientos Apartados BBVA.\n\tvvar:\t\t\t\t\tver variables\n\tggam:\t\t\t\t\tver gastos este mes.\n\tggas:\t\t\t\t\tver gastos mes siguiente.\n\tgre:\t\t\t\t\tver reduccion\n\tgcadap:\t\t\t\t\tver cada apartado separado.";
[[ "$mbbva" == "" ]] && { tmp=$(mktemp); ( find $shar -type f -iname "*movbancobbva*.csv" 2>/dev/null | sort | tail -n 1 ) >"$tmp" & pid=$!;sec=0;while kill -0 "$pid" 2>/dev/null;do printf '\r\033[K\tFind mbbva %ds' "$sec";sleep 1;((sec++));done;wait "$pid";stat=$?;mbbva=$(<"$tmp");rm -f "$tmp";printf '\r\033[K\tTerminado en %ds (salida %d)' "$sec" "$stat";printf '\r\033[K\tmbbva\t\t\t\t\t%s\n' "$(basename $mbbva)";echo "mbbva;${mbbva}" >>${histvar}; } || { echo -e "\tmbbva\t\t\t\t\tya establecido"; }
[[ "$ap" == "" ]] && { tmp=$(mktemp); ( find $shar -type f -iname "*movApartadosbancoBBVA*.txt" 2>/dev/null | sort | tail -n 1 ) >"$tmp" & pid=$!;sec=0;while kill -0 "$pid" 2>/dev/null;do printf '\r\033[K\tFind apart %ds' "$sec";sleep 1;((sec++));done;wait "$pid";stat=$?;ap=$(<"$tmp");rm -f "$tmp";printf '\r\033[K\tTerminado en %ds (salida %d)' "$sec" "$stat";printf '\r\033[K\tap\t\t\t\t\t%s\n' "$(basename $ap)";echo "ap;${ap}" >>${histvar}; } || { echo -e "\tap\t\t\t\t\tya establecido"; }
[[ "$red" == "" ]] && { tmp=$(mktemp); ( find $shar -type f -iname "*reducc*" 2>/dev/null | sort | tail -n 1 ) >"$tmp" & pid=$!;sec=0;while kill -0 "$pid" 2>/dev/null;do printf '\r\033[K\tFind reducc %ds' "$sec";sleep 1;((sec++));done;wait "$pid";stat=$?;red=$(<"$tmp");rm -f "$tmp";printf '\r\033[K\tTerminado en %ds (salida %d)' "$sec" "$stat";printf '\r\033[K\tred\t\t\t\t\t%s\n' "$(basename $red)";echo "red;${red}" >>${histvar}; } || { echo -e "\tred\t\t\t\t\tya establecido"; }
[[ "$mvbbva" == "" ]] && { tmp=$(mktemp); ( find $shar -type f -iname "*consultamovbancobbva*.sh" 2>/dev/null | sort | tail -n 1 ) >"$tmp" & pid=$!;sec=0;while kill -0 "$pid" 2>/dev/null;do printf '\r\033[K\tFind mvbbva %ds' "$sec";sleep 1;((sec++));done;wait "$pid";stat=$?;mvbbva=$(<"$tmp");rm -f "$tmp";printf '\r\033[K\tTerminado en %ds (salida %d)' "$sec" "$stat";printf '\r\033[K\tmvbbva\t\t\t\t%s\n' "$(basename $mvbbva)";echo "mvbbva;${mvbbva}" >>${histvar}; } || { echo -e "\tmvbbva\t\t\t\t\tya establecido"; }
[[ "$saldobbva" == "" ]] && { tmp=$(mktemp); ( find $shar -type f -iname "*movBBVAgastossaldo*.txt" 2>/dev/null | sort | tail -n 1 ) >"$tmp" & pid=$!;sec=0;while kill -0 "$pid" 2>/dev/null;do printf '\r\033[K\tFind saldobbva %ds' "$sec";sleep 1;((sec++));done;wait "$pid";stat=$?;saldobbva=$(<"$tmp");rm -f "$tmp";printf '\r\033[K\tTerminado en %ds (salida %d)' "$sec" "$stat";printf '\r\033[K\tsaldobbva\t\t\t\t%s\n' "$(basename $saldobbva)";echo "saldobbva;${saldobbva}" >>${histvar}; } || { echo -e "\tsaldobbva\t\t\t\tya establecido"; }
[[ "$functest" == "" ]] && { tmp=$(mktemp); ( find $shar -type f -iname "*functest.sh*" 2>/dev/null | sort | tail -n 1) >"$tmp" & pid=$!;sec=0;while kill -0 "$pid" 2>/dev/null;do printf '\r\033[KFind functest %ds' "$sec";sleep 1;((sec++));done;wait "$pid";stat=$?;functest=$(<"$tmp");rm -f "$tmp";printf '\r\033[K\tTerminado en %ds (salida %d)' "$sec" "$stat";printf '\r\033[K\tfunctest\t\t\t%s\n' "$(basename $functest)";echo "functest;${functest}" >>${histvar}; } || { echo -e "\tfunctest\t\t\t\tvalor ya establecido."; }

avvar () {
  export mvbbva=$(realpath -- "${BASH_SOURCE[0]}");
  ap="";red="";mbbva="";saldobbva="";
  source $mvbbva;
}

gmv () {
  #echo -e "Cuando el fichero se recibe tiene mas lineas iniciales\nPor ello debe modificarse tail -n +2 \"\$f\" por tail -n +5 \"\$f\""
  f="$mbbva";basename "$f";
  tmp="bbva_tmp.csv";
  tail -n +5 "$f" | csvcut -d ',' -c 2- > "$tmp";
  csvsql -d ',' -y 0 --tables bbva --query "SELECT Fecha,Concepto,Movimiento,Importe,Disponible FROM bbva" "$tmp" | csvlook -d ',' --max-column-width 35
  #head -n 1 "$tmp";
  rm "$tmp";
}

gap () {
if (( $# > 0 )) ;
  then q="SELECT SUBSTR(FECHA,1,10) AS FECHA,APARTADO,INGDEV,printf('%.2f', ROUND(IMPORTE,2)) AS IMPORTE,printf('%.2f',ROUND(SALDO,2)) AS SALDO,COMENTARIO FROM T WHERE APARTADO LIKE '%${1}%' ORDER BY substr(\"FECHA\",7,4) || substr(\"FECHA\",4,2) || substr(\"FECHA\",1,2) || substr(\"FECHA\",12,1) || substr(\"FECHA\",14,1) DESC, SALDO DESC";
  else q="SELECT SUBSTR(FECHA,1,10) AS FECHA,APARTADO,INGDEV,printf('%.2f', ROUND(IMPORTE,2)) AS IMPORTE,printf('%.2f',ROUND(SALDO,2)) AS SALDO,COMENTARIO FROM t ORDER BY APARTADO DESC, substr(\"FECHA\",7,4) || substr(\"FECHA\",4,2) || substr(\"FECHA\",1,2) || substr(\"FECHA\",12,1) || substr(\"FECHA\",14,1) DESC";
fi;
csvsql --date-format=- -d ';' --tables t --snifflimit 0 --query "${q}" $ap | csvlook -I -d ','
}

ggam () { form5 480 $(basename "$gastosm"); }
ggas () { form5 480 $(basename "$gastoss"); }

vvar () {
  [[ "$functest" == "" ]] && export functest=$(find $shar -type f -iname "*functest.sh*" 2>/dev/null | sort | tail -n 1);
  source "$functest" >/dev/null;
  init >/dev/null;
  echo -e "\tap\t\t"$(basename "$ap");
  echo -e "\tmbbva\t\t"$(basename "$mbbva");
  echo -e "\tred\t\t"$(basename "$red");
  echo -e "\tgastosm\t\t"$(basename "${gastosm}");
  echo -e "\tmvbbva\t\t"$(basename "$mvbbva");
  echo -e "\tsaldobbva\t\t"$(basename "$saldobbva");
}

gre () {
csvsql -d ';' --tables t --query "SELECT * FROM t ORDER BY IMPORTE DESC" $red | csvlook -d ',';
}

gcadap () {
  gn;
  aprs="${now}apartados.txt";
  csvsql --date-format=- -d ';' --tables t --query "SELECT DISTINCT APARTADO FROM t ORDER BY FECHA DESC" $ap | grep -v "APARTADO" >$aprs;
  while read -r apartado;do
    csvsql --date-format=- -d ';' --tables t --query "SELECT SUBSTR(FECHA,1,10) AS FECHA,APARTADO,INGDEV,printf('%.2f',ROUND(IMPORTE,2)) AS IMPORTE,printf('%.2f',ROUND(SALDO,2)) AS SALDO,COMENTARIO FROM t WHERE APARTADO=\"${apartado}\" ORDER BY substr(\"FECHA\",7,4) || substr(\"FECHA\",4,2) || substr(\"FECHA\",1,2) || substr(\"FECHA\",12,1) DESC" $ap | csvlook -I -d ',';
    echo -e "\n\n";done < <(cat $aprs);
  rm $aprs
}
