import streamlit as st
import geopandas as gpd
import zipfile
import tempfile
import os
import io
from pyproj import Geod
import leafmap.foliumap as leafmap
from shapely.ops import unary_union
from shapely.validation import make_valid
from shapely.geometry import Polygon, MultiPolygon
import pandas as pd

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
        return sum(abs(geod.geometry_area_perimeter(poly)[0]) for poly in geometry.geoms)
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
    accept_multiple_files=True
)

use_spherical = st.toggle("🌐 Use spherical (Turf-style) area calculation", value=True)
use_acres = st.toggle("📏 Show area in Acres", value=True)

if uploaded_files:
    gdfs = []
    map_center = [39.5, -98.35]  # Default center over the US
    m = leafmap.Map(center=map_center, zoom=4, height=600)

    visibility_state = {}
    map_bounds = []

    for file_idx, file in enumerate(uploaded_files):
        try:
            gdf = read_vector_file(file, file.name)
            gdf = gdf.explode(index_parts=False, ignore_index=True)
            gdf["source"] = file.name
            gdf["geometry"] = clean_geometries(gdf)

            if use_spherical:
                gdf["area_m2"] = gdf.geometry.apply(geodetic_area)
            else:
                try:
                    gdf_proj = gdf.to_crs(epsg=6933)
                except Exception:
                    try:
                        gdf_proj = gdf.to_crs(epsg=5070)
                        st.warning("EPSG:6933 not available. Using EPSG:5070 instead.")
                    except Exception:
                        st.warning("EPSG:6933 and 5070 unavailable. Falling back to EPSG:3857.")
                        gdf_proj = gdf.to_crs(epsg=3857)
                gdf["area_m2"] = gdf_proj.area

            gdf["area_acres"] = gdf["area_m2"] * ACRES_PER_SQ_METER
            gdf["area_m2"] = gdf["area_m2"].round(2)
            gdf["area_acres"] = gdf["area_acres"].round(2)
            gdf["poly_id"] = [f"{file_idx+1}-{i+1}" for i in range(len(gdf))]
            gdf["tooltip"] = gdf.apply(lambda row: f"{row['poly_id']}\n{row['area_acres']} acres\n{row['area_m2']} m²", axis=1)
            gdf["enabled"] = True

            gdfs.append(gdf)

        except Exception as e:
            st.error(f"❌ Failed to load {file.name}: {e}")

    st.subheader("🗺️ Map View")

    for gdf in gdfs:
        source_name = gdf['source'].iloc[0]
        st.subheader(f"📏 Area Table: {source_name}")

        unit = "area_acres" if use_acres else "area_m2"
        label = "Acres" if use_acres else "m²"

        df_table = pd.DataFrame({
            "ID": gdf["poly_id"],
            label: gdf[unit],
            "Visible": gdf["enabled"],
        })

        edited_df = st.data_editor(
            df_table.set_index("ID"),
            use_container_width=True,
            disabled=[label],
            column_config={
                label: st.column_config.NumberColumn(format="%.2f"),
                "Visible": st.column_config.CheckboxColumn()
            }
        )

        for idx, row in gdf.iterrows():
            pid = row["poly_id"]
            gdf.at[idx, "enabled"] = edited_df.loc[pid]["Visible"]

        total = edited_df[label][edited_df["Visible"]].sum()
        st.markdown(f"**Total: {round(total, 2)} {label}**")

        for idx, row in gdf.iterrows():
            if row["enabled"]:
                gdf_row = gpd.GeoDataFrame([row], crs=gdf.crs)
                m.add_gdf(gdf_row, layer_name=f"{row['poly_id']} ({source_name})", style={"fillOpacity": 0.4}, info_mode="on_hover")
                map_bounds.append(row.geometry.bounds)

                for hole in extract_holes(row.geometry):
                    m.add_gdf(gpd.GeoDataFrame(geometry=[hole], crs=gdf.crs), layer_name="hole", style={"color": "white", "fillColor": "white", "fillOpacity": 1})

    if len(map_bounds) > 0 and m is not None:
        try:
            minx = min(b[0] for b in map_bounds)
            miny = min(b[1] for b in map_bounds)
            maxx = max(b[2] for b in map_bounds)
            maxy = max(b[3] for b in map_bounds)
            m.fit_bounds([[miny, minx], [maxy, maxx]])
        except Exception as e:
            st.warning(f"Could not set map bounds: {e}")

    m.to_streamlit()

    if len(gdfs) >= 2:
        st.subheader("🔄 Side-by-side Comparison")
        comp_data = {
            f"{gdfs[0]['source'].iloc[0]} ({'acres' if use_acres else 'm²'})": gdfs[0]["area_acres" if use_acres else "area_m2"].reset_index(drop=True),
            f"{gdfs[1]['source'].iloc[0]} ({'acres' if use_acres else 'm²'})": gdfs[1]["area_acres" if use_acres else "area_m2"].reset_index(drop=True),
        }
        comp_df = pd.DataFrame(comp_data)
        comp_df["Difference"] = (comp_df.iloc[:, 0] - comp_df.iloc[:, 1]).round(2)
        st.dataframe(comp_df)

    if len(gdfs) == 2:
        st.subheader("📌 Boundary Difference")
        try:
            poly1 = unary_union(gdfs[0].geometry)
            poly2 = unary_union(gdfs[1].geometry)

            only_in_1 = poly1.difference(poly2)
            only_in_2 = poly2.difference(poly1)

            if not only_in_1.is_empty:
                m.add_gdf(gpd.GeoDataFrame(geometry=[only_in_1], crs="EPSG:4326"), layer_name="Only in 1st", style={"color": "red"})
            if not only_in_2.is_empty:
                m.add_gdf(gpd.GeoDataFrame(geometry=[only_in_2], crs="EPSG:4326"), layer_name="Only in 2nd", style={"color": "blue"})

            st.markdown("🔴 **Red = Only in 1st file**  🔵 **Blue = Only in 2nd file**")
        except Exception as e:
            st.warning(f"⚠️ Could not compute boundary difference: {e}")
else:
    st.info("Upload 1 or more files to visualize and compare field boundaries.")
