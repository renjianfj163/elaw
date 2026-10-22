"""命令行：环评报告 .doc/.docx/.pdf → blocks.json。"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from app.parse.convert import convert_doc_to_docx
from app.parse.docx_walk import parse_docx, write_blocks
from app.parse.pdf_text import parse_pdf


def parse_report(source: Path, work_dir: Path | None = None):
    source = source.resolve()
    suffix = source.suffix.lower()
    if suffix == ".pdf":
        return parse_pdf(source)
    if suffix == ".docx":
        return parse_docx(source)
    if work_dir is None:
        # 转换结果放在源文件旁边。系统临时目录上 LibreOffice 有时写不进 docx。
        with tempfile.TemporaryDirectory(prefix="elaw-docx-", dir=source.parent) as temporary:
            docx = convert_doc_to_docx(source, Path(temporary))
            return parse_docx(docx)
    docx = convert_doc_to_docx(source, work_dir)
    return parse_docx(docx)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="把环评报告解析成 blocks.json")
    parser.add_argument("source", type=Path, help=".doc、.docx 或带文字层的 .pdf")
    parser.add_argument("-o", "--output", type=Path, required=True, help="blocks.json 路径")
    parser.add_argument("--work-dir", type=Path, default=None, help="存放转换后的 docx")
    args = parser.parse_args(argv)
    result = parse_report(args.source, args.work_dir)
    write_blocks(result, args.output)
    stats = result.stats
    print(
        f"blocks={len(result.blocks)} tables={stats.table_count} "
        f"top={stats.top_level_table_count} images={stats.image_block_count} "
        f"media={stats.media_file_count}"
    )


if __name__ == "__main__":
    main()
