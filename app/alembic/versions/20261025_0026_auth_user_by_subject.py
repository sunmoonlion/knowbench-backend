"""本地用户只按 subject 认，同一个人只有一份（账 175）。

C2b 换域名后 Casdoor 的 issuer 从 casdoor.sunmoonai.com 变成 casdoor.dev.sunmoonai.com，
`auth_user` 按（issuer, subject）唯一，同一个 Casdoor 用户再登录就建了第二份本地用户、
第二个 actor id，配对、项目、会话都留在旧份名下。Casdoor 的 subject 是用户 id，稳定；
issuer 只核不作键。

迁移：同一 subject 有多份时，留最新那份（它是现在登录的那份，浏览器会话里带的就是
它的 id），把旧份名下所有 `owner_actor_id` / `actor_id` 改到新份；两份都有的「每人
一条」或按人唯一的记录以新份为准、旧份删掉；然后删旧份，唯一约束改成只按 subject。

Revision ID: 20261025_0026
Revises: 20261023_0025
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from alembic import op

revision = "20261025_0026"
down_revision = "20261023_0025"
branch_labels = None
depends_on = None

ACTOR_COLUMNS = ("owner_actor_id", "actor_id")


def _actor_columns(conn) -> list[tuple[str, str, str]]:
    rows = conn.execute(
        sa.text(
            """
            select table_name, column_name, data_type from information_schema.columns
            where table_schema = current_schema() and table_name <> 'auth_user'
              and column_name = any(:names)
            order by table_name, column_name
            """
        ),
        {"names": list(ACTOR_COLUMNS)},
    ).all()
    return [(str(t), str(c), str(d)) for t, c, d in rows]


def _value(data_type: str, actor_id: uuid.UUID):
    return actor_id if data_type == "uuid" else str(actor_id)


def _repoint(
    conn, table: str, column: str, data_type: str, old: uuid.UUID, new: uuid.UUID
) -> None:
    params = {"old": _value(data_type, old), "new": _value(data_type, new)}
    try:
        with conn.begin_nested():
            conn.execute(
                sa.text(
                    f'update "{table}" set "{column}" = :new where "{column}" = :old'
                ),
                params,
            )
        return
    except DBAPIError:
        pass
    # 整表改不动：撞了按人唯一的键（两份都有这条，新份的留着），或者这张表的行不许改
    # （knowbench 的钉住分身版本有触发器）。逐条来：先试改；改不了就删掉再按新份插一条
    # 一样的；插不进去（新份已有同样的一条）就只删
    rows = (
        conn.execute(
            sa.text(f'select ctid, * from "{table}" where "{column}" = :old'),
            {"old": params["old"]},
        )
        .mappings()
        .all()
    )
    for row in rows:
        ctid = row["ctid"]
        try:
            with conn.begin_nested():
                conn.execute(
                    sa.text(
                        f'update "{table}" set "{column}" = :new'
                        f' where ctid = :c and "{column}" = :old'
                    ),
                    {**params, "c": ctid},
                )
            continue
        except DBAPIError:
            pass
        values = {k: v for k, v in row.items() if k != "ctid"}
        values[column] = params["new"]
        names = ", ".join(f'"{k}"' for k in values)
        marks = ", ".join(f":{k}" for k in values)
        with conn.begin_nested():
            conn.execute(sa.text(f'delete from "{table}" where ctid = :c'), {"c": ctid})
        try:
            with conn.begin_nested():
                conn.execute(
                    sa.text(f'insert into "{table}" ({names}) values ({marks})'), values
                )
        except DBAPIError:
            pass


def upgrade() -> None:
    conn = op.get_bind()
    columns = _actor_columns(conn)
    subjects = (
        conn.execute(
            sa.text(
                "select subject from auth_user group by subject having count(*) > 1"
            )
        )
        .scalars()
        .all()
    )
    for subject in subjects:
        rows = (
            conn.execute(
                sa.text(
                    "select id from auth_user where subject = :s"
                    " order by created_at desc, id"
                ),
                {"s": subject},
            )
            .scalars()
            .all()
        )
        keep = rows[0]
        for old in rows[1:]:
            for table, column, data_type in columns:
                _repoint(conn, table, column, data_type, old, keep)
            conn.execute(sa.text("delete from auth_user where id = :i"), {"i": old})
    op.drop_constraint("uq_auth_user_issuer_subject", "auth_user", type_="unique")
    op.create_unique_constraint("uq_auth_user_subject", "auth_user", ["subject"])


def downgrade() -> None:
    # 合并过的用户不拆回去；只把唯一约束换回（issuer, subject）
    op.drop_constraint("uq_auth_user_subject", "auth_user", type_="unique")
    op.create_unique_constraint(
        "uq_auth_user_issuer_subject", "auth_user", ["issuer", "subject"]
    )
