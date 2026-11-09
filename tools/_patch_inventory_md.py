from pathlib import Path

old = "按扩展名归类。这是文件格式，不是打开正文后的文字型或图片型。"
new = """按扩展名归类。这是文件格式，不是打开正文后的文字型或图片型。

各大类包含的扩展名如下。带这些扩展名的文件计入对应大类；有扩展名但不在下面任何一组里的，计入「其他」；没有扩展名的，计入「无扩展名」。

| 大类 | 包含的扩展名 |
|---|---|
| 文档 | pdf、doc、docx、docm、wps、rtf、odt、ofd |
| 表格 | xls、xlsx、xlsm、csv、et、ods |
| 演示 | ppt、pptx、pps、dps |
| 图片 | jpg、jpeg、png、gif、bmp、tif、tiff、webp、heic |
| 压缩包 | zip、rar、7z、tar、gz、bz2 |
| 文本 | txt、log、md、json、xml、html、htm |
| 图纸 | dwg、dxf、dwt |
| 专业数据 | pro、mdb、accdb、db、sqlite、shp、gdb |
| 其他 | 有扩展名，但不属于上面任何一组。本批实际出现的包括 mp4、ps、eps、mps、rmvb、ai、psd、indd、tmp、bak、cfg、djvu、wmv、rm、otf、ttf、mp3、css、js、ini 等 |
| 无扩展名 | 清单里扩展名为空 |"""

targets = [
    Path(r"D:\inventory_report.md"),
    Path(r"D:\Users\张选宇\Documents\xwechat_files\wxid_35gr7ixxuc8o22_5f10\temp\RWTemp\2026-10\5806e4688efdbb13a59ad7a08a8ff1a6\inventory_report.md"),
]
for p in targets:
    if not p.exists():
        print("missing", p)
        continue
    text = p.read_text(encoding="utf-8")
    if "包含的扩展名如下" in text:
        print("already", p)
        continue
    if old not in text:
        print("anchor missing", p)
        continue
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    print("updated", p)
