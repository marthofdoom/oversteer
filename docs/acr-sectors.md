# Assetto Corsa Rally: the real sector lines

Research of 2026-10-06 (branch `sectors-research`). The question: where are
ACR's split lines along the road, so the sector times can stop being
estimated from the game's sector lengths. Answer: **all 46 stage variants'
start, split and finish lines were read from the installed game**, as
distances along the same road spline the shared memory's distance uses. No
application code or `acr.json` was changed; `scripts/acr-sectors.py`
regenerates every number below from a game install.

## 1. Result in short

- Each stage variant has `RaceSector` actors in its gameplay data layer: index
  0 is the start line, the last index the finish, the ones between are the
  splits (N sectors = N+1 lines; the circuit has N lines, the first is the lap
  line). 46/46 variants found, 44 point-to-point stages plus Livigno's two
  circuit directions.
- Each location has one centre spline per direction (`SplinesActorForward`,
  `SplinesActorReverse`, `CenterSpline`); cut variants use their direction's
  full spline, as the shared memory does (a cut's distance starts at
  thousands of metres).
- That spline **is** the shared memory's distance: its computed length is the
  game's `trackSplineLength` (Cwmbiga: 12077.93 m here, 12077.95 m reported
  by the game), and projecting the car's position from marth's v4 captures
  onto it gives the captured distance to a median of 0.0 m (Sommet de
  Munster) and 0.1 m (Aghii Theodori - Loutraki), car median 0.4-1.1 m off
  the line.
- The game's clock agrees with the lines: it stops when the car's distance
  reaches the finish line (Loutraki reverse 10463.2 m vs line 10463.0;
  Sommet 10477.6 vs 10476.5, at the 60 Hz sample spacing) and starts with the
  car standing 2.6-2.9 m short of the start line (its centre, with the nose at
  the line; Afon Bidno's measured `start_m` 238.2 is 4.7 m short of 242.9).
- Splits are therefore timed where the car's distance crosses a line, and
  Oversteer can time them itself from data it already has: the split time is
  the stage clock interpolated at the sample where `lap_distance` passes the
  line. Nothing has to be read from the game at run time (track B below is
  not needed).
- The current estimate (`sector_bounds`: the table's 0.1 km lengths scaled
  start to finish) is off by a median of 43 m on its worst split per stage
  (40 stages it places), up to 386 m (Hafren Forest), about 1.5-2 s of split
  time at stage speeds. The game's own `sectors_km` are rough: Cwmbiga's
  first sector is 2.84 km, not 2.5; Afon Biga's last is 2.80, not 2.3; Fedw
  Fain's and Banc Gwyn's look swapped with each other.
- Side finding: three cuts start well past their table's first pace note
  (Hafren Forest 450.1 m vs 176.5; Zeli Reverse 358.0 vs 265.1; Aghii
  Theodori Reverse 363.1 vs 312.0). The note table they share with the full
  stage starts earlier, so the app's `start_line()` (first note − 25 m) puts
  their start line 51-274 m too early; with `START_LINE_PAST` = 30 m a run on
  Hafren Forest may not count as a stage start. The real lines fix this too.
- Afon Bidno's `finish_m` 5287.4 (brake-onset estimate, medium) should be the
  line, 5294.1 m.

## 2. Method

Game build of 2026-09-12 (update 0.6), UE 5.6. The paks are IoStore
containers (`.utoc`/`.ucas`), Oodle-compressed, not encrypted. Tools, all in
`~/.cache/oversteer-re/` (nothing installed system-wide): retoc 0.1.5
(trumank/retoc, MIT; built with a local Rust toolchain; its oodle_loader
fetched `liboo2corelinux64.so.9` on first use). `retoc to-legacy -f
Levels/<Map>/_Generated_` turns one map's world-partition cells into legacy
packages (1-2 s per map, up to ~1 GB on disk, deleted after each map); the
script parses those in Python.

Properties are cooked *unversioned* (no names, only the class's property
indices), so each layout was worked out from the bytes and checked across
all maps:

- `RaceSector` (/Script/acr, an actor): property 0 its root component (a
  BoxComponent named `Trigger`), 1 a `SplineLocations` component, 2 the
  sector index (int; 0 is not stored). The actor's name string (`RaceSector3`
  or `Sector3`) always agrees with the index.
