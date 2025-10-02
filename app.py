import re
import requests
import streamlit as st
from typing import Optional, Tuple, Dict, Any, List
from streamlit_folium import st_folium
import folium

st.set_page_config(page_title="UK LPA & NCA Lookup", page_icon="🗺️", layout="centered")

# -----------------------------
# Constants & utilities
# -----------------------------
POSTCODES_IO = "https://api.postcodes.io/postcodes/"
POSTCODES_IO_REVERSE = "https://api.postcodes.io/postcodes"
NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"

# Natural England NCA polygons (point-in-polygon)
NCA_FEATURESERVER_LAYER = (
    "https://services.arcgis.com/JJzESW51TqeY9uat/arcgis/rest/services/"
    "National_Character_Areas_England/FeatureServer/0/query"
)

# UK Local Authority District (LPA boundary) polygons (ONS):
# Using 2023 LADs (BFC = full-resolution clipped). Any LAD layer with polygon geometry will work.
# We’ll do a point-in-polygon (no name matching needed).
LPA_FEATURESERVER_LAYER = (
    "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
    "Local_Authority_Districts_December_2023_UK_BFC/FeatureServer/0/query"
)

POSTCODE_RX = re.compile(r"^(GIR\s?0AA|[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2})$", flags=re.IGNORECASE)

def looks_like_uk_postcode(s: str) -> bool:
    return bool(POSTCODE_RX.match((s or "").strip()))

