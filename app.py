import re
import requests
import streamlit as st
from typing import Optional, Tuple
from math import isnan

st.set_page_config(page_title="UK LPA & NCA Lookup", page_icon="🗺️", layout="centered")

# -----------------------------
# Constants & simple utilities
# -----------------------------
POSTCODES_IO = "https://api.postcodes.io/postcodes/"
POSTCODES_IO_REVERSE = "https://api.postcodes.io/postcodes"
NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
NCA_FEATURESERVER_LAYER = (
    "https://services.arcgis.com/JJzESW51TqeY9uat/arcgis/rest/services/"
    "National_Character_Areas_England/FeatureServer/0/query"
)

POSTCODE_RX = re.compile(
    r"^(GIR\s?0AA|[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2})$",
    flags=re.IGNORECASE
)

def looks_like_uk_postcode(s: str) -> bool:
    return bool(POSTCODE_RX.match((s or "").strip()))

# -----------------------------
# API wrappers (cached)
# -----------------------------

@st.cache_data(show_spinner=False, ttl=3600)
def get_postcode_info(postcode: str) -> Tuple[float, float, str, str]:
    """
    Return (lat, lon, lpa, normalised_postcode).
    Raises RuntimeError on failure.
    """
    pc = postcode.replace(" ", "").upper()
    try:
        r = requests.get(POSTCODES_IO + pc, timeout=10)
    except Exception as e:
        raise RuntimeError(f"Postcodes.io request failed: {e}")
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
    """
    Return (lat, lon) using Nominatim; raises RuntimeError on failure.
    """
    params = {
        "q": address,
        "format": "jsonv2",
        "limit": 1,
        "addressdetails": 0,
    }
    headers = {"User-Agent": "UK-LPA-NCA-Lookup/1.0 (contact: stuartntis@googlemail.com)"}
    try:
        r = requests.get(NOMINATIM_SEARCH, params=params, headers=headers, timeout=15)
    except Exception as e:
        raise RuntimeError(f"Nominatim request failed: {e}")
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
    try:
        r = requests.get(POSTCODES_IO_REVERSE, params=params, timeout=10)
    except Exception as e:
        raise RuntimeError(f"postcodes.io reverse request failed: {e}")
    if r.status_code != 200:
        raise RuntimeError(f"postcodes.io reverse error HTTP {r.status_code}")
    js = r.json()
    results = js.get("result") or []
    if not results:
        return None, "Unknown"
    res = results[0]
    lpa = res.get("admin_district") or res.get("admin_county") or res.get("parish") or "Unknown"
    postcode_norm = res.get("postcode")
    return postcode_norm, lpa

@st.cache_data(show_spinner=False, ttl=3600)
def get_nca_name_from_point(lat: float, lon: float) -> Optional[str]:
    """Return NCA name (NCA_Name or JCANAME) for the given WGS84 point, else None."""
    geometry = {"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}
    params = {
        "f": "json",
        "geometry": str(geometry),  # ArcGIS accepts JSON string here
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "JCANAME,NCA_Name",
        "returnGeometry": "false",
    }
    try:
        r = requests.get(NCA_FEATURESERVER_LAYER, params=params, timeout=20)
    except Exception as e:
        raise RuntimeError(f"NCA service request failed: {e}")
    if r.status_code != 200:
        raise RuntimeError(f"NCA service error HTTP {r.status_code}")
    js = r.json()
    if "error" in js:
        msg = js["error"].get("message", "Unknown ArcGIS error")
        raise RuntimeError(f"NCA service error: {msg}")
    feats = js.get("features") or []
    if not feats:
        return None
    a = feats[0].get("attributes") or {}
    return a.get("NCA_Name") or a.get("JCANAME")

# -----------------------------
# UI
# -----------------------------
st.title("🗺️ UK LPA & NCA Lookup")
st.caption("Enter a **postcode** or a **free-text address**. We’ll find the Local Planning Authority and National Character Area.")

with st.form("lookup_form", clear_on_submit=False):
    postcode_in = st.text_input("Postcode (leave blank to use address)", value="")
    address_in = st.text_input("Address (if no postcode)", value="")
    submitted = st.form_submit_button("Lookup")

if submitted:
    try:
        lat = lon = None
        lpa = "Unknown"
        shown_pc = None
        notes = []

        if postcode_in.strip():
            # Try postcode; if it fails but looks like one, we’ll fallback to geocoding it as an address.
            try:
                lat, lon, lpa, shown_pc = get_postcode_info(postcode_in.strip())
            except RuntimeError as e:
                if looks_like_uk_postcode(postcode_in):
                    notes.append(f"Note: Postcode lookup failed ({e}). Falling back to address geocoding.")
                    lat, lon = geocode_address_nominatim(postcode_in.strip())
                    nearest_pc, lpa = get_nearest_postcode_lpa_from_coords(lat, lon)
                    shown_pc = nearest_pc
                else:
                    notes.append("Input didn’t validate as a UK postcode. Using address geocoding.")
                    lat, lon = geocode_address_nominatim(postcode_in.strip())
                    nearest_pc, lpa = get_nearest_postcode_lpa_from_coords(lat, lon)
                    shown_pc = nearest_pc
        else:
            if not address_in.strip():
                st.warning("Please enter either a postcode or an address.")
                st.stop()
            lat, lon = geocode_address_nominatim(address_in.strip())
            nearest_pc, lpa = get_nearest_postcode_lpa_from_coords(lat, lon)
            shown_pc = nearest_pc

        # NCA lookup
        nca = get_nca_name_from_point(lat, lon)

        # Results
        st.success("Lookup complete.")
        if notes:
            for n in notes:
                st.caption(n)

        cols = st.columns(2)
        with cols[0]:
            st.metric("Local Planning Authority (LPA)", lpa)
            st.metric("National Character Area (NCA)", nca or "Not found")

        with cols[1]:
            st.map({"lat": [lat], "lon": [lon]}, zoom=11)

        if shown_pc:
            st.caption(f"Nearest Postcode: {shown_pc}")

    except RuntimeError as e:
        st.error(str(e))
    except Exception as e:
        st.error(f"Unexpected error: {e}")
