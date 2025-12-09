"""Converter between QMK-like and ZMK Studio DTS physical layout formats, with visualizer."""

import base64
import gzip
import io
import json
import os
import re
from urllib.parse import quote_from_bytes, unquote_to_bytes

import streamlit as st
from streamlit import session_state as state

from keymap_drawer.draw import KeymapDrawer
from keymap_drawer.config import DrawConfig
from keymap_drawer.physical_layout import layout_factory, QmkLayout

APP_URL = "https://physical-layout-vis.streamlit.app/"
IS_STREAMLIT_CLOUD = os.getenv("USER") == "appuser"


def handle_exception(container, message: str, exc: Exception):
    """Display exception in given container."""
    container.error(icon="❗", body=message)
    container.exception(exc)


def _normalize_layout(qmk_spec: QmkLayout) -> QmkLayout:
    min_x, min_y = min(k.x for k in qmk_spec.layout), min(k.y for k in qmk_spec.layout)
    for key in qmk_spec.layout:
        key.x -= min_x
        key.y -= min_y
        if key.rx is not None:
            key.rx -= min_x
        if key.ry is not None:
            key.ry -= min_y
    return qmk_spec


def get_permalink(keymap_yaml: str) -> str:
    """Encode a keymap using a compressed base64 string and place it in query params to create a permalink."""
    b64_bytes = base64.b64encode(gzip.compress(keymap_yaml.encode("utf-8"), mtime=0), altchars=b"-_")
    return f"{APP_URL}?layout={quote_from_bytes(b64_bytes)}"


def decode_permalink_param(param: str) -> str:
    """Get a compressed base64 string from query params and decode it to keymap YAML."""
    return gzip.decompress(base64.b64decode(unquote_to_bytes(param), altchars=b"-_")).decode("utf-8")


def layout_to_svg(qmk_layout: QmkLayout, show_idx: bool) -> str:
    """Convert given internal QMK layout format to its SVG visualization."""
    physical_layout = qmk_layout.generate(50)
    with io.StringIO() as out:
        drawer = KeymapDrawer(
            config=DrawConfig(append_colon_to_layer_header=False, dark_mode="auto"),
            out=out,
            layers={"": list(range(len(physical_layout))) if show_idx else [""] * len(physical_layout)},
            layout=physical_layout,
        )
        drawer.print_board()
        return out.getvalue()


def layouts_to_json(layouts_map: dict[str, QmkLayout]) -> str:
    """Convert given internal QMK layout formats map to JSON representation."""
    out_layouts = {
        display_name: {"layout": qmk_layout.model_dump(exclude_defaults=True, exclude_unset=True)["layout"]}
        for display_name, qmk_layout in layouts_map.items()
    }
    return re.sub(r"\n {10}|\n {8}(?=\})", " ", json.dumps({"layouts": out_layouts}, indent=2))


def qmk_json_to_layouts(qmk_info_str: str) -> dict[str, QmkLayout]:
    """Convert given QMK-style JSON string layouts format map to internal QMK layout formats map."""
    qmk_info = json.loads(qmk_info_str)

    if isinstance(qmk_info, list):
        return {"Default": QmkLayout(layout=qmk_info)}  # shortcut for list-only representation
    return {name: _normalize_layout(QmkLayout(layout=val["layout"])) for name, val in qmk_info["layouts"].items()}


def ortho_to_layouts(
    ortho_layout: dict | None, cols_thumbs_notation: str | None, split_gap: float = 1.0
) -> dict[str, QmkLayout]:
    """Given ortho s (ortho layout description or cols+thumbs notation) convert it to the internal QMK layout format."""
    p_layout = layout_factory(
        DrawConfig(key_w=1, key_h=1, split_gap=split_gap),
        ortho_layout=ortho_layout,
        cols_thumbs_notation=cols_thumbs_notation,
    )
    return {
        "Default": QmkLayout(
            layout=[
                {"x": key.pos.x - key.width / 2, "y": key.pos.y - key.height / 2, "w": key.width, "h": key.height}
                for key in p_layout.keys
            ]
        )
    }


