filter1 () {
  while read -r line;do
    if [[ "$stop" != "1" ]] && [[ "${line:0:14}" != "SWISS STANDARD" ]] && [[ "${line:0:12}" != "SWISS GERMAN" ]] && [[ "${line:0:7}" != "ENGLISH" && "${line}" != "" ]];then
      stop="0";
      echo "${lang};${line}";
    elif [[ "${line:0:12}" == "SWISS GERMAN" ]];then stop="1";
    elif [[ "${line:0:5}" == "SWISS" ]];then lang="deu";
    fi;
  done };
stop="";lang="eng";
cat 2026-11-11-19-15-33swiss_it_networking_search_terms.txt | filter1
