# pcbtools

Python-based invoke tasks for EAGLE PCB projects. Replaces Eagle-dependent
CAM/ULP workflows — no Eagle installation required at build time.

## Tasks

| Task | Input | Output | Description |
|------|-------|--------|-------------|
| `bom` | `*.sch` | `*_bom.xlsx` | Bill of materials with Comment, Designator, Footprint, LCSC columns. Parts marked `NO FIT` are preserved as-is. |
| `cpl` | `*.brd` | `*_cpl.csv` | Pick-and-place centroid file with Designator, Mid X/Y, Layer, Rotation. |
| `gerbers` | `*.brd` | `*_<date>_<hash>.zip` | Full RS-274X Gerber + Excellon drill package (copper, soldermask, paste, silkscreen, outline). |
| `pins` | `*.sch` | stdout / pickle | All nets connected to every pin of a named part. |
| `pins-compare` | two pickles | stdout | Diff two pin connection snapshots. |
| `all` | `*.sch`, `*.brd` | all of the above | Runs bom, cpl, and gerbers for every schematic/board in the working directory. |
| `process` | `<name>` | bom + cpl | Shorthand for bom + cpl on `<name>.sch` / `<name>.brd`. |
| `clean` | — | — | Removes Eagle backup files and generated CSVs. |
| `setup-repo` | repo path | — | Bootstraps a new Eagle repo with `.gitignore`, symlinked `tasks.py`, and optional blank schematic/board. |

### Gerber outputs

| File | Layer |
|------|-------|
| `*_copper_l1.GTL` | Top copper |
| `*_copper_l2.G1` – `*_copper_l5.G4` | Inner copper layers 2–5 |
| `*_copper_l6.GBL` | Bottom copper |
| `*_Soldermask_Top.GTS` | Top soldermask |
| `*_Soldermask_Bot.GBS` | Bottom soldermask |
| `*_Paste_Top.GTP` | Top paste |
| `*_Paste_Bot.GBP` | Bottom paste |
| `*_Legend_Top.GTO` | Top silkscreen |
| `*_Legend_Bot.GBO` | Bottom silkscreen |
| `*_Profile_NP.GKO` | Board outline |
| `*_drill.XLN` | Excellon drill file |

Copper pours include isolation clearances (read from the `.brd` design rules).
Vector text (silkscreen labels, `>NAME` / `>VALUE` placeholders) is rendered
using the HersheySansMed stroke font.

## Usage

### As a git submodule

Add pcbtools as a submodule in your Eagle project repo:

```sh
git submodule add git@github.com:gswdh/eagle_tools.git pcbtools
```

Copy the project template into the repo root:

```sh
cp pcbtools/tasks_template.py tasks.py
```

The project `tasks.py` imports everything from `pcbtools/tasks.py` automatically.
Updates to pcbtools are picked up without any changes to the project file.

### Running tasks

```sh
invoke bom main.sch
invoke cpl main.brd
invoke gerbers main.brd
invoke all
```

## Dependencies

```
invoke
pandas
openpyxl
```
