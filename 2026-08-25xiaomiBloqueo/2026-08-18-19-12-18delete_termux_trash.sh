#!/data/data/com.termux/files/usr/bin/bash

echo "Limpiando cache de Termux..."
echo

# Cache del usuario
if [ -d "$HOME/.cache" ]; then
  echo "🧽 Borrando $HOME/.cache"
  rm -rf "$HOME/.cache"/*
fi

# Cache del sistema Termux
if [ -d "$PREFIX/var/cache" ]; then
  echo "🧽 Borrando $PREFIX/var/cache"
  rm -rf "$PREFIX/var/cache"/*
fi

# Archivos temporales
if [ -d "$PREFIX/tmp" ]; then
  echo "🧽 Borrando $PREFIX/tmp"
  rm -rf "$PREFIX/tmp"/*
fi

echo
echo "✔️ Cache de Termux limpiada."
