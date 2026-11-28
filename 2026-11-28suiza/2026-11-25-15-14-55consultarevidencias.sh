o=$(ls 2026-11-25-15-01*evide*.*);csvsql --tables t --query "SELECT puntos as p,SUBSTR(tema,1,5) as tema,SUBSTR(evidencia,1,100) as evidencia FROM t" $o | csvlook | more
