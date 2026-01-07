from __future__ import annotations

import json
import math
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
    parts: List[dict]
    purchased_items: List[dict]
    packaging_rules: dict
    products: List[dict]


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
            parts=raw.get("parts", []),
            purchased_items=raw.get("purchased_items", []),
            packaging_rules=raw.get("packaging_rules", {}),
            products=raw.get("products", []),
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
        candidate = sorted(tiers, key=lambda t: t["min_qty"])[0]
    if candidate is None:
        return 1.0, None
    return candidate["multiplier"], candidate


def compute_process_costs(
    process_steps: List[dict],
    processes_map: Dict[str, dict],
    qty: int,
    part_code: str,
) -> Tuple[float, List[dict]]:
    total = 0.0
    breakdown: List[dict] = []
    for step in process_steps:
        if not step.get("enabled", True):
            continue
        code = step.get("process_code")
        meta = processes_map.get(code, {})
        minutes = step.get("minutes_per_unit", 0.0)
        rate_per_min = meta.get("unit_rate_per_min", 0.0)
        setup_cost = meta.get("setup_cost", 0.0)
        runtime_cost = minutes * rate_per_min * qty
        process_total = runtime_cost + setup_cost
        total += process_total
        breakdown.append(
            {
                "part_code": part_code,
                "process_code": code,
                "name": meta.get("name", code),
                "minutes_per_unit": minutes,
                "rate_per_min": rate_per_min,
                "qty": qty,
                "runtime_cost": runtime_cost,
                "setup_cost": setup_cost,
                "total_cost": process_total,
            }
        )
    return total, breakdown


def compute_sheet_layout(
    sheet_option: dict,
    part: dict,
    qty: int,
    pieces_override: Optional[int],
) -> dict:
    sheet_length = sheet_option.get("sheet_length_mm", 0.0)
    sheet_width = sheet_option.get("sheet_width_mm", 0.0)
    edge_margin = float(part.get("edge_margin_mm", 10))
    kerf = float(part.get("kerf_mm", 2))
    allow_rotate = bool(part.get("allow_rotate", True))
    nest_efficiency = float(part.get("nest_efficiency", 0.85))
    blank_length = float(part.get("blank_length_mm", 0.0))
    blank_width = float(part.get("blank_width_mm", 0.0))

    usable_L = max(sheet_length - 2 * edge_margin, 0.0)
    usable_W = max(sheet_width - 2 * edge_margin, 0.0)
    pitch_L = blank_length + kerf
    pitch_W = blank_width + kerf

    nx_a = math.floor(usable_L / pitch_L) if pitch_L > 0 else 0
    ny_a = math.floor(usable_W / pitch_W) if pitch_W > 0 else 0
    count_a = nx_a * ny_a

    nx_b = 0
    ny_b = 0
    count_b = 0
    if allow_rotate:
        nx_b = math.floor(usable_L / pitch_W) if pitch_W > 0 else 0
        ny_b = math.floor(usable_W / pitch_L) if pitch_L > 0 else 0
        count_b = nx_b * ny_b

    raw_count = max(count_a, count_b)
    pieces_calc = max(1, math.floor(raw_count * nest_efficiency)) if raw_count > 0 else 1
    pieces_per_sheet = pieces_override if pieces_override and pieces_override > 0 else pieces_calc
    sheets_needed = math.ceil(qty / pieces_per_sheet) if pieces_per_sheet > 0 else qty

    return {
        "count_a": count_a,
        "count_b": count_b,
        "raw_count": raw_count,
        "pieces_per_sheet_calc": pieces_calc,
        "pieces_per_sheet": pieces_per_sheet,
        "sheets_needed": sheets_needed,
    }


