"""Converter between QMK-like and ZMK Studio DTS physical layout formats, with visualizer."""

import base64
import gzip
import io
import json
import os
import re
import tempfile
import zipfile
from textwrap import indent
from pathlib import Path
from urllib.parse import quote_from_bytes, unquote_to_bytes
from urllib.request import urlopen
from multiprocessing import Pool
from itertools import starmap

import streamlit as st
from streamlit import session_state as state
import pandas as pd

from keymap_drawer.draw import KeymapDrawer
from keymap_drawer.config import DrawConfig
from keymap_drawer.physical_layout import layout_factory, QmkLayout
from keymap_drawer.parse.dts import DeviceTree

APP_URL = "https://zmk-physical-layout-converter.streamlit.app/"
DTS_TEMPLATE = """\
#include <physical_layouts.dtsi>

/ {{
{pl_nodes}\
}};
"""
PL_TEMPLATE = """\
physical_layout{idx}: physical_layout_{idx} {{
    compatible = "zmk,physical-layout";
    display-name = "{name}";

    kscan = <&kscan{idx}>;
    transform = <&matrix_transform{idx}>;
{keys}\
}};
"""
KEYS_TEMPLATE = """
keys  //                     w   h    x    y     rot    rx    ry
    = {key_attrs_string}
    ;
"""
KEY_TEMPLATE = "<&key_physical_attrs {w:>3} {h:>3} {x:>4} {y:>4} {rot:>7} {rx:>5} {ry:>5}>"
PHYSICAL_ATTR_PHANDLES = {"&key_physical_attrs"}

IS_STREAMLIT_CLOUD = os.getenv("USER") == "appuser"


COL_CFG = {
    "_index": st.column_config.NumberColumn("Index"),
    "x": st.column_config.NumberColumn(format="%.2f", min_value=0, required=True),
    "y": st.column_config.NumberColumn(format="%.2f", min_value=0, required=True),
    "w": st.column_config.NumberColumn(min_value=0),
    "h": st.column_config.NumberColumn(min_value=0),
    "r": st.column_config.NumberColumn(min_value=-180, max_value=180),
    "rx": st.column_config.NumberColumn(format="%.2f"),
    "ry": st.column_config.NumberColumn(format="%.2f"),
}


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


@st.cache_data
def _get_initial_layout():
    with open("example.json", encoding="utf-8") as f:
        return f.read()


@st.cache_data(max_entries=10)
def dts_to_layouts(dts_str: str) -> dict[str, QmkLayout]:
    """Convert given DTS string containing physical layouts to internal QMK layout format."""
    dts = DeviceTree(dts_str, None, True)

    def parse_binding_params(bindings):
        params = {
            k: int(v.lstrip("(").rstrip(")")) / 100 for k, v in zip(("w", "h", "x", "y", "r", "rx", "ry"), bindings)
        }
        if params["r"] == 0:
            del params["rx"], params["ry"]
        return params

    bindings_to_position = {"key_physical_attrs": parse_binding_params}

    if nodes := dts.get_compatible_nodes("zmk,physical-layout"):
        defined_layouts = {node.get_string("display-name"): node.get_phandle_array("keys") for node in nodes}
    elif keys_array := dts.root.get_phandle_array("keys"):
        defined_layouts = {"Default": keys_array}
    else:
        raise ValueError('No `compatible = "zmk,physical-layout"` nodes nor a single `keys` property found')

    out_layouts = {}
    for display_name, position_bindings in defined_layouts.items():
        assert display_name is not None, "No `display_name` property found for a physical layout node"
        assert position_bindings is not None, f'No `keys` property found for layout "{display_name}"'
        keys = []
        for binding_arr in position_bindings:
            binding = binding_arr.split()
            assert binding[0].lstrip("&") in bindings_to_position, f"Unrecognized position binding {binding[0]}"
            keys.append(bindings_to_position[binding[0].lstrip("&")](binding[1:]))
        out_layouts[display_name] = _normalize_layout(QmkLayout(layout=keys))
    return out_layouts


