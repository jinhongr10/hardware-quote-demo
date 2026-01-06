from __future__ import annotations

import json
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st


# ---------- 数据加载 ----------
@dataclass
class DataBundle:
    settings: dict
    materials: List[dict]
    processes: List[dict]
    quantity_tiers: List[dict]
    product_templates: List[dict]


def load_data(path: Path) -> Optional[DataBundle]:
    if not path.exists():
        st.error(f"数据文件未找到: {path}")
        return None
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    try:
        return DataBundle(
            settings=raw.get("settings", {}),
            materials=raw.get("materials", []),
            processes=raw.get("processes", []),
            quantity_tiers=raw.get("quantity_tiers", []),
            product_templates=raw.get("product_templates", []),
        )
    except Exception as exc:  # pragma: no cover - defensive
        st.error(f"数据解析失败: {exc}")
        return None


# ---------- 计算逻辑 ----------
def find_multiplier(tiers: List[dict], qty: int) -> Tuple[float, Optional[dict]]:
    candidate = None
    for tier in sorted(tiers, key=lambda t: t["min_qty"]):
        min_q = tier["min_qty"]
        max_q = tier.get("max_qty")
        if qty >= min_q and (max_q is None or qty <= max_q):
            candidate = tier
    if candidate is None and tiers:
        # 选择 min_qty 最小的档位作为兜底
        candidate = sorted(tiers, key=lambda t: t["min_qty"])[0]
    if candidate is None:
        return 1.0, None
    return candidate["multiplier"], candidate


def compute_process_costs(
    template_processes: List[dict],
    processes_map: Dict[str, dict],
    qty: int,
    user_inputs: Dict[str, dict],
) -> Tuple[float, List[dict]]:
    total = 0.0
    breakdown: List[dict] = []
    for proc in template_processes:
        code = proc["process_code"]
        meta = processes_map.get(code)
        if not meta:
            continue
        ui = user_inputs.get(code, {})
        if not ui.get("enabled", True):
            continue
        basis = ui.get("basis", "per_hour")
        minutes = ui.get("minutes", proc.get("minutes", 0.0))
        rate_per_min = meta.get("unit_rate_per_min", 0.0)
        setup_cost = meta.get("setup_cost", 0.0)

        if basis == "fixed":
            runtime_cost = 0.0
        else:
            runtime_cost = minutes * rate_per_min * qty

        process_total = runtime_cost + setup_cost
        total += process_total
        breakdown.append(
            {
                "process_code": code,
                "name": meta.get("name", code),
                "basis": basis,
                "minutes_per_unit": minutes,
                "rate_per_min": rate_per_min,
                "qty": qty if basis != "fixed" else 0,
                "runtime_cost": runtime_cost,
                "setup_cost": setup_cost,
                "total_cost": process_total,
            }
        )
    return total, breakdown


def calculate_quote(
    template: dict,
    qty: int,
    scrap_rate: float,
    overhead_pct: float,
    tax_pct: float,
    margin_pct: float,
    pricing_mode: str,
    packaging_per_piece: float,
    shipping_per_order: float,
    materials_map: Dict[str, dict],
    processes_map: Dict[str, dict],
    tiers: List[dict],
    process_inputs: Dict[str, dict],
) -> Tuple[dict, List[dict], Optional[dict]]:
    material = materials_map.get(template["material_code"], {})
    material_price = material.get("price_per_kg", 0.0)
    weight = template.get("weight_kg_per_unit", 0.0)
    material_cost = qty * weight * material_price * (1 + scrap_rate)

    process_cost, process_breakdown = compute_process_costs(
        template.get("default_processes", []),
        processes_map,
        qty,
        process_inputs,
    )

    packaging_cost = qty * packaging_per_piece
    shipping_cost = shipping_per_order
    subtotal = material_cost + process_cost + packaging_cost + shipping_cost
    overhead = subtotal * overhead_pct
    pre_tax = subtotal + overhead
    tax = pre_tax * tax_pct
    total_cost = pre_tax + tax

    if pricing_mode == "gross_margin":
        final_price_total = total_cost / (1 - margin_pct) if margin_pct < 1 else total_cost
    else:  # markup
        final_price_total = total_cost * (1 + margin_pct)

    multiplier, matched_tier = find_multiplier(tiers, qty)
    final_price_total *= multiplier
    unit_price = final_price_total / qty if qty else 0.0

    summary = {
        "material_cost": material_cost,
        "process_cost": process_cost,
        "packaging_cost": packaging_cost,
        "shipping_cost": shipping_cost,
        "overhead": overhead,
        "tax": tax,
        "subtotal": subtotal,
        "pre_tax": pre_tax,
        "total_cost": total_cost,
        "final_price_total": final_price_total,
        "unit_price": unit_price,
        "multiplier": multiplier,
    }
    return summary, process_breakdown, matched_tier