def evaluate_sheet_options(
    sheet_options: List[dict],
    part: dict,
    qty: int,
) -> Tuple[List[dict], Optional[dict]]:
    if not sheet_options:
        return [], None
    target_thickness = float(part.get("thickness_mm", 0.0))
    diffs = [abs(float(opt.get("thickness_mm", 0.0)) - target_thickness) for opt in sheet_options]
    min_diff = min(diffs) if diffs else 0.0
    candidates = [opt for opt in sheet_options if abs(float(opt.get("thickness_mm", 0.0)) - target_thickness) == min_diff]

    rows: List[dict] = []
    for opt in candidates:
        calc = compute_sheet_layout(opt, part, qty, None)
        cost = calc["sheets_needed"] * float(opt.get("sheet_price", 0.0))
        sheet_spec = f'{opt.get("sheet_length_mm")}x{opt.get("sheet_width_mm")}x{opt.get("thickness_mm")}mm'
        rows.append(
            {
                "sheet_spec": sheet_spec,
                "sheet_price": float(opt.get("sheet_price", 0.0)),
                "pieces_per_sheet": calc["pieces_per_sheet"],
                "sheets_needed": calc["sheets_needed"],
                "material_cost": cost,
                "sheet_option": opt,
                "calc": calc,
            }
        )

    rows_sorted = sorted(rows, key=lambda r: r["material_cost"])
    recommended = rows_sorted[0] if rows_sorted else None
    return rows_sorted, recommended


def format_currency(value: float, currency: str) -> str:
    return f"{currency} {value:,.2f}"


# ---------- UI 组件 ----------
def render_master_data_tab(bundle: DataBundle) -> None:
    st.subheader("主数据查看")
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**材料库**")
        df_materials = pd.DataFrame(bundle.materials).rename(
            columns={
                "code": "材料编码",
                "name": "材料名称",
                "category": "类别",
                "unit": "计价单位",
                "density_g_cm3": "密度(g/cm³)",
                "price_per_kg": "单价(kg)",
                "surface_finish": "表面",
                "notes": "备注",
                "pricing_mode": "计价方式",
                "sheet_options": "板材规格",
            }
        )
        st.dataframe(df_materials, use_container_width=True)
    with col2:
        st.markdown("**工艺库**")
        df_processes = pd.DataFrame(bundle.processes).rename(
            columns={
                "code": "工艺编码",
                "name": "工艺名称",
                "unit_rate_per_min": "分钟费率",
                "setup_cost": "开机费",
                "description": "说明",
            }
        )
        st.dataframe(df_processes, use_container_width=True)

    col3, col4 = st.columns(2)
    with col3:
        st.markdown("**自制件库**")
        df_parts = pd.DataFrame(bundle.parts).rename(
            columns={
                "part_code": "自制件编码",
                "name": "名称",
                "material_code": "材料编码",
                "thickness_mm": "板厚(mm)",
                "blank_length_mm": "展开长度(mm)",
                "blank_width_mm": "展开宽度(mm)",
                "allow_rotate": "允许旋转",
                "edge_margin_mm": "边距(mm)",
                "kerf_mm": "切缝(mm)",
                "nest_efficiency": "排版效率",
                "process_steps": "工艺步骤",
            }
        )
        st.dataframe(df_parts, use_container_width=True)
    with col4:
        st.markdown("**外购件库**")
        df_purchased = pd.DataFrame(bundle.purchased_items).rename(
            columns={
                "item_code": "外购件编码",
                "name": "名称",
                "unit_cost": "单价",
                "uom": "单位",
                "waste_pct": "损耗率",
                "moq_qty": "最小采购量",
            }
        )
        st.dataframe(df_purchased, use_container_width=True)

    col5, col6 = st.columns(2)
    with col5:
        st.markdown("**包装规则**")
        df_pack_unit = pd.DataFrame(bundle.packaging_rules.get("per_unit", [])).rename(
            columns={
                "item_code": "包装编码",
                "unit_cost": "单价",
                "qty_per_unit": "单件用量",
            }
        )
        df_pack_carton = pd.DataFrame(bundle.packaging_rules.get("per_carton", [])).rename(
            columns={
                "item_code": "包装编码",
                "unit_cost": "单价",
                "qty_per_carton": "每箱数量",
                "units_per_carton": "每箱装量",
            }
        )
        st.markdown("**按件包装**")
        st.dataframe(df_pack_unit, use_container_width=True)
        st.markdown("**按箱包装**")
        st.dataframe(df_pack_carton, use_container_width=True)
    with col6:
        st.markdown("**成品库**")
        df_products = pd.DataFrame(bundle.products).rename(
            columns={
                "sku": "成品SKU",
                "name": "成品名称",
                "units_per_carton": "每箱装量",
                "bom_lines": "BOM明细",
            }
        )
        st.dataframe(df_products, use_container_width=True)


