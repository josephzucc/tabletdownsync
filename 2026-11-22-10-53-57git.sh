a=$(pwd);
echo -e "Git v1.00\nEs necesario que al ejecutar el git el folder sea\n${ds}";
if [[ "$ds" == "" ]];then echo "No esta establecida la variable \$ds";return 1;fi;
#gh auth login
source "$functest";gn;
if (( $# < 1 ));then echo "Si no especificas nombre del commit ponemos la fecha/hora actual";set -- ${now};echo $1;fi;
git init
git add .
git remote -v
git commit -m "${1}"
git push # i had already do git push -u tabletdownsync master, master is branch, tabletdownsync is remote repository
cd "$a";
