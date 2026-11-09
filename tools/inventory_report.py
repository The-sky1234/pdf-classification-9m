# -*- coding: utf-8 -*-
import csv
import html
from collections import Counter
from pathlib import Path

HOME = Path.home()
CANDIDATES = [HOME / "all_file_lists", HOME]
SRC = next((p for p in CANDIDATES if any(p.glob("all_files_*.csv"))), HOME)
OUT = HOME / "dedup-by-size"
OUT.mkdir(parents=True, exist_ok=True)

GROUPS = {
    "文档": {"pdf", "doc", "docx", "docm", "wps", "rtf", "odt", "ofd"},
    "表格": {"xls", "xlsx", "xlsm", "csv", "et", "ods"},
    "演示": {"ppt", "pptx", "pps", "dps"},
    "图片": {"jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp", "heic"},
    "压缩包": {"zip", "rar", "7z", "tar", "gz", "bz2"},
    "文本": {"txt", "log", "md", "json", "xml", "html", "htm"},
    "图纸": {"dwg", "dxf", "dwt"},
    "专业数据": {"pro", "mdb", "accdb", "db", "sqlite", "shp", "gdb"},
}
EXT_GROUP = {ext: name for name, exts in GROUPS.items() for ext in exts}
SCENES = [
    ("安全资格与培训", ("注册安全工程师", "安全生产", "安全专业", "注安", "一建", "一消")),
    ("地震与地质", ("地震", "地质", "煤矿")),
    ("能源与统计", ("煤炭", "焦炭", "海关", "发电", "原油", "统计", "能源")),
    ("标准规范", ("标准", "规范", "规程")),
    ("合同协议", ("合同", "协议")),
    ("发票票据", ("发票", "收据", "票据")),
    ("财务报表", ("资产负债表", "利润表", "现金流量", "财务报表", "审计")),
    ("招投标", ("招标", "投标", "中标")),
    ("图纸说明", ("图纸", "说明书", "验收")),
    ("公文通知", ("通知", "纪要", "批复", "函")),
]
SIZE_LABELS = ["0字节", "1B-10KB", "10KB-1MB", "1MB-10MB", "10MB-100MB", "100MB-1GB", "1GB以上"]


def size_bucket(n):
    if n <= 0:
        return 0
    if n < 10 * 1024:
        return 1
    if n < 1024 ** 2:
        return 2
    if n < 10 * 1024 ** 2:
        return 3
    if n < 100 * 1024 ** 2:
        return 4
    if n < 1024 ** 3:
        return 5
    return 6


def top_dir(path):
    parts = [p for p in path.split("/") if p and p != "wangzengzheng"]
    return parts[0] if parts else "(根目录)"


def year_of(text):
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 4 and digits[:4] in {str(y) for y in range(1990, 2036)}:
        return digits[:4]
    return "未知"


def scene_of(text):
    for name, words in SCENES:
        if any(word in text for word in words):
            return name
    return "未识别"


files = total = empty = noext = 0
ext_n, ext_b = Counter(), Counter()
group_n, group_b = Counter(), Counter()
dir_n, dir_b = Counter(), Counter()
year_n, year_b = Counter(), Counter()
scene_n, scene_b = Counter(), Counter()
buckets_n, buckets_b = Counter(), Counter()
largest = []
csv_list = sorted(SRC.glob("all_files_*.csv"))
if not csv_list:
    raise SystemExit("no all_files_*.csv under %s" % SRC)

for csv_path in csv_list:
    seen = 0
    with csv_path.open(newline="", encoding="utf-8", errors="replace") as handle:
        for row in csv.DictReader(handle):
            path = (row.get("完整路径") or "").strip()
            name = (row.get("文件名") or "").strip()
            if not path:
                continue
            ext = (row.get("扩展名") or "").strip().lower().lstrip(".")
            try:
                size = int(float(row.get("文件大小_字节") or 0))
            except ValueError:
                size = 0
            folder = top_dir(path)
            year = year_of(row.get("修改时间") or "")
            scene = scene_of(path + "/" + name)
            group = EXT_GROUP.get(ext, "无扩展名" if not ext else "其他")
            files += 1
            total += size
            seen += 1
            if size <= 0:
                empty += 1
            if not ext:
                noext += 1
            key = ext or "(无扩展名)"
            ext_n[key] += 1
            ext_b[key] += size
            group_n[group] += 1
            group_b[group] += size
            dir_n[folder] += 1
            dir_b[folder] += size
            year_n[year] += 1
            year_b[year] += size
            scene_n[scene] += 1
            scene_b[scene] += size
            b = size_bucket(size)
            buckets_n[b] += 1
            buckets_b[b] += size
            if len(largest) < 30:
                largest.append((size, path))
                largest.sort(reverse=True)
            elif size > largest[-1][0]:
                largest[-1] = (size, path)
                largest.sort(reverse=True)
    print("read", csv_path.name, seen, flush=True)