def format_currency(value: float, currency: str) -> str:
    return f"{currency} {value:,.2f}"


# ---------- UI 组件 ----------
def render_master_data_tab(bundle: DataBundle) -> None:
    st.subheader("主数据查看")
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Materials**")
        st.dataframe(pd.DataFrame(bundle.materials), use_container_width=True)
    with col2:
        st.markdown("**Processes**")
        st.dataframe(pd.DataFrame(bundle.processes), use_container_width=True)

    col3, col4 = st.columns(2)
    with col3:
        st.markdown("**Product Templates**")
        st.dataframe(pd.DataFrame(bundle.product_templates), use_container_width=True)
    with col4:
        st.markdown("**Quantity Tiers**")
        st.dataframe(pd.DataFrame(bundle.quantity_tiers), use_container_width=True)


def render_quote_tab(bundle: DataBundle) -> None:
    settings = bundle.settings
    materials_map = {m["code"]: m for m in bundle.materials}
    processes_map = {p["code"]: p for p in bundle.processes}
    templates = bundle.product_templates

    st.subheader("报价计算 + 导出")

    if not templates:
        st.warning("暂无 product_templates 数据")
        return

    template_options = {
        f'{t["sku"]} | {t["name"]}': t for t in templates
    }
    selected_label = st.selectbox("选择产品模板 (sku | name)", list(template_options.keys()))
    template = template_options[selected_label]

    info_cols = st.columns(3)
    info_cols[0].info(f"材料: {template.get('material_code', '-')}")
    info_cols[1].info(f"重量(kg/pc): {template.get('weight_kg_per_unit', 0)}")
    info_cols[2].info(f"默认工艺数: {len(template.get('default_processes', []))}")

    st.markdown("**基础信息**")
    c1, c2, c3, c4 = st.columns(4)
    quote_no = c1.text_input("Quote No", value="Q-2024-001")
    customer = c2.text_input("Customer", value="ACME")
    currency = c3.text_input("Currency", value=settings.get("currency", "USD"))
    qty = c4.number_input("Quantity", min_value=1, value=100, step=1)

    st.markdown("**参数调整**")
    p1, p2, p3, p4 = st.columns(4)
    scrap_rate = p1.number_input(
        "scrap_rate", min_value=0.0, max_value=1.0, value=float(settings.get("wastage_pct", 0.03)), step=0.01, format="%.4f"
    )
    overhead_pct = p2.number_input(
        "overhead_pct", min_value=0.0, max_value=1.0, value=float(settings.get("management_fee_pct", 0.05)), step=0.01, format="%.4f"
    )
    tax_pct = p3.number_input(
        "tax_pct", min_value=0.0, max_value=1.0, value=float(settings.get("tax_pct", 0.13)), step=0.01, format="%.4f"
    )
    margin_pct = p4.number_input(
        "margin_pct", min_value=0.0, max_value=1.0, value=float(settings.get("default_profit_pct", 0.18)), step=0.01, format="%.4f"
    )

    p5, p6, p7 = st.columns(3)
    pricing_mode = p5.selectbox("pricing_mode", options=["gross_margin", "markup"])
    packaging_per_piece = p6.number_input(
        "packaging_per_piece", min_value=0.0, value=float(settings.get("packaging_cost_per_unit", 0.6)), step=0.1, format="%.4f"
    )
    shipping_per_order = p7.number_input(
        "shipping_per_order", min_value=0.0, value=float(settings.get("freight_cost_per_order", 120.0)), step=1.0, format="%.2f"
    )

    st.markdown("**工艺列表（可勾选/调整工时）**")
    user_process_inputs: Dict[str, dict] = {}
    for idx, proc in enumerate(template.get("default_processes", [])):
        code = proc["process_code"]
        meta = processes_map.get(code, {})
        with st.expander(f"{meta.get('name', code)} | {code}", expanded=True):
            row = st.columns(5)
            enabled = row[0].checkbox("启用", value=True, key=f"enable_{code}_{idx}")
            basis = row[1].selectbox(
                "basis", options=["per_hour", "per_piece", "fixed"], index=0, key=f"basis_{code}_{idx}"
            )
            minutes = row[2].number_input(
                "minutes_per_unit",
                min_value=0.0,
                value=float(proc.get("minutes", 0.0)),
                step=0.1,
                format="%.2f",
                key=f"minutes_{code}_{idx}",
            )
            row[3].write(f"rate_per_min: {meta.get('unit_rate_per_min', 0)}")
            row[4].write(f"setup_fee: {meta.get('setup_cost', 0)}")
            user_process_inputs[code] = {
                "enabled": enabled,
                "basis": basis,
                "minutes": minutes,
            }

    summary, process_breakdown, matched_tier = calculate_quote(
        template=template,
        qty=int(qty),
        scrap_rate=scrap_rate,
        overhead_pct=overhead_pct,
        tax_pct=tax_pct,
        margin_pct=margin_pct,
        pricing_mode=pricing_mode,
        packaging_per_piece=packaging_per_piece,
        shipping_per_order=shipping_per_order,
        materials_map=materials_map,
        processes_map=processes_map,
        tiers=bundle.quantity_tiers,
        process_inputs=user_process_inputs,
    )

    st.markdown("**成本拆分**")
    metric_items = [
        ("材料成本", summary["material_cost"]),
        ("工艺成本", summary["process_cost"]),
        ("包装成本", summary["packaging_cost"]),
        ("运费", summary["shipping_cost"]),
        ("制造费用", summary["overhead"]),
        ("税", summary["tax"]),
        ("Total Cost", summary["total_cost"]),
        ("Final Price Total", summary["final_price_total"]),
        ("Unit Price", summary["unit_price"]),
    ]
    metric_cols = st.columns(3)
    for i, (label, value) in enumerate(metric_items):
        col = metric_cols[i % 3]
        col.metric(label, format_currency(value, currency))

    if matched_tier:
        st.info(
            f"数量档位: {matched_tier.get('label', '')} | multiplier={matched_tier.get('multiplier')} "
            f"(min_qty={matched_tier.get('min_qty')}, max_qty={matched_tier.get('max_qty')})"
        )

    st.markdown("**工艺明细**")
    if process_breakdown:
        df_process = pd.DataFrame(process_breakdown)
        st.dataframe(df_process, use_container_width=True)
    else:
        st.warning("未启用任何工艺")

    # 导出 Excel
    st.markdown("**导出 Excel**")
    header_df = pd.DataFrame(
        [
            {"field": "Quote No", "value": quote_no},
            {"field": "Customer", "value": customer},
            {"field": "Currency", "value": currency},
            {"field": "Template SKU", "value": template.get("sku")},
            {"field": "Template Name", "value": template.get("name")},
            {"field": "Quantity", "value": qty},
            {"field": "Material", "value": template.get("material_code")},
        ]
    )

    summary_df = pd.DataFrame(
        [
            {"item": "material_cost", "value": summary["material_cost"]},
            {"item": "process_cost", "value": summary["process_cost"]},
            {"item": "packaging_cost", "value": summary["packaging_cost"]},
            {"item": "shipping_cost", "value": summary["shipping_cost"]},
            {"item": "overhead", "value": summary["overhead"]},
            {"item": "tax", "value": summary["tax"]},
            {"item": "total_cost", "value": summary["total_cost"]},
            {"item": "final_price_total", "value": summary["final_price_total"]},
            {"item": "unit_price", "value": summary["unit_price"]},
            {"item": "multiplier", "value": summary["multiplier"]},
            {"item": "pricing_mode", "value": pricing_mode},
            {"item": "margin_pct", "value": margin_pct},
        ]
    )

    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        header_df.to_excel(writer, sheet_name="Quote_Header", index=False)
        summary_df.to_excel(writer, sheet_name="Cost_Summary", index=False)
        pd.DataFrame(process_breakdown).to_excel(writer, sheet_name="Process_Breakdown", index=False)
    buffer.seek(0)

    st.download_button(
        label="下载报价 Excel",
        data=buffer,
        file_name=f"{quote_no or 'quote'}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---------- 主入口 ----------
def main() -> None:
    st.set_page_config(page_title="定制五金报价系统 Demo", layout="wide")
    st.title("定制五金报价系统 Demo（可算报价）")

    data_path = Path(__file__).parent / "data.json"
    bundle = load_data(data_path)
    if not bundle:
        return

    tab1, tab2 = st.tabs(["主数据查看", "报价计算 + 导出"])
    with tab1:
        render_master_data_tab(bundle)
    with tab2:
        render_quote_tab(bundle)


if __name__ == "__main__":
    main()