def layout_to_svg(qmk_layout: QmkLayout) -> str:
    """Convert given internal QMK layout format to its SVG visualization."""
    physical_layout = qmk_layout.generate(50)
    with io.StringIO() as out:
        drawer = KeymapDrawer(
            config=DrawConfig(append_colon_to_layer_header=False, dark_mode="auto"),
            out=out,
            layers={"": list(range(len(physical_layout)))},
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


def layouts_to_dts(layouts_map: dict[str, QmkLayout]) -> str:
    """Convert given internal QMK layout formats map to DTS representation."""

    def num_to_str(num: float | int) -> str:
        if num >= 0:
            return str(round(num))
        return "(" + str(round(num)) + ")"

    pl_nodes = []
    for idx, (name, qmk_spec) in enumerate(layouts_map.items()):
        keys = KEYS_TEMPLATE.format(
            key_attrs_string="\n    , ".join(
                KEY_TEMPLATE.format(
                    w=num_to_str(100 * key.w),
                    h=num_to_str(100 * key.h),
                    x=num_to_str(100 * key.x),
                    y=num_to_str(100 * key.y),
                    rot=num_to_str(100 * key.r),
                    rx=num_to_str(100 * (key.rx or 0)),
                    ry=num_to_str(100 * (key.ry or 0)),
                )
                for key in qmk_spec.layout
            )
        )
        pl_nodes.append(PL_TEMPLATE.format(idx=idx, name=name, keys=indent(keys, "    ")))
    return DTS_TEMPLATE.format(pl_nodes=indent("\n".join(pl_nodes), "    "))


def layout_to_df(layout):
    """Get a pandas DF from given QmkLayout."""
    return pd.DataFrame(
        layout.model_dump(exclude_defaults=True, exclude_unset=True)["layout"],
        columns=["x", "y", "w", "h", "r", "rx", "ry"],
    )


def qmk_json_to_layouts(qmk_info_str: str) -> dict[str, QmkLayout]:
    """Convert given QMK-style JSON string layouts format map to internal QMK layout formats map."""
    qmk_info = json.loads(qmk_info_str)

    if isinstance(qmk_info, list):
        return {"Default": QmkLayout(layout=qmk_info)}  # shortcut for list-only representation
    return {name: _normalize_layout(QmkLayout(layout=val["layout"])) for name, val in qmk_info["layouts"].items()}


def ortho_to_layouts(
    ortho_layout: dict | None, cols_thumbs_notation: str | None, split_gap: float = 1.0
) -> dict[str, QmkLayout]:
    """Given ortho specs (ortho layout description or cols+thumbs notation) convert it to the internal QMK layout format."""
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


def _read_layout(common_path: Path, path: Path) -> tuple[str, None | dict[str, QmkLayout]]:
    name = str(path.relative_to(common_path))
    try:
        with open(path, encoding="utf-8") as f:
            return name, dts_to_layouts(f.read())
    except ValueError:
        return name, None


@st.cache_data
def get_shared_layouts() -> dict[str, dict[str, QmkLayout]]:
    """Get shared layouts from ZMK repo so they can be used as a starting point."""
    with urlopen("https://api.github.com/repos/zmkfirmware/zmk/zipball/main") as f:
        zip_bytes = f.read()
    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zipped:
            zipped.extractall(tmpdir)
        common_layouts = next(Path(tmpdir).iterdir()) / "app" / "dts" / "layouts"

        if IS_STREAMLIT_CLOUD:
            out = dict(starmap(_read_layout, ((common_layouts, path) for path in common_layouts.rglob("*.dtsi"))))
        else:
            with Pool() as mp:
                out = dict(
                    mp.starmap(_read_layout, ((common_layouts, path) for path in common_layouts.rglob("*.dtsi")))
                )
        return {k: v for k in sorted(out) if (v := out[k]) is not None}


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
                "[Details of the spec](https://github.com/caksoylar/keymap-drawer/blob/main/KEYMAP_SPEC.md#colsthumbs-notation-specification)"
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


@st.dialog("Edit layout")
def df_editor():
    """Show the dialog box that has the dataframe editor."""
    selected = st.selectbox("Layout to edit", list(state.layouts))
    df = st.data_editor(
        layout_to_df(state.layouts[selected]),
        column_config=COL_CFG,
        hide_index=False,
        height=600,
        use_container_width=True,
    )
    if st.button("Update"):
        state.layouts[selected] = QmkLayout(
            layout=[{k: v for k, v in record.items() if not pd.isna(v)} for record in df.to_dict("records")]
        )
        state.need_update = True
        st.rerun()


@st.dialog("Layout permalink", width="medium")
def show_permalink():
    st.code(get_permalink(state.json_field), language=None, wrap_lines=True)


def json_column() -> None:
    """Contents of the json column."""
    st.subheader("JSON description", anchor=False)
    with st.container(height=45, border=False):
        st.caption(
            "QMK-like physical layout spec description. "
            "Consider using [Keymap Layout Helper :material/open_in_new:](https://nickcoutsos.github.io/keymap-layout-tools/) to edit "
            "or import from KLE/KiCad!"
        )
    if state.need_update:
        state.json_field = layouts_to_json(state.layouts)

    st.text_area("JSON layout", key="json_field", height=800, label_visibility="collapsed")
    json_button = st.button("Update DTS using this ➡️", use_container_width=True)
    if json_button:
        print("1.0 updating rest from json")
        try:
            state.layouts = qmk_json_to_layouts(state.json_field)
        except Exception as exc:
            handle_exception(st, "Failed to parse JSON", exc)
        else:
            state.need_update = True


def dts_column() -> None:
    """Contents of the DTS column."""
    st.subheader(
        "ZMK DTS",
        anchor=False,
    )
    with st.container(height=45, border=False):
        st.caption(
            "Physical layout in ZMK [physical layout specification :material/open_in_new:]"
            "(https://zmk.dev/docs/development/hardware-integration/physical-layouts#optional-keys-property) format."
        )
    if state.need_update:
        state.dts_field = layouts_to_dts(state.layouts)
    st.text_area("Devicetree", key="dts_field", height=800, label_visibility="collapsed")
    dts_button = st.button("⬅️Update JSON using this", use_container_width=True)
    if dts_button:
        print("2.1 updating rest from dts")
        try:
            state.layouts = dts_to_layouts(state.dts_field)
        except Exception as exc:
            handle_exception(st, "Failed to parse DTS", exc)
        else:
            state.need_update = True


def svg_column() -> None:
    """Contents of the SVG column."""
    st.subheader("Visualization", anchor=False)
    svgs = {name: layout_to_svg(layout) for name, layout in state.layouts.items()}
    shown = st.selectbox(label="Select", label_visibility="collapsed", options=list(svgs))
    st.image(svgs[shown])


def main() -> None:
    """Main body of the web app."""
    st.set_page_config(page_title="ZMK physical layout converter", page_icon=":keyboard:", layout="wide")
    st.html('<style>textarea[class^="st-"] { font-family: monospace; font-size: 12px; }</style>')
    st.header("ZMK physical layouts converter", anchor=False)
    st.caption("Tool to convert and visualize physical layout representations for ZMK Studio")

    if "need_update" not in state:
        state.need_update = False

    updated = state.need_update

    if layout_json := st.query_params.get("layout"):
        state.layouts = qmk_json_to_layouts(decode_permalink_param(layout_json))
        state.need_update = True
        print("0.0 read json from query params")
        st.query_params.clear()
        st.rerun()

    if "layouts" not in state:
        state.layouts = qmk_json_to_layouts(_get_initial_layout())
        state.need_update = True

    with st.container(horizontal=True):
        with st.popover("Initialize from ortho params", use_container_width=True):
            ortho_layout = _ortho_form()
            if ortho_layout is not None:
                state.layouts = ortho_layout
                state.need_update = True
                ortho_layout = None

        with st.popover("Initialize from ZMK shared layouts", use_container_width=True):
            st.write("Choose one of the shared layouts in ZMK as a starting point to edit.")
            st.write(
                ":warning: If you can use the layouts without modifications, prefer `#include`ing them in your config. "
                "See [`corne`](https://github.com/zmkfirmware/zmk/blob/main/app/boards/shields/corne/corne.dtsi#L9-L18) "
                "or [`bt60`](https://github.com/zmkfirmware/zmk/blob/main/app/boards/arm/bt60/bt60_v1.dts#L9-L121) as examples."
            )
            shared_layouts = get_shared_layouts()
            with st.form("shared_layouts"):
                selected = st.selectbox("Shared layouts", list(shared_layouts))
                if st.form_submit_button("Use this") and selected is not None:
                    state.layouts = shared_layouts[selected]
                    state.need_update = True

        if st.button("Edit with dataframe editor", use_container_width=True):
            df_editor()

        st.link_button("Tool to edit position maps :material/open_in_new:", "https://zmk-layout-helper.netlify.app/")

    json_col, dts_col, svg_col = st.columns([0.25, 0.4, 0.35], vertical_alignment="top")

    with json_col:
        json_column()

    with dts_col:
        dts_column()

    with svg_col:
        svg_column()

    permabutton = st.button(label="Generate permalink to layout")
    if permabutton:
        show_permalink()

    if updated:
        state.need_update = False

    if state.need_update:
        st.rerun()


if __name__ == "__main__":
    main()
