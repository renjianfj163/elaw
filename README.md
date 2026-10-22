# 环评报告合规核查

上传一份核技术利用类环评报告表，只对照依据库里已核验的文件，输出核对表。辅助预审，不构成法律意见。

规格在 `SPEC.md`。`hospital-eia-compliance-spec.md` 已废止。依据原文由使用者放入 `data/basis/`，不采集、不把种子清单当成事实。当前仓库里的 PostgreSQL 与法规采集代码是旧方案，后续里程碑按 `SPEC.md` 第 9 节替换，不要继续沿用。

## 在 Windows 上打开

给别人用的安装包由 `packaging/build-windows.ps1` 生成，里面带 Python 3.11.9。解压后双击 `启动（无需Docker）.bat`，不用另装 Python。旧版 Word（.doc）仍要本机有 LibreOffice。带文字层的 PDF 可以直接上传，扫描件请先另存为 .docx。依据原文和模型密钥不在包里。

从本仓库开发时，目录里没有这份 Python，脚本会改用本机的 Python 3.11 建立虚拟环境。

已经安装 Docker Desktop 时，可以改用 `启动（Docker）.bat`。这条路使用 PostgreSQL 和 pgvector，不放进用户安装包。

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
