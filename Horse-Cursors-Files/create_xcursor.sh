#!/usr/bin/env bash

# Garante que o script pare se houver erro crítico
set -e

echo "=== Compilando arquivos .cursor em Xcursor ==="

for file in *.cursor; do
    # Verifica se existem arquivos .cursor na pasta
    [ -e "$file" ] || { echo "Nenhum arquivo .cursor encontrado."; exit 1; }
    
    # Remove a extensão .cursor para definir o nome do arquivo final
    output_name="Cursor-${file%.cursor}"
    
    echo "Gerando Xcursor: $output_name (a partir de $file)"
    xcursorgen "$file" "$output_name"
done

echo "Compilação concluída com sucesso!"
