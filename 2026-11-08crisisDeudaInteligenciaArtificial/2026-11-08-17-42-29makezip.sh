source ${functest};
gn;
z="${now}zipfinal";
while read -r file;do eval "zip ${z} $file";done < <( ls ${1}*.* )
