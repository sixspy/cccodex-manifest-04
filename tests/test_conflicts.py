import threading

from app.db import init_db
from app.services import list_conflicts, list_objects, register_object


def _open_batch_id(authed, zone="bronze_orders"):
    batches = authed.get("/api/batches", params={"status": "收集中"}).json()["items"]
    return next(b["id"] for b in batches if b["zone_code"] == zone)


def _register(authed, bid, key, digest, size=10, **extra):
    body = {"object_key": key, "content_hash": digest, "byte_size": size, **extra}
    return authed.post(f"/api/batches/{bid}/objects", json=body)


def test_dedup_same_fingerprint_idempotent(authed):
    bid = _open_batch_id(authed)
    r1 = _register(authed, bid, "raw/a.parquet", "hash-a")
    assert r1.status_code == 200
    assert r1.json()["outcome"] == "已登记"

    r2 = _register(authed, bid, "raw/a.parquet", "hash-a")
    assert r2.status_code == 200
    assert r2.json()["outcome"] == "去重复验"
    # 复验：回显的库存指纹与灌入一致，且未重复写行
    assert r2.json()["object"]["content_hash"] == "hash-a"
    objs = authed.get(f"/api/batches/{bid}").json()["objects"]
    assert len(objs) == 1


def test_mismatch_default_reject_locks_key(authed):
    bid = _open_batch_id(authed)
    _register(authed, bid, "raw/a.parquet", "hash-a")

    r = _register(authed, bid, "raw/a.parquet", "hash-b")
    assert r.status_code == 409
    assert "拒登" in r.json()["error"]
    assert r.json()["conflict"]["outcome"] == "已拒登"
    assert r.json()["conflict"]["strategy"] == "reject_lock"

    # 旧指纹行还活着，且已上锁
    obj = authed.get(f"/api/batches/{bid}").json()["objects"][0]
    assert obj["content_hash"] == "hash-a"
    assert obj["locked"] == 1

    # 锁定键再灌（任意指纹）一律拒
    again = _register(authed, bid, "raw/a.parquet", "hash-c")
    assert again.status_code == 409
    assert "锁定" in again.json()["error"]

    # 冲突队列可见拒登芯片
    items = authed.get("/api/conflicts", params={"batch_id": bid}).json()["items"]
    assert len(items) == 1
    assert items[0]["old_hash"] == "hash-a"
    assert items[0]["new_hash"] == "hash-b"


def test_repackage_unlock_requires_original_hash(authed):
    bid = _open_batch_id(authed)
    _register(authed, bid, "raw/a.parquet", "hash-a")
    _register(authed, bid, "raw/a.parquet", "hash-b")  # 拒登并锁定

    # 重打包指纹与原登记不符 → 漂移，拒
    bad = _register(authed, bid, "raw/a.parquet", "hash-x", repackage=True)
    assert bad.status_code == 409
    assert "不符" in bad.json()["error"]

    # 带原指纹重打包 → 解锁，之后可正常灌
    ok = _register(authed, bid, "raw/a.parquet", "hash-a", repackage=True)
    assert ok.status_code == 200
    assert ok.json()["outcome"] == "已解锁"
    obj = ok.json()["object"]
    assert obj["locked"] == 0
    assert obj["content_hash"] == "hash-a"

    r = _register(authed, bid, "raw/a.parquet", "hash-a")
    assert r.json()["outcome"] == "去重复验"


def test_rebuild_forces_recheck_before_close(authed):
    bid = _open_batch_id(authed)
    _register(authed, bid, "raw/a.parquet", "hash-a")

    r = _register(authed, bid, "raw/a.parquet", "hash-b", on_conflict="rebuild")
    assert r.status_code == 200
    assert r.json()["outcome"] == "已重建"
    obj = r.json()["object"]
    # 旧指纹行不再存活：同一行换上新指纹，旧指纹只留在冲突记录里
    assert obj["content_hash"] == "hash-b"
    assert obj["recheck"] == 1

    conflict = r.json()["conflict"]
    assert conflict["outcome"] == "已重建"
    assert conflict["old_hash"] == "hash-a"
    assert conflict["new_hash"] == "hash-b"

    # 待复检对象卡住闭合
    close = authed.post(f"/api/batches/{bid}/close", json={})
    assert close.status_code == 409
    assert "复检" in close.json()["error"]

    # 强制复检放行后可闭合
    re = authed.post(f"/api/objects/{obj['id']}/recheck")
    assert re.status_code == 200
    assert re.json()["object"]["recheck"] == 0
    close2 = authed.post(f"/api/batches/{bid}/close", json={})
    assert close2.status_code == 200
    assert close2.json()["run"]["object_count"] == 1


