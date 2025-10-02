import json
import re
import requests
import streamlit as st
from typing import Optional, Tuple, Dict, Any, List
from streamlit_folium import st_folium
import folium

# -------------------------------
# Page config with logo as favicon
# -------------------------------
st.set_page_config(
    page_title="UK LPA & NCA Lookup",
    page_icon="wild_capital_uk_logo.png",
    layout="centered"
)

# --------------------------------
# Endpoints & utilities
# --------------------------------
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

POSTCODE_RX = re.compile(r"^(GIR\s?0AA|[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2})$", flags=re.IGNORECASE)

def looks_like_uk_postcode(s: str) -> bool:
    return bool(POSTCODE_RX.match((s or "").strip()))

# --------------------------------
# Cached API wrappers
# --------------------------------
@st.cache_data(show_spinner=False, ttl=3600)
def get_postcode_info(postcode: str) -> Tuple[float, float, str, str]:
    """Return (lat, lon, lpa, normalised_postcode); raises RuntimeError on failure."""
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
    """Return (lat, lon) using Nominatim; raises RuntimeError on failure."""
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
    """Return (nearest_postcode, lpa)."""
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
    """Query an ArcGIS FeatureServer polygon layer with a WGS84 point; return first feature (attrs+geometry) or {}."""
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
    """Convert ArcGIS polygon geometry (rings) to a GeoJSON-like dict for Folium."""
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

# --------------------------------
# Header (centered logo + title + subtitle)
# --------------------------------
st.markdown(
    """
    <div style="text-align: center;">
        <img src="wild_capital_uk_logo.png" width="180" style="margin-bottom: 0.5em;">
        <h1 style="margin-top: 0.2em; margin-bottom: 0.25em;">UK LPA & NCA Lookup</h1>
        <p style="font-size: 1.1em; color: #555; margin-top: 0;">
            Enter a postcode or a free-text address. We’ll find the Local Planning Authority and National Character Area, and draw their boundaries.
        </p>
    </div>
    """,
    unsafe_allow_html=True
)

# --------------------------------
# CSS for wrap-friendly result boxes
# --------------------------------
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

# --------------------------------
# Input form
# --------------------------------
with st.form("lookup_form", clear_on_submit=False):
    postcode_in = st.text_input("Postcode (leave blank to use address)", value="")
    address_in = st.text_input("Address (if no postcode)", value="")
    submitted = st.form_submit_button("Lookup")

# --------------------------------
# Processing & Results
# --------------------------------
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

        # Fetch polygons
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
        nca_geojson = _arcgis_polygon_to_geojson((nca_feat or {}).get("geometry"))
        lpa_geojson = _arcgis_polygon_to_geojson((lpa_feat or {}).get("geometry"))

        fmap = folium.Map(location=[lat, lon], zoom_start=11, control_scale=True)

        # LPA polygon (red outline)
        if lpa_geojson:
            folium.GeoJson(
                lpa_geojson,
                name=f"LPA: {lpa_name}",
                style_function=lambda x: {"color": "red", "fillOpacity": 0.05, "weight": 2},
                tooltip=f"LPA: {lpa_name}"
            ).add_to(fmap)

        # NCA polygon (yellow outline)
        if nca_geojson:
            folium.GeoJson(
                nca_geojson,
                name=f"NCA: {nca_name}",
                style_function=lambda x: {"color": "yellow", "fillOpacity": 0.05, "weight": 3},
                tooltip=f"NCA: {nca_name}"
            ).add_to(fmap)

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
        bounds = []
        def extend_bounds(geojson, bounds_list):
            if not geojson:
                return
            if geojson["type"] == "Polygon":
                bounds_list.extend(geojson["coordinates"][0])
            elif geojson["type"] == "MultiPolygon":
                for part in geojson["coordinates"]:
                    bounds_list.extend(part[0])

        extend_bounds(lpa_geojson, bounds)
        extend_bounds(nca_geojson, bounds)
        bounds.append([lon, lat])
        latlon_bounds = [[y, x] for x, y in bounds] if bounds else [[lat, lon], [lat, lon]]
        if latlon_bounds:
            fmap.fit_bounds(latlon_bounds, padding=(20, 20))

        st.write("")
        st.markdown("### Map")
        st_folium(fmap, height=540, returned_objects=[], use_container_width=True)

    except RuntimeError as e:
        st.error(str(e))
    except Exception as e:
        st.error(f"Unexpected error: {e}")
