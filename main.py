import io
import os
import tempfile
import zipfile
from typing import List

import geopandas as gpd
import leafmap.foliumap as leafmap
import pandas as pd
import streamlit as st
from pyproj import Geod
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.validation import make_valid

st.set_page_config(page_title="Field Area Comparator", layout="wide")
st.title("🗌️ Field Boundary Comparison & Area Calculator")

ACRES_PER_SQ_METER = 0.000247105
geod = Geod(ellps="WGS84")

COLOR_PALETTE = [
    "#1f77b4",
    "#ff7f0e",
    "#2ca02c",
    "#d62728",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#7f7f7f",
    "#bcbd22",
    "#17becf",
]


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
                        return gpd.read_file(os.path.join(tmpdir, name), driver="KML")

    elif ext == ".kml":
        return gpd.read_file(file, driver="KML")

    elif ext in [".geojson", ".json"]:
        return gpd.read_file(file)

    else:
        raise ValueError("Unsupported file format.")


def geodetic_area(geometry: BaseGeometry) -> float:
    if geometry.geom_type == "Polygon":
        area, _ = geod.geometry_area_perimeter(geometry)
        return abs(area)
    elif geometry.geom_type == "MultiPolygon":
        return sum(
            abs(geod.geometry_area_perimeter(poly)[0]) for poly in geometry.geoms
        )
    return 0


def clean_geometries(gdf: gpd.GeoDataFrame) -> gpd.GeoSeries:
    return gdf.geometry.apply(lambda geom: make_valid(geom).buffer(0))


def extract_holes(geometry: BaseGeometry) -> List[Polygon]:
    holes = []
    if isinstance(geometry, Polygon):
        holes.extend(Polygon(interior) for interior in geometry.interiors)
    elif isinstance(geometry, MultiPolygon):
        for poly in geometry.geoms:
            holes.extend(Polygon(interior) for interior in poly.interiors)
    return holes


def count_parts(geometry: BaseGeometry) -> int:
    if isinstance(geometry, Polygon):
        return 1
    elif isinstance(geometry, MultiPolygon):
        return len([g for g in geometry.geoms if isinstance(g, (Polygon))])
    elif isinstance(geometry, GeometryCollection):
        return len(
            [g for g in geometry.geoms if isinstance(g, (Polygon, MultiPolygon))]
        )

    return 0


def count_holes(geometry: BaseGeometry) -> int:
    if isinstance(geometry, Polygon):
        return len(geometry.interiors)
    elif isinstance(geometry, MultiPolygon):
        return sum(len(poly.interiors) for poly in geometry.geoms)
    elif isinstance(geometry, GeometryCollection):
        return sum(count_holes(geom) for geom in geometry.geoms)
    return 0


def get_poly_type(geometry: BaseGeometry) -> str:
    if isinstance(geometry, Polygon):
        return "polygon"
    elif isinstance(geometry, MultiPolygon):
        return "multipolygon"
    elif isinstance(geometry, GeometryCollection):
        return "geom collection"

    return "unknown"


# UI
uploaded_files = st.file_uploader(
    "Upload 1 or 2 field boundary files (ZIP, KML, KMZ, GeoJSON)",
    type=["zip", "kml", "kmz", "geojson", "json"],
    accept_multiple_files=True,
)

COLOR_PALETTE = [
    "#cc7ee4",
    "#ff7f0e",
    "#2ca02c",
    "#d62728",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#7f7f7f",
    "#bcbd22",
    "#17becf",
]

file_colors = {}
if uploaded_files:
    st.markdown("### 🎨 Choose Boundary Colors")
    for idx, file in enumerate(uploaded_files):
        default_color = COLOR_PALETTE[idx % len(COLOR_PALETTE)]
        file_colors[file.name] = st.color_picker(
            f"Color for {file.name}", default_color
        )

hole_color = st.color_picker("Color for holes", "#ffffff")

use_spherical = st.toggle("🌐 Use spherical (Turf-style) area calculation", value=True)
use_acres = st.toggle("📏 Show area in Acres", value=True)


