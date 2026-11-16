python3 indicepdf.py . --merge
[[ "$functest" != "" ]] && source $functest >/dev/null;
gn;

chdt () { while read -r line;do dt=${line:29:10};de=$(dia="${line:37:2}";
case ${line:34:2} in "09") mes="septiembre";; "10") mes="octubre";;esac;echo " ${dia} de ${mes} de 2026");
echo ${line} | sed "s/${dt}/${de}/g";done };
getIndPie () { if [[ ! -t 0 ]];then text2=$(cat);text1=$(cat indice_pdf.txt | head -n 3);
text3=$(cat indice_pdf.txt | tail -n 2);echo -e "$text1\n\n$text2\n\n$text3";fi; };

clear;
gn;
d="${now}indice.txt";
echo "Pagina | Nombre Documento" >>$d;

{ tmp=$(mktemp);
( chdt () {
while read -r line;do
  dt=${line:29:10};de=$(dia="${line:37:2}";
  case ${line:34:2} in "09") mes="septiembre";; "10") mes="octubre";;esac;
  echo " ${dia} de ${mes} de 2026");
  echo ${line} | sed "s/${dt}/${de}/g";
done; };
getIndPie () {
  if [[ ! -t 0 ]];then
    text2=$(cat);
    text1=$(cat indice_pdf.txt | head -n 3);
    text3=$(cat indice_pdf.txt | tail -n 2);
    echo -e "$text1\n\n$text2\n\n$text3";
  fi;
};
cat indice_pdf.txt | cut -c "1-39" | tail +4 | head -n 5 | chdt >>$d;cat $d | csvlook -d '|' | getIndPie; ) >"$tmp" & pid=$!;
sec=0;while kill -0 "$pid" 2>/dev/null;do printf '\r\033[K\tConstruyendo1 %ds' "$sec";sleep 1;((sec++));done;
wait "$pid";stat=$?;echo "$tmp"; }
#cat indice_pdf.txt | cut -c "1-39" | tail +4 | head -n 5 | chdt >>$d;cat $d | csvlook -d '|' | getIndPie;

rm $d

#[[ "$functest" != "" ]] && source $functest >/dev/null;gn;chdt () { while read -r line;do dt=${line:29:10};de=$(dia="${line:37:2}";case ${line:34:2} in "09") mes="septiembre";; "10") mes="octubre";;esac;echo " ${dia} de ${mes} de 2026");echo ${line} | sed "s/${dt}/${de}/g";done };getIndPie () { if [[ ! -t 0 ]];then text2=$(cat);text1=$(cat indice_pdf.txt | head -n 3);text3=$(cat indice_pdf.txt | tail -n 2);echo -e "$text1\n\n$text2\n\n$text3";fi; };clear;gn;d="${now}indice.txt";echo "Pagina | Nombre Documento" >>$d;cat indice_pdf.txt | cut -c "1-39" | tail +4 | head -n 5 | chdt >>$d;cat $d | csvlook -d '|' | getIndPie >${now}indiceFinal.txt;rm $d

( python3 $ds/txt2pdf.py *indiceFinal.txt 2>/dev/null; ) & pid=$!;sec=0;while kill -0 "$pid" 2>/dev/null;do printf '\r\033[K\tConstruyendo2 %ds' "$sec";sleep 1;((sec++));done;wait "$pid";stat=$?;
rm unir_pdf.sh 2>/dev/null;
rm lista_pdf.txt 2>/dev/null;
rm combinado.pdf 2>/dev/null