def _ortho_form() -> dict[str, QmkLayout] | None:
    out = None
    nonsplit, split, cols_thumbs = st.tabs(["Non-split", "Split", "Cols+Thumbs Notation"])
    with nonsplit:
        with st.form("ortho_nonsplit"):
            params = {
                "split": False,
                "rows": st.number_input("Number of rows", min_value=1, max_value=10),
                "columns": st.number_input("Number of columns", min_value=1, max_value=20),
                "thumbs": {"Default (1u)": 0, "MIT (1x2u)": "MIT", "2x2u": "2x2u"}[
                    st.selectbox("Thumbs type", options=("Default (1u)", "MIT (1x2u)", "2x2u"))  # type: ignore
                ],
            }
            submitted = st.form_submit_button("Generate")
            if submitted:
                try:
                    out = ortho_to_layouts(ortho_layout=params, cols_thumbs_notation=None)
                except Exception as exc:
                    handle_exception(st, "Failed to generate non-split layout", exc)
    with split:
        with st.form("ortho_split"):
            params = {
                "split": True,
                "rows": st.number_input("Number of rows", min_value=1, max_value=10),
                "columns": st.number_input("Number of columns", min_value=1, max_value=10),
                "thumbs": st.number_input("Number of thumb keys", min_value=0, max_value=10),
                "drop_pinky": st.checkbox("Drop pinky"),
                "drop_inner": st.checkbox("Drop inner index"),
            }
            split_gap = st.number_input("Gap between split halves", value=1.0, min_value=0.0, max_value=10.0, step=0.5)
            submitted = st.form_submit_button("Generate")
            if submitted:
                try:
                    out = ortho_to_layouts(ortho_layout=params, cols_thumbs_notation=None, split_gap=split_gap)
                except Exception as exc:
                    handle_exception(st, "Failed to generate split layout", exc)
    with cols_thumbs:
        with st.form("ortho_cpt"):
            st.caption(
                "[Details of the spec](https://github.com/caksoylar/keymap-drawer/blob/main/PHYSICAL_LAYOUTS.md#colsthumbs-notation-specification)"
            )
            cpt_spec = st.text_input("Cols+Thumbs notation spec", placeholder="23333+2 3+333331")
            split_gap = st.number_input("Gap between split halves", value=1.0, min_value=0.0, max_value=10.0, step=0.5)
            submitted = st.form_submit_button("Generate")
            if submitted:
                try:
                    out = ortho_to_layouts(ortho_layout=None, cols_thumbs_notation=cpt_spec, split_gap=split_gap)
                except Exception as exc:
                    handle_exception(st, "Failed to generate from cols+thumbs notation spec", exc)
    return out


@st.dialog("Layout permalink", width="medium")
def show_permalink():
    st.code(get_permalink(layouts_to_json(state.layouts)), language=None, wrap_lines=True)


def svg_column(show_idx: bool) -> None:
    """Contents of the SVG column."""
    st.subheader("Visualization", anchor=False)
    svg = layout_to_svg(state.layouts["Default"], show_idx)
    # shown = st.selectbox(label="Select", label_visibility="collapsed", options=list(svgs))
    st.image(svg)


def main() -> None:
    """Main body of the web app."""
    st.set_page_config(page_title="Physical layout visualizer", page_icon=":keyboard:")
    st.header("Physical layout visualizer", anchor=False)

    if layout_json := st.query_params.get("layout"):
        state.layouts = qmk_json_to_layouts(decode_permalink_param(layout_json))
        st.query_params.clear()
        print("0.0 read json from query params")
        st.rerun()
    elif layout_cpt := st.query_params.get("cpt"):
        layout_cpt = layout_cpt.replace(" ", "+")
        state.layouts = ortho_to_layouts(ortho_layout=None, cols_thumbs_notation=layout_cpt, split_gap=float(st.query_params.get("gap", "1.0")))
        st.query_params.clear()
        print("0.0 read cpt from query params")
        st.rerun()

    if "layouts" not in state:
        state.layouts = {"Default": QmkLayout(layout=[{"x": 0.0, "y": 0.0}])}

    with st.container(horizontal_alignment="center"):
        ortho_layout = _ortho_form()
        if ortho_layout is not None:
            state.layouts = ortho_layout
            ortho_layout = None

    show_idx = st.toggle("Show key indices")
    with st.container(horizontal_alignment="center"):
        svg_column(show_idx)

    permabutton = st.button(label="Generate permalink to layout")
    if permabutton:
        show_permalink()



if __name__ == "__main__":
    main()
