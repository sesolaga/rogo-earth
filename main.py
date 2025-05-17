import io
import os
import tempfile
import zipfile

import geopandas as gpd
import leafmap.foliumap as leafmap
import pandas as pd
import streamlit as st
from pyproj import Geod
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union
from shapely.validation import make_valid

st.set_page_config(page_title="Field Area Comparator", layout="wide")
st.title("🗌️ Field Boundary Comparison & Area Calculator")

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


def clean_geometries(gdf):
    return gdf.geometry.apply(lambda geom: make_valid(geom).buffer(0))


def extract_holes(geometry):
    holes = []
    if isinstance(geometry, Polygon):
        for interior in geometry.interiors:
            holes.append(Polygon(interior))
    elif isinstance(geometry, MultiPolygon):
        for poly in geometry.geoms:
            for interior in poly.interiors:
                holes.append(Polygon(interior))
    return holes


uploaded_files = st.file_uploader(
    "Upload 1 or more field boundary files (ZIP, KML, KMZ, GeoJSON)",
    type=["zip", "kml", "kmz", "geojson", "json"],
    accept_multiple_files=True,
)

use_spherical = st.toggle("🌐 Use spherical (Turf-style) area calculation", value=True)

if uploaded_files:
    gdfs = []
    m = leafmap.Map(height=600)

    for file_idx, file in enumerate(uploaded_files):
        try:
            gdf = read_vector_file(file, file.name)
            gdf = gdf.explode(index_parts=False, ignore_index=True)
            gdf["source"] = file.name
            gdf["geometry"] = clean_geometries(gdf)

            if use_spherical:
                gdf["area_m2"] = gdf.geometry.apply(geodetic_area)
            else:
                gdf_proj = gdf.to_crs(epsg=6933)
                gdf["area_m2"] = gdf_proj.area

            gdf["area_acres"] = gdf["area_m2"] * ACRES_PER_SQ_METER
            gdf["area_m2"] = gdf["area_m2"].round(2)
            gdf["area_acres"] = gdf["area_acres"].round(2)
            gdf["poly_id"] = [f"{file_idx+1}-{i+1}" for i in range(len(gdf))]
            gdf["tooltip"] = gdf.apply(
                lambda row: f"{row['poly_id']}\n{row['area_acres']} acres\n{row['area_m2']} m²",
                axis=1,
            )

            for idx, row in gdf.iterrows():
                gdf_row = gpd.GeoDataFrame([row], crs=gdf.crs)
                m.add_gdf(
                    gdf_row,
                    layer_name=f"{row['poly_id']} ({file.name})",
                    style={"fillOpacity": 0.4},
                    info_mode="on_hover",
                )

                # Add holes
                for hole in extract_holes(row.geometry):
                    m.add_gdf(
                        gpd.GeoDataFrame(geometry=[hole], crs=gdf.crs),
                        layer_name="hole",
                        style={
                            "color": "white",
                            "fillColor": "white",
                            "fillOpacity": 1,
                        },
                    )

            gdfs.append(gdf)

        except Exception as e:
            st.error(f"❌ Failed to load {file.name}: {e}")

    m.to_streamlit()

    st.subheader("📏 Area Summary Table")
    for gdf in gdfs:
        st.markdown(f"**{gdf['source'].iloc[0]}**")
        st.dataframe(gdf[["poly_id", "area_acres", "area_m2"]].set_index("poly_id"))

    if len(gdfs) >= 2:
        st.subheader("🔄 Side-by-side Comparison")
        comp_df = pd.DataFrame(
            {
                "File 1 (acres)": gdfs[0]["area_acres"].reset_index(drop=True),
                "File 2 (acres)": gdfs[1]["area_acres"].reset_index(drop=True),
            }
        )
        comp_df["Difference"] = (
            comp_df["File 1 (acres)"] - comp_df["File 2 (acres)"]
        ).round(2)
        st.dataframe(comp_df)

    if len(gdfs) == 2:
        st.subheader("📌 Boundary Difference")
        try:
            poly1 = unary_union(gdfs[0].geometry)
            poly2 = unary_union(gdfs[1].geometry)

            only_in_1 = poly1.difference(poly2)
            only_in_2 = poly2.difference(poly1)

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

            st.markdown("🔴 **Red = Only in 1st file**  🔵 **Blue = Only in 2nd file**")
        except Exception as e:
            st.warning(f"⚠️ Could not compute boundary difference: {e}")
else:
    st.info("Upload 1 or more files to visualize and compare field boundaries.")
