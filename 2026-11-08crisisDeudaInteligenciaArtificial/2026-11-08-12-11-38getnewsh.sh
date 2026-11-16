add () {
  while read -r file;do
    i=$(echo ${file: -5:2});
    j=$((i+1));
    k=$(printf "%02d" "$j");
    file2=$(echo $file | sed "s/${i}/${k}/g" );
    echo "cp \"$file\" \"$file2\"";done;
};
t="ls -1 *crisis*.sh";
echo "${t}";eval "${t}";
o=$(ls *crisis*.sh | sort | tail -n 1 | add );
echo "$o";
eval "$o";
