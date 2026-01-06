# 定制五金报价系统 Demo 设计说明

## 报价公式（基于样例）
- `material_total = unit_material_cost * quantity`
- `labor_total = unit_labor_cost * quantity`
- `overhead = (material_total + labor_total) * overhead_pct`
- `subtotal = material_total + labor_total + overhead`
- `profit = subtotal * profit_pct`
- `tax = (subtotal + profit) * tax_pct`
- `quote_total = subtotal + profit + tax`

## 字段含义
- `id`：物料编码或SKU
- `name`：名称
- `category`：品类
- `material`：材质/规格描述
- `unit_material_cost`：单件材料成本
- `unit_labor_cost`：单件人工成本
- `overhead_pct`：制造费用率（相对于材料+人工）
- `profit_pct`：利润率
- `tax_pct`：增值税率（作用在含利润的小计上）
- `quantity`：数量
- 派生字段：`material_total`、`labor_total`、`overhead`、`subtotal`、`profit`、`tax`、`quote_total`

## 可扩展点
- 加入前端交互：表单录入/编辑、批量导入 Excel、字段校验、下载报价单。
- 参数化公式：按客户或产品线配置费率、不同税率，支持多币种和汇率换算。
- 版本管理：记录历史报价、对比差异、导出 PDF。
- 权限与审批：角色分级、审批流、日志审计。
- 集成：对接 ERP/MES/CRM，自动同步物料与工艺路线。
