#!/usr/bin/env python3
"""Horse Cursor palette swap.

Ferramenta para recolorir os PNGs do tema Horse-Cursor, gerar um preview
interativo e criar/instalar variantes do tema lado a lado.

Exemplos:
    python3 palette_swap.py --extract
    python3 palette_swap.py --preview --map palette_map.csv
    python3 palette_swap.py --viewer --map palettes/black.csv
    python3 palette_swap.py --build --variant Red --map palettes/Red.csv
    python3 palette_swap.py --build-all
    python3 palette_swap.py --install --variant Red
    python3 palette_swap.py --package --variant Red
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from collections import Counter
from html import escape as html_escape
from dataclasses import dataclass
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Pillow não encontrado. Instale com: python3 -m pip install Pillow")


ROOT = Path(__file__).resolve().parent
SRC_DIR = ROOT / "Horse-Cursors-Files"
THEME_DIR = ROOT / "Horse-Cursor"
PALETTES_DIR = ROOT / "palettes"
BUILD_DIR = ROOT / "build"
DIST_DIR = ROOT / "dist"
RELEASES_DIR = ROOT / "releases"
PREVIEW_DIR = ROOT / "preview"
DEFAULT_MAP = ROOT / "palette_map.csv"

# Versão do tema compilado. Aparece em Version= no index.theme de cada
# variante e é incrementada quando o pacote muda de forma relevante.
THEME_VERSION = "1.0"

HEX_RE = re.compile(r"^#?([0-9A-Fa-f]{6})$")
VARIANT_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# O olho é um único pixel escuro cercado pelos tons claros da cara. Essas são
# as cores claras originais usadas nesse contorno; a detecção sempre roda nos
# PNGs originais, então continua estável mesmo depois de recolorir.
EYE_LIGHT_COLORS: frozenset[tuple[int, int, int]] = frozenset(
    {
        (0xFF, 0xB8, 0x48),
        (0xFF, 0xD4, 0x8E),
        (0xFF, 0xAA, 0x00),
        (0xDC, 0x92, 0x00),
    }
)

TARGET_COLOR = "color"
TARGET_EYE = "eye"
TARGET_MANE = "mane"
TARGETS = {TARGET_COLOR, TARGET_EYE, TARGET_MANE}

# A crina fica na parte superior do sprite. Consideramos "região da crina" os
# pixels da cor alvo acima desta fração da altura opaca do cavalo. A detecção
# usa o bounding box de cada frame, então funciona mesmo quando o cavalo está
# deslocado (Move, Vertical, etc.).
MANE_TOP_RATIO = 0.35

# Cores da arte original que também formam a crina. O `--extract` cria uma
# linha `target=mane` para elas, permitindo recolorir cascos e crina separado.
MANE_SOURCE_COLORS: tuple[tuple[int, int, int], ...] = ((0x50, 0x32, 0x00),)

# Cores da arte original que também formam a crina. O `--extract` cria uma
# linha `target=mane` para elas, permitindo recolorir cascos e crina separado.
MANE_SOURCE_COLORS: tuple[tuple[int, int, int], ...] = ((0x50, 0x32, 0x00),)

# Cores que ficam fora do swap por padrão. Elas só entram se aparecerem
# explicitamente no CSV de mapeamento.
IGNORED_COLORS: dict[tuple[int, int, int], str] = {
    (255, 0, 0): "#FF0000 — X do Horse-Unavail",
    (255, 72, 72): "#FF4848 — X claro do Horse-Unavail",
    (0, 0, 0): "#000000 — contorno opaco do Horse-Help",
}

# Papéis derivados da legenda `pallet`. Cores que não têm correspondência
# exata na legenda ficam como "a definir".
KNOWN_ROLES: dict[tuple[int, int, int], str] = {
    (0xDC, 0x92, 0x00): "sombreamento",
    (0x96, 0x62, 0x00): "partes escuras",
    (0xB9, 0x7A, 0x00): "sombra",
    (0x50, 0x32, 0x00): "cascos/contorno",
    (0xFF, 0xAA, 0x00): "quase geral",
    (0x56, 0x56, 0x56): "pelo cinza",
    (0x73, 0x4A, 0x00): "escuro (a definir)",
    (0xFF, 0xB8, 0x48): "partes claras",
    (0x3E, 0x3E, 0x3E): "cinza escuro (a definir)",
    (0xFF, 0xD4, 0x8E): "brilho",
}


@dataclass(frozen=True)
class MapRow:
    orig: tuple[int, int, int]
    new: tuple[int, int, int]
    role: str = ""
    target: str = TARGET_COLOR

    @property
    def is_eye(self) -> bool:
        return self.target == TARGET_EYE

    @property
    def is_region(self) -> bool:
        return self.target != TARGET_COLOR


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#%02X%02X%02X" % rgb


def parse_hex(value: str) -> tuple[int, int, int]:
    match = HEX_RE.match(value.strip())
    if not match:
        raise ValueError(f"cor inválida: {value!r} (use #RRGGBB)")
    digits = match.group(1).upper()
    return (
        int(digits[0:2], 16),
        int(digits[2:4], 16),
        int(digits[4:6], 16),
    )


def iter_pixels(im: Image.Image):
    """Pillow 13 deprecates getdata; use get_flattened_data when available."""
    getter = getattr(im, "get_flattened_data", None)
    if getter is not None:
        return getter()
    return im.getdata()


def find_eye_pixels(
    im: Image.Image,
    eye_rgb: tuple[int, int, int],
) -> list[tuple[int, int]]:
    """Coordenadas do olho: pixel de `eye_rgb` cercado só por tons claros."""
    im = im.convert("RGBA")
    px = im.load()
    width, height = im.size
    hits: list[tuple[int, int]] = []

    for y in range(1, height - 1):
        for x in range(1, width - 1):
            r, g, b, a = px[x, y]
            if not a or (r, g, b) != eye_rgb:
                continue
            neighbors = (px[x + 1, y], px[x - 1, y], px[x, y + 1], px[x, y - 1])
            if all(n[3] and n[:3] in EYE_LIGHT_COLORS for n in neighbors):
                hits.append((x, y))

    return hits


def sprite_bbox(im: Image.Image) -> tuple[int, int, int, int] | None:
    """Caixa dos pixels opacos (minx, miny, maxx, maxy)."""
    im = im.convert("RGBA")
    px = im.load()
    width, height = im.size
    xs = [x for x in range(width) if any(px[x, y][3] for y in range(height))]
    ys = [y for y in range(height) if any(px[x, y][3] for x in range(width))]
    if not xs or not ys:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def find_mane_pixels(
    im: Image.Image,
    mane_rgb: tuple[int, int, int],
    ratio: float = MANE_TOP_RATIO,
) -> list[tuple[int, int]]:
    """Coordenadas da crina: pixels de `mane_rgb` na parte superior do sprite."""
    im = im.convert("RGBA")
    box = sprite_bbox(im)
    if box is None:
        return []

    _minx, miny, _maxx, maxy = box
    span = max(maxy - miny, 1)
    px = im.load()
    width, height = im.size
    hits: list[tuple[int, int]] = []

    for y in range(height):
        if (y - miny) / span >= ratio:
            continue
        for x in range(width):
            r, g, b, a = px[x, y]
            if a and (r, g, b) == mane_rgb:
                hits.append((x, y))

    return hits


def luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


def find_region_pixels(
    im: Image.Image,
    rgb: tuple[int, int, int],
    target: str,
) -> list[tuple[int, int]]:
    if target == TARGET_EYE:
        return find_eye_pixels(im, rgb)
    if target == TARGET_MANE:
        return find_mane_pixels(im, rgb)
    return []


def detect_eye_color(
    files: list[Path],
) -> tuple[int, int, int] | None:
    """Descobre a cor do olho nos PNGs originais.

    O olho é o único pixel de sua cor que aparece cercado pelos tons claros
    da cara em todos os frames. Quando mais de uma cor se encaixa (o brilho
    ao lado do olho, por exemplo), a mais escura vence.
    """
    exact_frames: Counter = Counter()
    multi_frames: Counter = Counter()

    for path in files:
        im = Image.open(path).convert("RGBA")
        px = im.load()
        width, height = im.size
        per_color: Counter = Counter()

        for y in range(1, height - 1):
            for x in range(1, width - 1):
                r, g, b, a = px[x, y]
                if not a:
                    continue
                neighbors = (
                    px[x + 1, y],
                    px[x - 1, y],
                    px[x, y + 1],
                    px[x, y - 1],
                )
                if all(n[3] and n[:3] in EYE_LIGHT_COLORS for n in neighbors):
                    per_color[(r, g, b)] += 1

        for rgb, count in per_color.items():
            if count == 1:
                exact_frames[rgb] += 1
            else:
                multi_frames[rgb] += 1

    candidates = [
        rgb
        for rgb, frames in exact_frames.items()
        if frames == len(files) and not multi_frames.get(rgb)
    ]
    if not candidates:
        return None
    return min(candidates, key=luminance)


def scan_images(src_dir: Path = SRC_DIR) -> tuple[list[Path], Counter]:
    files = sorted(src_dir.glob("*.png"))
    if not files:
        raise SystemExit(f"[erro] Nenhum PNG encontrado em {src_dir}")

    counts: Counter = Counter()
    for path in files:
        im = Image.open(path).convert("RGBA")
        for r, g, b, a in iter_pixels(im):
            if a:
                counts[(r, g, b)] += 1
    return files, counts


def read_map(path: Path) -> list[MapRow]:
    if not path.exists():
        raise SystemExit(f"[erro] Mapa não encontrado: {path}")

    rows: list[MapRow] = []
    seen: set[tuple[tuple[int, int, int], str]] = set()

    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise SystemExit(f"[erro] {path}: CSV sem cabeçalho")

        field_map = {name.strip().lower(): name for name in reader.fieldnames}
        if not {"orig", "new"}.issubset(field_map):
            raise SystemExit(
                f"[erro] {path}: o CSV precisa das colunas orig,new,role"
            )

        def cell(raw: dict, name: str) -> str:
            key = field_map.get(name)
            return (raw.get(key) or "").strip() if key else ""

        for lineno, raw in enumerate(reader, start=2):
            orig_s = cell(raw, "orig")
            if not orig_s:
                continue
            try:
                orig = parse_hex(orig_s)
                new_s = cell(raw, "new")
                new = parse_hex(new_s) if new_s else orig
            except ValueError as exc:
                raise SystemExit(f"[erro] {path}:{lineno}: {exc}")

            target = (cell(raw, "target") or TARGET_COLOR).lower()
            if target not in TARGETS:
                raise SystemExit(
                    f"[erro] {path}:{lineno}: target inválido: {target!r} "
                    f"(use {TARGET_COLOR} ou {TARGET_EYE})"
                )

            key = (orig, target)
            if key in seen:
                raise SystemExit(
                    f"[erro] {path}:{lineno}: linha duplicada "
                    f"{rgb_to_hex(orig)} ({target})"
                )
            seen.add(key)
            rows.append(MapRow(orig, new, cell(raw, "role"), target))

    return rows


def make_rows(counts: Counter) -> list[MapRow]:
    rows: list[MapRow] = []
    for rgb, _count in counts.most_common():
        if rgb in IGNORED_COLORS:
            continue
        rows.append(MapRow(rgb, rgb, KNOWN_ROLES.get(rgb, "a definir")))
    for rgb in MANE_SOURCE_COLORS:
        if rgb in counts:
            rows.append(MapRow(rgb, rgb, "crina", TARGET_MANE))
    return rows


def write_map(path: Path, rows: list[MapRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["orig", "new", "role", "target"])
        for row in rows:
            writer.writerow(
                [
                    rgb_to_hex(row.orig),
                    rgb_to_hex(row.new),
                    row.role,
                    row.target,
                ]
            )


def make_lut(
    rows: list[MapRow],
) -> dict[tuple[int, int, int], tuple[int, int, int]]:
    """Mapeamento cor → cor, ignorando as linhas de região (olho, crina)."""
    return {
        row.orig: row.new
        for row in rows
        if not row.is_region and row.orig != row.new
    }


def region_pairs(
    rows: list[MapRow],
) -> list[tuple[str, tuple[int, int, int], tuple[int, int, int]]]:
    return [
        (row.target, row.orig, row.new) for row in rows if row.is_region
    ]


def recolor(
    im: Image.Image,
    lut: dict[tuple[int, int, int], tuple[int, int, int]],
    regions: list[
        tuple[str, tuple[int, int, int], tuple[int, int, int]]
    ]
    | None = None,
) -> tuple[Image.Image, Counter, Counter]:
    """Recolore a imagem; devolve (imagem, cores alteradas, pixels de região)."""
    im = im.convert("RGBA")
    px = im.load()
    width, height = im.size

    # A detecção usa a imagem original, antes de qualquer troca de cor. As
    # regiões reservam seus pixels sempre, mesmo com cor final igual à
    # original: assim a linha normal do CSV não pinta a crina/olho junto.
    region_pixels: dict[
        tuple[int, int], tuple[str, tuple[int, int, int], tuple[int, int, int]]
    ] = {}
    for target, orig, new in regions or []:
        for pos in find_region_pixels(im, orig, target):
            if pos in region_pixels:
                previous = region_pixels[pos][0]
                print(
                    f"[aviso] {pos} está em mais de uma região "
                    f"({previous}, {target}); usando {target}."
                )
            region_pixels[pos] = (target, orig, new)

    changed: Counter = Counter()
    region_changed: Counter = Counter()

    for y in range(height):
        for x in range(width):
            r, g, b, a = px[x, y]
            if not a:
                continue
            if (x, y) in region_pixels:
                continue
            new = lut.get((r, g, b))
            if new is not None:
                px[x, y] = (new[0], new[1], new[2], a)
                changed[(r, g, b)] += 1

    for (x, y), (target, orig, new) in region_pixels.items():
        if orig == new:
            continue
        px[x, y] = (new[0], new[1], new[2], 255)
        region_changed[(target, orig)] += 1

    return im, changed, region_changed


def validate_map(
    rows: list[MapRow],
    counts: Counter,
    strict: bool = False,
) -> tuple[list[tuple[tuple[int, int, int], int]], list[tuple[int, int, int]]]:
    mapped = {row.orig for row in rows}
    unmapped = [
        (rgb, counts[rgb])
        for rgb in counts
        if rgb not in mapped and rgb not in IGNORED_COLORS
    ]
    unknown = [row.orig for row in rows if row.orig not in counts]

    if unmapped:
        print("[aviso] Cores opacas sem mapa (serão mantidas):")
        for rgb, count in sorted(unmapped, key=lambda item: -item[1]):
            print(f"         {rgb_to_hex(rgb)}  {count:,} px")
        if strict:
            raise SystemExit(
                "[erro] --strict: mapeie todas as cores opacas ou remova --strict"
            )

    if unknown:
        print("[aviso] Cores no mapa que não existem nos PNGs:")
        for rgb in unknown:
            print(f"         {rgb_to_hex(rgb)}")

    return unmapped, unknown


@dataclass(frozen=True)
class CursorDef:
    name: str
    path: Path
    frames: tuple[tuple[str, int], ...]
    hotspot: tuple[int, int]


def nose_anchor(im: Image.Image) -> tuple[int, int] | None:
    """Ponto de referência do focinho: coluna opaca mais à esquerda e a base
    da pontinha (primeiro trecho contínuo de pixels dessa coluna).

    É a mesma referência usada pelos hotspots originais da maioria dos
    cursores. Assim o cavalo fica sempre na mesma posição relativa ao ponteiro.
    """
    im = im.convert("RGBA")
    px = im.load()
    width, height = im.size

    for x in range(width):
        ys = [y for y in range(height) if px[x, y][3]]
        if not ys:
            continue
        bottom = ys[0]
        for y in ys[1:]:
            if y != bottom + 1:
                break
            bottom = y
        return (x, bottom)
    return None


def resolve_hotspots(
    cursors: list[CursorDef],
    align: bool = True,
) -> dict[str, tuple[int, int]]:
    """Hotspot final de cada cursor.

    Com `align`, usa o focinho do primeiro frame (pose de repouso) como âncora
    fixa da sequência. Sem `align`, mantém o hotspot original do arquivo
    `.cursor`.
    """
    hotspots: dict[str, tuple[int, int]] = {}
    for cursor in cursors:
        anchor = None
        if align and cursor.frames:
            first = cursor.path.parent / cursor.frames[0][0]
            anchor = nose_anchor(Image.open(first))
        hotspots[cursor.name] = anchor or cursor.hotspot
    return hotspots


def write_cursor_config(
    cursor: CursorDef,
    hotspot: tuple[int, int],
    out_path: Path,
) -> None:
    """Escreve uma cópia do `.cursor` com o hotspot alinhado.

    As linhas inválidas/comentários são preservadas como estão.
    """
    xhot, yhot = hotspot
    lines: list[str] = []
    for raw_line in cursor.path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        parts = line.split()
        if len(parts) >= 5:
            size, filename, delay = parts[0], parts[-2], parts[-1]
            lines.append(f"{size} {xhot} {yhot} {filename} {delay}")
        else:
            lines.append(raw_line)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_cursors(
    src_dir: Path = SRC_DIR,
) -> list[CursorDef]:
    cursor_files = sorted(src_dir.glob("*.cursor"))
    if not cursor_files:
        raise SystemExit(f"[erro] Nenhum .cursor encontrado em {src_dir}")

    cursors: list[CursorDef] = []
    for cursor_file in cursor_files:
        frames: list[tuple[str, int]] = []
        hotspot: tuple[int, int] | None = None
        for lineno, raw_line in enumerate(
            cursor_file.read_text(encoding="utf-8").splitlines(), start=1
        ):
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 5:
                raise SystemExit(
                    f"[erro] {cursor_file}:{lineno}: linha inválida: {raw_line!r}"
                )
            filename = parts[-2]
            try:
                delay = int(parts[-1])
                if hotspot is None:
                    hotspot = (int(parts[1]), int(parts[2]))
            except ValueError:
                raise SystemExit(
                    f"[erro] {cursor_file}:{lineno}: valor inválido: {raw_line!r}"
                )
            if not (src_dir / filename).exists():
                raise SystemExit(
                    f"[erro] {cursor_file}:{lineno}: frame não encontrado: {filename}"
                )
            frames.append((filename, delay))

        if not frames:
            raise SystemExit(f"[erro] {cursor_file}: nenhum frame")
        cursors.append(
            CursorDef(
                name=cursor_file.stem,
                path=cursor_file,
                frames=tuple(frames),
                hotspot=hotspot or (0, 0),
            )
        )

    return cursors



def build_preview_rows(rows: list[MapRow], counts: Counter) -> list[MapRow]:
    seen: set[tuple[tuple[int, int, int], str]] = set()
    seen_orig: set[tuple[int, int, int]] = set()
    preview: list[MapRow] = []

    for row in rows:
        key = (row.orig, row.target)
        if key in seen:
            continue
        seen.add(key)
        seen_orig.add(row.orig)
        preview.append(row)

    for rgb, _count in counts.most_common():
        if rgb in seen_orig or rgb in IGNORED_COLORS:
            continue
        seen_orig.add(rgb)
        preview.append(MapRow(rgb, rgb, KNOWN_ROLES.get(rgb, "a definir")))

    preview.sort(
        key=lambda row: (
            -counts.get(row.orig, 0),
            rgb_to_hex(row.orig),
            row.is_region,
        )
    )
    return preview


def load_font(size: int):
    for candidate in (
        "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ):
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def checkerboard(
    size: int,
    cell: int = 8,
    c1: tuple[int, int, int] = (48, 48, 48),
    c2: tuple[int, int, int] = (64, 64, 64),
) -> Image.Image:
    tile = Image.new("RGB", (size, size), c1)
    draw = ImageDraw.Draw(tile)
    for y in range(0, size, cell):
        for x in range(0, size, cell):
            if ((x // cell) + (y // cell)) % 2:
                draw.rectangle(
                    (x, y, x + cell - 1, y + cell - 1), fill=c2
                )
    return tile


def generate_contact_sheet(
    rows: list[MapRow],
    sequences: dict[str, list[tuple[str, int]]],
    out_path: Path,
    scale: int = 3,
    cols: int = 8,
) -> None:
    lut = make_lut(rows)
    regions = region_pairs(rows)

    frame_order: list[str] = []
    seen: set[str] = set()
    for frames in sequences.values():
        for filename, _delay in frames:
            if filename not in seen:
                seen.add(filename)
                frame_order.append(filename)

    sprite = 32 * scale
    gap = 14
    margin = 20
    label_h = 22
    cell_w = sprite * 2 + 8
    cell_h = sprite + label_h
    n_rows = math.ceil(len(frame_order) / cols) if frame_order else 1
    sheet_w = margin * 2 + cols * cell_w + (cols - 1) * gap

    title_font = load_font(20)
    label_font = load_font(10)
    small_font = load_font(9)

    pal_cell_w = 150
    pal_cell_h = 52
    pal_cols = max(1, (sheet_w - margin * 2) // pal_cell_w)
    pal_rows = math.ceil(len(rows) / pal_cols) if rows else 1
    pal_y = margin + 62
    grid_y = pal_y + pal_rows * pal_cell_h + 16
    sheet_h = grid_y + n_rows * cell_h + (n_rows - 1) * gap + margin

    sheet = Image.new("RGB", (sheet_w, sheet_h), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)

    draw.text(
        (margin, margin),
        "Horse Cursor — Palette Swap — contact sheet",
        font=title_font,
        fill=(240, 240, 240),
    )
    draw.text(
        (margin, margin + 26),
        "Original à esquerda · recolorido à direita · xadrez = transparência",
        font=small_font,
        fill=(160, 160, 160),
    )
    draw.text(
        (margin, margin + 46),
        "Paleta aplicada",
        font=label_font,
        fill=(200, 200, 200),
    )

    for index, row in enumerate(rows):
        col = index % pal_cols
        line = index // pal_cols
        x = margin + col * pal_cell_w
        y = pal_y + line * pal_cell_h
        draw.rectangle((x, y, x + 18, y + 18), fill=row.orig, outline=(10, 10, 10))
        draw.rectangle(
            (x + 22, y, x + 40, y + 18), fill=row.new, outline=(10, 10, 10)
        )
        draw.text(
            (x, y + 22),
            f"{rgb_to_hex(row.orig)} → {rgb_to_hex(row.new)}",
            font=small_font,
            fill=(220, 220, 220),
        )
        label = row.role or "—"
        if row.is_region and row.target not in label.lower():
            label += f" · {row.target}"
        draw.text(
            (x, y + 34),
            label[:24],
            font=small_font,
            fill=(150, 150, 150),
        )

    background = checkerboard(sprite, cell=max(4, scale * 2))
    for index, filename in enumerate(frame_order):
        col = index % cols
        line = index // cols
        x = margin + col * (cell_w + gap)
        y = grid_y + line * (cell_h + gap)

        original = Image.open(SRC_DIR / filename).convert("RGBA")
        recolored, _changed, _region_changed = recolor(original, lut, regions)

        for offset, img in enumerate((original, recolored)):
            tile = background.copy()
            scaled = img.resize((sprite, sprite), Image.Resampling.NEAREST)
            tile.paste(scaled, (0, 0), scaled)
            px = x + offset * (sprite + 8)
            sheet.paste(tile, (px, y))
            draw.rectangle(
                (px, y, px + sprite - 1, y + sprite - 1),
                outline=(70, 70, 70),
            )

        draw.text(
            (x, y + sprite + 3),
            filename,
            font=label_font,
            fill=(210, 210, 210),
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)


def json_for_script(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace(
        "</", "<\\/"
    )


def build_preview_payload(
    rows: list[MapRow],
    counts: Counter,
    sequences: dict[str, list[tuple[str, int]]],
    hotspots: dict[str, tuple[int, int]] | None = None,
) -> dict:
    frame_names = sorted(
        {filename for frames in sequences.values() for filename, _ in frames}
    )
    frames_data = {}
    for filename in frame_names:
        data = (SRC_DIR / filename).read_bytes()
        frames_data[filename] = (
            "data:image/png;base64," + base64.b64encode(data).decode("ascii")
        )

    palette_data = [
        {
            "orig": rgb_to_hex(row.orig),
            "new": rgb_to_hex(row.new),
            "role": row.role,
            "target": row.target,
            "count": counts.get(row.orig, 0),
            "found": row.orig in counts,
        }
        for row in rows
    ]

    region_rows = [row for row in rows if row.is_region]
    region_data: dict[
        str, dict[str, dict[str, list[list[int]]]]
    ] = {}
    region_counts: Counter = Counter()
    if region_rows:
        for filename in frame_names:
            im = Image.open(SRC_DIR / filename).convert("RGBA")
            frame_regions: dict[str, dict[str, list[list[int]]]] = {}
            for row in region_rows:
                coords = find_region_pixels(im, row.orig, row.target)
                if not coords:
                    continue
                key = rgb_to_hex(row.orig)
                frame_regions.setdefault(row.target, {})[key] = [
                    [x, y] for x, y in coords
                ]
                region_counts[(row.target, key)] += len(coords)
            region_data[filename] = frame_regions

        for entry in palette_data:
            if entry["target"] != TARGET_COLOR:
                count = region_counts.get(
                    (entry["target"], entry["orig"]), 0
                )
                entry["count"] = count
                entry["found"] = count > 0

    cursor_data = {
        name: [[filename, delay] for filename, delay in frames]
        for name, frames in sequences.items()
    }

    hotspot_data = {
        name: list(hotspots[name])
        for name in sequences
        if hotspots and name in hotspots
    }

    ignored_data = [
        label
        for rgb, label in IGNORED_COLORS.items()
        if rgb in counts and all(row.orig != rgb for row in rows)
    ]

    meta_data = {
        "frame_count": len(frame_names),
        "cursor_count": len(sequences),
        "png_count": len(list(SRC_DIR.glob("*.png"))),
        "opaque_pixels": sum(counts.values()),
    }

    return {
        "palette": palette_data,
        "cursors": cursor_data,
        "hotspots": hotspot_data,
        "frames": frames_data,
        "ignored": ignored_data,
        "regions": region_data,
        "meta": meta_data,
    }


def render_template(
    template: str,
    payload: dict,
    out_path: Path,
    extra_json: dict | None = None,
    extra_text: dict | None = None,
) -> None:
    html_text = template
    replacements = {
        "__PALETTE_JSON__": payload["palette"],
        "__CURSORS_JSON__": payload["cursors"],
        "__HOTSPOTS_JSON__": payload["hotspots"],
        "__FRAMES_JSON__": payload["frames"],
        "__IGNORED_JSON__": payload["ignored"],
        "__REGIONS_JSON__": payload["regions"],
        "__META_JSON__": payload["meta"],
    }
    if extra_json:
        replacements.update(extra_json)
    for placeholder, value in replacements.items():
        html_text = html_text.replace(placeholder, json_for_script(value))
    for placeholder, value in (extra_text or {}).items():
        html_text = html_text.replace(placeholder, value)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_text, encoding="utf-8")


def generate_html(
    rows: list[MapRow],
    counts: Counter,
    sequences: dict[str, list[tuple[str, int]]],
    out_path: Path,
    hotspots: dict[str, tuple[int, int]] | None = None,
) -> None:
    payload = build_preview_payload(rows, counts, sequences, hotspots)
    render_template(HTML_TEMPLATE, payload, out_path)


def generate_viewer(
    rows: list[MapRow],
    counts: Counter,
    sequences: dict[str, list[tuple[str, int]]],
    out_path: Path,
    hotspots: dict[str, tuple[int, int]] | None = None,
    palette_name: str = "",
) -> None:
    payload = build_preview_payload(rows, counts, sequences, hotspots)
    render_template(
        VIEWER_TEMPLATE,
        payload,
        out_path,
        extra_json={"__PALETTE_NAME_JSON__": palette_name},
        extra_text={"__PALETTE_NAME_HTML__": html_escape(palette_name)},
    )


def validate_variant(variant: str) -> None:
    if variant in {".", ".."} or not VARIANT_RE.fullmatch(variant):
        raise SystemExit(
            f"[erro] nome de variante inválido: {variant!r} "
            "(use letras, números, ., _ ou -)"
        )


def upsert_theme_key(text: str, key: str, value: str, after: str) -> str:
    """Define key=value, substituindo a chave ou inserindo após `after`."""
    line = f"{key}={value}"
    pattern = re.compile(rf"(?m)^{re.escape(key)}=.*$")
    if pattern.search(text):
        return pattern.sub(lambda _match: line, text, count=1)

    anchor = re.search(rf"(?m)^{re.escape(after)}=.*$", text)
    if anchor:
        return text[: anchor.end()] + "\n" + line + text[anchor.end() :]
    return text.replace("[Icon Theme]", f"[Icon Theme]\n{line}", 1)


def palette_label(map_path: Path) -> str:
    try:
        return str(map_path.resolve().relative_to(ROOT))
    except ValueError:
        return map_path.name


def update_index_theme(
    path: Path,
    variant: str,
    map_path: Path | None = None,
    align: bool = True,
    version: str = THEME_VERSION,
) -> None:
    if not path.exists():
        raise SystemExit(f"[erro] index.theme não encontrado: {path}")

    text = path.read_text(encoding="utf-8")
    if re.search(r"(?m)^Name=", text):
        text = re.sub(
            r"(?m)^Name=.*$", f"Name=Horse-Cursor {variant}", text, count=1
        )
    else:
        text = text.replace(
            "[Icon Theme]", f"[Icon Theme]\nName=Horse-Cursor {variant}", 1
        )

    if re.search(r"(?m)^Comment=", text):
        text = re.sub(
            r"(?m)^Comment=(.*)$",
            lambda match: f"Comment={match.group(1).rstrip()} (variante: {variant})",
            text,
            count=1,
        )

    # Versionamento e proveniência do build: versão do tema, variante,
    # CSV de paleta usado e regra de hotspot aplicada.
    text = upsert_theme_key(text, "Version", version, after="Comment")
    text = upsert_theme_key(
        text, "X-Horse-Cursor-Variant", variant, after="Version"
    )
    if map_path is not None:
        text = upsert_theme_key(
            text,
            "X-Horse-Cursor-Palette",
            palette_label(map_path),
            after="X-Horse-Cursor-Variant",
        )
    hotspot_after = (
        "X-Horse-Cursor-Palette" if map_path is not None else "X-Horse-Cursor-Variant"
    )
    text = upsert_theme_key(
        text,
        "X-Horse-Cursor-Hotspot",
        "nose" if align else "original",
        after=hotspot_after,
    )

    path.write_text(text, encoding="utf-8")


def build_variant(
    variant: str,
    map_path: Path,
    strict: bool = False,
    align: bool = True,
) -> Path:
    validate_variant(variant)
    if shutil.which("xcursorgen") is None:
        raise SystemExit(
            "[erro] xcursorgen não encontrado. Instale o pacote xcursorgen."
        )
    if not THEME_DIR.exists():
        raise SystemExit(f"[erro] Tema original não encontrado: {THEME_DIR}")

    map_rows = read_map(map_path)
    files, counts = scan_images()
    validate_map(map_rows, counts, strict=strict)
    lut = make_lut(map_rows)
    regions = region_pairs(map_rows)

    build_dir = BUILD_DIR / variant
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)

    changed: Counter = Counter()
    region_changed: Counter = Counter()
    for src in files:
        im = Image.open(src).convert("RGBA")
        im, frame_changed, frame_region_changed = recolor(im, lut, regions)
        changed.update(frame_changed)
        region_changed.update(frame_region_changed)
        im.save(build_dir / src.name)

    cursors = parse_cursors()
    hotspots = resolve_hotspots(cursors, align=align)
    for cursor in cursors:
        write_cursor_config(
            cursor, hotspots[cursor.name], build_dir / cursor.path.name
        )

    out_dir = build_dir / "out"
    out_dir.mkdir()
    for cursor in cursors:
        output = out_dir / f"Cursor-{cursor.name}"
        result = subprocess.run(
            ["xcursorgen", cursor.path.name, str(output.relative_to(build_dir))],
            cwd=build_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise SystemExit(
                f"[erro] xcursorgen falhou para {cursor_file.name}:\n"
                f"{result.stderr.strip()}"
            )
        if not output.exists() or output.stat().st_size == 0:
            raise SystemExit(f"[erro] xcursorgen não gerou {output}")

    dist_theme = DIST_DIR / f"Horse-Cursor-{variant}"
    if dist_theme.exists():
        shutil.rmtree(dist_theme)
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(THEME_DIR, dist_theme, symlinks=True)

    for compiled in sorted(out_dir.iterdir()):
        shutil.copy2(compiled, dist_theme / "cursors" / compiled.name)

    update_index_theme(
        dist_theme / "index.theme", variant, map_path=map_path, align=align
    )

    total_changed = sum(changed.values()) + sum(region_changed.values())
    print(f"[ok] Variante: {variant}")
    print(f"     Mapa: {map_path}")
    print(f"     PNGs recoloridos: {len(files)} em {build_dir}")
    print(f"     Pixels alterados: {total_changed:,}")
    for rgb, count in changed.most_common(6):
        print(f"       {rgb_to_hex(rgb)} -> {rgb_to_hex(lut[rgb])}: {count:,} px")
    for row in map_rows:
        if row.is_region:
            count = region_changed.get((row.target, row.orig), 0)
            print(
                f"       {row.target} {rgb_to_hex(row.orig)} -> "
                f"{rgb_to_hex(row.new)}: {count:,} px"
            )
    if total_changed == 0:
        print(
            "[aviso] Nenhuma cor mudou (mapa identidade). "
            "O tema é igual ao original."
        )
    print(f"[ok] Tema: {dist_theme}")
    return dist_theme


def cmd_extract(args: argparse.Namespace) -> None:
    files, counts = scan_images()
    rows = make_rows(counts)
    out = Path(args.out).expanduser()

    if out.exists() and not args.force:
        print(f"[ok] {out} já existe; use --force para sobrescrever.")
        return

    eye = detect_eye_color(files)
    if eye is not None:
        rows.append(MapRow(eye, eye, "olho", TARGET_EYE))

    region_totals: Counter = Counter()
    region_rows = [row for row in rows if row.is_region]
    for path in files:
        im = Image.open(path).convert("RGBA")
        for row in region_rows:
            region_totals[(row.target, row.orig)] += len(
                find_region_pixels(im, row.orig, row.target)
            )

    write_map(out, rows)
    PALETTES_DIR.mkdir(exist_ok=True)
    print(f"[ok] {len(rows)} cores escritas em {out}")
    for row in rows:
        if row.is_region:
            count = region_totals.get((row.target, row.orig), 0)
            print(
                f"     {rgb_to_hex(row.orig)} -> {rgb_to_hex(row.new)}  "
                f"{row.role}  ({count:,} px)  [somente {row.target}]"
            )
            continue
        print(
            f"     {rgb_to_hex(row.orig)} -> {rgb_to_hex(row.new)}  "
            f"{row.role}  ({counts[row.orig]:,} px)"
        )

    ignored = [label for rgb, label in IGNORED_COLORS.items() if rgb in counts]
    if ignored:
        print("[info] Fora do swap por padrão:")
        for label in ignored:
            print(f"       {label}")


def resolve_map_rows(
    args: argparse.Namespace, files: list[Path], counts: Counter
) -> tuple[Path, list[MapRow]]:
    map_path = Path(args.map).expanduser() if args.map else DEFAULT_MAP

    if map_path.exists():
        return map_path, read_map(map_path)
    if args.map:
        raise SystemExit(f"[erro] Mapa não encontrado: {map_path}")

    map_rows = make_rows(counts)
    eye = detect_eye_color(files)
    if eye is not None:
        map_rows.append(MapRow(eye, eye, "olho", TARGET_EYE))
    for rgb in MANE_SOURCE_COLORS:
        if rgb in counts:
            map_rows.append(MapRow(rgb, rgb, "crina", TARGET_MANE))
    write_map(map_path, map_rows)
    print(f"[ok] {map_path} criado a partir das cores atuais.")
    return map_path, map_rows


def cmd_preview(args: argparse.Namespace) -> None:
    files, counts = scan_images()
    map_path, map_rows = resolve_map_rows(args, files, counts)

    PALETTES_DIR.mkdir(exist_ok=True)
    validate_map(map_rows, counts, strict=False)

    cursors = parse_cursors()
    hotspots = resolve_hotspots(cursors, align=not args.no_align)
    sequences = {cursor.name: list(cursor.frames) for cursor in cursors}
    preview_rows = build_preview_rows(map_rows, counts)

    html_path = PREVIEW_DIR / "index.html"
    sheet_path = PREVIEW_DIR / "contact_sheet.png"

    generate_html(preview_rows, counts, sequences, html_path, hotspots)
    generate_contact_sheet(preview_rows, sequences, sheet_path)

    if args.no_align:
        print("[info] Hotspots originais (sem alinhamento pelo focinho).")
    else:
        adjusted = [
            cursor
            for cursor in cursors
            if hotspots[cursor.name] != cursor.hotspot
        ]
        if adjusted:
            print("[ok] Hotspots alinhados pelo focinho:")
            for cursor in adjusted:
                old = cursor.hotspot
                new = hotspots[cursor.name]
                print(
                    f"       {cursor.name}: ({old[0]},{old[1]}) -> "
                    f"({new[0]},{new[1]})"
                )

    print(f"[ok] Preview: {html_path}")
    print(f"[ok] Contact sheet: {sheet_path}")
    print(f"     Abra com: xdg-open {html_path}")


def cmd_viewer(args: argparse.Namespace) -> None:
    files, counts = scan_images()
    map_path, map_rows = resolve_map_rows(args, files, counts)
    validate_map(map_rows, counts, strict=False)

    cursors = parse_cursors()
    hotspots = resolve_hotspots(cursors, align=not args.no_align)
    sequences = {cursor.name: list(cursor.frames) for cursor in cursors}
    viewer_rows = build_preview_rows(map_rows, counts)

    out_path = PREVIEW_DIR / "viewer.html"
    generate_viewer(
        viewer_rows,
        counts,
        sequences,
        out_path,
        hotspots,
        palette_name=map_path.name,
    )

    print(f"[ok] Viewer: {out_path}")
    print(f"     Paleta: {map_path}")
    print(f"     Abra com: xdg-open {out_path}")


def cmd_build(args: argparse.Namespace) -> None:
    if not args.variant:
        raise SystemExit("[erro] --build exige --variant")
    if not args.map:
        raise SystemExit("[erro] --build exige --map")
    build_variant(
        args.variant,
        Path(args.map).expanduser(),
        strict=args.strict,
        align=not args.no_align,
    )


def cmd_build_all(args: argparse.Namespace) -> None:
    if not PALETTES_DIR.exists():
        raise SystemExit(f"[erro] Pasta não encontrada: {PALETTES_DIR}")

    maps = sorted(PALETTES_DIR.glob("*.csv"))
    if not maps:
        raise SystemExit(f"[erro] Nenhum CSV em {PALETTES_DIR}")

    for map_path in maps:
        variant = re.sub(r"[^A-Za-z0-9._-]+", "-", map_path.stem).strip("-")
        if not variant:
            print(f"[aviso] Ignorando {map_path.name}: nome de variante vazio")
            continue
        print(f"\n=== Build {variant} ({map_path.name}) ===")
        build_variant(
            variant, map_path, strict=args.strict, align=not args.no_align
        )


def cmd_install(args: argparse.Namespace) -> None:
    if not args.variant:
        raise SystemExit("[erro] --install exige --variant")
    validate_variant(args.variant)

    src = DIST_DIR / f"Horse-Cursor-{args.variant}"
    if not src.exists():
        raise SystemExit(
            f"[erro] Tema não encontrado: {src}\n"
            f"       Rode --build --variant {args.variant} primeiro."
        )

    dest = (
        Path(args.dest).expanduser()
        if args.dest
        else Path.home() / ".local" / "share" / "icons"
    )
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)

    target = dest / src.name
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(src, target, symlinks=True)

    print(f"[ok] Instalado em: {target}")
    print(
        f'     Selecione "Horse-Cursor {args.variant}" '
        "nas configurações de cursor."
    )
    print(
        "     Se o tema não aparecer, reabra a configuração do desktop "
        "ou faça logout/login."
    )


def package_tar_gz(src: Path, out: Path) -> None:
    with tarfile.open(out, "w:gz") as tar:
        tar.add(src, arcname=src.name)


def package_zip(src: Path, out: Path) -> None:
    """Zip preserving symlinks and file modes (Unix attributes)."""
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(src, followlinks=False):
            root_path = Path(root)
            dirs.sort()
            files.sort()

            for dirname in dirs:
                path = root_path / dirname
                if not path.is_symlink():
                    continue
                arcname = str(Path(src.name) / path.relative_to(src))
                add_zip_symlink(zf, path, arcname)

            for filename in files:
                path = root_path / filename
                arcname = str(Path(src.name) / path.relative_to(src))
                if path.is_symlink():
                    add_zip_symlink(zf, path, arcname)
                else:
                    zf.write(path, arcname)


def add_zip_symlink(zf: zipfile.ZipFile, path: Path, arcname: str) -> None:
    info = zipfile.ZipInfo(arcname)
    info.create_system = 3  # Unix
    info.external_attr = (0o120777 << 16) | 0o777
    zf.writestr(info, os.readlink(path))


def cmd_package(args: argparse.Namespace) -> None:
    if not args.variant:
        raise SystemExit("[erro] --package exige --variant")
    validate_variant(args.variant)

    src = DIST_DIR / f"Horse-Cursor-{args.variant}"
    if not src.exists():
        raise SystemExit(
            f"[erro] Tema não encontrado: {src}\n"
            f"       Rode --build --variant {args.variant} primeiro."
        )

    RELEASES_DIR.mkdir(parents=True, exist_ok=True)

    tar_gz = RELEASES_DIR / f"Horse-Cursor-{args.variant}.tar.gz"
    package_tar_gz(src, tar_gz)
    print(f"[ok] Pacote: {tar_gz}")

    zip_out = RELEASES_DIR / f"Horse-Cursor-{args.variant}.zip"
    package_zip(src, zip_out)
    print(f"[ok] Pacote: {zip_out}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Palette swap dos cursores Horse-Cursor.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--extract",
        action="store_true",
        help="gera o CSV de paleta a partir dos PNGs",
    )
    group.add_argument(
        "--preview",
        action="store_true",
        help="gera preview/index.html + contact_sheet.png",
    )
    group.add_argument(
        "--viewer",
        "--colorswap",
        action="store_true",
        dest="viewer",
        help="gera preview/viewer.html (só o colorswap, com carregamento de CSV)",
    )
    group.add_argument(
        "--build", action="store_true", help="cria uma variante do tema"
    )
    group.add_argument(
        "--build-all",
        action="store_true",
        help="cria todas as variantes de palettes/*.csv",
    )
    group.add_argument(
        "--install",
        action="store_true",
        help="instala uma variante em ~/.local/share/icons",
    )
    group.add_argument(
        "--package",
        action="store_true",
        help="gera releases/Horse-Cursor-<Variante>.tar.gz e .zip",
    )
    parser.add_argument(
        "--map", type=Path, help="CSV de mapeamento (orig,new,role,target)"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_MAP,
        help=f"saída do --extract (padrão: {DEFAULT_MAP.name})",
    )
    parser.add_argument(
        "--variant", help="nome da variante (ex.: Red)"
    )
    parser.add_argument(
        "--dest",
        help="destino do --install (padrão: ~/.local/share/icons)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="aborta se alguma cor opaca ficar fora do mapa",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="sobrescreve o CSV no --extract",
    )
    parser.add_argument(
        "--no-align",
        action="store_true",
        help="mantém os hotspots originais (não alinha pelo focinho)",
    )
    args = parser.parse_args(argv)

    if args.extract:
        cmd_extract(args)
    elif args.preview:
        cmd_preview(args)
    elif args.viewer:
        cmd_viewer(args)
    elif args.build:
        cmd_build(args)
    elif args.build_all:
        cmd_build_all(args)
    elif args.install:
        cmd_install(args)
    elif args.package:
        cmd_package(args)
    return 0


VIEWER_TEMPLATE = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Horse Cursor — Colorswap</title>
<style>
:root {
  --bg: #151515;
  --panel: #202020;
  --panel-2: #2a2a2a;
  --text: #ededed;
  --muted: #a0a0a0;
  --accent: #f0a500;
  --border: #373737;
  --ok: #7fb069;
  --warn: #e0a458;
  --error: #ff6b6b;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 24px;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, Ubuntu, sans-serif;
  background: var(--bg);
  color: var(--text);
}
code {
  background: #333;
  padding: 1px 5px;
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.9em;
}
header {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 16px;
}
h1 { margin: 0 0 6px; font-size: 1.5rem; }
h1 .accent { color: var(--accent); font-weight: 500; }
.sub { margin: 0; color: var(--muted); font-size: 0.9rem; }
.status-box {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 4px;
  font-size: 0.82rem;
}
#summary { color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
#csv-status[data-kind="ok"] { color: var(--ok); }
#csv-status[data-kind="warn"] { color: var(--warn); }
#csv-status[data-kind="error"] { color: var(--error); }
.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin: 18px 0 4px;
}
.file-button, button {
  display: inline-flex;
  align-items: center;
  background: var(--accent);
  color: #1a1a1a;
  border: 0;
  border-radius: 6px;
  padding: 8px 14px;
  font: inherit;
  font-weight: 600;
  cursor: pointer;
}
.file-button:hover, button:hover { filter: brightness(1.08); }
button.ghost {
  background: var(--panel-2);
  color: var(--text);
  border: 1px solid var(--border);
}
.toggle {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: var(--muted);
  font-size: 0.88rem;
  cursor: pointer;
  margin-left: auto;
}
h2 {
  margin: 30px 0 12px;
  font-size: 1.05rem;
  border-bottom: 1px solid var(--border);
  padding-bottom: 6px;
}
.legend {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(290px, 1fr));
  gap: 6px 14px;
}
.legend-row {
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 7px 10px;
  font-size: 0.82rem;
}
.legend-row.region { border-color: #5a4a22; }
.swatch {
  width: 18px;
  height: 18px;
  border-radius: 4px;
  border: 1px solid #0c0c0c;
  flex: 0 0 auto;
}
.arrow { color: var(--muted); }
.legend-row .hex {
  background: none;
  padding: 0;
  font-size: 0.82rem;
  min-width: 66px;
}
.legend-row .role {
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}
.badge {
  background: #3b3222;
  color: #e9bd6a;
  border-radius: 99px;
  padding: 2px 8px;
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.legend-row .count {
  color: var(--muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.75rem;
  white-space: nowrap;
}
.warn { color: var(--error); font-size: 0.72rem; }
.cursors-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 12px;
}
.cursor-card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px;
}
.cursor-card h3 {
  margin: 0 0 10px;
  font-size: 0.95rem;
  font-weight: 600;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.anim-pair { display: flex; gap: 16px; justify-content: center; }
.anim { display: flex; flex-direction: column; align-items: center; gap: 6px; }
.anim span {
  font-size: 0.72rem;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.anim canvas {
  width: 144px;
  height: 144px;
  image-rendering: pixelated;
  image-rendering: crisp-edges;
  border: 1px solid var(--border);
  border-radius: 6px;
  background-color: #333;
  background-image:
    linear-gradient(45deg, #444 25%, transparent 25%),
    linear-gradient(-45deg, #444 25%, transparent 25%),
    linear-gradient(45deg, transparent 75%, #444 75%),
    linear-gradient(-45deg, transparent 75%, #444 75%);
  background-size: 16px 16px;
  background-position: 0 0, 0 8px, 8px -8px, -8px 0;
}
body.only-new .before-wrap { display: none; }
.hotspot {
  margin: 10px 0 0;
  text-align: center;
  color: var(--muted);
  font-size: 0.75rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
footer {
  margin-top: 36px;
  color: var(--muted);
  font-size: 0.82rem;
  border-top: 1px solid var(--border);
  padding-top: 12px;
}
.drop-overlay {
  position: fixed;
  inset: 0;
  display: none;
  align-items: center;
  justify-content: center;
  background: rgba(10, 10, 10, 0.82);
  border: 3px dashed var(--accent);
  color: var(--text);
  font-size: 1.2rem;
  z-index: 10;
  pointer-events: none;
}
body.dragging .drop-overlay { display: flex; }
@media (max-width: 600px) {
  body { padding: 12px; }
  .anim canvas { width: 108px; height: 108px; }
  .anim-pair { gap: 8px; }
  .toggle { margin-left: 0; }
}
</style>
</head>
<body>
<div class="drop-overlay"><span>Solte o CSV para visualizar</span></div>

<header>
  <div>
    <h1>Horse-Cursor <span class="accent">colorswap</span></h1>
    <p class="sub">Só o resultado da troca de cores. Paleta: <code id="palette-name">__PALETTE_NAME_HTML__</code></p>
  </div>
  <div class="status-box">
    <span id="summary">carregando frames…</span>
    <span id="csv-status" role="status" aria-live="polite"></span>
  </div>
</header>

<div class="toolbar">
  <label class="file-button" for="csv-file">Carregar CSV</label>
  <input type="file" id="csv-file" accept=".csv,text/csv" hidden>
  <button type="button" id="reset" class="ghost">Paleta padrão</button>
  <label class="toggle"><input type="checkbox" id="compare-toggle" checked> Comparar com original</label>
</div>

<h2>Paleta aplicada</h2>
<div class="legend" id="palette-legend"></div>
<p class="note" id="ignored-note"></p>

<h2>Cursores</h2>
<div class="cursors-grid" id="cursors-grid"></div>

<footer>
  Gerado por <code>palette_swap.py --viewer</code> · <span id="meta"></span>
</footer>

<script>
const PALETTE = __PALETTE_JSON__;
const CURSORS = __CURSORS_JSON__;
const HOTSPOTS = __HOTSPOTS_JSON__;
const FRAMES = __FRAMES_JSON__;
const IGNORED = __IGNORED_JSON__;
const REGIONS = __REGIONS_JSON__;
const META = __META_JSON__;
const PALETTE_NAME = __PALETTE_NAME_JSON__;

const CANVAS_SIZE = 36;
const ANCHOR_X = 2;
const ANCHOR_Y = 12;

const images = {};
const recolored = new Map();
const stateKey = p => p.orig + '|' + p.target;
const state = new Map(PALETTE.map(p => [stateKey(p), p.new]));
const totalImages = Object.keys(FRAMES).length;

function esc(value) {
  return String(value).replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}

function hexToRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function rgbToHex(r, g, b) {
  return '#' + [r, g, b].map(v => v.toString(16).padStart(2, '0')).join('').toUpperCase();
}

function setCsvStatus(message, kind) {
  const el = document.getElementById('csv-status');
  el.textContent = message;
  el.dataset.kind = kind || '';
}

function buildLegend() {
  const host = document.getElementById('palette-legend');
  for (const p of PALETTE) {
    const key = stateKey(p);
    const row = document.createElement('div');
    row.className = 'legend-row' + (p.target !== 'color' ? ' region' : '');
    row.dataset.key = key;
    row.innerHTML = `
      <span class="swatch orig" style="background:${p.orig}" title="Original ${p.orig}"></span>
      <span class="arrow">→</span>
      <span class="swatch new" style="background:${p.new}" title="Nova ${p.new}"></span>
      <code class="hex">${p.new}</code>
      <span class="role">${esc(p.role || '—')}</span>
      ${p.target !== 'color' ? `<span class="badge">${esc(p.target)}</span>` : ''}
      <span class="count">${p.count.toLocaleString('pt-BR')} px</span>
      ${p.found ? '' : '<span class="warn">ausente</span>'}`;
    host.appendChild(row);
  }
  if (IGNORED.length) {
    document.getElementById('ignored-note').textContent =
      'Fora do swap por padrão: ' + IGNORED.join(' · ') + '.';
  } else {
    document.getElementById('ignored-note').textContent =
      'Todas as cores opacas dos PNGs estão na paleta.';
  }
}

function updateLegend(key, value) {
  const row = document.querySelector(`.legend-row[data-key="${CSS.escape(key)}"]`);
  if (!row) return;
  row.querySelector('.new').style.background = value;
  row.querySelector('.hex').textContent = value;
}

function buildCursorCards() {
  const grid = document.getElementById('cursors-grid');
  for (const name of Object.keys(CURSORS)) {
    const card = document.createElement('div');
    card.className = 'cursor-card';
    card.dataset.cursor = name;
    const hot = HOTSPOTS[name] || [0, 0];
    card.innerHTML = `
      <h3>${esc(name)}</h3>
      <div class="anim-pair">
        <div class="anim before-wrap"><span>Original</span><canvas class="before" width="${CANVAS_SIZE}" height="${CANVAS_SIZE}"></canvas></div>
        <div class="anim"><span>Recolorido</span><canvas class="after" width="${CANVAS_SIZE}" height="${CANVAS_SIZE}"></canvas></div>
      </div>
      <p class="hotspot">ponteiro em (${hot[0]}, ${hot[1]})</p>`;
    grid.appendChild(card);
  }
}

function loadImages() {
  return Promise.all(Object.entries(FRAMES).map(([name, url]) => new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => { images[name] = img; resolve(); };
    img.onerror = () => reject(new Error('falha ao carregar ' + name));
    img.src = url;
  })));
}

function buildRecolored() {
  if (Object.keys(images).length < totalImages) return;

  const lut = new Map();
  const regionLut = new Map();
  for (const p of PALETTE) {
    const neu = state.get(stateKey(p)) || p.new;
    if (neu === p.orig) continue;
    if (p.target !== 'color') regionLut.set(stateKey(p), hexToRgb(neu));
    else lut.set(p.orig, hexToRgb(neu));
  }

  recolored.clear();
  let changedPixels = 0;
  let changedFrames = 0;

  for (const [name, img] of Object.entries(images)) {
    const canvas = document.createElement('canvas');
    canvas.width = 32;
    canvas.height = 32;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.clearRect(0, 0, 32, 32);
    ctx.drawImage(img, 0, 0);

    const regionIndex = new Map();
    for (const [target, byColor] of Object.entries(REGIONS[name] || {})) {
      for (const [orig, coords] of Object.entries(byColor)) {
        for (const [x, y] of coords) {
          regionIndex.set((y * 32 + x) * 4, orig + '|' + target);
        }
      }
    }

    if (lut.size || regionLut.size) {
      const data = ctx.getImageData(0, 0, 32, 32);
      const px = data.data;
      let frameChanged = 0;
      for (let i = 0; i < px.length; i += 4) {
        if (px[i + 3] === 0) continue;
        if (regionIndex.has(i)) continue;
        const key = rgbToHex(px[i], px[i + 1], px[i + 2]);
        const neu = lut.get(key);
        if (neu) {
          px[i] = neu[0];
          px[i + 1] = neu[1];
          px[i + 2] = neu[2];
          frameChanged++;
        }
      }
      for (const [i, key] of regionIndex) {
        const neu = regionLut.get(key);
        if (!neu) continue;
        px[i] = neu[0];
        px[i + 1] = neu[1];
        px[i + 2] = neu[2];
        frameChanged++;
      }
      if (frameChanged) {
        ctx.putImageData(data, 0, 0);
        changedPixels += frameChanged;
        changedFrames++;
      }
    }

    recolored.set(name, canvas);
  }

  document.getElementById('summary').textContent =
    `${changedPixels.toLocaleString('pt-BR')} pixels alterados em ${changedFrames} frames · ${Object.keys(CURSORS).length} cursores`;
}

let rebuildTimer = null;
function scheduleRebuild() {
  clearTimeout(rebuildTimer);
  rebuildTimer = setTimeout(buildRecolored, 40);
}

function frameOffset(name) {
  const [hx, hy] = HOTSPOTS[name] || [0, 0];
  return [ANCHOR_X - hx, ANCHOR_Y - hy];
}

function drawCursorFrame(ctx, name, image) {
  const [dx, dy] = frameOffset(name);
  ctx.clearRect(0, 0, CANVAS_SIZE, CANVAS_SIZE);
  ctx.strokeStyle = 'rgba(255, 255, 255, 0.14)';
  ctx.lineWidth = 1;
  ctx.strokeRect(dx + 0.5, dy + 0.5, 32, 32);
  if (image) ctx.drawImage(image, dx, dy);
}

function startAnimations() {
  for (const card of document.querySelectorAll('.cursor-card')) {
    const name = card.dataset.cursor;
    const seq = CURSORS[name];
    if (!seq || !seq.length) continue;

    const before = card.querySelector('canvas.before');
    const after = card.querySelector('canvas.after');
    const bctx = before.getContext('2d');
    const actx = after.getContext('2d');
    let index = 0;

    const step = () => {
      const [file, delay] = seq[index % seq.length];
      card.dataset.frame = file;
      drawCursorFrame(bctx, name, images[file]);
      drawCursorFrame(actx, name, recolored.get(file));
      index++;
      setTimeout(step, delay);
    };

    step();
  }
}

function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = '';
  let inQuotes = false;

  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i++;
        } else {
          inQuotes = false;
        }
      } else {
        field += ch;
      }
    } else if (ch === '"') {
      inQuotes = true;
    } else if (ch === ',') {
      row.push(field);
      field = '';
    } else if (ch === '\n') {
      row.push(field);
      field = '';
      rows.push(row);
      row = [];
    } else if (ch !== '\r') {
      field += ch;
    }
  }

  if (field !== '' || row.length) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}

function normalizeHex(value) {
  const raw = String(value || '').trim().replace(/^#/, '').toUpperCase();
  return /^[0-9A-F]{6}$/.test(raw) ? '#' + raw : null;
}

function applyCsv(text) {
  const rows = parseCsv(text.replace(/^\uFEFF/, '')).filter(
    row => row.length && row.some(cell => cell.trim() !== '')
  );
  const first = String(rows[0]?.[0] ?? '').trim().toLowerCase();
  const start = first === 'orig' ? 1 : 0;
  let applied = 0;
  const missing = [];
  const invalid = [];

  for (const row of rows.slice(start)) {
    if (row.length < 2) continue;
    const orig = normalizeHex(row[0]);
    const neu = normalizeHex(row[1]);
    if (!orig || !neu) {
      invalid.push(row.join(','));
      continue;
    }
    const target = (row[3] || 'color').trim().toLowerCase() || 'color';
    const key = orig + '|' + target;
    if (!state.has(key)) {
      missing.push(target === 'color' ? orig : `${orig} [${target}]`);
      continue;
    }
    state.set(key, neu);
    updateLegend(key, neu);
    applied++;
  }

  if (applied) buildRecolored();
  return { applied, missing, invalid };
}

function describe(result) {
  const parts = [`${result.applied} linhas aplicadas`];
  if (result.missing.length) parts.push(`${result.missing.length} fora da paleta`);
  if (result.invalid.length) parts.push(`${result.invalid.length} inválidas`);
  return parts.join(' · ');
}

function readCsvFile(file) {
  if (!file) return;
  file.text().then(text => {
    const result = applyCsv(text);
    const kind = result.applied ? (result.missing.length || result.invalid.length ? 'warn' : 'ok') : 'error';
    setCsvStatus(`${file.name}: ${describe(result)}.`, kind);
    document.getElementById('palette-name').textContent = file.name;
    if (result.missing.length) console.warn('Linhas fora da paleta:', result.missing);
    if (result.invalid.length) console.warn('Linhas inválidas:', result.invalid);
  }).catch(error => {
    console.error(error);
    setCsvStatus('Não foi possível ler o CSV: ' + error.message, 'error');
  });
}

document.getElementById('csv-file').addEventListener('change', event => {
  const input = event.target;
  readCsvFile(input.files && input.files[0]);
  input.value = '';
});

document.getElementById('reset').addEventListener('click', () => {
  for (const p of PALETTE) {
    state.set(stateKey(p), p.new);
    updateLegend(stateKey(p), p.new);
  }
  buildRecolored();
  document.getElementById('palette-name').textContent = PALETTE_NAME;
  setCsvStatus('Paleta padrão restaurada.', 'ok');
});

document.getElementById('compare-toggle').addEventListener('change', event => {
  document.body.classList.toggle('only-new', !event.target.checked);
});

let dragDepth = 0;
window.addEventListener('dragenter', event => {
  event.preventDefault();
  dragDepth++;
  document.body.classList.add('dragging');
});
window.addEventListener('dragover', event => event.preventDefault());
window.addEventListener('dragleave', event => {
  event.preventDefault();
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) document.body.classList.remove('dragging');
});
window.addEventListener('drop', event => {
  event.preventDefault();
  dragDepth = 0;
  document.body.classList.remove('dragging');
  const file = event.dataTransfer.files && event.dataTransfer.files[0];
  readCsvFile(file);
});

document.getElementById('meta').textContent =
  `${META.frame_count} frames · ${META.cursor_count} cursores · ${META.opaque_pixels.toLocaleString('pt-BR')} pixels opacos`;

buildLegend();
buildCursorCards();
loadImages().then(() => {
  buildRecolored();
  startAnimations();
}).catch(error => {
  console.error(error);
  setCsvStatus('Falha ao carregar os frames: ' + error.message, 'error');
});
</script>
</body>
</html>
"""

