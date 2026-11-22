valle="";llano="";punta="";
p=1;
if (( $# > 0 ));then echo "Dia ${1}";fecha="WHERE dia == \"${1}\"";else echo "Dia 2026-11-11";fecha="WHERE dia = \"2026-11-11\"";fi;
t="2026-11-13avilaLaAdradaTravesiaJuegoDeLaBola/2026-11-13bbddpreciospvpc.txt";
while read -r vm;do
  case $p in 1) valle="$vm";p=2;; 2) llano="$vm";p=3;; 3) punta="$vm";;
  esac;
done < <(csvsql --tables t --query "SELECT preciomedio FROM t ${fecha}" $t | tail -n 3);
echo "valle";
vallellano=$(echo "scale=4;((($llano/$valle)-1)*100)" | bc);
llanovalle=$(echo "scale=4;((($valle/$llano)-1)*100)" | bc );
echo "Llano es ${vallellano:0:5} % mas caro que valle, que es ${llanovalle:0:5} % mas barata";
vallepunta=$(echo "scale=4;((($punta/$valle)-1)*100)" | bc);
puntavalle=$(echo "scale=4;((($valle/$punta)-1)*100)" | bc);
echo "Punta es ${vallepunta:0:5} % mas caro que valle, que es ${puntavalle:0:5} % mas barata";
llanopunta=$(echo "scale=4;((($punta/$llano)-1)*100)" | bc)
puntallano=$(echo "scale=4;((($llano/$punta)-1)*100)" | bc)
echo "Punta es ${llanopunta:0:5} % mas caro que llano, que es ${puntallano:0:5} % mas barata";
