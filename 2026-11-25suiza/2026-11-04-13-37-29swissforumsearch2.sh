ex="python3 foroleer.py swissforum \"\${busq}\";";
echo -e "self-employed notification 90 days\\neasygov" | while read -r busq;do
  eval "${ex}";
done;