HTML_TEMPLATE = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Horse Cursor — Palette Swap Preview</title>
<style>
:root {
  --bg: #171717;
  --panel: #222222;
  --panel-2: #2c2c2c;
  --text: #eeeeee;
  --muted: #a8a8a8;
  --accent: #f0a500;
  --border: #3a3a3a;
  --ok: #7fb069;
  --warn: #ff6b6b;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 24px;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, Ubuntu, sans-serif;
  background: var(--bg);
  color: var(--text);
}
h1 { margin: 0 0 8px; font-size: 1.55rem; }
h2 {
  margin: 32px 0 12px;
  font-size: 1.15rem;
  border-bottom: 1px solid var(--border);
  padding-bottom: 6px;
}
p { line-height: 1.5; }
code {
  background: #333;
  padding: 1px 5px;
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.9em;
}
.note { color: var(--muted); font-size: 0.9rem; }
.palette-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 8px;
}
.palette-row {
  display: flex;
  align-items: center;
  gap: 12px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 12px;
}
.swatches { display: flex; align-items: center; gap: 6px; flex: 0 0 auto; }
.swatch {
  width: 28px;
  height: 28px;
  border-radius: 6px;
  border: 1px solid #000;
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.15);
}
.arrow { color: var(--muted); }
.color-block {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
}
.hex-label {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.72rem;
  line-height: 1;
  color: var(--text);
  letter-spacing: 0.02em;
  white-space: nowrap;
}
input[type="color"] {
  width: 38px;
  height: 30px;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: transparent;
  cursor: pointer;
}
.meta { display: flex; flex-direction: column; gap: 2px; font-size: 0.85rem; min-width: 0; }
.meta code { font-size: 0.8rem; }
.role { color: var(--muted); }
.count { color: var(--ok); }
.warn { color: var(--warn); }
.region-row {
  border-color: #7a5c00;
  background: #2b2410;
  box-shadow: inset 0 0 0 1px rgba(240, 165, 0, 0.25);
}
.badge {
  align-self: flex-start;
  font-size: 0.65rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  background: var(--accent);
  color: #111;
  border-radius: 999px;
  padding: 1px 7px;
  font-weight: 700;
}
.controls {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-top: 16px;
  padding: 12px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
}
.controls label { display: flex; align-items: center; gap: 8px; color: var(--muted); }
.controls input[type="text"] {
  background: var(--panel-2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
  width: 140px;
  font: inherit;
}
button {
  background: var(--accent);
  color: #111;
  border: 0;
  border-radius: 6px;
  padding: 8px 14px;
  font: inherit;
  font-weight: 600;
  cursor: pointer;
}
button.secondary {
  background: var(--panel-2);
  color: var(--text);
  border: 1px solid var(--border);
}
button:hover { filter: brightness(1.08); }
button:active { transform: translateY(1px); }
.file-button {
  display: inline-flex;
  align-items: center;
  background: var(--panel-2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 14px;
  font-weight: 600;
  cursor: pointer;
}
.file-button:hover { filter: brightness(1.08); }
#status { color: var(--muted); font-size: 0.9rem; }
#export-status { font-size: 0.9rem; color: var(--muted); }
#export-status[data-kind="ok"] { color: var(--ok); }
#export-status[data-kind="error"] { color: var(--warn); }
#export-status[data-kind="warn"] { color: var(--text); }
#export-details {
  margin-top: 12px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
}
#export-details summary {
  cursor: pointer;
  color: var(--muted);
  font-size: 0.9rem;
}
#export-details textarea {
  margin-top: 10px;
  width: 100%;
  min-height: 120px;
  resize: vertical;
  background: var(--panel-2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.8rem;
  white-space: pre;
}
.cursors-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 12px;
}
.align-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 14px;
  color: var(--muted);
  font-size: 0.9rem;
  cursor: pointer;
}
.hotspot {
  margin: 10px 0 0;
  text-align: center;
  color: var(--muted);
  font-size: 0.75rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.cursor-card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px;
}
.cursor-card h3 {
  margin: 0 0 10px;
  font-size: 0.95rem;
  font-weight: 600;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.anim-pair { display: flex; gap: 16px; justify-content: center; }
.anim { display: flex; flex-direction: column; align-items: center; gap: 6px; }
.anim span {
  font-size: 0.72rem;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.anim canvas {
  width: 144px;
  height: 144px;
  image-rendering: pixelated;
  image-rendering: crisp-edges;
  border: 1px solid var(--border);
  border-radius: 6px;
  background-color: #333;
  background-image:
    linear-gradient(45deg, #444 25%, transparent 25%),
    linear-gradient(-45deg, #444 25%, transparent 25%),
    linear-gradient(45deg, transparent 75%, #444 75%),
    linear-gradient(-45deg, transparent 75%, #444 75%);
  background-size: 16px 16px;
  background-position: 0 0, 0 8px, 8px -8px, -8px 0;
}
footer {
  margin-top: 40px;
  color: var(--muted);
  font-size: 0.85rem;
}
@media (max-width: 600px) {
  body { padding: 12px; }
  .anim canvas { width: 108px; height: 108px; }
  .anim-pair { gap: 8px; }
}
</style>
</head>
<body>
<header>
  <h1>Horse Cursor — Palette Swap</h1>
  <p class="note">Ajuste as cores abaixo e veja os cursores mudarem na hora. Quando gostar, exporte o CSV e rode o build.</p>
</header>

<section>
  <h2>Paleta</h2>
  <div class="palette-grid" id="palette-grid"></div>
  <p class="note" id="ignored-note"></p>
  <div class="controls">
    <label>Variante <input type="text" id="variant-name" value="MinhaCor" spellcheck="false" autocomplete="off"></label>
    <button type="button" id="reset" class="secondary">Reset</button>
    <button type="button" id="export">Exportar CSV</button>
    <button type="button" id="copy-csv" class="secondary">Copiar CSV</button>
    <label class="file-button" for="csv-file">Carregar CSV</label>
    <input type="file" id="csv-file" accept=".csv,text/csv" hidden>
    <span id="export-status" role="status" aria-live="polite"></span>
    <span id="status">carregando…</span>
  </div>
  <details id="export-details">
    <summary>Ver conteúdo do CSV</summary>
    <textarea id="csv-text" readonly spellcheck="false"></textarea>
  </details>
</section>

<div id="regions"></div>

<section>
  <h2>Cursores</h2>
  <label class="align-toggle">
    <input type="checkbox" id="align-toggle" checked>
    Alinhar pelo ponteiro (hotspot)
  </label>
  <div class="cursors-grid" id="cursors-grid"></div>
</section>

<footer>
  Gerado por <code>palette_swap.py</code> · <span id="meta"></span>
</footer>

<script>
const PALETTE = __PALETTE_JSON__;
const CURSORS = __CURSORS_JSON__;
const HOTSPOTS = __HOTSPOTS_JSON__;
const FRAMES = __FRAMES_JSON__;
const IGNORED = __IGNORED_JSON__;
const REGIONS = __REGIONS_JSON__;
const META = __META_JSON__;

const CANVAS_SIZE = 36;
const ANCHOR_X = 2;
const ANCHOR_Y = 12;
let alignByHotspot = true;

const REGION_META = {
  eye: {
    title: 'Olho',
    note: 'Essa cor vale <strong>só para o pixel do olho</strong>. As outras partes que usam a mesma cor original continuam com o mapeamento normal da paleta acima.'
  },
  mane: {
    title: 'Crina',
    note: 'Essa cor vale <strong>só para os pixels da crina</strong> (parte superior do cavalo). Os cascos e contornos com a mesma cor original continuam com o mapeamento normal da paleta acima.'
  }
};

const images = {};
const recolored = new Map();
const stateKey = p => p.orig + '|' + p.target;
const state = new Map(PALETTE.map(p => [stateKey(p), p.new]));
const totalImages = Object.keys(FRAMES).length;

function esc(value) {
  return String(value).replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}

function hexToRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function rgbToHex(r, g, b) {
  return '#' + [r, g, b].map(v => v.toString(16).padStart(2, '0')).join('').toUpperCase();
}

function paletteRow(p) {
  const isRegion = p.target !== 'color';
  const badge = (REGION_META[p.target]?.title || p.target).toLowerCase();
  const perFrame = p.target === 'eye' ? ' · 1 por frame' : '';
  const row = document.createElement('div');
  row.className = 'palette-row' + (isRegion ? ' region-row' : '');
  row.innerHTML = `
    <div class="swatches">
      <span class="swatch" style="background:${p.orig}" title="Original ${p.orig}"></span>
      <span class="arrow">→</span>
      <div class="color-block">
        <span class="hex-label" title="Cor selecionada">${p.new}</span>
        <input type="color" value="${p.new}" data-key="${stateKey(p)}" aria-label="Nova cor para ${p.orig}">
      </div>
    </div>
    <div class="meta">
      ${isRegion ? `<span class="badge">${esc(badge)}</span>` : ''}
      <code>${esc(p.orig)}</code>
      <span class="role">${esc(p.role || '—')}</span>
      <span class="count">${p.count.toLocaleString('pt-BR')} px${perFrame}</span>
      ${p.found ? '' : '<span class="warn">não encontrada nos PNGs</span>'}
    </div>`;
  return row;
}

function buildPaletteUI() {
  const grid = document.getElementById('palette-grid');
  const regionsHost = document.getElementById('regions');
  const groups = new Map();
  for (const p of PALETTE) {
    if (p.target === 'color') {
      grid.appendChild(paletteRow(p));
    } else {
      if (!groups.has(p.target)) groups.set(p.target, []);
      groups.get(p.target).push(p);
    }
  }
  for (const [target, rows] of groups) {
    const meta = REGION_META[target] || {
      title: target,
      note: `Essa cor vale só para a região <strong>${esc(target)}</strong>.`
    };
    const section = document.createElement('section');
    section.innerHTML = `<h2>${esc(meta.title)}</h2><p class="note">${meta.note}</p>`;
    const rowGrid = document.createElement('div');
    rowGrid.className = 'palette-grid';
    for (const p of rows) rowGrid.appendChild(paletteRow(p));
    section.appendChild(rowGrid);
    regionsHost.appendChild(section);
  }
  document.body.addEventListener('input', event => {
    const input = event.target.closest('input[type="color"]');
    if (!input || !input.dataset.key) return;
    const value = input.value.toUpperCase();
    state.set(input.dataset.key, value);
    const label = input.closest('.color-block')?.querySelector('.hex-label');
    if (label) label.textContent = value;
    scheduleRebuild();
  });
  if (IGNORED.length) {
    document.getElementById('ignored-note').textContent =
      'Fora do swap por padrão: ' + IGNORED.join(' · ') + '. Para incluí-las, adicione linhas no CSV.';
  } else {
    document.getElementById('ignored-note').textContent =
      'Todas as cores opacas dos PNGs estão no mapa.';
  }
}

function buildCursorCards() {
  const grid = document.getElementById('cursors-grid');
  for (const name of Object.keys(CURSORS)) {
    const card = document.createElement('div');
    card.className = 'cursor-card';
    card.dataset.cursor = name;
    const hot = HOTSPOTS[name] || [0, 0];
    card.innerHTML = `
      <h3>${esc(name)}</h3>
      <div class="anim-pair">
        <div class="anim"><span>Original</span><canvas class="before" width="${CANVAS_SIZE}" height="${CANVAS_SIZE}"></canvas></div>
        <div class="anim"><span>Novo</span><canvas class="after" width="${CANVAS_SIZE}" height="${CANVAS_SIZE}"></canvas></div>
      </div>
      <p class="hotspot">ponteiro em (${hot[0]}, ${hot[1]})</p>`;
    grid.appendChild(card);
  }
}

function loadImages() {
  return Promise.all(Object.entries(FRAMES).map(([name, url]) => new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => { images[name] = img; resolve(); };
    img.onerror = () => reject(new Error('falha ao carregar ' + name));
    img.src = url;
  })));
}

function buildRecolored() {
  if (Object.keys(images).length < totalImages) return;

  const lut = new Map();
  const regionLut = new Map();
  for (const p of PALETTE) {
    const neu = state.get(stateKey(p)) || p.new;
    if (neu === p.orig) continue;
    if (p.target !== 'color') regionLut.set(stateKey(p), hexToRgb(neu));
    else lut.set(p.orig, hexToRgb(neu));
  }

  recolored.clear();
  let changedPixels = 0;
  let changedFrames = 0;

  for (const [name, img] of Object.entries(images)) {
    const canvas = document.createElement('canvas');
    canvas.width = 32;
    canvas.height = 32;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.clearRect(0, 0, 32, 32);
    ctx.drawImage(img, 0, 0);

    const regionIndex = new Map();
    for (const [target, byColor] of Object.entries(REGIONS[name] || {})) {
      for (const [orig, coords] of Object.entries(byColor)) {
        for (const [x, y] of coords) {
          regionIndex.set((y * 32 + x) * 4, orig + '|' + target);
        }
      }
    }

    if (lut.size || regionLut.size) {
      const data = ctx.getImageData(0, 0, 32, 32);
      const px = data.data;
      let frameChanged = 0;
      for (let i = 0; i < px.length; i += 4) {
        if (px[i + 3] === 0) continue;
        if (regionIndex.has(i)) continue;
        const key = rgbToHex(px[i], px[i + 1], px[i + 2]);
        const neu = lut.get(key);
        if (neu) {
          px[i] = neu[0];
          px[i + 1] = neu[1];
          px[i + 2] = neu[2];
          frameChanged++;
        }
      }
      for (const [i, key] of regionIndex) {
        const neu = regionLut.get(key);
        if (!neu) continue;
        px[i] = neu[0];
        px[i + 1] = neu[1];
        px[i + 2] = neu[2];
        frameChanged++;
      }
      if (frameChanged) {
        ctx.putImageData(data, 0, 0);
        changedPixels += frameChanged;
        changedFrames++;
      }
    }

    recolored.set(name, canvas);
  }

  document.getElementById('status').textContent =
    `${changedPixels.toLocaleString('pt-BR')} pixels alterados em ${changedFrames} frames`;
  syncCsvPreview();
}

let rebuildTimer = null;
function scheduleRebuild() {
  clearTimeout(rebuildTimer);
  rebuildTimer = setTimeout(buildRecolored, 40);
}

function frameOffset(name) {
  const [hx, hy] = HOTSPOTS[name] || [0, 0];
  return alignByHotspot ? [ANCHOR_X - hx, ANCHOR_Y - hy] : [0, 0];
}

function drawCursorFrame(ctx, name, image) {
  const [dx, dy] = frameOffset(name);
  const [hx, hy] = HOTSPOTS[name] || [0, 0];
  const px = Math.floor(dx + hx) + 0.5;
  const py = Math.floor(dy + hy) + 0.5;

  ctx.clearRect(0, 0, CANVAS_SIZE, CANVAS_SIZE);
  ctx.strokeStyle = 'rgba(255, 255, 255, 0.14)';
  ctx.lineWidth = 1;
  ctx.strokeRect(dx + 0.5, dy + 0.5, 32, 32);
  if (image) ctx.drawImage(image, dx, dy);
}

function startAnimations() {
  for (const card of document.querySelectorAll('.cursor-card')) {
    const name = card.dataset.cursor;
    const seq = CURSORS[name];
    if (!seq || !seq.length) continue;

    const before = card.querySelector('canvas.before');
    const after = card.querySelector('canvas.after');
    const bctx = before.getContext('2d');
    const actx = after.getContext('2d');
    let index = 0;

    const step = () => {
      const [file, delay] = seq[index % seq.length];
      card.dataset.frame = file;
      drawCursorFrame(bctx, name, images[file]);
      drawCursorFrame(actx, name, recolored.get(file));
      index++;
      setTimeout(step, delay);
    };

    step();
  }
}

document.getElementById('align-toggle').addEventListener('change', event => {
  alignByHotspot = event.target.checked;
  for (const card of document.querySelectorAll('.cursor-card')) {
    const name = card.dataset.cursor;
    const file = card.dataset.frame;
    if (!file) continue;
    drawCursorFrame(card.querySelector('canvas.before').getContext('2d'), name, images[file]);
    drawCursorFrame(card.querySelector('canvas.after').getContext('2d'), name, recolored.get(file));
  }
});

document.getElementById('reset').addEventListener('click', () => {
  for (const p of PALETTE) {
    state.set(stateKey(p), p.new);
    const input = document.querySelector(`input[data-key="${stateKey(p)}"]`);
    if (!input) continue;
    input.value = p.new;
    const label = input.closest('.color-block')?.querySelector('.hex-label');
    if (label) label.textContent = p.new.toUpperCase();
  }
  buildRecolored();
});

function variantFileName() {
  const rawName = document.getElementById('variant-name').value.trim() || 'variante';
  const name = rawName.replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '') || 'variante';
  return name + '.csv';
}

function csvText() {
  const lines = ['orig,new,role,target'];
  for (const p of PALETTE) {
    const neu = state.get(stateKey(p)) || p.orig;
    const role = String(p.role || '').replace(/"/g, '""');
    lines.push(`${p.orig},${neu},"${role}",${p.target || 'color'}`);
  }
  return lines.join('\n') + '\n';
}

function setExportStatus(message, kind) {
  const el = document.getElementById('export-status');
  el.textContent = message;
  el.dataset.kind = kind || '';
}

function syncCsvPreview() {
  document.getElementById('csv-text').value = csvText();
}

async function exportCsv() {
  const text = csvText();
  syncCsvPreview();
  const fileName = variantFileName();
  const blob = new Blob([text], { type: 'text/csv;charset=utf-8' });

  if (typeof window.showSaveFilePicker === 'function') {
    try {
      const handle = await window.showSaveFilePicker({
        suggestedName: fileName,
        types: [{ description: 'CSV', accept: { 'text/csv': ['.csv'] } }]
      });
      const writable = await handle.createWritable();
      await writable.write(blob);
      await writable.close();
      setExportStatus(`CSV salvo como ${handle.name || fileName}.`, 'ok');
      return;
    } catch (error) {
      if (error && error.name === 'AbortError') {
        setExportStatus('Exportação cancelada.', 'warn');
        return;
      }
      console.warn('showSaveFilePicker indisponível, usando download:', error);
    }
  }

  try {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = fileName;
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    setExportStatus(`Download iniciado: ${fileName} (pasta de downloads do navegador).`, 'ok');
  } catch (error) {
    console.error(error);
    setExportStatus('Não foi possível baixar o arquivo. Use "Copiar CSV" ou copie o texto abaixo.', 'error');
  }
}

async function copyCsv() {
  const text = csvText();
  syncCsvPreview();
  let copied = false;

  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      copied = true;
    } catch (error) {
      console.warn('navigator.clipboard falhou:', error);
    }
  }

  if (!copied) {
    const area = document.getElementById('csv-text');
    document.getElementById('export-details').open = true;
    area.focus();
    area.select();
    try {
      copied = document.execCommand('copy');
    } catch (error) {
      console.warn('execCommand("copy") falhou:', error);
    }
  }

  if (copied) {
    setExportStatus('CSV copiado para a área de transferência.', 'ok');
  } else {
    document.getElementById('export-details').open = true;
    setExportStatus('Não foi possível copiar automaticamente. Selecione o texto abaixo e copie manualmente.', 'error');
  }
}

function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = '';
  let inQuotes = false;

  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i++;
        } else {
          inQuotes = false;
        }
      } else {
        field += ch;
      }
    } else if (ch === '"') {
      inQuotes = true;
    } else if (ch === ',') {
      row.push(field);
      field = '';
    } else if (ch === '\n') {
      row.push(field);
      field = '';
      rows.push(row);
      row = [];
    } else if (ch !== '\r') {
      field += ch;
    }
  }

  if (field !== '' || row.length) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}