# -----------------------------
# API wrappers (cached)
# -----------------------------
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
    headers = {"User-Agent": "WildCapital-LPA-NCA/1.0 (contact: stuartntis@googlemail.com)"}
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
    """
    Query an ArcGIS FeatureServer polygon layer with a WGS84 point; return first feature (attrs+geometry) or {}.
    """
    geometry = {"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}
    params = {
        "f": "json",
        "geometry": str(geometry),
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": out_fields,
        "returnGeometry": "true",
        "outSR": 4326
    }
    r = requests.get(layer_url, params=params, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"ArcGIS error HTTP {r.status_code}")
    js = r.json()
    if "error" in js:
        msg = js["error"].get("message", "Unknown ArcGIS error")
        raise RuntimeError(f"ArcGIS service error: {msg}")
    feats = js.get("features") or []
    if not feats:
        return {}
    return feats[0]  # first intersecting polygon

def _arcgis_polygon_to_geojson(geom: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Convert ArcGIS polygon geometry (rings) to a GeoJSON-like dict for Folium.
    Handles simple/multipart by returning a MultiPolygon if multiple rings.
    """
    if not geom or "rings" not in geom:
        return None
    rings = geom["rings"]
    if not rings:
        return None
    # Simplistic conversion: treat each ring as its own polygon (holes ignored for display purposes).
    # This is fine for visualisation; for analytical correctness you'd need ring orientation to attach holes.
    if len(rings) == 1:
        return {"type": "Polygon", "coordinates": [rings[0]]}
    else:
        return {"type": "MultiPolygon", "coordinates": [[ring] for ring in rings]}

@st.cache_data(show_spinner=False, ttl=3600)
def get_nca_feature(lat: float, lon: float) -> Dict[str, Any]:
    return _arcgis_point_in_polygon(NCA_FEATURESERVER_LAYER, lat, lon, out_fields="JCANAME,NCA_Name")

@st.cache_data(show_spinner=False, ttl=3600)
def get_lpa_feature(lat: float, lon: float) -> Dict[str, Any]:
    # Out fields vary by dataset; common LAD fields include LAD23NM (name) and LAD23CD (code).
    return _arcgis_point_in_polygon(LPA_FEATURESERVER_LAYER, lat, lon, out_fields="*")

def get_nca_name_from_feature(feat: Dict[str, Any]) -> Optional[str]:
    attrs = (feat or {}).get("attributes") or {}
    return attrs.get("NCA_Name") or attrs.get("JCANAME")

def get_lpa_name_from_feature(feat: Dict[str, Any]) -> Optional[str]:
    attrs = (feat or {}).get("attributes") or {}
    # Try likely name fields (newest first)
    for key in ("LAD23NM", "LAD22NM", "LAD21NM", "LAD20NM", "NAME"):
        if attrs.get(key):
            return attrs[key]
    return None

# -----------------------------
# UI (with wrapped boxes)
# -----------------------------
st.title("🗺️ UK LPA & NCA Lookup")
st.caption("Enter a **postcode** or a **free-text address**. We’ll find the Local Planning Authority and National Character Area, and draw their boundaries.")

# CSS for wrap-friendly result boxes
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

with st.form("lookup_form", clear_on_submit=False):
    postcode_in = st.text_input("Postcode (leave blank to use address)", value="")
    address_in = st.text_input("Address (if no postcode)", value="")
    submitted = st.form_submit_button("Lookup")

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

        # Fetch polygons (point-in-polygon)
        nca_feat = get_nca_feature(lat, lon)
        lpa_feat = get_lpa_feature(lat, lon)

        nca_name = get_nca_name_from_feature(nca_feat) or "Not found"
        lpa_name = get_lpa_name_from_feature(lpa_feat) or lpa_text or "Unknown"

        # Results (wrap-friendly)
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

        # ---------- Map (bottom, full width of the content column) ----------
        # Build GeoJSONs from ArcGIS geometry for Folium
        nca_geojson = _arcgis_polygon_to_geojson((nca_feat or {}).get("geometry"))
        lpa_geojson = _arcgis_polygon_to_geojson((lpa_feat or {}).get("geometry"))

        # Create folium map centered at the point
        fmap = folium.Map(location=[lat, lon], zoom_start=11, control_scale=True)

        # Add LPA polygon (thin outline)
        if lpa_geojson:
            folium.GeoJson(
                lpa_geojson,
                name=f"LPA: {lpa_name}",
                style_function=lambda x: {"fillOpacity": 0.05, "weight": 2},
                tooltip=f"LPA: {lpa_name}"
            ).add_to(fmap)

        # Add NCA polygon (slightly heavier outline)
        if nca_geojson:
            folium.GeoJson(
                nca_geojson,
                name=f"NCA: {nca_name}",
                style_function=lambda x: {"fillOpacity": 0.05, "weight": 3},
                tooltip=f"NCA: {nca_name}"
            ).add_to(fmap)

        # Add the point marker
        folium.Marker([lat, lon], tooltip="Location").add_to(fmap)

        # Fit bounds to whatever geometry we have
        bounds = []
        if lpa_geojson:
            # collect coords
            if lpa_geojson["type"] == "Polygon":
                bounds.extend(lpa_geojson["coordinates"][0])
            elif lpa_geojson["type"] == "MultiPolygon":
                for part in lpa_geojson["coordinates"]:
                    bounds.extend(part[0])
        if nca_geojson:
            if nca_geojson["type"] == "Polygon":
                bounds.extend(nca_geojson["coordinates"][0])
            elif nca_geojson["type"] == "MultiPolygon":
                for part in nca_geojson["coordinates"]:
                    bounds.extend(part[0])
        # Always include the point
        bounds.append([lon, lat])  # careful: Folium bounds expect [lat, lon] later

        # Convert bounds to [lat, lon] pairs
        latlon_bounds = [[y, x] for x, y in bounds] if bounds else [[lat, lon], [lat, lon]]

        if latlon_bounds:
            fmap.fit_bounds(latlon_bounds, padding=(20, 20))

        st.write(" ")  # small spacer
        st.markdown("### Map")
        st_folium(fmap, height=520, returned_objects=[], use_container_width=True)

    except RuntimeError as e:
        st.error(str(e))
    except Exception as e:
        st.error(f"Unexpected error: {e}")
