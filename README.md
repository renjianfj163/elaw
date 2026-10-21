# 医院环评合规预审

单机使用的环保审批预审工具。辅助预审，不构成法律意见。

规格在 `hospital-eia-compliance-spec.md`。种子清单里的法规名称不是已核验的事实，自动采集到的日期和状态码也要对照官网确认后才能用于评估。

## 在 Windows 上打开

解压后双击 `启动（无需Docker）.bat`。浏览器会打开本机工作台：上传法规，或按种子清单去公开网站核验。

已经安装 Docker Desktop 时，可以改用 `启动（Docker）.bat`。这条路使用 PostgreSQL 和 pgvector。

DeepSeek 的密钥放在环境变量 `DEEPSEEK_API_KEY`，不要写进仓库。当前这版的法规核验不调用模型。

## 开发

```bash
cd backend
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
set PYTHONPATH=.
set DATABASE_URL=sqlite://
.venv\Scripts\python -m pytest
```

数值比较在 `backend/app/rules/evaluator.py`。引用校验在 `backend/app/verify/citation.py`。