if uploaded_files:
    gdfs = []
    map_center = [39.5, -98.35]
    zoom = 4
    m = leafmap.Map(center=map_center, zoom=zoom, height=600)
    map_bounds = []
    color_legend = []

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
                        st.warning(
                            "EPSG:6933 and 5070 unavailable. Falling back to EPSG:3857."
                        )
                        gdf_proj = gdf.to_crs(epsg=3857)
                gdf["area_m2"] = gdf_proj.area

            gdf["area_acres"] = gdf["area_m2"] * ACRES_PER_SQ_METER
            gdf["area_m2"] = gdf["area_m2"].round(2)
            gdf["area_acres"] = gdf["area_acres"].round(2)
            gdf["poly_id"] = [f"{file_idx+1}-{i+1}" for i in range(len(gdf))]
            gdf["tooltip"] = gdf.apply(
                lambda row: f"{row['poly_id']}\n{row['area_acres']} acres\n{row['area_m2']} m²",
                axis=1,
            )
            gdf["enabled"] = True
            gdf["poly_type"] = gdf.geometry.apply(get_poly_type)
            gdf["parts"] = gdf.geometry.apply(count_parts)
            gdf["holes"] = gdf.geometry.apply(count_holes)

            color = COLOR_PALETTE[file_idx % len(COLOR_PALETTE)]
            gdf["color"] = color
            color_legend.append((file.name, color))

            gdfs.append(gdf)

        except Exception as e:
            st.error(f"❌ Failed to load {file.name}: {e}")

    st.subheader("🗺️ Map View")

    st.subheader("🗂️ Map Layer Controls")
    layer_visibility = {}
    for gdf in gdfs:
        source_name = gdf["source"].iloc[0]
        layer_visibility[source_name] = st.checkbox(
            f"Show layer: {source_name}", value=True
        )

    show_holes = st.checkbox("Show holes", value=True)
    show_diff = st.checkbox("Show boundary differences (if 2 files)", value=True)

    for gdf in gdfs:
        source_name = gdf["source"].iloc[0]
        st.subheader(f"📏 Area Table: {source_name}")

        unit = "area_acres" if use_acres else "area_m2"
        label = "Acres" if use_acres else "m²"

        df_table = pd.DataFrame(
            {
                "ID": gdf["poly_id"],
                "Type": gdf["poly_type"],
                "Parts": gdf["parts"],
                "Holes": gdf["holes"],
                label: gdf[unit],
                "Visible": gdf["enabled"],
            }
        )

        selected_poly_id = st.selectbox(
            f"Select a polygon to highlight from {source_name}",
            options=gdf["poly_id"],
            index=(
                0
                if st.session_state.get("clicked_poly_id") not in gdf["poly_id"].values
                else gdf["poly_id"].tolist().index(st.session_state["clicked_poly_id"])
            ),
        )

        edited_df = st.data_editor(
            df_table.set_index("ID"),
            use_container_width=True,
            disabled=[label, "Parts"],
            column_config={
                "Type": st.column_config.TextColumn(),
                "Parts": st.column_config.NumberColumn(format="%d"),
                "Holes": st.column_config.NumberColumn(format="%d"),
                label: st.column_config.NumberColumn(format="%.2f"),
                "Visible": st.column_config.CheckboxColumn(),
            },
        )

        for idx, row in gdf.iterrows():
            pid = row["poly_id"]
            gdf.at[idx, "enabled"] = edited_df.loc[pid]["Visible"]

        # Compute totals for visible features
        visible_df = edited_df[edited_df["Visible"]]

        total_acres = visible_df[label].sum()
        total_parts = visible_df["Parts"].sum()
        total_holes = visible_df["Holes"].sum()

        # Display totals
        st.markdown(f"**Total: {round(total_acres, 2)} {label}**, {int(total_parts)} parts, {int(total_holes)} holes")

        if layer_visibility.get(source_name, True):
            for idx, row in gdf.iterrows():
                if row["enabled"]:
                    highlight = row["poly_id"] == selected_poly_id
                    gdf_row = gpd.GeoDataFrame([row], crs=gdf.crs)
                    m.add_gdf(
                        gdf_row,
                        layer_name=f"{row['poly_id']} ({source_name})",
                        style={
                            "fillOpacity": 0.9 if highlight else 0.4,
                            "weight": 4 if highlight else 1,
                            "color": (
                                "lightgreen"
                                if highlight
                                else file_colors.get(source_name, "#000000")
                            ),
                            "fillColor": (
                                file_colors.get(source_name, "#000000")
                            ),
                        },
                        info_mode="on_hover",
                    )
                    map_bounds.append(row.geometry.bounds)

                    if show_holes:
                        for hole in extract_holes(row.geometry):
                            m.add_gdf(
                                gpd.GeoDataFrame(geometry=[hole], crs=gdf.crs),
                                layer_name="hole",
                                style={
                                    "color": hole_color,
                                    "fillColor": hole_color,
                                    "fillOpacity": 1,
                                },
                            )

    if map_bounds:
        try:
            minx = min(b[0] for b in map_bounds)
            miny = min(b[1] for b in map_bounds)
            maxx = max(b[2] for b in map_bounds)
            maxy = max(b[3] for b in map_bounds)
            m.fit_bounds([[miny, minx], [maxy, maxx]])

        except Exception as e:
            st.warning(f"Could not set map bounds: {e}")

    # Comparison Tables
    if len(gdfs) >= 2:
        label = "Acres" if use_acres else "m²"

        st.subheader("🔄 Side-by-side Comparison")
        comp_data = {
            f"{gdfs[0]['source'].iloc[0]} ({'acres' if use_acres else 'm²'})": gdfs[0][
                "area_acres" if use_acres else "area_m2"
            ].reset_index(drop=True),
            f"{gdfs[1]['source'].iloc[0]} ({'acres' if use_acres else 'm²'})": gdfs[1][
                "area_acres" if use_acres else "area_m2"
            ].reset_index(drop=True),
        }
        comp_df = pd.DataFrame(comp_data)
        comp_df["Difference"] = (comp_df.iloc[:, 0] - comp_df.iloc[:, 1]).round(2)
        st.dataframe(comp_df)
        st.markdown(f"**Total diff: {round(comp_df["Difference"].sum(), 2)} {label}**")

    if len(gdfs) == 2 and show_diff:
        st.subheader("📌 Boundary Difference")

        try:
            # Merge and clean
            poly1 = unary_union(gdfs[0].geometry.apply(lambda g: make_valid(g).buffer(0)))
            poly2 = unary_union(gdfs[1].geometry.apply(lambda g: make_valid(g).buffer(0)))

            only_in_1 = poly1.difference(poly2)
            only_in_2 = poly2.difference(poly1)

            def safe_add_diff(m, geometry, crs, label, color):
                if not geometry.is_empty and isinstance(geometry, (Polygon, MultiPolygon)):
                    m.add_gdf(
                        gpd.GeoDataFrame(geometry=[geometry], crs=crs),
                        layer_name=label,
                        style={"color": color, "weight": 4, "fillOpacity": 0.4}
                    )
                else:
                    st.warning(f"⚠️ Difference result for '{label}' is not displayable.")

            safe_add_diff(m, only_in_1, gdfs[0].crs, "Only in 1st", "red")
            safe_add_diff(m, only_in_2, gdfs[1].crs, "Only in 2nd", "blue")

            st.markdown("🔴 **Red = Only in 1st file**  🔵 **Blue = Only in 2nd file**")

            st.write("Geom types:", poly1.geom_type, poly2.geom_type)
            st.write("Only in first - empty?", only_in_1.is_empty)
            st.write("Only in second - empty?", only_in_2.is_empty)

        except Exception as e:
            st.warning(f"⚠️ Could not compute boundary difference: {e}")

    m.to_streamlit()
    # Display color legend
    with st.expander("🎨 Color Legend", expanded=True):
        for name, color in color_legend:
            st.markdown(
                f"<div style='display:flex;align-items:center;gap:10px;'>"
                f"<div style='width:20px;height:20px;background:{color};border-radius:3px;'></div>"
                f"<span>{name}</span></div>",
                unsafe_allow_html=True,
            )

else:
    st.info("Upload 1 or 2 files to visualize and compare field boundaries.")
