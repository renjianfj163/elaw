# 环评报告合规核查

上传一份核技术利用类环评报告表，只对照依据库里已核验的文件，输出核对表。辅助预审，不构成法律意见。

规格在 `SPEC.md`。`hospital-eia-compliance-spec.md` 已废止。依据原文由使用者放入 `data/basis/`，不采集、不把种子清单当成事实。当前仓库里的 PostgreSQL 与法规采集代码是旧方案，后续里程碑按 `SPEC.md` 第 9 节替换，不要继续沿用。

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
