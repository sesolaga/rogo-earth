import io
import os
import tempfile
import zipfile
from typing import List

import geopandas as gpd
import leafmap.foliumap as leafmap
import pandas as pd
import streamlit as st
from geopandas.geodataframe import GeoDataFrame
from pyproj import Geod
from pyproj.exceptions import CRSError
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.validation import make_valid

st.set_page_config(page_title="Field Area Comparator", layout="wide")
st.title("🗌️ Field Boundary Comparison & Area Calculator")

ACRES_PER_SQ_METER = 0.000247105
DEFAULT_CENTER_LAT = 60.76788
DEFAULT_CENTER_LON = -87.1633111
DEFAULT_ZOOM_LEVEL = 10
MAP_HEIGHT = 800

geod = Geod(ellps="WGS84")

COLOR_PALETTE = [
    "#007BFF",
    "#FF9F40",
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

    raise ValueError("Unsupported file format.")


def geodetic_area(geometry: BaseGeometry) -> float:
    if isinstance(geometry, Polygon):
        area, _ = geod.geometry_area_perimeter(geometry)
        return abs(area)
    elif isinstance(geometry, MultiPolygon):
        return sum(
            abs(geod.geometry_area_perimeter(poly)[0]) for poly in geometry.geoms
        )

    return 0


def clean_geometries(gdf: gpd.GeoDataFrame) -> gpd.GeoSeries:
    return gpd.GeoSeries(
        gdf.geometry.apply(lambda geom: make_valid(geom).buffer(0)), crs=gdf.crs
    )


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


def upload_files_ui():
    return st.file_uploader(
        "Upload 1 or 2 field boundary files (ZIP, KML, KMZ, GeoJSON)",
        type=["zip", "kml", "kmz", "geojson", "json"],
        accept_multiple_files=True,
    )


def boundary_color_controls(uploaded_files):
    file_colors = {}

    st.markdown("### 🎨 Choose Boundary Colors")

    for idx, file in enumerate(uploaded_files):
        default_color = COLOR_PALETTE[idx % len(COLOR_PALETTE)]
        file_colors[file.name] = st.color_picker(
            f"Color for {file.name}", default_color
        )

    return file_colors


def get_gdf_projection(gdf: gpd.GeoDataFrame) -> GeoDataFrame | None:
    gdf_proj = None

    try:
        gdf_proj = gdf.to_crs(epsg=6933)
    except CRSError:
        try:
            gdf_proj = gdf.to_crs(epsg=5070)
            st.warning("EPSG:6933 not available. Using EPSG:5070.")
        except CRSError:
            st.warning("EPSG:5070 also unavailable. Falling back to EPSG:3857.")

            try:
                gdf_proj = gdf.to_crs(epsg=3857)
            except CRSError:
                st.error(
                    "Failed to reproject to EPSG:3857. Cannot calculate planar area."
                )

    return gdf_proj


def process_uploaded_files(uploaded_files, use_spherical, file_colors):
    gdfs = []
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
                gdf_proj = get_gdf_projection(gdf)
                if gdf_proj is None:
                    continue

                gdf["area_m2"] = gdf_proj.area

            gdf["area_acres"] = gdf["area_m2"] * ACRES_PER_SQ_METER
            gdf["area_m2"] = gdf["area_m2"].round(2)
            gdf["area_acres"] = gdf["area_acres"].round(2)
            gdf["poly_id"] = [f"{file_idx+1}-{i+1}" for i in range(len(gdf))]
            gdf["enabled"] = True
            gdf["poly_type"] = gdf.geometry.apply(get_poly_type)
            gdf["parts"] = gdf.geometry.apply(count_parts)
            gdf["holes"] = gdf.geometry.apply(count_holes)

            color = file_colors[file.name]
            gdf["color"] = color
            color_legend.append((file.name, color))

            gdfs.append(gdf)
        except Exception as e:
            st.error(f"❌ Failed to load {file.name}: {e}")

    return gdfs, color_legend


def display_layer_controls(gdfs: List[GeoDataFrame]):
    layer_visibility = {}
    st.subheader("🗂️ Map Layer Controls")
    for gdf in gdfs:
        source_name = gdf["source"].iloc[0]
        layer_visibility[source_name] = st.checkbox(
            f"Show layer: {source_name}", value=True
        )

    return layer_visibility


def add_layer_for_gdf(
    gdf,
    layer_visibility,
    file_colors,
    m,
    show_holes,
    hole_color,
):
    source_name = gdf["source"].iloc[0]
    selected_poly_id = st.session_state.get(
        f"{source_name}_clicked_poly_id", gdf["poly_id"].iloc[0]
    )

    if not layer_visibility.get(source_name, True):
        return

    for _, row in gdf.iterrows():
        if not row["enabled"]:
            continue

        highlight = row["poly_id"] == selected_poly_id
        gdf_row = gpd.GeoDataFrame([row], crs=gdf.crs)

        m.add_gdf(
            gdf_row,
            layer_name=f"{row['poly_id']} ({source_name})",
            style={
                "fillOpacity": 0.9 if highlight else 0.4,
                "weight": 4 if highlight else 1,
                "color": ("lightgreen" if highlight else file_colors[source_name]),
                "fillColor": file_colors[source_name],
            },
            info_mode="on_hover",
        )

        if not show_holes:
            continue

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


def add_layers(
    gdfs,
    layer_visibility,
    file_colors,
    m,
    show_holes,
    hole_color,
):
    for gdf in gdfs:
        add_layer_for_gdf(gdf, layer_visibility, file_colors, m, show_holes, hole_color)


def make_selectbox_callback(source_name):
    def callback():
        st.session_state[f"{source_name}_clicked_poly_id"] = st.session_state[
            f"selectbox_{source_name}"
        ]

    return callback


def display_area_table(
    gdf,
    label,
    unit,
):
    source_name = gdf["source"].iloc[0]

    st.subheader(f"📏 Area Table: {source_name}")

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

    st.selectbox(
        f"Select a polygon to highlight from {source_name}",
        options=gdf["poly_id"],
        index=(
            0
            if st.session_state.get(f"{source_name}_clicked_poly_id")
            not in gdf["poly_id"].values
            else gdf["poly_id"]
            .tolist()
            .index(st.session_state[f"{source_name}_clicked_poly_id"])
        ),
        key=f"selectbox_{source_name}",  # 👈 unique key
        on_change=make_selectbox_callback(source_name),
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

    visible_df = edited_df[edited_df["Visible"]]
    total_area = visible_df[label].sum()
    total_parts = visible_df["Parts"].sum()
    total_holes = visible_df["Holes"].sum()

    st.markdown(
        f"**Total: {round(total_area, 2)} {label}**, {int(total_parts)} parts, {int(total_holes)} holes"
    )


def display_comparison_table(gdfs, use_acres):
    label = "Acres" if use_acres else "m²"

    raw_col_1 = gdfs[0]["area_acres" if use_acres else "area_m2"]
    raw_col_2 = gdfs[1]["area_acres" if use_acres else "area_m2"]

    # Pad shorter column with NaNs
    max_len = max(len(raw_col_1), len(raw_col_2))
    raw_col_1 = raw_col_1.reindex(range(max_len)).reset_index(drop=True)
    raw_col_2 = raw_col_2.reindex(range(max_len)).reset_index(drop=True)

    missing_1 = raw_col_1.isna()
    missing_2 = raw_col_2.isna()

    col_1 = raw_col_1.fillna(0)
    col_2 = raw_col_2.fillna(0)
    diff = (col_1 - col_2).round(2)

    st.subheader("🔄 Side-by-side Comparison (highlights missing areas in yellow)")
    comp_df = pd.DataFrame(
        {
            f"{gdfs[0]['source'].iloc[0]} ({label})": col_1,
            f"{gdfs[1]['source'].iloc[0]} ({label})": col_2,
            "Difference": diff,
        }
    )

    def highlight_missing(row_idx):
        style_row = []
        style_row.append("background-color: #fff3cd" if missing_1.iloc[row_idx] else "")
        style_row.append("background-color: #fff3cd" if missing_2.iloc[row_idx] else "")
        style_row.append("")  # No style for diff
        return style_row

    style_mask = pd.DataFrame(
        [highlight_missing(i) for i in range(len(comp_df))],
        columns=comp_df.columns,
    )

    st.dataframe(
        comp_df.style.apply(lambda _: style_mask, axis=None).format(
            precision=2, na_rep="—"
        )  # 👈 Format all numeric values
    )

    st.markdown(f"**Total diff: {round(diff.sum(), 2)} {label}**")


def display_boundary_difference(gdfs, m):
    try:
        st.subheader("📌 Boundary Difference")

        poly1 = unary_union(gdfs[0].geometry.apply(lambda g: make_valid(g).buffer(0)))
        poly2 = unary_union(gdfs[1].geometry.apply(lambda g: make_valid(g).buffer(0)))

        poly1 = make_valid(poly1).buffer(0)
        poly2 = make_valid(poly2).buffer(0)

        only_in_1 = poly1.difference(poly2)
        only_in_2 = poly2.difference(poly1)

        def safe_add_diff(geometry, crs, label, color):
            if not geometry.is_empty and isinstance(geometry, (Polygon, MultiPolygon)):
                m.add_gdf(
                    gpd.GeoDataFrame(geometry=[geometry], crs=crs),
                    layer_name=label,
                    style={"color": color, "weight": 4, "fillOpacity": 0.4},
                )

        safe_add_diff(only_in_1, gdfs[0].crs, "Only in 1st", "red")
        safe_add_diff(only_in_2, gdfs[1].crs, "Only in 2nd", "blue")

        st.markdown("🔴 **Red = Only in 1st file**  🔵 **Blue = Only in 2nd file**")
    except Exception as e:
        st.warning(f"⚠️ Could not compute boundary difference: {e}")


def set_map_bounds(m, gdfs):
    map_bounds = []
    for gdf in gdfs:
        map_bounds.extend(gdf.total_bounds.reshape(2, 2).tolist())

    if not map_bounds:
        return

    try:
        minx = min(x[0] for x in map_bounds)
        miny = min(x[1] for x in map_bounds)
        maxx = max(x[0] for x in map_bounds)
        maxy = max(x[1] for x in map_bounds)

        # TODO: doesn't work for some KML files - investigate
        m.fit_bounds([[miny, minx], [maxy, maxx]])

        bbox = box(minx, miny, maxx, maxy)
        bbox_gdf = gpd.GeoDataFrame(geometry=[bbox], crs="EPSG:4326")
        m.add_gdf(
            bbox_gdf,
            layer_name="Bounding Box",
            style={"color": "red", "fillOpacity": 0},
        )
    except Exception as e:
        st.warning(f"Could not set map bounds: {e}")


def render_ui():
    uploaded_files = upload_files_ui()

    if not uploaded_files:
        st.info("Upload 1 or 2 files to visualize and compare field boundaries.")

        return

    file_colors = boundary_color_controls(uploaded_files)
    hole_color = st.color_picker("Color for holes", "#ffffff")
    use_spherical = st.toggle(
        "🌐 Use spherical (Turf-style) area calculation", value=True
    )
    use_acres = st.toggle("📏 Show area in Acres", value=True)

    gdfs, color_legend = process_uploaded_files(
        uploaded_files, use_spherical, file_colors
    )

    m = leafmap.Map(
        center=[DEFAULT_CENTER_LAT, DEFAULT_CENTER_LON],
        zoom=DEFAULT_ZOOM_LEVEL,
        height=MAP_HEIGHT,
    )
    set_map_bounds(m, gdfs)

    unit = "area_acres" if use_acres else "area_m2"
    label = "Acres" if use_acres else "m²"

    layer_visibility = display_layer_controls(gdfs)
    show_holes = st.checkbox("Show holes", value=True)

    add_layers(
        gdfs,
        layer_visibility,
        file_colors,
        m,
        show_holes,
        hole_color,
    )

    if len(gdfs) == 2:
        show_diff = st.checkbox("Show boundary differences", value=True)
        if show_diff:
            display_boundary_difference(gdfs, m)

    m.to_streamlit()

    with st.expander("🎨 Color Legend", expanded=True):
        for name, color in color_legend:
            st.markdown(
                f"<div style='display:flex;align-items:center;gap:10px;'>"
                f"<div style='width:20px;height:20px;background:{color};border-radius:3px;'></div>"
                f"<span>{name}</span></div>",
                unsafe_allow_html=True,
            )

    for gdf in gdfs:
        display_area_table(
            gdf,
            label,
            unit,
        )

    if len(gdfs) == 2:
        display_comparison_table(gdfs, use_acres)


render_ui()