def test_recheck_requires_pending_state(authed):
    bid = _open_batch_id(authed)
    r = _register(authed, bid, "raw/a.parquet", "hash-a")
    oid = r.json()["object"]["id"]
    re = authed.post(f"/api/objects/{oid}/recheck")
    assert re.status_code == 409
    assert "待复检" in re.json()["error"]


def test_conflicts_queue_and_detail_stay_aligned(authed):
    bid = _open_batch_id(authed)
    _register(authed, bid, "raw/a.parquet", "hash-a")
    _register(authed, bid, "raw/a.parquet", "hash-b")  # 拒登
    _register(authed, bid, "raw/c.parquet", "hash-c")
    _register(authed, bid, "raw/c.parquet", "hash-c2", on_conflict="rebuild")  # 重建

    detail = authed.get(f"/api/batches/{bid}").json()
    queue = authed.get("/api/conflicts").json()["items"]
    assert len(queue) == 2
    # 三面一致：详情内嵌冲突与全局队列同源，对象行状态与结局对齐
    assert {c["id"] for c in detail["conflicts"]} == {c["id"] for c in queue}
    objs = {o["object_key"]: o for o in detail["objects"]}
    assert objs["raw/a.parquet"]["content_hash"] == "hash-a"  # 拒登：旧指纹活着
    assert objs["raw/a.parquet"]["locked"] == 1
    assert objs["raw/c.parquet"]["content_hash"] == "hash-c2"  # 重建：新指纹上岗
    assert objs["raw/c.parquet"]["recheck"] == 1


def test_unknown_strategy_rejected(authed):
    bid = _open_batch_id(authed)
    _register(authed, bid, "raw/a.parquet", "hash-a")
    r = _register(authed, bid, "raw/a.parquet", "hash-b", on_conflict="merge")
    assert r.status_code == 400
    assert "策略" in r.json()["error"]


def test_concurrent_rebuild_same_key_serializes(tmp_path):
    """两名灌数员抢同一键：写锁排队，结局链式对齐，不互相覆盖。"""
    db = str(tmp_path / "race.db")
    init_db(db)
    register_object(1, "k", "h0", 1, db_path=db)

    results = []

    def pump(digest):
        res = register_object(1, "k", digest, 2, db_path=db, on_conflict="rebuild")
        results.append((digest, res["outcome"]))

    t1 = threading.Thread(target=pump, args=("hX",))
    t2 = threading.Thread(target=pump, args=("hY",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert sorted(o for _, o in results) == ["已重建", "已重建"]
    objs = list_objects(1, db_path=db)
    assert len(objs) == 1
    final = objs[0]["content_hash"]
    assert final in ("hX", "hY")

    chain = sorted(list_conflicts(1, db_path=db), key=lambda c: c["id"])
    assert len(chain) == 2
    assert chain[0]["old_hash"] == "h0"
    # 后一笔基于前一笔的落成结局再判，链式衔接
    assert chain[1]["old_hash"] == chain[0]["new_hash"]
    assert chain[1]["new_hash"] == final


def test_concurrent_same_fingerprint_dedups(tmp_path):
    """同键同指纹并发灌：一笔重建，一笔去重复验，绝不写两行。"""
    db = str(tmp_path / "race2.db")
    init_db(db)
    register_object(1, "k", "h0", 1, db_path=db)

    outcomes = []

    def pump():
        res = register_object(1, "k", "h1", 2, db_path=db, on_conflict="rebuild")
        outcomes.append(res["outcome"])

    threads = [threading.Thread(target=pump) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes) == ["去重复验", "已重建"]
    objs = list_objects(1, db_path=db)
    assert len(objs) == 1
    assert objs[0]["content_hash"] == "h1"
    assert len(list_conflicts(1, db_path=db)) == 1
