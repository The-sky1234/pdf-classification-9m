from __future__ import annotations

import json
import os
from functools import lru_cache

L1 = {"L1_AQSC": "安全生产", "L1_FZZJ": "防灾减灾救灾", "L1_YJJY": "应急救援", "L1_ZHGL": "综合管理"}
L2 = {
 "L1_AQSC": ["消防监管","矿山监管监察","危险化学品监管","石油开采安全监管","安全生产执法和工贸监管","调查评估和统计","安全生产综合协调"],
 "L1_FZZJ": ["风险监测和综合减灾","森林草原火灾防治","防汛抗旱","地质灾害","防震减灾","救灾和物资保障"],
 "L1_YJJY": ["指挥调度","救援力量","预案管理"],
 "L1_ZHGL": ["综合办公","人事管理","队伍管理","政策法规","国际合作","规划财务","新闻宣传","科技和信息化","党建管理","离退休干部管理","纪检监察"],
}
LOW5 = {"LV01":"内容不足","LV02":"重复件","LV03":"无效文件","LV04":"无业务匹配","LV05":"命中否定词"}
ARCHIVE_TYPES = ("PATENT", "LOW_VALUE", "BUSINESS", "PENDING")
SCENES_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "contracts", "phase1", "scenes-v3.json")


def all_l2(): return [(k,v) for k,vs in L2.items() for v in vs]


def is_valid_pair(l1, l2) -> bool:
    return l1 in L2 and l2 in L2[l1]


def target_dir(a,t,l1=None,l2=None,reason=None):
    """非法组合抛 ValueError。不用 assert，python -O 下也必须拦住。"""
    if a=="PATENT": return "01_专利文件/"
    if a=="LOW_VALUE":
        if reason not in LOW5:
            raise ValueError(f"低价值原因必须是 LV01-LV05：{reason!r}")
        return f"02_低价值文件/{LOW5[reason]}/"
    if a=="BUSINESS":
        if not is_valid_pair(l1, l2):
            raise ValueError(f"非法父子：{l1!r}/{l2!r}")
        return f"03_业务分类/{L1[l1]}/{l2}/"
    if a=="PENDING": return "04_待复核暂存/"
    raise ValueError(f"未知归档类型：{a!r}")


@lru_cache(maxsize=1)
def scene_descriptions() -> dict:
    """一级、二级的判别说明，由 tools/build_scene_knowledge.py 从业务分类 Excel 生成。"""
    with open(SCENES_PATH, encoding="utf-8") as handle:
        data = json.load(handle)
    if set(data["l1"]) != set(L1):
        raise ValueError("scenes-v3.json 的一级与 taxonomy_v3 不一致")
    for key, names in L2.items():
        if list(data["l2"].get(key, {})) != names:
            raise ValueError(f"scenes-v3.json 的二级与 taxonomy_v3 不一致：{key}")
    return data
