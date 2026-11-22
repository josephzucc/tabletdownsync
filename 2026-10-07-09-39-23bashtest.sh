source functest.sh;
getNow;
echo "${now}: Hola ${1}"
echo "${now}: Reading messages on ${1}"
id=$(cat "${1}" | grep "s/pid=//g")
out=0
l="${id}.txt"
while (( out == 0 ));do
  if ! [ -f ${l} ];then echo "${now}: No exis";fi;
  echo "${now}: Reading ${l}"
  command=$( cat ${l} | tail -n 1 );
  getNow;
  if [[ "$command" != "" ]];then echo "{now}: Saliendo...";out=1;else echo "${now}: Command vacio";fi
done
