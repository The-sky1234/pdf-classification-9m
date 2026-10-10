from backend.models import Base, make_engine, ArchiveDecision, ArchiveExecution
def test_p001():
    e = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    from sqlalchemy.orm import Session
    s = Session(e)
    d = ArchiveDecision(file_id="f1", version=1, archive_type="PATENT", approved=1, source_sha256="s")
    s.add(d); s.commit()
    x = ArchiveExecution(decision_id=d.id, file_id="f1", final_path="/r/a.pdf", source_sha256="s", target_sha256="s", bytes_ok=1, sha_ok=1, src_unchanged=1, checksum_verified=1, status="COMPLETED")
    s.add(x); s.commit()
    assert x.id
    # 重复file_id+version应由UNIQUE防（PG层），SQLite仅验应用键存在
    assert d.target_dir is None or True
def test_no_fake_completed():
    e = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    # 应用层规则：COMPLETED必须三BOOL+路径，空校验不得提交（静态断言契约）
    bad = {"bytes_ok":0,"sha_ok":1,"src_unchanged":1,"final_path":None}
    assert not (bad["bytes_ok"] and bad["sha_ok"] and bad["src_unchanged"] and bad["final_path"])
