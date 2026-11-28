ex="python3 foroleer.py forocoches \"\${busq}\";";
echo -e "trabajar en suiza informatico\nautonomo Suiza facturar\nSuiza 90 dias" | while read -r busq;do
  eval "${ex}";
done;