- `Trigger` (BoxComponent): property 125 RelativeLocation (the actor's world
  location, as it is the root), 0 BoxExtent, 126 RelativeRotation,
  127 RelativeScale3D. Only the location is used: in Wales most split boxes
  have no rotation or size of their own (class defaults), so the box is not a
  reliable plane; in Alsace the boxes are large and placed up to 33 m to the
  side of the road. Where a box does have its own size and yaw, the spline's
  crossing of its mid-plane is within 3 m of the projection.
- The variant of a cell: the same cell holds the variant's
  `PacenoteSetupActor`, which imports its `DT_Pacenote<...>` table; the
  table's name (location, Full/Cut/Short N, Forward/Reverse) matches one
  `game_variant_id` (Saverne's tables say Cut where the ids say Short).
  Livigno has no pace notes: its lines are matched to the circuit spline they
  lie on (the 918 m `Rim1`, the stage table's "Main Circuit") in the order
  they are met.
- `CenterSpline` (/Script/dmphysics DMSplineComponent): an
  FInterpCurveVector of 279-872 points about 20 m apart (Livigno 22-61) (UE units, cm;
  UE x, y, z = ACR x, z, y × 100), Hermite segments with arrive and leave
  tangents. Its length is integrated at 64 samples per segment, and a line's
  distance is its location's closest point on it. The other splines of the
  same actor (`IdealSpline`, `LeftBorderSpline`, `RightBorderSpline`,
  `ContourSpline`) were not needed.

The game logic in `acr.exe` names a `SplineSortedSectors` / `NextSector`
pair on the race game mode and a `PlayerRaceSectorsTracker` with
`SectorsRecords`; with the clock evidence above this reads as: sectors are
sorted by their spline distance and passed when the car's distance reaches
the next one, not by box overlap.

Checks run on every variant (`scripts/acr-sectors.py` prints them): the
number of lines is the table's number of sectors + 1 (circuit: equal); every
sector is within 0.1 km of the table's length except Hafren North's four
variants named above (0.2-0.5 km), La Bollène's first forward sector (0.2)
and three others by 0.11-0.14 km;
every finish is 42-320 m before the table's last note (the stop control);
every start is 11-56 m before the first note except the three cuts above;
every line is within 33 m of the spline (Munster's boxes sit beside the
road; everywhere else within 5.2 m).

## 3. The lines, per variant

Distances in m along the direction's centre spline (the shared memory's
`lap_distance`). "Estimate − real" is `oversteer.stage_tables.sector_bounds`
today (start, each split, finish) minus the line; empty where it places no
sectors.

| Variant (`game_variant_id`) | Track | Start | Splits | Finish | Sectors (km) | Table `sectors_km` | Estimate − real, per line (m) |
|---|---|---:|---|---:|---|---|---|
| AlsaceS2MunsterFullReverse | Alsace Descente | 205.6 | 2138.8, 4758.9, 6770.4, 8558.9 | 10522.4 | 1.93 2.62 2.01 1.79 1.96 | [1.9, 2.6, 2.0, 1.8, 2.0] | -21 -54 -74 -86 -74 -38 |
| AlsaceS2MunsterFullForward | Alsace Montée | 193.7 | 2368.0, 4157.0, 6168.3, 8789.0 | 10476.5 | 2.17 1.79 2.01 2.62 1.69 | [2.2, 1.8, 2.0, 2.6, 1.7] | -18 +8 +18 +7 -14 -1 |
| AlsaceS2MunsterShort1Reverse | Alsace Forêt | 3779.3 | 6770.4, 8558.9 | 10522.4 | 2.99 1.79 1.96 | [3.0, 1.8, 2.0] | -3 +6 +18 +54 |
| AlsaceS2MunsterShort1Forward | Alsace Luttenbach | 193.7 | 2368.0, 4157.0 | 7147.9 | 2.17 1.79 2.99 | [2.2, 1.8, 3.0] | -19 +6 +17 +26 |
| AlsaceS2MunsterShort2Reverse | Alsace Petit Ballon | 205.6 | 2138.8, 4758.9 | 5779.6 | 1.93 2.62 1.02 | [1.9, 2.6, 1.0] | -21 -54 -74 -95 |
| AlsaceS2MunsterShort2Forward | Alsace Sommet | 5147.7 | 6168.3, 8788.5 | 10476.5 | 1.02 2.62 1.69 | [1.0, 2.6, 1.7] | -7 -27 -47 -35 |
| AlsaceS4SaverneFullForward | Alsace Forêt | 112.5 | 2780.4, 7342.6 | 9189.4 | 2.67 4.56 1.85 | [2.7, 4.6, 1.8] | -15 +17 +55 +8 |
| AlsaceS4SaverneFullReverse | Alsace Steigenbach | 263.5 | 2170.6, 6733.3 | 9250.9 | 1.91 4.56 2.52 | [1.9, 4.6, 2.5] | +1 -6 +31 +13 |
| AlsaceS4SaverneShort1Forward | Alsace Obersteigen | 4610.8 | 7342.6 | 9189.5 | 2.73 1.85 | [2.7, 1.8] | +1 -31 -78 |
| AlsaceS4SaverneShort1Reverse | Alsace La Mossig | 263.9 | 2170.6 | 4902.9 | 1.91 2.73 | [1.8, 2.7] | +0 -106 -139 |
| WelesS4HafrenSouthFullForward | Wales Afon Bidno | 242.9 | 1985.1, 3655.2 | 5294.1 | 1.74 1.67 1.64 | [1.8, 1.7, 1.6] | -5 +35 +48 -7 |
| WelesS4HafrenSouthFullReverse | Wales Severn | 220.8 | 1944.5, 3614.7 | 5277.5 | 1.72 1.67 1.66 | [1.7, 1.7, 1.7] | (no estimate) |
| WelesS3HafrenNorthFullForward | Wales Cwmbiga | 157.9 | 2995.4, 6408.5, 9584.9 | 11769.0 | 2.84 3.41 3.18 2.18 | [2.5, 3.4, 3.2, 2.2] | -16 -354 -367 -343 -328 |
| WelesS3HafrenNorthFullReverse | Wales Afon Biga | 309.4 | 2492.9, 5669.2, 9082.2 | 11878.4 | 2.18 3.18 3.41 2.80 | [2.2, 3.2, 3.4, 2.3] | -14 +2 +26 +13 -484 |
| WelesS3HafrenNorthCut1Forward | Wales Hafren Forest | 450.1 | 2193.5, 4227.4 | 6525.5 | 1.74 2.03 2.30 | [1.7, 2.0, 2.3] | -309 -352 -386 -384 |
| WelesS3HafrenNorthCut1Reverse | Wales Fedw Fain | 5552.3 | 7850.2, 9884.2 | 11878.2 | 2.30 2.03 1.99 | [2.1, 1.7, 1.6] | (no estimate) |
| WelesS3HafrenNorthCut2Forward | Wales Banc Gwyn | 6363.9 | 8440.4, 10166.9 | 11769.1 | 2.08 1.73 1.60 | [2.3, 2.0, 1.7] | (no estimate) |
| WelesS3HafrenNorthCut2Reverse | Wales Ospreys | 308.9 | 1910.9, 3637.4 | 5728.3 | 1.60 1.73 2.09 | [1.6, 1.7, 2.1] | -14 -16 -42 -33 |
| LivignoTestTrack01FullForward | Livigno Circuit Main Circuit | lap line 113.6 | 383.0, 672.1 | (lap) | 0.27 0.29 0.36 | [0.3, 0.3, 0.4] | (circuit) |
| LivignoTestTrack01FullReverse | Livigno Circuit Main Reverse | lap line 804.4 | 246.1, 535.0 (lap distance, wraps at 918.0) | (lap) | 0.36 0.29 0.27 | [0.4, 0.3, 0.3] | (circuit) |
| MonteCarloS1BolleneFullForward | Monte Carlo La Bollène | 173.7 | 1778.7, 4593.5, 9504.5, 12014.5, 15152.4 | 18210.0 | 1.60 2.81 4.91 2.51 3.14 3.06 | [1.6, 2.8, 4.9, 2.5, 3.1, 3.2] | -14 -20 -34 -45 -55 -93 +49 |
| MonteCarloS1BolleneFullReverse | Monte Carlo Peïra Cava | 351.7 | 3524.4, 6662.4, 9172.3, 14082.9, 16897.6 | 18503.6 | 3.17 3.14 2.51 4.91 2.81 1.61 | [3.2, 3.1, 2.5, 4.9, 2.8, 1.6] | -12 +15 -23 -33 -44 -58 -64 |
| MonteCarloS1BolleneCut1Forward | Monte Carlo Turini Montée | 173.7 | 1778.7, 4593.5, 9504.5 | 12014.5 | 1.60 2.81 4.91 2.51 | [1.8, 2.8, 4.9, 2.5] | -14 +180 +166 +155 +145 |
| MonteCarloS1BolleneCut1Reverse | Monte Carlo Turini Descente | 6662.4 | 9172.3, 14082.9, 16897.6 | 18503.6 | 2.51 4.91 2.81 1.61 | [2.5, 4.9, 2.8, 1.6] | -14 -24 -35 -50 -56 |
| MonteCarloS1BolleneCut2Forward | Monte Carlo Turini - Peïra Cava | 12014.5 | 15152.4 | 18210.1 | 3.14 3.06 | [3.1, 3.2] | -23 -61 +81 |
| MonteCarloS1BolleneCut2Reverse | Monte Carlo Peïra Cava - Turini | 351.7 | 3524.4 | 6662.4 | 3.17 3.14 | [3.2, 3.1] | -12 +15 -23 |
| MonteCarloS1BolleneCut3Forward | Monte Carlo Pra d'Alart | 8634.8 | 11036.8, 12402.1 | 13858.9 | 2.40 1.37 1.46 | [2.4, 1.4, 1.5] | -18 -20 +15 +58 |
| MonteCarloS1BolleneCut3Reverse | Monte Carlo Sommet de Turini | 4753.6 | 6274.8, 7639.9 | 10009.5 | 1.52 1.37 2.37 | [1.5, 1.4, 2.4] | -22 -43 -8 +23 |
| MonteCarloS2SisteronFullForward | Monte Carlo Sisteron - St. Genie | 353.5 | 3496.4, 6486.7, 10235.2 | 13456.0 | 3.14 2.99 3.75 3.22 | [3.1, 3.0, 3.7, 3.2] | +14 -29 -20 -68 -89 |
| MonteCarloS2SisteronFullReverse | Monte Carlo St. Geniez - Sistero | 431.7 | 3634.2, 7333.0, 10492.4 | 13567.7 | 3.20 3.70 3.16 3.08 | [3.2, 3.7, 3.1, 3.2] | +21 +18 +20 -40 +85 |
| MonteCarloS2SisteronCut1Forward | Monte Carlo Sisteron - Mézien | 353.2 | 2146.4, 4847.6 | 7587.8 | 1.79 2.70 2.74 | [1.8, 2.7, 2.7] | +14 +21 +19 -21 |
| MonteCarloS2SisteronCut1Reverse | Monte Carlo Mézien - Sisteron | 6321.1 | 9170.1, 11739.6 | 13567.7 | 2.85 2.57 1.83 | [2.8, 2.6, 1.8] | -1 -50 -20 -48 |
| MonteCarloS2SisteronCut2Forward | Monte Carlo Mézien - St. Geniez | 8237.5 | 9941.8, 12101.0 | 13456.1 | 1.70 2.16 1.36 | [1.7, 2.2, 1.4] | -4 -9 +32 +77 |
| MonteCarloS2SisteronCut2Reverse | Monte Carlo St. Geniez - Mézien | 431.4 | 1828.1, 4009.9 | 5673.1 | 1.40 2.18 1.66 | [1.4, 2.2, 1.7] | +21 +25 +43 +80 |
| GreeceS4LoutrakiFullForward | Greece Loutraki - Aghii Theodori | 228.0 | 3150.6, 6635.9 | 10431.5 | 2.92 3.49 3.80 | [2.9, 3.5, 3.8] | +8 -15 +0 +4 |
| GreeceS4LoutrakiFullReverse | Greece Aghii Theodori - Loutraki | 300.9 | 4138.2, 7623.7 | 10463.0 | 3.84 3.49 2.84 | [3.8, 3.5, 2.8] | -24 -61 -47 -86 |
| GreeceS4LoutrakiCut1Forward | Greece New Loutraki | 229.4 | 2710.2 | 5157.3 | 2.48 2.45 | [2.5, 2.4] | +7 +26 -21 |
| GreeceS4LoutrakiCut1Reverse | Greece New Loutraki Reverse | 5538.5 | 8064.1 | 10462.9 | 2.53 2.40 | [2.5, 2.4] | -2 -28 -26 |
| GreeceS4LoutrakiCut2Forward | Greece Aghii Theodori | 4652.3 | 7627.6 | 10431.7 | 2.98 2.80 | [3.0, 2.8] | -10 +14 +10 |
| GreeceS4LoutrakiCut2Reverse | Greece Aghii Theodori Reverse | 363.1 | 3146.5 | 6059.1 | 2.78 2.91 | [2.8, 2.9] | -86 -70 -82 |
| GreeceS3ElatiaFullForward | Greece Elatia - Zeli | 214.9 | 4041.1, 7418.3, 10270.2 | 11860.3 | 3.83 3.38 2.85 1.59 | [3.8, 3.4, 2.9, 1.6] | -13 -40 -17 +31 +41 |
| GreeceS3ElatiaFullReverse | Greece Zeli - Elatia | 243.7 | 1773.5, 4915.1, 7756.1 | 11900.5 | 1.53 3.14 2.84 4.14 | [1.5, 3.1, 2.8, 4.1] | -14 -43 -85 -126 -170 |
| GreeceS3ElatiaCut1Forward | Greece Elatia | 215.3 | 4041.2 | 6530.9 | 3.83 2.49 | [3.8, 2.5] | (no estimate) |
| GreeceS3ElatiaCut1Reverse | Greece Elatia Reverse | 5712.1 | 7739.1 | 11900.6 | 2.03 4.16 | [2.0, 4.2] | -10 -37 +2 |
| GreeceS3ElatiaCut2Forward | Greece Zeli | 6334.0 | 10266.6 | 11860.3 | 3.93 1.59 | [3.9, 1.6] | +4 -29 -22 |
| GreeceS3ElatiaCut2Reverse | Greece Zeli Reverse | 358.0 | 1751.2 | 5883.0 | 1.39 4.13 | [1.4, 4.1] | -128 -121 -153 |

## 4. Confidence

- **High** for the 44 point-to-point stages: positions straight from the
  level data; the spline matches the shared memory's distance (length to
  0.02 m, car projections to 0.1 m) and the finish line matches the game
  clock to about 1 m on the two stages with v4 captures (Sommet de Munster,
  Aghii Theodori - Loutraki). The splits themselves cannot be checked against
  the game, as the game publishes no split; they come from the same actors,
  the same parse and the same projection as the finish and start lines that
  were checked.
