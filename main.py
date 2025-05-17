import io
import os
import tempfile
import zipfile

import geopandas as gpd
import leafmap.foliumap as leafmap
import streamlit as st
from pyproj import Geod

st.set_page_config(page_title="Field Area Calculator", layout="wide")
st.title("🌾 Field Boundary Area Calculator")

ACRES_PER_SQ_METER = 0.000247105
geod = Geod(ellps="WGS84")


def extract_zip(zip_bytes: bytes) -> str:
    tmpdir = tempfile.mkdtemp()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        z.extractall(tmpdir)

    return tmpdir


def read_vector_file(file: io.BytesIO, filename: str) -> gpd.GeoDataFrame:
    ext = os.path.splitext(filename)[1].lower()

    if ext == ".zip":
        tmpdir = extract_zip(file.read())
        for f in os.listdir(tmpdir):
            if f.endswith(".shp"):
                return gpd.read_file(os.path.join(tmpdir, f))

    elif ext == ".kmz":
        with zipfile.ZipFile(file, "r") as zf:
            with tempfile.TemporaryDirectory() as tmpdir:
                zf.extractall(tmpdir)
                for name in zf.namelist():
                    if name.endswith(".kml"):
                        kml_path = os.path.join(tmpdir, name)
                        return gpd.read_file(kml_path, driver="KML")

    elif ext == ".kml":
        return gpd.read_file(file, driver="KML")

    elif ext in [".geojson", ".json"]:
        return gpd.read_file(file)

    else:
        raise ValueError("Unsupported file format.")


def geodetic_area(geometry):
    if geometry.geom_type == "Polygon":
        area, _ = geod.geometry_area_perimeter(geometry)

        return abs(area)
    elif geometry.geom_type == "MultiPolygon":
        return sum(
            abs(geod.geometry_area_perimeter(poly)[0]) for poly in geometry.geoms
        )
    return 0


uploaded_file = st.file_uploader(
    "Upload a ZIP of shapefiles, KML, KMZ, or GeoJSON",
    type=["zip", "kml", "kmz", "geojson", "json"],
)

use_spherical = st.toggle("🌐 Use spherical (Turf-style) area calculation", value=True)

if uploaded_file:
    try:
        gdf = read_vector_file(uploaded_file, uploaded_file.name)
        gdf = gdf.explode(index_parts=False, ignore_index=True)

        if use_spherical:
            gdf["area_m2"] = gdf.geometry.apply(geodetic_area)
        else:
            gdf_proj = gdf.to_crs(epsg=6933)
            gdf["area_m2"] = gdf_proj.area

        total_area_m2 = gdf["area_m2"].sum()
        total_area_acres = total_area_m2 * ACRES_PER_SQ_METER

        st.success(
            f"✅ Total Area: {total_area_acres:.2f} acres ({total_area_m2:,.2f} m²) — "
            f"{'Spherical (Turf-style)' if use_spherical else 'Projected (equal-area EPSG:6933)'}"
        )

        # Map display
        centroid = gdf.geometry.centroid
        m = leafmap.Map(center=[centroid.y.mean(), centroid.x.mean()], zoom=15)
        m.add_gdf(gdf, layer_name="Field Boundary")
        m.to_streamlit(height=600)

    except Exception as e:
        st.error(f"❌ Error reading file: {e}")
