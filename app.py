import json
import re
import requests
import streamlit as st
from typing import Optional, Tuple, Dict, Any, List
from streamlit_folium import st_folium
import folium
import base64
from pathlib import Path
import math

# -------------------------------
# Page config with logo as favicon
# -------------------------------
st.set_page_config(
    page_title="UK LPA & NCA Lookup",
    page_icon="wild_capital_uk_logo.png",
    layout="centered"
)

# -------------------------------
# Helper: embed logo inline
# -------------------------------
def inline_logo_b64(path: str) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    data = p.read_bytes()
    b64 = base64.b64encode(data).decode("utf-8")
    return f"data:image/png;base64,{b64}"

# -------------------------------
# Endpoints & utilities
# -------------------------------
POSTCODES_IO = "https://api.postcodes.io/postcodes/"
POSTCODES_IO_REVERSE = "https://api.postcodes.io/postcodes"
NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"

# Natural England — National Character Areas (polygon layer 0)
NCA_FEATURESERVER_LAYER = (
    "https://services.arcgis.com/JJzESW51TqeY9uat/arcgis/rest/services/"
    "National_Character_Areas_England/FeatureServer/0"
)

# ONS — Local Authority Districts (December 2024) Boundaries UK (BFC), polygon layer 0
LPA_FEATURESERVER_LAYER = (
    "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
    "Local_Authority_Districts_December_2024_Boundaries_UK_BFC/FeatureServer/0"
)

# EA OGC API – Features: WFD River Water Body Catchments Cycle 3 (Classification 2022)
EA_WB_CATCHMENTS_COLL = (
    "https://environment.data.gov.uk/geoservices/datasets/"
    "cd84a955-fd0a-4f5d-9bcb-b869c8906f9e/ogc/features/v1/"
    "collections/WFD_River_Water_Body_Catchments_Cycle_3_Classification_2022/items"
)

POSTCODE_RX = re.compile(r"^(GIR\s?0AA|[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2})$", flags=re.IGNORECASE)

def looks_like_uk_postcode(s: str) -> bool:
    return bool(POSTCODE_RX.match((s or "").strip()))

# -------------------------------
# Geo helpers: point-in-polygon for GeoJSON
# -------------------------------
def _point_in_ring(lon: float, lat: float, ring: List[List[float]]) -> bool:
    """
    Ray casting algorithm for a linear ring (lon/lat pairs). True if point is inside or on edge.
    """
    inside = False
    n = len(ring)
    if n < 3:
        return False
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        # Check if on segment (edge-inclusive)
        # colinear + within bounding box
        dx, dy = x2 - x1, y2 - y1
        if dx == dy == 0:
            continue
        if min(y1, y2) - 1e-12 <= lat <= max(y1, y2) + 1e-12:
            if abs(dx) > 1e-18:
                t = (lat - y1) / dy if abs(dy) > 1e-18 else None
                # horizontal segment handled below
            # Edge crossing test
        cond = ((y1 > lat) != (y2 > lat)) and (lon < (x2 - x1) * (lat - y1) / (y2 - y1 + 1e-18) + x1)
        if cond:
            inside = not inside
        # Edge-inclusive quick check
        # Handle point exactly on segment:
        if min(x1, x2) - 1e-12 <= lon <= max(x1, x2) + 1e-12 and min(y1, y2) - 1e-12 <= lat <= max(y1, y2) + 1e-12:
            # Check colinearity
            if abs((y2 - y1) * (lon - x1) - (x2 - x1) * (lat - y1)) <= 1e-12:
                return True
    return inside