- How a split registers: by the car's spline distance (the finish evidence),
  i.e. at the line's projected distance; if the game instead used the
  car's front or the box overlap it would move a split by at most a few
  metres (0.1-0.2 s), the size of the start-line offset above.
- **Medium** for Livigno: the lines and lap length are certain, but how the
  game's distance runs on the circuit (where 0 is, whether it wraps) has no
  capture yet.

## 5. Proposed `acr.json` field and use

Per stage, `sector_lines_m`: the lines along the road spline, start first,
finish last (N+1 values for N sectors), from `scripts/acr-sectors.py --json`
(its `gates_m`), e.g. Afon Bidno:

```json
"sector_lines_m": [242.9, 1985.1, 3655.2, 5294.1],
"sector_lines_source": "game level data (RaceSector actors on the centre spline), scripts/acr-sectors.py, build 2026-09-12"
```

For Livigno's circuits the same list holds the lap line then the splits (no
finish entry), with `"circuit": true`.

Then, in the app (for a later change, reviewed as usual):
- `sector_bounds()`: consecutive pairs of `sector_lines_m`, confidence high,
  ahead of the scaled estimate (kept for stages without lines).
- Split time = the stage clock (bridge v4 `stage_time`) interpolated where
  `lap_distance` crosses a line; with an older capture, the run time at the
  crossing.
