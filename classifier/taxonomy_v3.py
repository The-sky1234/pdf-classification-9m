from __future__ import annotations
L1 = {"L1_AQSC": "安全生产", "L1_FZZJ": "防灾减灾救灾", "L1_YJJY": "应急救援", "L1_ZHGL": "综合管理"}
L2 = {
 "L1_AQSC": ["消防监管","矿山监管监察","危险化学品监管","石油开采安全监管","安全生产执法和工贸监管","调查评估和统计","安全生产综合协调"],
 "L1_FZZJ": ["风险监测和综合减灾","森林草原火灾防治","防汛抗旱","地质灾害","防震减灾","救灾和物资保障"],
 "L1_YJJY": ["指挥调度","救援力量","预案管理"],
 "L1_ZHGL": ["综合办公","人事管理","队伍管理","政策法规","国际合作","规划财务","新闻宣传","科技和信息化","党建管理","离退休干部管理","纪检监察"],
}
LOW5 = {"LV01":"内容不足","LV02":"重复件","LV03":"无效文件","LV04":"无业务匹配","LV05":"命中否定词"}
def all_l2(): return [(k,v) for k,vs in L2.items() for v in vs]
def target_dir(a,t,l1=None,l2=None,reason=None):
    if a=="PATENT": return "01_专利文件/"
    if a=="LOW_VALUE": return f"02_低价值文件/{LOW5[reason]}/"
    if a=="BUSINESS":
        assert l2 in L2.get(l1,[]), "非法父子"
        return f"03_业务分类/{L1[l1]}/{l2}/"
    return "04_待复核暂存/"
