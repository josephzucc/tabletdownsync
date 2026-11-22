geturlbygroupid () {
  if (( $# > 0 )); then
    idg=${1};
    source ../functest.sh >/dev/null;
    gn;
    d="${now}listadofb.txt";
    retigurl="";
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
    retigurl=$(cat $d | csvsql -d ';' --tables t --query "SELECT URL FROM t WHERE CODIGO = '${idg}' LIMIT 1" $d);
    retigurl=${retigurl:4};
    rm $d;
    echo "${retigurl}";
  else gn;echo "${now}: geturlbygroupid no se ha especificado idgroup";
  fi;
};

term="habitacion";
gs="2413\n2423\n2411\n2416\n2409\n2415\n2407\n2418\n2406\n2420\n2424\n2417\n2408\n2419\n2412\n2414\n2402\n2401\n2421\n2405\n2422\n2425\n2399\n2410\n2404\n2403\n173\n181";
echo -e "${gs}" | while read -r idg;do
  idg="G${idg}";
  #echo "Buscando ${term} en ${idg}.....";
  #busqueda por termino no funciona, cambio a busqueda por url. Cojo urls de tabla
  #https://www.facebook.com/groups/881977559292967/search/?q=habitacion
  # Mas recientes
  # https://www.facebook.com/groups/554641458397577/search?q=habitacion
  # &filters=eyJycF9jaHJvbm9fc29ydDowIjoie1wibmFtZVwiOlwiY2hyb25vc29ydFwiLFwiYXJnc1wiOlwiXCJ9In0%3D
  #ex="python3 fb.py buscaren ${idg} ${term} -n 50";
  urlbase=$(geturlbygroupid ${idg});
  url="${urlbase}search/?q=${term}";
  ex="python3 fb.py buscaren ${idg} \"${url}\""
  echo $ex
  eval "${ex}";
done

