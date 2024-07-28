import io
import json
from textwrap import indent

import streamlit as st

from keymap_drawer.draw import KeymapDrawer
from keymap_drawer.config import DrawConfig
from keymap_drawer.physical_layout import layout_factory, QmkLayout, _get_qmk_info
from keymap_drawer.parse.dts import DeviceTree

DTS_TEMPLATE = """\
#include <physical_layouts.dtsi>

/ {{
{pl_nodes}\
}};
"""
PL_TEMPLATE = """\
layout_{idx}: layout_{idx} {{
    compatible = "zmk,physical-layout";
    display-name = "{name}";

    kscan = <&kscan_{idx}>;
    transform = <&matrix_transform_{idx}>;
{keys}\
}};
"""
KEYS_TEMPLATE = """
keys  //                     w   h    x    y     rot   rx   ry
    = {key_attrs_string}
    ;
"""
KEY_TEMPLATE = "<&key_physical_attrs {w:>3d} {h:>3d} {x:>4d} {y:>4d} {rot} {rx:>4d} {ry:>4d}>"
PHYSICAL_ATTR_PHANDLES = {"&key_physical_attrs"}


def dts_to_layouts(dts_str: str) -> dict[str, QmkLayout]:
    dts = DeviceTree(dts_str, None, True)

    bindings_to_position = {
        "key_physical_attrs": lambda bindings: {
            k: int(v.lstrip("(").rstrip(")")) / 100 for k, v in zip(("w", "h", "x", "y", "r", "rx", "ry"), bindings)
        }
    }

    if not (nodes := dts.get_compatible_nodes("zmk,physical-layout")):
        raise ValueError("No zmk,physical-layout nodes found")

    defined_layouts = {node.get_string("display-name"): node.get_phandle_array("keys") for node in nodes}

    out_layouts = {}
    for display_name, position_bindings in defined_layouts.items():
        keys = []
        for binding in position_bindings:
            binding = binding.split()
            assert binding[0].lstrip("&") in bindings_to_position, f"Unrecognized position binding {binding[0]}"
            keys.append(bindings_to_position[binding[0].lstrip("&")](binding[1:]))
        out_layouts[display_name] = QmkLayout(layout=keys)
    return out_layouts


def layout_to_svg(qmk_layout: QmkLayout) -> str:
    physical_layout = qmk_layout.generate(60)
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
    out_layouts = {
        display_name: {"layout": qmk_layout.model_dump(exclude_defaults=True, exclude_unset=True)["layout"]}
        for display_name, qmk_layout in layouts_map.items()
    }
    return json.dumps({"layouts": out_layouts}, indent=2)


def layouts_to_dts(layouts_map: dict[str, QmkLayout]) -> str:
    def rot_to_str(rot: int) -> str:
        rot = int(100 * rot)
        if rot >= 0:
            return f"{rot:>7d}"
        return f"{'(' + str(rot) + ')':>7}"

    pl_nodes = []
    for idx, (name, qmk_spec) in enumerate(layouts_map.items()):
        keys =  KEYS_TEMPLATE.format(
            key_attrs_string="\n    , ".join(
                KEY_TEMPLATE.format(
                    w=int(100 * key.w),
                    h=int(100 * key.h),
                    x=int(100 * key.x),
                    y=int(100 * key.y),
                    rot=rot_to_str(key.r),
                    rx=int(100 * (key.rx or 0)),
                    ry=int(100 * (key.ry or 0)),
                )
                for key in qmk_spec.layout
            )
        )
        pl_nodes.append(PL_TEMPLATE.format(idx=idx, name=name, keys=indent(keys, "    ")))
    return DTS_TEMPLATE.format(pl_nodes=indent("\n".join(pl_nodes), "    "))


def qmk_info_to_layouts(qmk_info_str: str) -> dict[str, QmkLayout]:
    qmk_info = json.loads(qmk_info_str)

    if isinstance(qmk_info, list):
        return {"Default": QmkLayout(layout=qmk_info)}  # shortcut for list-only representation
    return {name: QmkLayout(layout=val["layout"]) for name, val in qmk_info["layouts"].items()}


def ortho_to_layouts(ortho_layout: dict, cols_thumbs_notation: str) -> dict[str, QmkLayout]:
    p_layout = layout_factory(
        DrawConfig(key_w=1, key_h=1, split_gap=1),
        ortho_layout=ortho_layout,
        cols_thumbs_notation=cols_thumbs_notation,
    )
    return {
        "Default": QmkLayout(
            layout=[{"x": key.pos.x, "y": key.pos.y, "w": key.width, "h": key.height} for key in p_layout.keys]
        )
    }


def main() -> None:
    need_rerun = False
    if "layouts" not in st.session_state:
        st.session_state.layouts = None

    st.set_page_config(page_title="ZMK physical layout converter", page_icon=":keyboard:", layout="wide")
    st.html('<style>textarea[class^="st-"] { font-family: monospace; font-size: 12px; }</style>')
    st.header("ZMK physical layouts converter")
    st.caption("Tool to convert and visualize physical layout representations for ZMK Studio")

    json_col, dts_col, svg_col = st.columns([0.25, 0.4, 0.35], vertical_alignment="top")
    with json_col:
        st.subheader(
            "JSON format description",
            help="QMK-like physical layout spec description, similar to `qmk_info_json` option mentioned in the "
            "[docs](https://github.com/caksoylar/keymap-drawer/blob/main/KEYMAP_SPEC.md#qmk-infojson-specification).",
        )
        if new_val := st.session_state.get("json_field_update"):
            st.session_state.json_field = new_val
            st.session_state.json_field_update = None
        st.text_area("JSON layout", key="json_field", height=800, label_visibility="collapsed")
        update_from_json = st.button("Update DTS using this")
        if update_from_json:
            st.session_state.layouts = qmk_info_to_layouts(st.session_state.json_field)
            st.session_state.dts_field = layouts_to_dts(st.session_state.layouts)
    with dts_col:
        st.subheader(
            "ZMK devicetree node",
            help="Docs TBD on the format",
        )
        st.text_area("Devicetree", key="dts_field", height=800, label_visibility="collapsed")
        update_from_dts = st.button("Update JSON using this")

    if update_from_dts:
        st.session_state.layouts = dts_to_layouts(st.session_state.dts_field)
        st.session_state.json_field_update = layouts_to_json(st.session_state.layouts)
        need_rerun = True

    with svg_col:
        st.subheader("Visualization")
        if st.session_state.layouts is not None:
            svgs = {name: layout_to_svg(layout) for name, layout in st.session_state.layouts.items()}
            tabs = st.tabs(list(svgs))
            for i, svg in enumerate(svgs.values()):
                tabs[i].image(svg)

    if need_rerun:
        st.rerun()

if __name__ == "__main__":
    main()