def render_quote_tab(bundle: DataBundle) -> None:
    settings = bundle.settings
    materials_map = {m["code"]: m for m in bundle.materials}
    processes_map = {p["code"]: p for p in bundle.processes}
    parts_map = {p["part_code"]: p for p in bundle.parts}
    purchased_map = {i["item_code"]: i for i in bundle.purchased_items}
    products = bundle.products

    packaging_rules = bundle.packaging_rules or {}
    packaging_map: Dict[str, dict] = {}
    for rule in packaging_rules.get("per_unit", []):
        packaging_map[rule["item_code"]] = {"type": "per_unit", **rule}
    for rule in packaging_rules.get("per_carton", []):
        packaging_map[rule["item_code"]] = {"type": "per_carton", **rule}

    st.subheader("成品BOM报价")

    if not products:
        st.warning("暂无成品数据")
        return

    product_options = [p["sku"] for p in products]
    product_name_map = {p["sku"]: p["name"] for p in products}
    selected_sku = st.selectbox(
        "选择成品SKU",
        options=product_options,
        format_func=lambda k: f"{k} | {product_name_map.get(k, '')}",
    )
    product = next(p for p in products if p["sku"] == selected_sku)

    info_cols = st.columns(3)
    info_cols[0].info(f"成品名称：{product.get('name', '-')}")
    info_cols[1].info(f"每箱装量：{product.get('units_per_carton', '-')}")
    info_cols[2].info(f"BOM行数：{len(product.get('bom_lines', []))}")

    st.markdown("**订单信息**")
    c1, c2, c3, c4 = st.columns(4)
    quote_no = c1.text_input("报价单号", value="Q-2024-001")
    customer = c2.text_input("客户名称", value="ACME")
    currency = c3.text_input("币种", value=settings.get("currency", "USD"))
    qty = c4.number_input("成品数量", min_value=1, value=100, step=1)

    st.session_state["quote_header"] = {
        "quote_no": quote_no,
        "customer": customer,
        "currency": currency,
    }

    st.markdown("**报价参数**")
    p1, p2, p3 = st.columns(3)
    overhead_pct = p1.number_input(
        "管理费率",
        min_value=0.0,
        max_value=1.0,
        value=float(settings.get("management_fee_pct", 0.05)),
        step=0.01,
        format="%.4f",
    )
    tax_pct = p2.number_input(
        "税率",
        min_value=0.0,
        max_value=1.0,
        value=float(settings.get("tax_pct", 0.13)),
        step=0.01,
        format="%.4f",
    )
    margin_pct = p3.number_input(
        "利润率",
        min_value=0.0,
        max_value=1.0,
        value=float(settings.get("default_profit_pct", 0.18)),
        step=0.01,
        format="%.4f",
    )

    with st.expander("高级设置", expanded=False):
        a1, a2 = st.columns(2)
        scrap_rate = a1.number_input(
            "损耗率（仅重量计价）",
            min_value=0.0,
            max_value=1.0,
            value=float(settings.get("wastage_pct", 0.03)),
            step=0.01,
            format="%.4f",
        )
        pricing_labels = {"gross_margin": "按毛利率", "markup": "按加价率"}
        pricing_mode = a2.selectbox(
            "报价方式",
            options=list(pricing_labels.keys()),
            format_func=lambda k: pricing_labels[k],
        )
        shipping_per_order = st.number_input(
            "订单运费",
            min_value=0.0,
            value=float(settings.get("freight_cost_per_order", 120.0)),
            step=1.0,
            format="%.2f",
        )
    st.session_state["order_shipping"] = shipping_per_order

    bom_rows: List[dict] = []
    process_breakdown: List[dict] = []
    alternatives_export: List[dict] = []
    material_total = 0.0
    process_total = 0.0
    purchased_total = 0.0
    packaging_total = 0.0

    st.markdown("**板材推荐与备选**")
    for idx, line in enumerate(product.get("bom_lines", [])):
        line_type = line.get("type")
        code = line.get("code")
        qty_per_unit = float(line.get("qty_per_unit", 1))
        optional = bool(line.get("optional", False))

        if line_type == "part":
            part = parts_map.get(code)
            if not part:
                st.warning(f"未找到自制件：{code}")
                continue
            part_qty = int(math.ceil(qty * qty_per_unit))
            material = materials_map.get(part.get("material_code", ""), {})
            material_pricing_mode = material.get("pricing_mode", "by_weight")
            sheet_result = None
            alternatives_df = None

            st.markdown(f"**自制件：{part.get('part_code')} | {part.get('name')}**")
            if material_pricing_mode == "by_sheet":
                rows, recommended = evaluate_sheet_options(material.get("sheet_options", []), part, part_qty)
                if recommended:
                    sheet_result = recommended
                    r1, r2, r3, r4, r5 = st.columns(5)
                    r1.metric("推荐规格", recommended["sheet_spec"])
                    r2.metric("板材单价", recommended["sheet_price"])
                    r3.metric("单张可出数", recommended["pieces_per_sheet"])
                    r4.metric("所需张数", recommended["sheets_needed"])
                    r5.metric("材料成本", recommended["material_cost"])

                    alternatives_df = pd.DataFrame(
                        [
                            {
                                "sheet_spec": r["sheet_spec"],
                                "sheet_price": r["sheet_price"],
                                "pieces_per_sheet": r["pieces_per_sheet"],
                                "sheets_needed": r["sheets_needed"],
                                "material_cost": r["material_cost"],
                            }
                            for r in rows
                        ]
                    ).rename(
                        columns={
                            "sheet_spec": "板材规格",
                            "sheet_price": "板材单价",
                            "pieces_per_sheet": "单张可出数",
                            "sheets_needed": "所需张数",
                            "material_cost": "材料成本",
                        }
                    )
                    st.dataframe(alternatives_df, use_container_width=True)

                    alternatives_export.extend(
                        [
                            {
                                "part_code": part.get("part_code"),
                                "sheet_spec": r["sheet_spec"],
                                "sheet_price": r["sheet_price"],
                                "pieces_per_sheet": r["pieces_per_sheet"],
                                "sheets_needed": r["sheets_needed"],
                                "material_cost": r["material_cost"],
                            }
                            for r in rows
                        ]
                    )

                    with st.expander(f"工程覆盖 - {part.get('part_code')}", expanded=False):
                        override_enabled = st.checkbox("启用板材覆盖", value=False, key=f"override_{idx}")
                        if override_enabled:
                            option_labels = [r["sheet_spec"] for r in rows]
                            selected = st.selectbox(
                                "选择板材规格",
                                options=option_labels,
                                key=f"sheet_select_{idx}",
                            )
                            selected_row = next(r for r in rows if r["sheet_spec"] == selected)
                            pieces_override = st.number_input(
                                "覆盖单张可出数",
                                min_value=1,
                                value=int(selected_row["pieces_per_sheet"]),
                                step=1,
                                key=f"pieces_override_{idx}",
                            )
                            calc = compute_sheet_layout(
                                selected_row["sheet_option"], part, part_qty, int(pieces_override)
                            )
                            cost = calc["sheets_needed"] * float(selected_row["sheet_option"].get("sheet_price", 0.0))
                            sheet_result = {
                                "sheet_spec": selected_row["sheet_spec"],
                                "sheet_price": selected_row["sheet_price"],
                                "pieces_per_sheet": calc["pieces_per_sheet"],
                                "sheets_needed": calc["sheets_needed"],
                                "material_cost": cost,
                            }
                else:
                    st.warning(f"自制件 {code} 未配置板材规格")
            else:
                st.info("该材料为重量计价，未启用板材推荐")

            if sheet_result:
                material_cost = float(sheet_result.get("material_cost", 0.0))
                sheet_spec = sheet_result.get("sheet_spec")
                pieces_per_sheet = sheet_result.get("pieces_per_sheet")
                sheets_needed = sheet_result.get("sheets_needed")
            else:
                material_cost = 0.0
                sheet_spec = None
                pieces_per_sheet = None
                sheets_needed = None

            process_cost, process_rows = compute_process_costs(
                part.get("process_steps", []),
                processes_map,
                part_qty,
                part.get("part_code"),
            )
            process_breakdown.extend(process_rows)

            material_total += material_cost
            process_total += process_cost

            line_total = material_cost + process_cost
            bom_rows.append(
                {
                    "line_type": "part",
                    "code": part.get("part_code"),
                    "name": part.get("name"),
                    "qty_total": part_qty,
                    "uom": "pc",
                    "unit_cost": line_total / part_qty if part_qty else 0.0,
                    "line_total": line_total,
                    "material_cost": material_cost,
                    "process_cost": process_cost,
                    "sheet_spec": sheet_spec,
                    "pieces_per_sheet": pieces_per_sheet,
                    "sheets_needed": sheets_needed,
                    "optional": optional,
                }
            )

        elif line_type == "purchased":
            item = purchased_map.get(code)
            if not item:
                st.warning(f"未找到外购件：{code}")
                continue
            base_qty = qty * qty_per_unit
            waste_pct = float(item.get("waste_pct", 0.0))
            moq_qty = item.get("moq_qty")
            total_qty = max(base_qty, moq_qty) if moq_qty else base_qty
            line_total = total_qty * float(item.get("unit_cost", 0.0)) * (1 + waste_pct)
            purchased_total += line_total
            bom_rows.append(
                {
                    "line_type": "purchased",
                    "code": item.get("item_code"),
                    "name": item.get("name"),
                    "qty_total": total_qty,
                    "uom": item.get("uom"),
                    "unit_cost": item.get("unit_cost"),
                    "line_total": line_total,
                    "material_cost": 0.0,
                    "process_cost": 0.0,
                    "sheet_spec": None,
                    "pieces_per_sheet": None,
                    "sheets_needed": None,
                    "optional": optional,
                }
            )

        elif line_type == "packaging":
            rule = packaging_map.get(code)
            if not rule:
                st.warning(f"未找到包装规则：{code}")
                continue
            if rule["type"] == "per_unit":
                qty_per_unit_rule = float(line.get("qty_per_unit", rule.get("qty_per_unit", 1)))
                total_qty = qty * qty_per_unit_rule
                line_total = total_qty * float(rule.get("unit_cost", 0.0))
            else:
                units_per_carton = rule.get("units_per_carton") or product.get("units_per_carton")
                qty_per_carton = float(rule.get("qty_per_carton", 1))
                cartons = math.ceil(qty / units_per_carton) if units_per_carton else 0
                total_qty = cartons * qty_per_carton
                line_total = total_qty * float(rule.get("unit_cost", 0.0))
            packaging_total += line_total
            bom_rows.append(
                {
                    "line_type": "packaging",
                    "code": rule.get("item_code"),
                    "name": rule.get("item_code"),
                    "qty_total": total_qty,
                    "uom": "pack",
                    "unit_cost": rule.get("unit_cost"),
                    "line_total": line_total,
                    "material_cost": 0.0,
                    "process_cost": 0.0,
                    "sheet_spec": None,
                    "pieces_per_sheet": None,
                    "sheets_needed": None,
                    "optional": optional,
                }
            )

    shipping_cost = shipping_per_order
    subtotal = material_total + process_total + purchased_total + packaging_total + shipping_cost
    overhead = subtotal * overhead_pct
    pre_tax = subtotal + overhead
    tax = pre_tax * tax_pct
    total_cost = pre_tax + tax

    if pricing_mode == "gross_margin":
        final_price_total = total_cost / (1 - margin_pct) if margin_pct < 1 else total_cost
    else:
        final_price_total = total_cost * (1 + margin_pct)

    multiplier, matched_tier = find_multiplier(bundle.quantity_tiers, int(qty))
    final_price_total *= multiplier
    unit_price = final_price_total / qty if qty else 0.0

    st.markdown("**BOM明细**")
    df_bom = pd.DataFrame(bom_rows).rename(
        columns={
            "line_type": "类型",
            "code": "物料编码",
            "name": "名称",
            "qty_total": "数量",
            "uom": "单位",
            "unit_cost": "单价",
            "line_total": "行总价",
            "material_cost": "材料成本",
            "process_cost": "工艺成本",
            "sheet_spec": "推荐板材",
            "pieces_per_sheet": "单张可出数",
            "sheets_needed": "所需张数",
            "optional": "可选项",
        }
    )
    st.dataframe(df_bom, use_container_width=True)

    st.markdown("**成本拆分**")
    metric_items = [
        ("材料成本", material_total),
        ("工艺成本", process_total),
        ("外购件成本", purchased_total),
        ("包装成本", packaging_total),
        ("运费", shipping_cost),
        ("管理费", overhead),
        ("税额", tax),
        ("总成本", total_cost),
        ("报价总额", final_price_total),
        ("单价", unit_price),
    ]
    metric_cols = st.columns(3)
    for i, (label, value) in enumerate(metric_items):
        metric_cols[i % 3].metric(label, format_currency(value, currency))

    if matched_tier:
        st.info(
            f"数量档位：{matched_tier.get('label', '')} | 价格系数={matched_tier.get('multiplier')} "
            f"(min_qty={matched_tier.get('min_qty')}, max_qty={matched_tier.get('max_qty')})"
        )

    st.markdown("**工艺明细**")
    if process_breakdown:
        df_process = pd.DataFrame(process_breakdown).rename(
            columns={
                "part_code": "自制件编码",
                "process_code": "工艺编码",
                "name": "工艺名称",
                "minutes_per_unit": "单件工时(分钟)",
                "rate_per_min": "分钟费率",
                "qty": "数量",
                "runtime_cost": "工时费用",
                "setup_cost": "开机费",
                "total_cost": "工艺合计",
            }
        )
        st.dataframe(df_process, use_container_width=True)
    else:
        st.warning("未产生任何工艺费用")

    if st.button("➕ 添加到报价单"):
        line_id = len(st.session_state.get("quote_lines", [])) + 1
        line = {
            "line_id": line_id,
            "sku": product.get("sku"),
            "product_name": product.get("name"),
            "qty": int(qty),
            "material_code": "-",
            "unit_price": unit_price,
            "line_total": final_price_total,
            "cost_total": total_cost,
            "margin_pct": margin_pct,
            "tax_pct": tax_pct,
            "overhead_pct": overhead_pct,
            "scrap_rate": scrap_rate,
            "process_summary": "BOM",
            "process_cost": process_total,
            "material_cost": material_total,
            "packaging_cost": packaging_total,
            "shipping_alloc": 0.0,
        }
        st.session_state["quote_lines"].append(line)
        st.success("已添加到报价单")

    st.markdown("**导出报价明细**")
    header_df = pd.DataFrame(
        [
            {"field": "Quote No", "value": quote_no},
            {"field": "Customer", "value": customer},
            {"field": "Currency", "value": currency},
            {"field": "Product SKU", "value": product.get("sku")},
            {"field": "Product Name", "value": product.get("name")},
            {"field": "Quantity", "value": qty},
            {"field": "Total Material Cost", "value": material_total},
        ]
    )

    summary_df = pd.DataFrame(
        [
            {"item": "material_total", "value": material_total},
            {"item": "process_total", "value": process_total},
            {"item": "purchased_total", "value": purchased_total},
            {"item": "packaging_total", "value": packaging_total},
            {"item": "shipping_cost", "value": shipping_cost},
            {"item": "overhead", "value": overhead},
            {"item": "tax", "value": tax},
            {"item": "total_cost", "value": total_cost},
            {"item": "final_price_total", "value": final_price_total},
            {"item": "unit_price", "value": unit_price},
            {"item": "multiplier", "value": multiplier},
            {"item": "pricing_mode", "value": pricing_mode},
            {"item": "margin_pct", "value": margin_pct},
        ]
    )

    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        header_df.to_excel(writer, sheet_name="Quote_Header", index=False)
        summary_df.to_excel(writer, sheet_name="Cost_Summary", index=False)
        pd.DataFrame(bom_rows).to_excel(writer, sheet_name="BOM_Breakdown", index=False)
        pd.DataFrame(process_breakdown).to_excel(writer, sheet_name="Process_Breakdown", index=False)
        if alternatives_export:
            pd.DataFrame(alternatives_export).to_excel(writer, sheet_name="Sheet_Alternatives", index=False)
    buffer.seek(0)

    st.download_button(
        label="下载单品报价 Excel",
        data=buffer,
        file_name=f"{quote_no or 'quote'}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    st.markdown("**当前报价单明细表**")
    quote_lines = st.session_state.get("quote_lines", [])
    df_lines = pd.DataFrame(quote_lines).rename(
        columns={
            "line_id": "行号",
            "sku": "SKU",
            "product_name": "成品名称",
            "qty": "数量",
            "material_code": "材料编码",
            "unit_price": "单价",
            "line_total": "行总价",
            "cost_total": "成本合计",
            "margin_pct": "利润率",
            "tax_pct": "税率",
            "overhead_pct": "管理费率",
            "scrap_rate": "损耗率",
            "process_summary": "工艺汇总",
            "process_cost": "工艺成本",
            "material_cost": "材料成本",
            "packaging_cost": "包装成本",
            "shipping_alloc": "运费分摊",
        }
    )
    st.dataframe(df_lines, use_container_width=True)

    if quote_lines:
        lines_subtotal = sum(float(line.get("line_total", 0.0)) for line in quote_lines)
        order_shipping = float(st.session_state.get("order_shipping", 0.0))
        final_total = lines_subtotal + order_shipping
        header_currency = st.session_state["quote_header"].get("currency", "")

        m1, m2, m3 = st.columns(3)
        m1.metric("行小计", format_currency(lines_subtotal, header_currency))
        m2.metric("订单运费", format_currency(order_shipping, header_currency))
        m3.metric("订单总计", format_currency(final_total, header_currency))

        st.markdown("**删除报价行**")
        line_id_options = [line.get("line_id") for line in quote_lines]
        line_label_map = {line.get("line_id"): f'{line.get("line_id")} | {line.get("product_name")} | qty={line.get("qty")}' for line in quote_lines}
        selected_line = st.selectbox(
            "选择要删除的行",
            options=line_id_options,
            format_func=lambda k: line_label_map.get(k, str(k)),
        )
        if st.button("删除该行"):
            st.session_state["quote_lines"] = [l for l in st.session_state["quote_lines"] if l.get("line_id") != selected_line]
            st.success(f"已删除报价行 {selected_line}")

        st.markdown("**下载整张报价单 Excel**")
        header_df = pd.DataFrame(
            [
                {
                    "quote_no": st.session_state["quote_header"].get("quote_no", ""),
                    "customer": st.session_state["quote_header"].get("customer", ""),
                    "currency": header_currency,
                    "order_shipping": order_shipping,
                    "lines_subtotal": lines_subtotal,
                    "final_total": final_total,
                }
            ]
        )
        lines_columns = [
            "line_id",
            "sku",
            "product_name",
            "qty",
            "unit_price",
            "line_total",
            "material_cost",
            "process_cost",
            "packaging_cost",
            "cost_total",
            "process_summary",
        ]
        lines_df = pd.DataFrame(quote_lines)
        lines_df = lines_df.reindex(columns=lines_columns)

        buffer_quote = BytesIO()
        with pd.ExcelWriter(buffer_quote, engine="openpyxl") as writer:
            header_df.to_excel(writer, sheet_name="Quote_Header", index=False)
            lines_df.to_excel(writer, sheet_name="Quote_Lines", index=False)
        buffer_quote.seek(0)

        st.download_button(
            label="下载整张报价单 Excel",
            data=buffer_quote,
            file_name=f"{st.session_state['quote_header'].get('quote_no') or 'quote_cart'}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    else:
        st.info("请先添加产品再导出整张报价单。")


# ---------- 主入口 ----------
def main() -> None:
    st.set_page_config(page_title="定制五金报价系统 Demo", layout="wide")
    st.title("定制五金报价系统 Demo（成品BOM驱动）")

    if "quote_lines" not in st.session_state:
        st.session_state["quote_lines"] = []
    if "quote_header" not in st.session_state:
        st.session_state["quote_header"] = {"quote_no": "", "customer": "", "currency": ""}
    if "order_shipping" not in st.session_state:
        st.session_state["order_shipping"] = 0.0

    data_path = Path(__file__).parent / "data.json"
    bundle = load_data(data_path)
    if not bundle:
        return

    tab1, tab2 = st.tabs(["主数据查看", "报价计算与导出"])
    with tab1:
        render_master_data_tab(bundle)
    with tab2:
        if st.button("清空当前报价单"):
            st.session_state["quote_lines"] = []
            st.session_state["quote_header"] = {"quote_no": "", "customer": "", "currency": ""}
            st.session_state["order_shipping"] = 0.0
        render_quote_tab(bundle)


if __name__ == "__main__":
    main()
