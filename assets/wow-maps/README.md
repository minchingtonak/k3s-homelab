# wow-maps — continent map tiles for the Grafana player-position panel

XYZ tiles of the three WotLK continent maps, served to Grafana's Geomap panel
(`xyz` basemap) straight from this repo via `raw.githubusercontent.com`. The
`Player positions` panel on the wow-realm dashboard plots online characters on
them. Nothing here is deployed to the cluster; it is a static asset directory.

## Where the numbers come from

Everything derives from one projection, so tiles and markers cannot disagree:

1. World yards → continent-image pixels: ported from
   [azerothcore/playermap](https://github.com/azerothcore/playermap)
   `index.php:get_player_position` (per-continent scale factors, axis flips,
   per-map offsets, and the two map-530 special cases that plot Eversong/
   Ghostlands and Azuremyst/Bloodmyst onto the Azeroth image).
2. Image pixels → fake lon/lat: linear. Each continent image occupies a
   lon/lat box of `[0, 20·width/height] × [-10, 10]`. Azeroth and Northrend
   are 966×732 (lon span 26.393442623); Outland is 966×695 (27.798561151).
3. Fake lon/lat → tiles: rendered in Web Mercator, i.e. each tile pixel row
   is un-mercator'd back to lat and sampled linearly from the source image.
   Markers (which Grafana places by plain lon/lat) and tiles therefore share
   one geometry by construction.

The same math lives in two other places that must stay in sync with this file:

- `k8s/apps/wow/sql-exporter.yaml` — `wow_character_position_lon` /
  `wow_character_position_lat` compute the fake lon/lat in SQL.
- `generate_tiles.py` here — regenerates the tiles; its self-test asserts the
  image→lon/lat→Mercator→lon/lat→image round trip is exact for every landmark.

## Ground truth

`game_tele` rows from the world DB (id, x, y, z, orientation, map, name) —
each must land on its city's pixel on the source image:

| City          | yards (x, y, map)        | image px   | image     |
| ------------- | ------------------------ | ---------- | --------- |
| Stormwind     | (-8833.4, 628.6, 0)      | (736, 513) | azeroth   |
| Ironforge     | (-4918.9, -940.4, 0)     | (776, 415) | azeroth   |
| Undercity     | (1584.1, 240.3, 0)       | (746, 251) | azeroth   |
| Booty Bay     | (-14297.2, 531.0, 0)     | (739, 650) | azeroth   |
| Orgrimmar     | (1629.9, -4373.6, 1)     | (304, 357) | azeroth   |
| Thunder Bluff | (-1277.4, 124.8, 1)      | (191, 430) | azeroth   |
| Darnassus     | (9949.6, 2284.2, 1)      | (137, 148) | azeroth   |
| Gadgetzan     | (-7177.2, -3785.3, 1)    | (289, 578) | azeroth   |
| Shattrath     | (-1838.2, 5301.8, 530)   | (476, 482) | outland   |
| Silvermoon    | (9487.7, -7279.2, 530)   | (881, 106) | azeroth   |
| Exodar        | (-3965.7, -11653.6, 530) | (46, 261)  | azeroth   |
| Dalaran       | (5808.0, 588.5, 571)     | (476, 351) | northrend |

Silvermoon and Exodar are on map 530 but plot on the Azeroth image — that is
the map-530 special case in the projection, not an error. Source images:
playermap's `img/map/*.jpg` — 966×732 (azeroth, northrend), 966×695 (outland).

## Regenerating

```bash
python3 generate_tiles.py --selftest-only   # round-trip check only
python3 generate_tiles.py --out tiles --zooms 4,5,6,7
```

Pillow is the only dependency. Regeneration takes a few minutes (pure-Python
pixel loop; 330 tiles across zooms 4–7). The source jpgs are committed beside
the tiles so regeneration never depends on playermap still hosting them.

## Attribution

Map images from [azerothcore/playermap](https://github.com/azerothcore/playermap)
(original author Dmitry Koterov, maintained by Helias). Projection constants
reproduced from that project's `index.php`.
