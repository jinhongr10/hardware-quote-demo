# 定制五金报价系统 Demo（Streamlit）

最小可运行版本：读取 `data.json`，计算并展示报价明细。

## 环境要求
- Python 3.9+
- 本仓库文件：`app.py`、`data.json`、`requirements.txt`

## 安装依赖
```bash
python -m venv .venv
source .venv/bin/activate  # Windows 使用 .venv\\Scripts\\activate
pip install -r requirements.txt
```

## 运行
```bash
streamlit run app.py
```

启动后浏览器会打开 `http://localhost:8501`。

## 演示指引
- 首页展示 `data.json` 的示例数据，并按样例公式生成报价字段。
- 修改 `data.json`（新增条目或调整费率、数量）后，重新运行即可实时查看变化。
- 若想展示批量导入或公式配置，可在 `app.py` 中添加表单或上传组件，这个 Demo 版本已预留数据字段。