- `start_line()` / `start_m`: the first line; a run whose distance at the
  clock start is a few metres short of it (2.6-4.7 m measured) is at the line.
  This fixes the three cuts whose start lines sit after their table's first
  note.
- `finish_m`: the last line (Afon Bidno 5294.1, not 5287.4); `acr-finish.py`
  stays useful as a check.
- `sectors_km` can stay as the game's published figure for display.

## 6. Making it automatic (track A)

What a reader needs from a user's install: ~18 world-partition cell packages
(of ~3,350) out of `pakchunk0`, all in Oodle-compressed IoStore containers.

- **Today's script** (`scripts/acr-sectors.py`): needs `retoc` (one static
  binary, MIT) which downloads Oodle's Linux library from a GitHub mirror of
  Epic's SDK on first use; Python parses the rest. 12 s for all nine maps;
  peak disk ~1 GB in a work directory emptied after each map. Robust to game
  patches as long as the classes keep their names (`RaceSector`,
  `SplinesActor`, `CenterSpline`, `PacenoteSetupActor`) and property order; a
  layout change makes it fail loudly, not silently.
- **Lightest in-app reader** (estimate, not built): read the `.utoc` table of
  contents in Python (header, chunk ids, offsets, compression blocks,
  directory index: a few hundred lines), decompress only the first block of
  each cell under `Levels/<Map>/_Generated_` to read its Zen package header,
  keep the cells importing `/Script/acr.RaceSector` or
  `/Script/dmphysics.SplinesActor`, and decompress only the blocks under the
  wanted exports (a few MB in all). The parse here works on those exports
  unchanged. The one hard dependency is Oodle: the game links it statically
  into `acr.exe` (no DLL to borrow), Epic's library may not be redistributed,
  and a pure-Python decoder for ~3,350 blocks would be slow. So the app would
  still need the user to fetch Oodle (as retoc does) or ship a decoder.