def tb(n):
    return "%.2f TB" % (n / 1024 ** 4) if n >= 1024 ** 4 else "%.2f GB" % (n / 1024 ** 3)


def row_html(cells):
    return "<tr>" + "".join("<td>%s</td>" % html.escape(str(c)) for c in cells) + "</tr>"


def table(headers, rows):
    head = "".join("<th>%s</th>" % html.escape(h) for h in headers)
    body = "".join(row_html(r) for r in rows)
    return "<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (head, body)


def ranked(counter_n, counter_b, limit=None):
    items = sorted(counter_n, key=lambda k: (-counter_b[k], -counter_n[k], k))
    if limit:
        items = items[:limit]
    out = []
    for key in items:
        n, b = counter_n[key], counter_b[key]
        out.append((key, f"{n:,}", tb(b), "%.1f%%" % (100 * n / files), "%.1f%%" % (100 * b / total if total else 0)))
    return out


sections = []
sections.append("<h2>总览</h2><ul><li>文件数：%s</li><li>总体积：%s（%s 字节）</li><li>空文件：%s</li><li>无扩展名：%s</li><li>扩展名种类：%s</li><li>一级目录数：%s</li></ul>" % (
    f"{files:,}", tb(total), f"{total:,}", f"{empty:,}", f"{noext:,}", f"{len(ext_n):,}", f"{len(dir_n):,}"))
sections.append("<h2>格式大类</h2><p>按扩展名归类。这是文件格式，不是打开正文后的文字型或图片型。</p>" + table(
    ["大类", "文件数", "体积", "文件数占比", "体积占比"], ranked(group_n, group_b)))
sections.append("<h2>全部扩展名</h2><p>每一种扩展名都列出。文件数占比是这种扩展名的文件个数除以全部文件数，体积占比是这种扩展名的字节数除以全部字节数。</p>" + table(
    ["扩展名", "文件数", "体积", "文件数占比", "体积占比"], ranked(ext_n, ext_b)))
sections.append("<h2>大小分段</h2>" + table(
    ["分段", "文件数", "体积", "文件数占比", "体积占比"],
    [(SIZE_LABELS[i], f"{buckets_n[i]:,}", tb(buckets_b[i]), "%.1f%%" % (100 * buckets_n[i] / files), "%.1f%%" % (100 * buckets_b[i] / total if total else 0)) for i in range(7)]))
sections.append("<h2>一级目录</h2>" + table(
    ["目录", "文件数", "体积", "文件数占比", "体积占比"], ranked(dir_n, dir_b, 40)))
year_rows = []
for key in sorted(year_n, key=lambda item: (item == "未知", item)):
    n, b = year_n[key], year_b[key]
    year_rows.append((key, f"{n:,}", tb(b), "%.1f%%" % (100 * n / files), "%.1f%%" % (100 * b / total if total else 0)))
sections.append("<h2>修改年份</h2>" + table(["年份", "文件数", "体积", "文件数占比", "体积占比"], year_rows))
sections.append("<h2>文件名与路径关键词</h2><p>一个文件只计入最先匹配到的一类。未出现这些词的归入未识别。</p>" + table(
    ["场景", "文件数", "体积", "文件数占比", "体积占比"], ranked(scene_n, scene_b)))
sections.append("<h2>最大的 30 个文件</h2>" + table(
    ["体积", "路径"], [(tb(size), path) for size, path in largest]))

page = (
    "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><title>档案清单报告</title>"
    "<style>body{font-family:sans-serif;margin:32px;color:#222}table{border-collapse:collapse;width:100%;margin:12px 0 28px}th,td{border:1px solid #ddd;padding:6px 8px;text-align:left}th{background:#f4f4f4}td:nth-child(n+2){text-align:right}h1{margin-bottom:0}p.note{color:#555}</style></head><body>"
    "<h1>档案清单报告</h1><p class=\"note\">数据来自 all_file_lists 的 CSV，只根据路径、扩展名、大小、目录和修改时间统计，没有打开文档正文。</p>"
    + "\n".join(sections)
    + "</body></html>"
)
(OUT / "inventory_report.html").write_text(page, encoding="utf-8")
lines = ["files %d" % files, "bytes %d" % total, "empty %d" % empty, "noext %d" % noext, ""]
lines.append("groups")
for item in ranked(group_n, group_b):
    lines.append("\t".join(item))
lines.append("")
lines.append("extensions")
for item in ranked(ext_n, ext_b):
    lines.append("\t".join(item))
(OUT / "inventory_report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
print("report_ready", OUT / "inventory_report.html", flush=True)
print("files", files, "bytes", total, flush=True)
