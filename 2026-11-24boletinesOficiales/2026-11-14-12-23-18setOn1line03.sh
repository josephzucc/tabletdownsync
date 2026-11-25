#while read -r file;do
#file2=$(echo "$file" | sed "s/\.txt/_unaLinea\.txt/g" );d=$(mktemp);
#awk '/^[0-9]{2}\/[0-9]{2}\/[0-9]{4}/ {collecting=1; buffer=$0; next} collecting && /https:\/\/www\.boe\.es\/buscar\/doc\.php/ {collecting=0; gsub(/[ \t]+/, " ", buffer); col1=buffer; col2=$0; next} collecting {buffer=buffer " " $0; next} /https:\/\/www\.boe\.es\/buscar\/doc\.php/ {col2=$0; next} /PDF: https:\/\// {col3=$0; print col1 "\037" col2 "\037" col3; col1=col2=col3=buffer=""}' ${file} >${d};csvsql --table t --delimiter $'\x1F' -q "SELECT * FROM t" ${d};rm $d;done < <( ls 2026-11-14-08-5*.txt );

init=10;
end=121;

awk '
BEGIN { print "anuncio\037urlboe\037urlpdf" }

/^[0-9]{2}\/[0-9]{2}\/[0-9]{4}/ {
    collecting=1
    buffer=$0
    next
}

collecting && /https:\/\/www\.boe\.es\/buscar\/doc\.php/ {
    collecting=0
    gsub(/[ \t]+/, " ", buffer)
    col1=buffer
    col2=$0
    next
}

collecting {
    buffer=buffer " " $0
    next
}

/https:\/\/www\.boe\.es\/buscar\/doc\.php/ {
    col2=$0
    next
}

/PDF: https:\/\// {
    col3=$0
    print col1 "\037" col2 "\037" col3
    col1=col2=col3=buffer=""
}
' 2026-11-14-08-5*.txt >> all_data.csv

terms=$(ls *terminosMigr*);

d=$(mktemp);
while read -r term;
do
  if (( 1 == 1 ));then
    echo "csvsql --tables t --delimiter $'\x1f' --quotechar '\"' --snifflimit 0 --query \"SELECT \
    anuncio FROM t WHERE anuncio LIKE '%${term}%'\" all_data.csv | tr -d '\"' >>\${d2}" >>${d};
  else
  echo "csvsql --tables t --delimiter $'\x1f' --quotechar '\"' --snifflimit 0 --query \"SELECT \
  CASE \
    WHEN INSTR(anuncio, 'Objeto:') > 0 THEN SUBSTR(anuncio,INSTR(anuncio,'Objeto:')+8,${end}) \
    WHEN INSTR(anuncio, 'Resolu') > 0 THEN SUBSTR(anuncio,INSTR(anuncio,'Resolu'),${end}) \
    WHEN INSTR(anuncio, 'Orden') > 0 THEN SUBSTR(anuncio,INSTR(anuncio,'Orden'),${end}) \
    WHEN INSTR(anuncio, 'Anuncio') > 0 THEN SUBSTR(anuncio,INSTR(anuncio,'Anuncio'),${end}) \
    ELSE SUBSTR(anuncio,1,${end}) \
  END as anuncio FROM t WHERE anuncio LIKE '%${term}%'\" all_data.csv | tr -d '\"' >>\${d2}" >>${d};
  fi;
done < <(cat $terms;)
source "$functest";gn;export d2="${now}salida.txt";
bash $d;
echo ${d2};
rm all_data.csv;
