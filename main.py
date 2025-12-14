import asyncio
import datetime
import logging
import os
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import psutil
from fastapi import (
    Body,
    Depends,
    FastAPI,
    HTTPException,
    WebSocket,
    status,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.websockets import WebSocketState
from starlette.websockets import WebSocketDisconnect

COLORS = [
    "\033[31m",  # red
    "\033[32m",  # green
    "\033[33m",  # yellow
    "\033[34m",  # blue
    "\033[35m",  # magenta
    "\033[36m",  # cyan
    "\033[91m",  # bright red
    "\033[92m",  # bright green
    "\033[93m",  # bright yellow
    "\033[94m",  # bright blue
    "\033[95m",  # bright magenta
    "\033[96m",  # bright cyan
    "\033[37m",  # white
    "\033[90m",  # bright black (gray)
    "\033[97m",  # bright white
    "\033[38;5;208m",  # orange
    "\033[38;5;214m",  # light orange
    "\033[38;5;178m",  # gold
    "\033[38;5;118m",  # lime
    "\033[38;5;81m",  # sky blue
    "\033[38;5;135m",  # purple
    "\033[38;5;141m",  # lavender
    "\033[38;5;203m",  # pink
]
RESET = "\033[0m"


class ProcessColorFormatter(logging.Formatter):
    def format(self, record):
        pid = record.process
        assert pid
        color = COLORS[pid % len(COLORS)]

        record.msg = f"{color}{record.msg}{RESET}"
        return super().format(record)


logger = logging.getLogger("uvicorn")
for handler in logger.handlers:
    handler.setFormatter(
        ProcessColorFormatter(
            "%(asctime)s | PID=%(process)s | %(levelname)s | %(filename)s:%(lineno)d | %(message)s"
        )
    )

tempdir = (
    Path(tempfile.gettempdir())
    / "netyaga-test-task-2025-12-14"
    / datetime.datetime.fromtimestamp(psutil.boot_time()).strftime("%d%m%Y%H%M%S")
)
tempdir.mkdir(exist_ok=True, parents=True)

# load_dotenv(dotenv_path="./.env")
port = os.environ.get("PORT") or 12696
admin_username = os.environ.get("ADMIN_USERNAME")
admin_password = os.environ.get("ADMIN_PASSWORD")
if not admin_username or not admin_password:
    raise Exception(
        f"Admin's username and password must be provided. Got: {admin_username=}, {admin_password=}"
    )

local_socket_listen_task = None

connections: dict[uuid.UUID, WebSocket] = {}


async def _local_socket_handle(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
):
    # wire protocol: first 128 bits are id, then message (in utf-8) till eof
    client_id_bin = await reader.read(16)
    msg_bin = await reader.read()
    # 0 corresponds to broadcast
    # multicast is not implemented
    client_id = uuid.UUID(int=int.from_bytes(client_id_bin))

    if client_id.int == 0:
        logger.info("Retranslating broadcast message to all websocket connections...")
        for ws in connections.values():
            msg = msg_bin.decode()
            await ws.send_json(msg)
        logger.info("Retranslated to %s clients", len(connections))
    elif client_id not in connections:
        return
    else:
        await connections[client_id].send_json(msg_bin.decode())


async def _listen_local_socket(local_socket_path: Path):
    """Listens worker exclusive socket"""
    logger.info("Listening unix socket: %s", local_socket_path)
    server = await asyncio.start_unix_server(
        _local_socket_handle,
        path=local_socket_path,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    local_socket_path = tempdir / f"worker-{os.getpid()}"

    global local_socket_listen_task  # noqa: PLW0603
    local_socket_listen_task = asyncio.create_task(
        _listen_local_socket(local_socket_path=local_socket_path)
    )

    yield

    local_socket_path.unlink()


app = FastAPI(lifespan=lifespan)
security = HTTPBasic()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    try:
        await websocket.accept()
        client_id = uuid.uuid4()
        connections[client_id] = websocket

        logger.info("Client %s connected!", client_id)
        await websocket.send_text(f"Your id: {client_id}")

        async def listen():
            while websocket.client_state != WebSocketState.DISCONNECTED:
                await websocket.receive_text()
                # Not implemented

        await listen()

        await websocket.close()
    except WebSocketDisconnect:
        logger.info("Client %s has disconneted", client_id)
    finally:
        del connections[client_id]


@app.post("/send")
async def send(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
    msg: Annotated[str, Body()],
    client_id: uuid.UUID | None = None,
    to_all: bool = False,
):
    if credentials.username != admin_username or credentials.password != admin_password:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)

    if not to_all and not client_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "'client_id' must be provided or 'to_all' must be set to true",
        )

    for socket in [p for p in tempdir.iterdir() if p.is_socket()]:
        _, writer = await asyncio.open_unix_connection(socket)

        client_id = client_id or uuid.UUID(int=0)

        writer.write(int(client_id).to_bytes(length=16))
        writer.write(msg.encode())

        await writer.drain()
        writer.close()
