from app.db import get_db, write_db

# 指纹失配互斥策略：拒绝锁定（须重打包） / 重建指纹（强制复检）
STRATEGIES = ("reject", "rebuild")


class ServiceError(Exception):
    def __init__(self, message: str, status_code: int = 400, payload: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}


def list_objects(batch_id: int, db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM landing_objects WHERE batch_id=? ORDER BY id",
            (batch_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_conflicts(
    batch_id: int | None = None, db_path: str | None = None
) -> list[dict]:
    with get_db(db_path) as conn:
        if batch_id:
            rows = conn.execute(
                "SELECT * FROM ingest_conflicts WHERE batch_id=? ORDER BY id DESC",
                (batch_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ingest_conflicts ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def _fetch_object(conn, object_id: int) -> dict:
    return dict(
        conn.execute(
            "SELECT * FROM landing_objects WHERE id=?", (object_id,)
        ).fetchone()
    )


def _record_conflict(
    conn,
    batch_id: int,
    key: str,
    old_hash: str,
    new_hash: str,
    strategy: str,
    outcome: str,
    actor: str,
    detail: str,
) -> dict:
    cur = conn.execute(
        "INSERT INTO ingest_conflicts("
        "batch_id, object_key, old_hash, new_hash, strategy, outcome, actor, detail) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (batch_id, key, old_hash, new_hash, strategy, outcome, actor, detail),
    )
    row = conn.execute(
        "SELECT * FROM ingest_conflicts WHERE id=?", (cur.lastrowid,)
    ).fetchone()
    return dict(row)


def register_object(
    batch_id: int,
    object_key: str,
    content_hash: str,
    byte_size: int,
    db_path: str | None = None,
    on_conflict: str = "reject",
    repackage: bool = False,
    actor: str = "",
) -> dict:
    """去重灌库入口：同键同指纹幂等放行；同键不同指纹按互斥策略一次落成。

    返回 {object, outcome, message, conflict}；outcome ∈
    已登记 / 去重复验 / 已拒登 / 已重建 / 已解锁。
    """
    key = (object_key or "").strip()
    digest = (content_hash or "").strip()
    if not key or not digest:
        raise ServiceError("对象键与内容指纹不能为空")
    if byte_size < 0:
        raise ServiceError("字节数不能为负")
    strategy = (on_conflict or "reject").strip()
    if strategy not in STRATEGIES:
        raise ServiceError("未知冲突策略：只能二选一（拒绝锁定 或 重建指纹）", 400)

    with write_db(db_path) as conn:
        batch = conn.execute(
            "SELECT * FROM landing_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)
        if batch["status"] != "收集中":
            raise ServiceError("仅收集中批次可登记对象", 409)
        row = conn.execute(
            "SELECT * FROM landing_objects WHERE batch_id=? AND object_key=?",
            (batch_id, key),
        ).fetchone()

        if row is None:
            if repackage:
                raise ServiceError("该键从未登记，无需重打包", 409)
            cur = conn.execute(
                "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, status) "
                "VALUES (?,?,?,?, '已登记')",
                (batch_id, key, digest, int(byte_size)),
            )
            return {
                "object": _fetch_object(conn, cur.lastrowid),
                "outcome": "已登记",
                "message": "已登记对象",
                "conflict": None,
            }

        stored = row["content_hash"]

        if row["locked"]:
            # 锁定键只认带原指纹的重打包；指纹对不上就是漂移，拒。
            if not repackage:
                raise ServiceError(
                    "该键已锁定：此前指纹失配被拒登，须按原指纹重打包后才能再灌", 409
                )
            if digest != stored:
                raise ServiceError("重打包指纹与原登记不符，已拒绝", 409)
            conn.execute(
                "UPDATE landing_objects SET locked=0, byte_size=? WHERE id=?",
                (int(byte_size), row["id"]),
            )
            return {
                "object": _fetch_object(conn, row["id"]),
                "outcome": "已解锁",
                "message": "重打包校验通过，键已解锁",
                "conflict": None,
            }

        if digest == stored:
            # 去重灌库：同键同指纹幂等放行，回显库存指纹供复验
            return {
                "object": dict(row),
                "outcome": "去重复验",
                "message": "同键同指纹，去重灌库复验通过，未重复写入",
                "conflict": None,
            }

        # 指纹失配：互斥结局一次落成（同事务内改对象行 + 写冲突队列）
        if strategy == "reject":
            # 旧指纹行保留并上锁，须重打包
            conn.execute(
                "UPDATE landing_objects SET locked=1 WHERE id=?", (row["id"],)
            )
            conflict = _record_conflict(
                conn, batch_id, key, stored, digest,
                "reject_lock", "已拒登", actor,
                "指纹失配拒登，键已锁定，须按原指纹重打包",
            )
            return {
                "object": _fetch_object(conn, row["id"]),
                "outcome": "已拒登",
                "message": "指纹对不上：已拒登并锁定该键，须按原指纹重打包",
                "conflict": conflict,
            }

        # 重建指纹：旧指纹行不再存活，新指纹待强制复检；条件更新防漂移
        cur = conn.execute(
            "UPDATE landing_objects SET content_hash=?, byte_size=?, recheck=1 "
            "WHERE id=? AND content_hash=?",
            (digest, int(byte_size), row["id"], stored),
        )
        if cur.rowcount != 1:
            raise ServiceError("对象状态已漂移，重建未落成，请刷新后重试", 409)
        conflict = _record_conflict(
            conn, batch_id, key, stored, digest,
            "rebuild", "已重建", actor,
            "指纹已重建，强制复检放行前不可闭合",
        )
        return {
            "object": _fetch_object(conn, row["id"]),
            "outcome": "已重建",
            "message": "指纹对不上：已按新指纹重建，强制复检放行后才可闭合",
            "conflict": conflict,
        }


def recheck_object(object_id: int, db_path: str | None = None) -> dict:
    """强制复检放行：仅待复检对象可放行，状态漂移即拒。"""
    with write_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM landing_objects WHERE id=?", (object_id,)
        ).fetchone()
        if not row:
            raise ServiceError("对象不存在", 404)
        if not row["recheck"]:
            raise ServiceError("该对象不在待复检状态", 409)
        cur = conn.execute(
            "UPDATE landing_objects SET recheck=0 WHERE id=? AND recheck=1",
            (object_id,),
        )
        if cur.rowcount != 1:
            raise ServiceError("对象状态已漂移，请刷新后重试", 409)
        return {
            "object": _fetch_object(conn, object_id),
            "outcome": "已放行",
            "message": "复检通过，对象已放行",
        }
