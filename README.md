Change time and images used by editing the final number in .cursor files inside `Horse-Cursors-Files`. For example:

```
32 1 9 Horse-Busy1.png 35
32 1 9 Horse-Busy2.png 35
[...]
```
The number at the end, '35', is the time in **milliseconds** that will take to change from a image to the next. If you want it faster, decrease it; increase it for slower animations.

## Palette swap / color variants

`palette_swap.py` recolors the PNGs, generates an interactive before/after preview, and builds/installs color variants of the theme side by side.

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

The **Olho** and **Crina** sections are separate: their pickers change only the eye pixel and the mane pixels in every frame, even when the rest of the horse uses the same original color. The hex code of the selected color is shown above each picker.

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

### 3. Build a variant

```bash
python3 palette_swap.py --build --variant Red --map palettes/Red.csv
```

The theme is written to `dist/Horse-Cursor-Red/`; the recolored PNGs stay in `build/Red/` for inspection. The variant's `index.theme` gets `Name=Horse-Cursor <Variant>`. To build every CSV in `palettes/`:

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

This creates `dist/Horse-Cursor-Red.tar.gz` with `Horse-Cursor-Red/` at the top level.

### Notes

- The original `Horse-Cursor/` theme is never modified.
- `build/`, `dist/` and `preview/` are generated and ignored by git.
- The `pallet` file is only a reference legend; the source of truth for a variant is its CSV.
- The eye is detected on the original PNGs as the single dark pixel surrounded by the light face colors, so it stays stable across all 103 frames and all variants.
- The mane is detected on the original PNGs as the pixels of the mane color in the top 35% of each frame's opaque bounding box. Hooves and outlines with the same color stay in the normal mapping.
- `palettes/Preto.csv` is a black variant sampled from `palettes/cavalo_preto_sprite.png`.
- Use `--strict` with `--build` to abort if any opaque color is missing from the map.
- Variants are separate Xcursor themes; the desktop uses one active theme at a time, but multiple variants can stay installed.
