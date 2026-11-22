find . -type f -iname "*manual*.txt" 2>/dev/null | sort | while read -r man;do
  echo $man;
  #bash 2026-11-03-19-53-54leermanuales2.sh "${man}";
done
