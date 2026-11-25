  man="$1";man=${man:2};
  echo -e "\n\n${man}";
  out="";i=1;l=$(cat ${man} | wc -l);
  while [[ "$out" == "" ]];do
   echo "$i $l"
   j=$((i+30));cat "$man" | sed -n "${i},${j}p";
   i=$((i+30));
   read -t 5 -p "out [ENTER=seguir] ? " out;
   if (( i > l ));then out="fin";fi;
  done;
