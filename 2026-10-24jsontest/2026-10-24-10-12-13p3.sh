program=$(find . -maxdepth 1 -type f -iname "*${1}*.py" 2>/dev/null | sort | tail -n 1);
python3 ${program} | jq -r '.name';
