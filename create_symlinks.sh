#!/usr/bin/env bash

# Função auxiliar para criar symlinks em lote. Execute esse arquivo dentro da
# pasta cursors
make_links() {
    local target="$1"
    shift
    for link in "$@"; do
        ln -sf "$target" "$link"
    done
}

echo "=== Criando Symlinks de Ponteiros ==="

# 1. Ponteiro Normal / Seleção
make_links "Cursor-Horse-Normal" "default" "left_ptr" "arrow" "top_left_arrow"

# 2. Ajuda
make_links "Cursor-Horse-Help" "help" "question_arrow" "whats_this" "dnd-ask"

# 3. Ocupado em segundo plano / Progress
make_links "Cursor-Horse-Working" "progress" "left_ptr_watch" "half-busy"

# 4. Ocupado / Wait
make_links "Cursor-Horse-Busy" "wait" "watch"

# 5. Precisão (Horse-Precisio)
make_links "Cursor-Horse-Precisio" "crosshair" "cross" "tcross"

# 6. Seleção de Texto
make_links "Cursor-Horse-Text" "text" "xterm" "ibeam"

# 7. Hyperlink (Mãozinha / Pointer)
make_links "Cursor-Horse-Text" "pointer" "hand2" "hand1"

# 8. Indisponível / Não permitido
make_links "Cursor-Horse-Unavail" "not-allowed" "forbidden" "circle" "crossed_circle" 

# 9. Redimensionamento Vertical
make_links "Cursor-Horse-Vertical" "v_double_arrow" "size_ver" "ns-resize" "sb_v_double_arrow" "top_side" "bottom_side" "n-resize" "s-resize"

# 10. Redimensionamento Horizontal
make_links "Cursor-Horse-Horizont" "h_double_arrow" "size_hor" "ew-resize" "sb_h_double_arrow" "left_side" "right_side" "w-resize" "e-resize"

# 11. Redimensionamento Diagonal 1 (\ - NW para SE)
make_links "Cursor-Horse-Diag1" "fd_double_arrow" "size_fdiag" "nwse-resize" "top_left_corner" "bottom_right_corner" "nw-resize" "se-resize"

# 12. Redimensionamento Diagonal 2 (/ - NE para SW)
make_links "Cursor-Horse-Diag2" "bk_double_arrow" "size_bdiag" "nesw-resize" "top_right_corner" "bottom_left_corner" "ne-resize" "sw-resize"

# 13. Mover / Mover Janela
make_links "Cursor-Horse-Move" "move" "fleur" "all-scroll" "size_all"

# 14. Seleção Alternativa (Horse-Alternat)
make_links "Cursor-Horse_Alternat" "up_arrow" "center_ptr"

echo "Todos os symlinks foram criados com sucesso!"
