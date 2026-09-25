# Targets still to hand-pick (cities 351–700)

`snap_to_buildings.py` found no OpenStreetMap building footprint within 400 m of the
PDF anchor for these cities, so their target is still the PDF's downtown point.
Skip them when regenerating/re-rendering from the snapped CSV until a building
has been chosen by hand.

To pick one: add a row to `data/cities_351_700/handpicked_anchors.csv`
(`n,city,lat,lon,anchor,snap,note`; see *Hand-picking* in the README), rerun
`snap_to_buildings.py data/cities_351_700/cities.csv --out data/cities_351_700/snapped.csv`,
then `batch_generate.py`. Don't edit `snapped.csv` by hand: the next snapping
run overwrites it.

| # | City | Country | Current anchor (lat, lon) |
|---|---|---|---|
| 576 | Boksburg | South Africa | -26.212, 28.2596 |
| 585 | Krugersdorp | South Africa | -26.0858, 27.7752 |
| 586 | Klerksdorp | South Africa | -26.8521, 26.6667 |
| 589 | Sasolburg | South Africa | -26.8136, 27.816 |
| 591 | Empangeni | South Africa | -28.7616, 31.8933 |
| 601 | Ariana | Tunisia | 36.8665, 10.1647 |

Skip list for `batch_generate.py` / `render_all.py --only`: Boksburg, Krugersdorp, Klerksdorp, Sasolburg, Empangeni, Ariana
