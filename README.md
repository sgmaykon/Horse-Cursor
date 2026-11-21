Change time and images used by editing the final number in .cursor files inside `Horse-Cursors-Files`. For example:

```
32 1 9 Horse-Busy1.png 35
32 1 9 Horse-Busy2.png 35
[...]
```
The number at the end, '35', is the time in **milliseconds** that will take to change from a image to the next. If you want it faster, decrease it; increase it for slower animations.

## Palette swap / color variants

`palette_swap.py` recolors the PNGs, generates an interactive before/after preview (plus a standalone colorswap viewer), and builds/installs color variants of the theme side by side.

Requirements: Python 3.9+, Pillow (`python3 -m pip install Pillow`) and `xcursorgen`.

### 1. Extract the current palette

```bash
python3 palette_swap.py --extract
```

This writes `palette_map.csv` with the real colors found in the PNGs (`orig,new,role,target`). The red X (`#FF0000`, `#FF4848`) and the opaque black outline (`#000000`) are left out by default.

The extract also adds an **eye** row (`#734A00` here) marked with `target=eye` and a **mane** row (`#503200` here) marked with `target=mane`. Those rows change only the eye pixel and the mane pixels; other parts that use the same color keep the normal mapping (so hooves and mane can be recolored independently).

### 2. Preview before applying

```bash
python3 palette_swap.py --preview --map palette_map.csv
```

Open `preview/index.html`, change the colors with the color pickers, watch the animations update live, and click **Exportar CSV**. Save the file as `palettes/<Name>.csv` (e.g. `palettes/Red.csv`). A static overview is also written to `preview/contact_sheet.png`.

Other buttons in the preview:

- **Carregar CSV** — load any `palettes/*.csv` (or a 3-column CSV) to visualize it; matching rows are applied to the pickers and the animations, and the variant name is taken from the file name.
- **Copiar CSV** — copy the current CSV to the clipboard (fallback when downloads are blocked).
- **Ver conteúdo do CSV** — shows the live CSV text so it can be checked or copied by hand.

The **Olho** and **Crina** sections are separate: their pickers change only the eye pixel and the mane pixels in every frame, even when the rest of the horse uses the same original color. The hex code of the selected color is shown above each picker.

### 2b. Standalone colorswap viewer

To check only the result of a CSV — no color pickers, no export — generate the separate viewer:

```bash
python3 palette_swap.py --viewer --map palettes/black.csv
xdg-open preview/viewer.html
```

`--viewer` and `--colorswap` are aliases. The page shows the original and the recolored animation side by side for all 13 cursors, a read-only palette legend with pixel counts, and the hotspot used for each cursor. The **Carregar CSV** button (or dragging a `.csv` onto the page) swaps the palette live; **Paleta padrão** restores the palette used to generate the page, and **Comparar com original** toggles between side-by-side and recolored-only.

### The CSV format

```
orig,new,role,target
#734A00,#CC00CC,escuro,color
#734A00,#00C8FF,olho,eye
#503200,#0A0A0C,cascos/contorno,color
#503200,#4A4952,crina,mane
```

- `target=color` (default) applies to every opaque pixel of `orig` that is not inside a region.
- `target=eye` applies only to the detected eye pixel. If a color row and an eye row share the same `orig`, the eye row wins for the eye pixel.
- `target=mane` applies only to the pixels of `orig` in the top `MANE_TOP_RATIO` (35%) of the horse's opaque bounding box, which is the mane region. The remaining pixels of that color (hooves, leg/tail outlines) follow the normal mapping.
- Old 3-column CSVs (`orig,new,role`) still work.

### Hotspot alignment

Each line of a `.cursor` file carries the hotspot (the exact pixel placed under the pointer):

```
32 <xhot> <yhot> Horse-Normal1.png 80
```

The original hotspots came from different references, so the horse was drawn at a different offset for a few actions and the pointer jumped when the action changed. `--preview` and `--build` now anchor every cursor to the same reference: the **nose tip**, i.e. the leftmost opaque column of the first frame and the bottom pixel of that tip. That is the rule the original files already follow for 9 of the 13 cursors; only these four changed:

