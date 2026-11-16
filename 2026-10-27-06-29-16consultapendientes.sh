source "${functest}";
gn;
a=$(pwd);echo "Folder actual $a"
f="${BASH_SOURCE}";f=$(dirname "$f");echo $f;
cd "$f";
d="${now}lista.txt";
while read -r file;do
  di=$(basename $(dirname ${file}));
  fi=$(basename "$file");
  echo "$fi (${di})" >>${d};
done < <(find . -type f -iname "*pendien*.txt" 2>/dev/null | grep -v "INBOX" | grep -v "_adj");
cat ${d} | sort -r;
rm $d;
cd "$a"
