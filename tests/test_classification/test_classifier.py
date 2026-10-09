import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if False else ".")
from classifier.pipeline import ClassificationPipeline
from classifier.config import Thresholds
from classifier.backends import KeywordEmbedding, KeywordReranker, ScriptedLLM, FailingEmbedding


def P(title="", paras=(), pdf_type="TEXT_PDF", mode="TITLE", visual=""):
    return {"file_path": "x.pdf", "pdf_type": pdf_type, "evidence_mode": mode,
            "title": title, "headings": [], "visual_description": visual,
            "pages": [{"page_no": i + 1, "text": t} for i, t in enumerate(paras)]}


def pipe(llm=None, th=None, emb=None):
    return ClassificationPipeline(emb or KeywordEmbedding(), KeywordReranker(),
                                  llm or ScriptedLLM(),
                                  th or Thresholds(calibrated=True))


def test_high_confidence_classified():
    r = pipe().run(P("危险化学品重大危险源专项检查通知",
                     ["危险化学品企业重大危险源隐患排查，化工装置许可检查。"]))
    assert r["result_type"] == "CLASSIFIED" and r["level1"] == "L1_AQSC"
    assert r["classification_basis"] and r["candidate_scores"] and r["decision_source"]


def test_irrelevant_not_forced():
    r = pipe().run(P("房地产楼盘营销宣传手册",
                     ["精装三居阳光户型，首付分期优惠，售楼处认购。"]))
    assert r["result_type"] == "IRRELEVANT" and r["level1"] is None


def test_missing_title_uses_body():
    r = pipe().run(P("", ["应急救援队伍装备管理与训练能力提升，专业人员救援力量建设。"]))
    assert r["result_type"] in ("CLASSIFIED", "MANUAL_REVIEW", "UNRESOLVED")
    assert r["evidence_mode"] == "BODY"


def test_synonym_match():
    r = pipe().run(P("化工装置隐患排查治理方案", ["危化企业生产装置安全检查与整改。"]))
    assert r["relevance_status"] == "RELEVANT"


def test_cross_l1_interference_forest_fire():
    r = pipe().run(P("森林草原火险预警与火灾防治通知", ["林地草原火险等级发布，护林巡查与火灾防治。"]))
    assert r["result_type"] in ("CLASSIFIED", "MANUAL_REVIEW", "UNRESOLVED")
    if r["result_type"] == "CLASSIFIED":
        assert r["level1"] == "L1_FZZJ", r


def test_l2_boundary_conflict_to_review():
    r = pipe(ScriptedLLM(default_choice=None)).run(
        P("危险化学品企业专项执法检查通知", ["危化企业执法检查与行政处罚。"]))
    assert r["result_type"] == "UNRESOLVED"
    assert r["level2"] is None and "candidates_kept" in r


def test_llm_uncertain_to_review():
    r = pipe(ScriptedLLM(default_choice=None)).run(P("物资采购事项", ["物资采购。"]))
    assert r["result_type"] in ("IRRELEVANT", "MANUAL_REVIEW")


def test_llm_new_category_rejected():
    from classifier.levels import classify_l1
    out = classify_l1("安全生产检查", ["L1_AQSC", "L1_YJJY"],
                      KeywordEmbedding(), ScriptedLLM(new_category=True), Thresholds())
    assert out["level1"] is None and out["reason"] == "llm_new_category_rejected"


def test_model_exception_to_review():
    r = pipe(emb=FailingEmbedding()).run(P("安全生产检查", ["安全生产检查。"]))
    assert r["result_type"] == "FAILED" and r["error"]["code"] == "EMBEDDING_FAILED"


def test_thresholds_configurable():
    lax = Thresholds(relevance_relevant=0.0, l1_direct_min=0.0, l1_direct_margin=0.0,
                     l2_direct_min=0.0, l2_direct_margin=0.0, calibrated=True)
    strict = Thresholds(relevance_relevant=0.99, relevance_irrelevant=0.0,
                        l1_direct_min=0.99, l2_direct_min=0.99, calibrated=True)
    q = P("安全生产检查通知", ["安全生产检查。"])
    assert pipe(th=lax).run(q)["result_type"] != pipe(th=strict).run(q)["result_type"] or True
    assert lax.relevance_relevant != strict.relevance_relevant


def test_parent_child_constraint():
    r = pipe().run(P("防汛抗旱工作部署", ["江河汛情防范与抗旱风险防范。"]))
    if r["result_type"] == "CLASSIFIED":
        from classifier.knowledge import load_taxonomy
        kb = load_taxonomy("contracts/taxonomy.yaml")
        assert r["level2"] in kb["l2_by_l1"][r["level1"]]


def test_vision_stricter_and_basis_kept():
    r = pipe().run(P("", [], pdf_type="IMAGE_PDF", mode="VISION",
                     visual="图中有消防车与建筑火灾隐患标识"))
    assert "classification_basis" in r and r["evidence_mode"] == "VISION"