| cursor | before | after |
| --- | --- | --- |
| Horse-Horizont | (5,17) | (5,9) |
| Horse-Move | (17,6) | (3,16) |
| Horse-Precisio | (4,10) | (5,9) |
| Horse-Vertical | (0,10) | (0,13) |

- The preview draws each action on a bigger canvas with a green crosshair marking the pointer, so alignment can be compared side by side. The **Alinhar pelo ponteiro (hotspot)** checkbox switches between the aligned view and the raw 32×32 canvas.
- The `.cursor` sources in `Horse-Cursors-Files/` are never modified: corrected hotspots are written into `build/<Variant>/` and compiled into the generated theme only.
- Pass `--no-align` to `--preview`, `--build` or `--build-all` to keep the original hotspots.

### 3. Build a variant

```bash
python3 palette_swap.py --build --variant Red --map palettes/Red.csv
```

The theme is written to `dist/Horse-Cursor-Red/`; the recolored PNGs stay in `build/Red/` for inspection. The variant's `index.theme` gets `Name=Horse-Cursor <Variant>` plus the version/provenance keys described in [Versioning](#versioning). To build every CSV in `palettes/`:

```bash
python3 palette_swap.py --build-all
```

### 4. Install

```bash
python3 palette_swap.py --install --variant Red
# or, for the legacy ~/.icons path:
python3 palette_swap.py --install --variant Red --dest ~/.icons
```

Then select **Horse-Cursor Red** in your desktop's cursor settings. Each variant is a separate theme folder, so Red, Blue, etc. can coexist.

### 5. Package (optional)

```bash
python3 palette_swap.py --package --variant Red
```

Releases are always published as `.tar.gz` (`releases/Horse-Cursor-<Variant>.tar.gz`). The archive contains the `Horse-Cursor-<Variant>/` folder with `index.theme` and `cursors/`: the compiled `Cursor-Horse-*` files plus all action symlinks (`default`, `pointer`, `move`, `v_double_arrow`, ...). Symlinks are stored as symlinks.

The original artwork can also be packaged with the corrected hotspots:

```bash
python3 palette_swap.py --build --variant original --map palette_map.csv
python3 palette_swap.py --package --variant original
```

### Versioning

Every generated theme records its release data in `index.theme`, so a package can be identified without external metadata:

```ini
Version=1.0
X-Horse-Cursor-Variant=black
X-Horse-Cursor-Palette=palettes/black.csv
X-Horse-Cursor-Hotspot=nose
```

- `Version` comes from `THEME_VERSION` in `palette_swap.py`. It identifies the compiled theme release; bump it when the compiled output changes.
- `Name` keeps the human-readable variant (`Horse-Cursor black`), so variants coexist instead of overwriting each other.
- `X-Horse-Cursor-*` keys are informational (Xcursor ignores them) and record the palette CSV and the hotspot rule used by the build: `nose` for the aligned default, `original` when built with `--no-align`.
- Git tags mark released versions (e.g. `v1.0.0`), and the archives are attached to the GitHub Release instead of being committed. `releases/` is gitignored except for its `.gitkeep`.

### Notes

- The original `Horse-Cursor/` theme is never modified.
- `build/`, `dist/` and `preview/` are generated and ignored by git; `releases/` only keeps a `.gitkeep`, since the archives are published as GitHub Releases.
- The `pallet` file is only a reference legend; the source of truth for a variant is its CSV.
- The eye is detected on the original PNGs as the single dark pixel surrounded by the light face colors, so it stays stable across all 103 frames and all variants.
- The mane is detected on the original PNGs as the pixels of the mane color in the top 35% of each frame's opaque bounding box. Hooves and outlines with the same color stay in the normal mapping.
- `palettes/black.csv` is the tuned black palette used for the packaged theme.
- Use `--strict` with `--build` to abort if any opaque color is missing from the map.
- Variants are separate Xcursor themes; the desktop uses one active theme at a time, but multiple variants can stay installed.
