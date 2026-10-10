from classifier.taxonomy_v3 import L1,L2,LOW5,target_dir,all_l2
def test_27():
    assert len(L1)==4 and len(all_l2())==27 and len(LOW5)==5
    assert target_dir("BUSINESS",None,"L1_ZHGL","纪检监察").endswith("纪检监察/")
    try: target_dir("BUSINESS",None,"L1_AQSC","纪检监察"); assert False
    except AssertionError: pass
def test_archive(tmp_path):
    from scheduler.archive_worker import archive_copy
    import os
    s=tmp_path/"a.pdf"; s.write_bytes(b"%PDF-1.4 test patent")
    r=archive_copy(str(s),str(tmp_path/"root"),"PATENT")
    assert os.path.isfile(r["final"])
    r2=archive_copy(str(s),str(tmp_path/"root"),"PATENT")
    assert r2["dedup"] or os.path.isfile(r2["final"])
    s2=tmp_path/"b.pdf"; s2.write_bytes(b"%PDF low")
    r3=archive_copy(str(s2),str(tmp_path/"root"),"BUSINESS",l1="L1_AQSC",l2="消防监管")
    assert "安全生产" in r3["final"]
def test_registry(tmp_path):
    from backend.registry import scan
    (tmp_path/"x.pdf").write_bytes(b"%PDF")
    rows=scan(str(tmp_path))
    assert len(rows)==1 and rows[0]["bytes"]>0
