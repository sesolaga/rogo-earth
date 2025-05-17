import io
import os
import tempfile
import zipfile

import geopandas as gpd
import leafmap.foliumap as leafmap
import streamlit as st
from pyproj import Geod
from shapely.ops import unary_union
from shapely.ops import unary_union
from shapely.validation import make_valid

st.set_page_config(page_title="Field Area Comparator", layout="wide")
st.title("🗺️ Field Boundary Comparison & Area Calculator")

ACRES_PER_SQ_METER = 0.000247105
geod = Geod(ellps="WGS84")


def extract_zip(zip_bytes: bytes) -> str:
    tmpdir = tempfile.mkdtemp()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        z.extractall(tmpdir)
    return tmpdir


def clean_geoms(gdf):
    return gdf.geometry.apply(lambda geom: make_valid(geom).buffer(0))


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


uploaded_files = st.file_uploader(
    "Upload 2+ field boundary files (ZIP, KML, KMZ, GeoJSON)",
    type=["zip", "kml", "kmz", "geojson", "json"],
    accept_multiple_files=True,
)

use_spherical = st.toggle("🌐 Use spherical (Turf-style) area calculation", value=True)

if uploaded_files and len(uploaded_files) >= 2:
    gdfs = []
    st.subheader("📏 Area Calculation Per File")
    for file in uploaded_files:
        try:
            gdf = read_vector_file(file, file.name)
            gdf = gdf.explode(index_parts=False, ignore_index=True)
            gdf["source"] = file.name

            if use_spherical:
                gdf["area_m2"] = gdf.geometry.apply(geodetic_area)
            else:
                gdf_proj = gdf.to_crs(epsg=6933)
                gdf["area_m2"] = gdf_proj.area

            gdf["area_acres"] = gdf["area_m2"] * ACRES_PER_SQ_METER
            total_area_m2 = gdf["area_m2"].sum()
            total_area_acres = total_area_m2 * ACRES_PER_SQ_METER

            st.markdown(
                f"**{file.name}**: `{total_area_acres:.2f}` acres | `{total_area_m2:,.2f}` m²"
            )
            gdfs.append(gdf)

        except Exception as e:
            st.error(f"❌ Failed to load {file.name}: {e}")

    if len(gdfs) >= 2:
        st.subheader("📌 Boundary Difference Map")

        # Clean before union
        gdf1_clean = gdfs[0].copy()
        gdf1_clean["geometry"] = clean_geoms(gdf1_clean)

        gdf2_clean = gdfs[1].copy()
        gdf2_clean["geometry"] = clean_geoms(gdf2_clean)

        poly1 = unary_union(gdf1_clean.geometry)
        poly2 = unary_union(gdf2_clean.geometry)

        # Union all shapes in each file
        only_in_1 = poly1.difference(poly2)
        only_in_2 = poly2.difference(poly1)
        intersection = poly1.intersection(poly2)

        map_center = (
            intersection.centroid if not intersection.is_empty else poly1.centroid
        )
        m = leafmap.Map(center=[map_center.y, map_center.x], zoom=15)

        # Add all files to map
        for gdf in gdfs:
            m.add_gdf(gdf, layer_name=gdf["source"].iloc[0])

        # Add differences
        if not only_in_1.is_empty:
            m.add_gdf(
                gpd.GeoDataFrame(geometry=[only_in_1], crs="EPSG:4326"),
                layer_name="Only in 1st",
                style={"color": "red"},
            )
        if not only_in_2.is_empty:
            m.add_gdf(
                gpd.GeoDataFrame(geometry=[only_in_2], crs="EPSG:4326"),
                layer_name="Only in 2nd",
                style={"color": "blue"},
            )

        m.to_streamlit(height=600)

        st.markdown("🔴 **Red = Only in 1st file**  🔵 **Blue = Only in 2nd file**")

else:
    st.info("Upload at least 2 files to compare boundaries.")