def geojson_contains_point(geom: Dict[str, Any], lon: float, lat: float) -> bool:
    """
    Return True if (lon,lat) is inside a GeoJSON Polygon/MultiPolygon (holes respected; edge-inclusive).
    """
    if not geom or "type" not in geom:
        return False
    gtype = geom["type"]
    if gtype == "Polygon":
        rings = geom.get("coordinates") or []
        if not rings:
            return False
        # First ring is outer; others are holes
        if not _point_in_ring(lon, lat, rings[0]):
            return False
        for hole in rings[1:]:
            if _point_in_ring(lon, lat, hole):
                return False
        return True
    elif gtype == "MultiPolygon":
        for poly in geom.get("coordinates") or []:
            if not poly:
                continue
            if _point_in_ring(lon, lat, poly[0]):
                # Must not be in any holes
                hole_hit = any(_point_in_ring(lon, lat, hole) for hole in poly[1:])
                if not hole_hit:
                    return True
        return False
    else:
        return False

# -------------------------------
# Cached API wrappers
# -------------------------------
@st.cache_data(show_spinner=False, ttl=3600)
def get_postcode_info(postcode: str) -> Tuple[float, float, str, str]:
    pc = postcode.replace(" ", "").upper()
    r = requests.get(POSTCODES_IO + pc, timeout=10)
    if r.status_code != 200:
        try:
            err = r.json().get("error", "")
        except Exception:
            err = ""
        raise RuntimeError(f"Postcode error ({r.status_code}): {err or 'unknown error'}")
    data = r.json().get("result") or {}
    lat = data.get("latitude")
    lon = data.get("longitude")
    lpa = data.get("admin_district") or data.get("admin_county") or data.get("parish") or "Unknown"
    if lat is None or lon is None:
        raise RuntimeError("No coordinates returned for this postcode.")
    return float(lat), float(lon), lpa, data.get("postcode", pc)

@st.cache_data(show_spinner=False, ttl=3600)
def geocode_address_nominatim(address: str) -> Tuple[float, float]:
    params = {"q": address, "format": "jsonv2", "limit": 1, "addressdetails": 0}
    headers = {"User-Agent": "WildCapital-LPA-NCA/1.0 (contact: your.email@company.com)"}
    r = requests.get(NOMINATIM_SEARCH, params=params, headers=headers, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"Nominatim error HTTP {r.status_code}")
    js = r.json()
    if not js:
        raise RuntimeError("No geocoding result for that address.")
    lat = js[0].get("lat")
    lon = js[0].get("lon")
    if lat is None or lon is None:
        raise RuntimeError("Geocoder did not return coordinates.")
    return float(lat), float(lon)

@st.cache_data(show_spinner=False, ttl=3600)
def get_nearest_postcode_lpa_from_coords(lat: float, lon: float) -> Tuple[Optional[str], str]:
    params = {"lon": lon, "lat": lat, "limit": 1}
    r = requests.get(POSTCODES_IO_REVERSE, params=params, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"postcodes.io reverse error HTTP {r.status_code}")
    js = r.json()
    results = js.get("result") or []
    if not results:
        return None, "Unknown"
    res = results[0]
    lpa = res.get("admin_district") or res.get("admin_county") or res.get("parish") or "Unknown"
    return res.get("postcode"), lpa