- **Recommended**: ship the derived numbers in `acr.json` (as for
  `sectors_km` and the pace-note fields: derived facts, no game data) and
  re-run `scripts/acr-sectors.py` when a game update adds or moves stages
  (the stage table already needs that step for new stages). It is one
  command, 12 s. Optionally later: a first-run helper that runs the same
  script when the user has `retoc` and the stage table has no lines for a
  stage the shared memory reports, so new stages work before an Oversteer
  release.

## 7. Reading splits live from the game (track B)

Not needed, since Oversteer can time splits from distance and clock (§5).
For the record, from a static look at `acr.exe` (strings and the UE script
object table only; the game was not run or attached to):

- The game reflects (UE `UClass`/`FProperty`) a `PlayerRaceSectorsTracker`
  (with `SectorsRecords` of `SectorRecord`, `OnSectorRecordAdded`,
  `OnRep_SectorsRecords`, `GetSectorsRecords`), `RaceSectorsPlayerData`
  (`SectorsPlayerData`, `SectorsTimeMs`), the race mode's
  `SplineSortedSectors`/`NextSector`, `MulticastCallSectorPassedEvent`, and
  HUD widgets (`AcrRaceSectorsProgressionWidget`, `CurrentInProgressSector`,
  `SectorTimesMsMap`).
