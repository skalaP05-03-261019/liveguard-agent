"""LangGraph Postgres checkpointer. 업무 스키마와 분리된 `agent` 스키마에 thread별 상태를 저장한다."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
import inspect
import os

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import BaseModel
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app import schemas

CHECKPOINT_SCHEMA = "agent"


@dataclass(frozen=True)
class CheckpointSettings:
    db_url: str

    def __post_init__(self) -> None:
        if not self.db_url:
            raise ValueError("AGENT_CHECKPOINT_DB_URL is required")

    @classmethod
    def from_env(cls) -> "CheckpointSettings":
        return cls(db_url=os.getenv("AGENT_CHECKPOINT_DB_URL", ""))


def allowed_state_types() -> list[tuple[str, str]]:
    # State에 들어가는 계약 모델만 복원을 허용한다. 체크포인트에 임의 클래스가 섞여도 실행되지 않게 한다.
    return sorted(
        (schemas.__name__, name)
        for name, value in vars(schemas).items()
        if inspect.isclass(value)
        and value.__module__ == schemas.__name__
        and issubclass(value, (BaseModel, Enum))
    )


def state_serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=allowed_state_types())


@asynccontextmanager
async def open_checkpointer(
    db_url: str, *, schema: str = CHECKPOINT_SCHEMA, initialize: bool = True
) -> AsyncIterator[AsyncPostgresSaver]:
    """스키마를 만들고 checkpointer 테이블을 준비한 뒤 연결을 연다. 앱 수명 동안 하나를 공유한다."""
    if initialize:
        with psycopg.connect(db_url, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
    scoped_url = make_conninfo(db_url, options=f"-c search_path={schema}")
    async with AsyncPostgresSaver.from_conn_string(scoped_url, serde=state_serializer()) as saver:
        if initialize:
            await saver.setup()
        yield saver
