ex="python3 empsui.py buscar freelancermap \"\${busq}\" --via-extension";busq="VBA Access\nVB.NET\nVisual Basic 6\nAltsystem Migration";echo -e $busq | while read -r busq;do eval "$ex";done
