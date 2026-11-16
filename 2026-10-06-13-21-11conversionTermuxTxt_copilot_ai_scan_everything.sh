#!/data/data/com.termux/files/usr/bin/bash

OUTPUT="everything_installed.txt"

echo "=== FULL SYSTEM ENUMERATION (NO FILTERS) ===" > "$OUTPUT"
echo "Generated on: $(date)" >> "$OUTPUT"
echo >> "$OUTPUT"

echo "=== ALL BINARIES IN PATH ===" >> "$OUTPUT"
compgen -c | sort -u >> "$OUTPUT"

echo >> "$OUTPUT"
echo "=== ALL PACKAGES INSTALLED VIA pkg ===" >> "$OUTPUT"
pkg list-installed >> "$OUTPUT"

echo >> "$OUTPUT"
echo "=== ALL PYTHON MODULES INSTALLED (pip list) ===" >> "$OUTPUT"
pip list >> "$OUTPUT"

echo >> "$OUTPUT"
echo "=== ALL PYTHON MODULES (pip freeze) ===" >> "$OUTPUT"
pip freeze >> "$OUTPUT"

echo >> "$OUTPUT"
echo "=== ALL FILES IN /data/data/com.termux/files/usr/bin ===" >> "$OUTPUT"
ls -1 /data/data/com.termux/files/usr/bin >> "$OUTPUT"

echo >> "$OUTPUT"
echo "=== ALL FILES IN /data/data/com.termux/files/usr/lib ===" >> "$OUTPUT"
ls -1 /data/data/com.termux/files/usr/lib >> "$OUTPUT"

echo >> "$OUTPUT"
echo "=== ALL FILES IN /data/data/com.termux/files/usr/share ===" >> "$OUTPUT"
ls -R /data/data/com.termux/files/usr/share >> "$OUTPUT"

echo >> "$OUTPUT"
echo "=== SCAN COMPLETE ===" >> "$OUTPUT"

echo "Report saved to $OUTPUT"
