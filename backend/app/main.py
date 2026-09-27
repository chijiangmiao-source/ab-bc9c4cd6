"""深空站 32 位帧计数接收器 —— HTTP 接口与静态页面。"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import window
from .db import Database, ReceiptConflict

DB_PATH = os.environ.get("RECEIVER_DB", "/data/receiver.db")

app = FastAPI(title="Deep-Space Frame Receiver")
db = Database(DB_PATH)

STATIC_DIR = Path(__file__).parent / "static"


class LinkIn(BaseModel):
    link_id: str = Field(min_length=1, max_length=128)


class FrameIn(BaseModel):
    raw_count: int = Field(ge=0, le=window.MASK32)
    receipt_id: str = Field(min_length=1, max_length=128)
    payload: Any = None


def state_snapshot(link_id: str) -> dict[str, Any]:
    state = db.get_state(link_id)
    return {
        "initialized": state.initialized,
        "highest": state.highest,
        "highest_dec": str(state.highest),  # 无符号 64 位，字符串形式避免 JS 丢精度
        "bitmap": f"0x{state.bitmap:016x}",
        "accepted_positions": window.positions(state),
        "accepted_positions_dec": [str(p) for p in window.positions(state)],
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/links", status_code=201)
def create_link(body: LinkIn) -> dict[str, Any]:
    created = db.create_link(body.link_id, time.time())
    if not created:
        raise HTTPException(status_code=409, detail="link already exists")
    return {"link_id": body.link_id, "created": True, **state_snapshot(body.link_id)}


@app.get("/api/links/{link_id}")
def get_link(link_id: str) -> dict[str, Any]:
    if not db.link_exists(link_id):
        raise HTTPException(status_code=404, detail="link not found")
    return {"link_id": link_id, **state_snapshot(link_id)}


@app.post("/api/links/{link_id}/frames")
def submit_frame(link_id: str, body: FrameIn) -> dict[str, Any]:
    try:
        result = db.ingest(link_id, body.raw_count, body.receipt_id, body.payload, time.time())
    except KeyError:
        raise HTTPException(status_code=404, detail="link not found")
    except ReceiptConflict:
        # 回执标识被复用，但链路 / 计数 / 载荷与首次不同 —— 拒绝。
        raise HTTPException(
            status_code=409,
            detail="receipt_id reused with a different link, count, or payload",
        )
    state: window.State = result["state"]
    return {
        "link_id": link_id,
        "raw_count": body.raw_count,
        "receipt_id": body.receipt_id,
        "verdict": result["verdict"],
        "replayed": result["replayed"],
        "extended_seq": result["extended_seq"],
        "extended_seq_dec": (
            str(result["extended_seq"]) if result["extended_seq"] is not None else None
        ),
        "highest": state.highest,
        "highest_dec": str(state.highest),
        "bitmap": f"0x{state.bitmap:016x}",
        "accepted_positions": window.positions(state),
        "accepted_positions_dec": [str(p) for p in window.positions(state)],
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