def _arcgis_point_in_polygon(layer_url: str, lat: float, lon: float, out_fields: str) -> Dict[str, Any]:
    geometry_dict = {"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}
    params = {
        "f": "json",
        "where": "1=1",
        "geometry": json.dumps(geometry_dict),
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": out_fields or "*",
        "returnGeometry": "true",
        "outSR": 4326
    }
    r = requests.get(f"{layer_url}/query", params=params, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"ArcGIS error HTTP {r.status_code}")
    js = r.json()
    if "error" in js:
        msg = js["error"].get("message", "Unknown ArcGIS error")
        raise RuntimeError(f"ArcGIS service error: {msg}")
    feats = js.get("features") or []
    return feats[0] if feats else {}

def _arcgis_polygon_to_geojson(geom: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not geom or "rings" not in geom:
        return None
    rings = geom["rings"]
    if not rings:
        return None
    if len(rings) == 1:
        return {"type": "Polygon", "coordinates": [rings[0]]}
    else:
        return {"type": "MultiPolygon", "coordinates": [[ring] for ring in rings]}

@st.cache_data(show_spinner=False, ttl=3600)
def get_nca_feature(lat: float, lon: float) -> Dict[str, Any]:
    return _arcgis_point_in_polygon(NCA_FEATURESERVER_LAYER, lat, lon, "JCANAME,NCA_Name")

@st.cache_data(show_spinner=False, ttl=3600)
def get_lpa_feature(lat: float, lon: float) -> Dict[str, Any]:
    return _arcgis_point_in_polygon(LPA_FEATURESERVER_LAYER, lat, lon, "LAD24NM,LAD24CD")

def get_nca_name_from_feature(feat: Dict[str, Any]) -> Optional[str]:
    a = (feat or {}).get("attributes") or {}
    return a.get("NCA_Name") or a.get("JCANAME")

def get_lpa_name_from_feature(feat: Dict[str, Any]) -> Optional[str]:
    a = (feat or {}).get("attributes") or {}
    return a.get("LAD24NM") or a.get("NAME")

# -------------------------------
# WFD River Water Body Catchments (Cycle 3) with strict containment
# -------------------------------
def _ogc_point_cql(lat: float, lon: float) -> str:
    # OGC API uses lon lat order
    return f"INTERSECTS(shape,POINT({lon} {lat}))"

def _bbox_around_point(lat: float, lon: float, meters: float = 150.0) -> Tuple[float, float, float, float]:
    dlat = meters / 111320.0
    dlon = meters / (40075000.0 * math.cos(math.radians(lat)) / 360.0)
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)

def _fetch_water_body_catchment(lat: float, lon: float) -> Optional[Dict[str, Any]]:
    """
    Return the ONE water body catchment feature that truly contains (lon,lat).
    1) Try CQL2 INTERSECTS to narrow candidates, then verify with our own point-in-polygon.
    2) If CQL fails, do a small bbox query, then pick the first that contains the point.
    """
    # First: CQL2 filter (INTERSECTS) to get nearby candidates
    try:
        params = {
            "f": "application/geo+json",
            "limit": 20,
            "filter-lang": "cql2-text",
            "filter": _ogc_point_cql(lat, lon)
        }
        r = requests.get(EA_WB_CATCHMENTS_COLL, params=params, timeout=20)
        if r.status_code == 200 and "application/geo+json" in r.headers.get("Content-Type", ""):
            gj = r.json()
            for feat in gj.get("features") or []:
                geom = feat.get("geometry")
                if geom and geojson_contains_point(geom, lon, lat):
                    return feat
    except Exception:
        pass

    # Fallback: tiny bbox; select the one that CONTAINS the point
    try:
        minx, miny, maxx, maxy = _bbox_around_point(lat, lon, meters=250)
        params = {
            "f": "application/geo+json",
            "limit": 50,
            "bbox": f"{minx},{miny},{maxx},{maxy}"
        }
        r = requests.get(EA_WB_CATCHMENTS_COLL, params=params, timeout=20)
        if r.status_code == 200 and "application/geo+json" in r.headers.get("Content-Type", ""):
            gj = r.json()
            candidates = gj.get("features") or []
            for feat in candidates:
                geom = feat.get("geometry")
                if geom and geojson_contains_point(geom, lon, lat):
                    return feat
    except Exception:
        pass

    # None contain the point
    return None

@st.cache_data(show_spinner=False, ttl=1800)
def get_water_body_catchment(lat: float, lon: float) -> Optional[Dict[str, Any]]:
    return _fetch_water_body_catchment(lat, lon)

def feature_geom(feat: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not feat:
        return None
    return feat.get("geometry")

# -------------------------------
# Header with inline logo + title
# -------------------------------
logo_src = inline_logo_b64("wild_capital_uk_logo.png")
st.markdown(
    f"""
    <div style="text-align: center;">
        {'<img src="'+logo_src+'" width="180" style="margin-bottom:0.5em;">' if logo_src else ''}
        <h1 style="margin-top:0.2em;margin-bottom:0.25em;">UK LPA & NCA Lookup</h1>
        <p style="font-size:1.1em;color:#555;margin-top:0;">
            Enter a postcode or a free-text address. We’ll find the Local Planning Authority and National Character Area, and draw their boundaries.
        </p>
    </div>
    """,
    unsafe_allow_html=True
)

# -------------------------------
# CSS for result boxes
# -------------------------------
st.markdown("""
<style>
.result-grid {display: grid; grid-template-columns: 1fr; gap: 0.75rem;}
@media (min-width: 768px) {.result-grid {grid-template-columns: 1fr 1fr;}}
.result-box {
  padding: 0.9rem 1rem;
  border: 1px solid #e6e6e6;
  border-radius: 0.6rem;
  background: #f8f9fa;
  word-wrap: break-word;
  overflow-wrap: anywhere;
  white-space: normal;
}
.result-label {font-size: 0.9rem; color: #666; margin-bottom: 0.25rem;}
.result-value {font-size: 1.05rem; font-weight: 600; line-height: 1.35;}
</style>
""", unsafe_allow_html=True)

# -------------------------------
# Input form
# -------------------------------
with st.form("lookup_form", clear_on_submit=False):
    postcode_in = st.text_input("Postcode (leave blank to use address)", value="")
    address_in = st.text_input("Address (if no postcode)", value="")
    with st.expander("Optional: Water body catchment overlay"):
        show_wb = st.checkbox("Show river water body catchment (Cycle 3)", value=False)
        hide_other_layers = st.checkbox("Hide LPA/NCA when catchment is shown", value=False)
    submitted = st.form_submit_button("Lookup")

# -------------------------------
# Processing & Results
# -------------------------------
if submitted:
    notes: List[str] = []
    try:
        lat = lon = None
        lpa_text = "Unknown"
        shown_pc = None

        if postcode_in.strip():
            try:
                lat, lon, lpa_text, shown_pc = get_postcode_info(postcode_in.strip())
            except RuntimeError as e:
                if looks_like_uk_postcode(postcode_in):
                    notes.append(f"Note: Postcode lookup failed ({e}). Falling back to address geocoding.")
                    lat, lon = geocode_address_nominatim(postcode_in.strip())
                    nearest_pc, lpa_text = get_nearest_postcode_lpa_from_coords(lat, lon)
                    shown_pc = nearest_pc
                else:
                    notes.append("Input didn’t validate as a UK postcode. Using address geocoding.")
                    lat, lon = geocode_address_nominatim(postcode_in.strip())
                    nearest_pc, lpa_text = get_nearest_postcode_lpa_from_coords(lat, lon)
                    shown_pc = nearest_pc
        else:
            if not address_in.strip():
                st.warning("Please enter either a postcode or an address.")
                st.stop()
            lat, lon = geocode_address_nominatim(address_in.strip())
            nearest_pc, lpa_text = get_nearest_postcode_lpa_from_coords(lat, lon)
            shown_pc = nearest_pc

        # Base polygons
        nca_feat = get_nca_feature(lat, lon)
        lpa_feat = get_lpa_feature(lat, lon)
        nca_name = get_nca_name_from_feature(nca_feat) or "Not found"
        lpa_name = get_lpa_name_from_feature(lpa_feat) or lpa_text or "Unknown"

        # Results
        st.success("Lookup complete.")
        if notes:
            for n in notes:
                st.caption(n)
        if shown_pc:
            st.caption(f"Nearest Postcode: {shown_pc}")

        st.markdown('<div class="result-grid">', unsafe_allow_html=True)
        st.markdown(
            f'''
            <div class="result-box">
              <div class="result-label">Local Planning Authority (LPA)</div>
              <div class="result-value">{lpa_name}</div>
            </div>
            ''',
            unsafe_allow_html=True
        )
        st.markdown(
            f'''
            <div class="result-box">
              <div class="result-label">National Character Area (NCA)</div>
              <div class="result-value">{nca_name}</div>
            </div>
            ''',
            unsafe_allow_html=True
        )
        st.markdown('</div>', unsafe_allow_html=True)

        # ---------- Map ----------
        fmap = folium.Map(location=[lat, lon], zoom_start=11, control_scale=True)

        show_lpa_nca = not (show_wb and hide_other_layers)

        # LPA (red)
        lpa_geojson = None
        if show_lpa_nca:
            lpa_geojson = _arcgis_polygon_to_geojson((lpa_feat or {}).get("geometry"))
            if lpa_geojson:
                folium.GeoJson(
                    lpa_geojson,
                    name=f"LPA: {lpa_name}",
                    style_function=lambda x: {"color": "red", "fillOpacity": 0.05, "weight": 2},
                    tooltip=f"LPA: {lpa_name}"
                ).add_to(fmap)

        # NCA (yellow)
        nca_geojson = None
        if show_lpa_nca:
            nca_geojson = _arcgis_polygon_to_geojson((nca_feat or {}).get("geometry"))
            if nca_geojson:
                folium.GeoJson(
                    nca_geojson,
                    name=f"NCA: {nca_name}",
                    style_function=lambda x: {"color": "yellow", "fillOpacity": 0.05, "weight": 3},
                    tooltip=f"NCA: {nca_name}"
                ).add_to(fmap)

        # Water body catchment (blue) — ONLY if the point is truly inside
        bounds = []
        if show_wb:
            wb_feat = get_water_body_catchment(lat, lon)
            if wb_feat:
                wb_geom = feature_geom(wb_feat)
                if wb_geom and geojson_contains_point(wb_geom, lon, lat):
                    props = wb_feat.get("properties", {})
                    wb_name = props.get("water_body_name") or props.get("name") or "Water body catchment"
                    folium.GeoJson(
                        wb_geom,
                        name=f"WFD water body: {wb_name}",
                        style_function=lambda x: {"color": "blue", "fillOpacity": 0.04, "weight": 3},
                        tooltip=f"WFD water body: {wb_name}"
                    ).add_to(fmap)
                    # extend bounds with catchment
                    if wb_geom["type"] == "Polygon":
                        bounds.extend(wb_geom["coordinates"][0])
                    elif wb_geom["type"] == "MultiPolygon":
                        for part in wb_geom["coordinates"]:
                            bounds.extend(part[0])

        # Red dot marker
        folium.CircleMarker(
            location=[lat, lon],
            radius=5,
            color="red",
            fill=True,
            fill_opacity=1.0,
            tooltip="Location"
        ).add_to(fmap)

        # Fit bounds
        def extend_bounds_from_geojson(geojson, _bounds):
            if not geojson:
                return
            if geojson["type"] == "Polygon":
                _bounds.extend(geojson["coordinates"][0])
            elif geojson["type"] == "MultiPolygon":
                for part in geojson["coordinates"]:
                    _bounds.extend(part[0])

        if show_lpa_nca:
            if lpa_geojson:
                extend_bounds_from_geojson(lpa_geojson, bounds)
            if nca_geojson:
                extend_bounds_from_geojson(nca_geojson, bounds)

        bounds.append([lon, lat])
        latlon_bounds = [[y, x] for x, y in bounds] if bounds else [[lat, lon], [lat, lon]]
        if latlon_bounds:
            fmap.fit_bounds(latlon_bounds, padding=(20, 20))

        st.write("")
        st.markdown("### Map")
        st_folium(fmap, height=560, returned_objects=[], use_container_width=True)

    except RuntimeError as e:
        st.error(str(e))
    except Exception as e:
        st.error(f"Unexpected error: {e}")