- A bridge-side reader inside the Wine prefix could find them by name:
  signature-scan `acr.exe` for `GUObjectArray` and the `FNamePool`, walk the
  objects for the live `PlayerRaceSectorsTracker` instance (not the
  `Default__` one), resolve `SectorsRecords`' offset and `SectorRecord`'s
  members through their `FProperty` chains, and poll the `TArray` with
  `ReadProcessMemory` (read-only, `PROCESS_VM_READ` only). The field names
  survive game patches; the engine-internal layouts (`FUObjectItem`,
  `FField`, `FProperty::Offset_Internal`) and the scan patterns are UE 5.6's
  and would need checking per engine upgrade.
- Cost: several hundred lines of C in the bridge, testing on marth's running
  game, and a documented read-only memory read of the game process (ACR has
  no anti-cheat, `docs/anti-cheat.md`), for the same split times §5 gets
  from data already captured. Not recommended unless the game is found to
  time splits in a way the distance does not reproduce.

## 8. Reproducing

```sh
# once: retoc (or put a release binary on PATH)
git clone https://github.com/trumank/retoc ~/.cache/oversteer-re/retoc
cd ~/.cache/oversteer-re/retoc && cargo build --release -p retoc_cli   # Rust >= 1.88 (edition 2024)
# every game update
scripts/acr-sectors.py --retoc ~/.cache/oversteer-re/retoc/target/release/retoc --json /path/out.json
```

The script prints each variant's lines, its sectors against the table's,
the current estimate's error, the pace-note distances, and flags (`!`) any
check that fails. It reads the game and `data/telemetry/stages/acr.json`
only, and writes only `--json` and, with `--write`, the `sector_lines_*` fields of `acr.json`
(idempotent; the rest of the table is as it was).
