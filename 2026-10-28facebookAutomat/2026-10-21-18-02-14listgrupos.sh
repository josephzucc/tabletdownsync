source ../functest.sh;gn;
d="${now}listadofb.txt";
echo "CODIGO;NOMBRE;PUBLICACIONES;URL" >>$d;
while read -r file;do
  id=${file:0:5};
  name=${file:9:62};
  pub=${file:71:3};
  id=$(echo $id | sed "s/[[:space:]]*$//g" );
  name=$(echo $name | sed -e "s/[[:space:]]*$//g" );
  url=${file:92:75};
  if [[ "${pub:0:1}" == "-" ]];then pub=0;fi;
  echo -e "${id};${name};${pub};${url}" >>${d};
done < <(python3 fb.py listas grupos | head -n -5 | tail -n +6 )
cat $d | csvsql -d ';' --tables t --query "SELECT CODIGO,NOMBRE,PUBLICACIONES as P,substr("URL",1,20) AS URL FROM t ORDER BY PUBLICACIONES DESC" $d | csvlook -d ',' --max-column-width 50;
rm $d
