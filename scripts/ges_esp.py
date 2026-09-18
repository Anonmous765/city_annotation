"""
ges_esp.py — Generate Google Earth Studio project (.esp) files for orbital
satellite/ground view pairs, parameterised by geographic coordinates.

Reverse-engineered from a hand-authored Earth Studio "quickstart" project.
Reproduces the exact schema (modelVersion 18) so files import cleanly via
drag-and-drop or File > Import.

Coordinate encoding used by Earth Studio
----------------------------------------
All attribute values are normalised to [0, 1]:
    longitude : rel = (lon + 180) / 360
    latitude  : rel = (lat + 90) / 180
    altitude  : rel = (alt_m - ALT_MIN) / (ALT_MAX - ALT_MIN)
                with ALT_MIN = -500, ALT_MAX = 65_117_481 (metres)
    worldTime : rel = (t_ms - min) / (max - min), where [min, max] is an
                arbitrary window the author sets; we use t +/- 24h and rel=0.5.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

# --- Earth Studio constants ------------------------------------------------
ALT_MIN = -500
ALT_MAX = 65_117_481
ALT_SPAN = ALT_MAX - ALT_MIN

M_PER_DEG_LAT = 111_320.0  # spherical approximation; <0.3% error, well below
                           # Earth Studio's own tile-alignment tolerance

LINEAR = {"x": 0, "y": 0, "type": "linear"}
AUTO_IN = {"x": -0.066, "y": 0, "influence": 0.5, "type": "auto"}
AUTO_OUT = {"x": 0.066, "y": 0, "influence": 0.5, "type": "auto"}


def lon_to_rel(lon: float) -> float:
    return (lon + 180.0) / 360.0


def lat_to_rel(lat: float) -> float:
    return (lat + 90.0) / 180.0


def alt_to_rel(alt_m: float) -> float:
    return (alt_m - ALT_MIN) / ALT_SPAN


def _kf(t, v, tin=None, tout=None):
    k = {"time": t, "value": v}
    if tin is not None:
        k["transitionIn"] = dict(tin)
    if tout is not None:
        k["transitionOut"] = dict(tout)
    return k


# --- View specification ----------------------------------------------------

@dataclass
class ViewSpec:
    """One camera configuration orbiting a fixed point of interest."""
    name: str
    orbit_radius_m: float      # horizontal distance from POI
    height_above_poi_m: float  # vertical distance above POI

    @property
    def pitch_deg(self) -> float:
        """Camera depression angle below horizontal (0 = horizon, 90 = nadir)."""
        return math.degrees(math.atan2(self.height_above_poi_m, self.orbit_radius_m))

    @property
    def slant_range_m(self) -> float:
        return math.hypot(self.orbit_radius_m, self.height_above_poi_m)


# Defaults are the modal values across the 300-city reference dataset
# (reference/city_satellite/): satellite radius 688 m appears in 91/300 projects and
# height ~1190 m in 100/300 (the dataset README prescribes "688 m, ~1200 m");
# ground_truth radius 624 m appears in 133/300 and height ~312 m in 124/300.
# Earth Studio's radius slider steps in 62.5 m increments.
SATELLITE = ViewSpec("satellite", orbit_radius_m=688.0, height_above_poi_m=1190.0)
GROUND = ViewSpec("ground_truth", orbit_radius_m=624.0, height_above_poi_m=312.0)

# Earth Studio's Orbit quickstart sets the target altitude to its own 3D
# surface, which sits above bare-earth DEMs: across the 300 reference cities
# altitudePOI - Mapzen/SRTM elevation has median +27 m (p10 +17, p90 +31).
POI_ABOVE_DEM_M = 27.0


@dataclass
class Site:
    """A point of interest to orbit."""
    city: str
    lat: float
    lon: float
    poi_alt_m: float           # ABSOLUTE altitude MSL of the orbit target (DEM + POI_ABOVE_DEM_M)
    world_time_utc: datetime | None = None
    meta: dict = field(default_factory=dict)


# --- Orbit geometry --------------------------------------------------------

def orbit_keyframes(site: Site, view: ViewSpec):
    """
    Five keyframes describing one full 360-degree orbit: N -> W -> S -> E -> N.
    Matches the reference project's phase and transition scheme exactly.
    """
    d_lat = view.orbit_radius_m / M_PER_DEG_LAT
    d_lon = view.orbit_radius_m / (M_PER_DEG_LAT * math.cos(math.radians(site.lat)))

    lat_seq = [site.lat + d_lat, site.lat, site.lat - d_lat, site.lat, site.lat + d_lat]
    lon_seq = [site.lon, site.lon - d_lon, site.lon, site.lon + d_lon, site.lon]
    times = [0, 0.25, 0.5, 0.75, 1]

    # Longitude is at an extremum on the odd indices -> smooth (auto) there,
    # zero-crossing on the even indices -> linear. Latitude is phase-shifted.
    lon_kfs, lat_kfs = [], []
    for i, (t, lo, la) in enumerate(zip(times, lon_seq, lat_seq)):
        lon_smooth = i % 2 == 1
        lon_kfs.append(_kf(t, lon_to_rel(lo),
                           AUTO_IN if lon_smooth else LINEAR,
                           AUTO_OUT if lon_smooth else LINEAR))
        lat_kfs.append(_kf(t, lat_to_rel(la),
                           LINEAR if lon_smooth else AUTO_IN,
                           LINEAR if lon_smooth else AUTO_OUT))

    # Earth Studio stores the camera altitude as a whole number of metres
    # (true for all 600 reference projects); mirror that.
    cam_alt_rel = alt_to_rel(round(site.poi_alt_m + view.height_above_poi_m))
    alt_kfs = [{"time": t, "value": cam_alt_rel} for t in times]

    return lon_kfs, lat_kfs, alt_kfs, cam_alt_rel


def _world_time_block(dt: datetime | None):
    if dt is None:
        # Fall back to a fixed, reproducible instant rather than "now" so the
        # dataset is deterministic across generation runs.
        dt = datetime(2026, 6, 21, 10, 0, 0, tzinfo=timezone.utc)
    t_ms = int(dt.timestamp() * 1000)
    return {
        "maxValueRange": t_ms + 86_400_000,
        "minValueRange": t_ms - 86_400_000,
        "relative": 0.5,
    }


# --- Project assembly ------------------------------------------------------

def build_esp(site: Site, view: ViewSpec, *, width=2048, height=2048,
              fps=30, n_frames=60, project_name=None) -> dict:
    lon_kfs, lat_kfs, alt_kfs, cam_alt_rel = orbit_keyframes(site, view)

    return {
        "type": "quickstart",
        "modelVersion": 18,
        "settings": {
            "name": project_name or f"{site.city}_{view.name}",
            "frameRate": fps,
            "dimensions": {"width": width, "height": height},
            "duration": n_frames,
            "timeFormat": "frames",
        },
        "scenes": [{
            "animationModel": {"roving": False, "logarithmic": False,
                               "groupedPosition": True},
            "duration": n_frames,
            "attributes": [
                {
                    "type": "cameraGroup", "inTimeline": True,
                    "attributes": [
                        {
                            "type": "cameraPositionGroup", "inTimeline": True,
                            "attributes": [{
                                "type": "position", "inTimeline": True,
                                "attributes": [
                                    {"type": "longitude",
                                     "value": {"relative": lon_kfs[0]["value"]},
                                     "keyframes": lon_kfs, "inTimeline": True},
                                    {"type": "latitude",
                                     "value": {"relative": lat_kfs[0]["value"]},
                                     "keyframes": lat_kfs, "inTimeline": True},
                                    {"type": "altitude",
                                     "value": {"maxValueRange": ALT_MAX,
                                               "minValueRange": ALT_MIN,
                                               "relative": cam_alt_rel,
                                               "logarithmic": False},
                                     "keyframes": alt_kfs, "inTimeline": True},
                                ],
                            }],
                        },
                        {
                            "type": "cameraTargetEffect", "inTimeline": True,
                            "attributes": [
                                {"type": "enabled", "value": {"relative": 1},
                                 "inTimeline": True},
                                {"type": "poi", "inTimeline": True, "attributes": [
                                    {"type": "longitudePOI",
                                     "value": {"relative": lon_to_rel(site.lon)},
                                     "keyframes": [{"time": 0, "value": lon_to_rel(site.lon)}],
                                     "inTimeline": True},
                                    {"type": "latitudePOI",
                                     "value": {"relative": lat_to_rel(site.lat)},
                                     "keyframes": [{"time": 0, "value": lat_to_rel(site.lat)}],
                                     "inTimeline": True},
                                    {"type": "altitudePOI",
                                     "value": {"maxValueRange": ALT_MAX,
                                               "minValueRange": ALT_MIN,
                                               "relative": alt_to_rel(site.poi_alt_m),
                                               "logarithmic": False},
                                     "keyframes": [{"time": 0,
                                                    "value": alt_to_rel(site.poi_alt_m)}],
                                     "inTimeline": True},
                                ]},
                                {"type": "influence", "value": {}, "inTimeline": True},
                            ],
                        },
                        {
                            "type": "cameraRotationGroup", "inTimeline": True,
                            "attributes": [
                                {"type": "rotationX", "value": {}, "inTimeline": True},
                                {"type": "rotationY", "value": {}, "inTimeline": True},
                                {"type": "rotationZ", "value": {}},
                            ],
                        },
                        {
                            "type": "cameraLensGroup",
                            "attributes": [
                                {"type": "fov", "value": {}},
                                {"type": "exposure", "value": {}},
                                {"type": "aperture", "value": {}},
                                {"type": "minFocusLength", "value": {}},
                            ],
                        },
                    ],
                },
                {
                    "type": "environmentGroup",
                    "attributes": [
                        {"type": "sunGroup", "attributes": [
                            {"type": "sunVisibility", "value": {}},
                            {"type": "worldTime",
                             "value": _world_time_block(site.world_time_utc)},
                        ]},
                        {"type": "cloudGroup", "attributes": [
                            {"type": "cloudVisibility", "value": {}},
                            {"type": "cloudopacity", "value": {}},
                            {"type": "cloudheight", "value": {}},
                            {"type": "clouddate",
                             "value": {"minValueRange": 1775588040000,
                                       "relative": 0.0005041015535493332,
                                       "maxValueRange": 1783443600000}},
                        ]},
                        {"type": "starsPlanetsGroup", "attributes": [
                            {"type": "starsEnabled", "value": {}},
                        ]},
                        {"type": "seawaterGroup", "attributes": [
                            {"type": "seawater", "value": {}},
                            {"type": "influence", "value": {"relative": 1}},
                        ]},
                        {"type": "buildingsEnabled", "value": {}},
                    ],
                },
            ],
            "cameraExport": {"logarithmic": False, "modelVersion": 2},
        }],
        "has_started": True,
        "has_finished": True,
        "playbackManager": {"range": {"start": 0, "end": n_frames}},
    }


def camera_track(site: Site, view: ViewSpec, n_frames=60):
    """
    Approximate camera extrinsics for every rendered frame: the ideal circle
    the five keyframes describe. Earth Studio interpolates the keyframes with
    bezier "auto" tangents, so the real path wobbles by up to ~3.5% of the
    radius (Koblenz ground_truth: 744-770 m for a 750 m orbit) and lands up to
    ~20 m from this circle between keyframes. Treat the JSON 3D tracking file
    that Earth Studio writes at render time as the true per-frame camera pose;
    use this only as a sanity check / preview.
    """
    d_lat = view.orbit_radius_m / M_PER_DEG_LAT
    d_lon = view.orbit_radius_m / (M_PER_DEG_LAT * math.cos(math.radians(site.lat)))
    cam_alt = round(site.poi_alt_m + view.height_above_poi_m)

    frames = []
    for i in range(n_frames):
        theta = 2 * math.pi * i / n_frames          # 0 = due north, then westward
        lat = site.lat + d_lat * math.cos(theta)
        lon = site.lon - d_lon * math.sin(theta)
        heading = (math.degrees(theta) + 180.0) % 360.0   # camera looks back at POI
        frames.append({
            "frame": i,
            "lat": lat, "lon": lon, "alt_m": cam_alt,
            "heading_deg": heading,
            "pitch_deg": -view.pitch_deg,
            "slant_range_m": view.slant_range_m,
        })
    return frames


def write_esp(path, site: Site, view: ViewSpec, **kw):
    with open(path, "w") as f:
        json.dump(build_esp(site, view, **kw), f, separators=(",", ":"))