function normalizeHex(value) {
  const raw = String(value || '').trim().replace(/^#/, '').toUpperCase();
  return /^[0-9A-F]{6}$/.test(raw) ? '#' + raw : null;
}

function applyCsv(text) {
  const rows = parseCsv(text.replace(/^\uFEFF/, '')).filter(
    row => row.length && row.some(cell => cell.trim() !== '')
  );
  const first = String(rows[0]?.[0] ?? '').trim().toLowerCase();
  const start = first === 'orig' ? 1 : 0;
  let applied = 0;
  const missing = [];
  const invalid = [];

  for (const row of rows.slice(start)) {
    if (row.length < 2) continue;
    const orig = normalizeHex(row[0]);
    const neu = normalizeHex(row[1]);
    if (!orig || !neu) {
      invalid.push(row.join(','));
      continue;
    }
    const target = (row[3] || 'color').trim().toLowerCase() || 'color';
    const key = orig + '|' + target;
    if (!state.has(key)) {
      missing.push(target === 'color' ? orig : `${orig} [${target}]`);
      continue;
    }
    state.set(key, neu);
    const input = document.querySelector(`input[data-key="${CSS.escape(key)}"]`);
    if (input) {
      input.value = neu;
      const label = input.closest('.color-block')?.querySelector('.hex-label');
      if (label) label.textContent = neu;
    }
    applied++;
  }

  if (applied) buildRecolored();
  return { applied, missing, invalid };
}

async function onCsvFile(event) {
  const input = event.target;
  const file = input.files && input.files[0];
  if (!file) return;

  try {
    const summary = applyCsv(await file.text());
    const parts = [`${summary.applied} linhas aplicadas`];
    if (summary.missing.length) {
      parts.push(`${summary.missing.length} fora da paleta`);
    }
    if (summary.invalid.length) {
      parts.push(`${summary.invalid.length} inválidas`);
    }
    const kind = summary.applied ? 'ok' : 'error';
    setExportStatus(`CSV carregado: ${parts.join(' · ')}.`, kind);
    if (summary.missing.length) {
      console.warn('Linhas fora da paleta:', summary.missing);
    }

    const variant = document.getElementById('variant-name');
    const stem = file.name.replace(/\.csv$/i, '');
    if (!variant.value.trim() || variant.value === 'MinhaCor') {
      variant.value = stem;
    }
  } catch (error) {
    console.error(error);
    setExportStatus('Não foi possível ler o CSV: ' + error.message, 'error');
  }
  input.value = '';
}

document.getElementById('export').addEventListener('click', exportCsv);
document.getElementById('copy-csv').addEventListener('click', copyCsv);
document.getElementById('csv-file').addEventListener('change', onCsvFile);

async function init() {
  buildPaletteUI();
  buildCursorCards();
  await loadImages();
  buildRecolored();
  startAnimations();
  document.getElementById('meta').textContent =
    `${META.frame_count} frames · ${META.cursor_count} cursores · ${META.opaque_pixels.toLocaleString('pt-BR')} pixels opacos`;
}

init().catch(error => {
  console.error(error);
  document.getElementById('status').textContent = 'Erro: ' + error.message;
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
