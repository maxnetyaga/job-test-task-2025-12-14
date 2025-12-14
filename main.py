import asyncio
import logging
import os
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

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

logger = logging.getLogger("uvicorn")

tempdir = Path(tempfile.gettempdir()) / "netyaga-test-task-2025-12-14"
tempdir.mkdir(exist_ok=True)

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
    addr = writer.get_extra_info("peername")
    print("Connected:", addr)

    # wire protocol: first 128 bits are id, then message (in utf-8) till eof
    client_id_bin = await reader.read(8)
    msg_bin = await reader.read()
    # 0 corresponds to broadcast
    # multicast is not implemented
    client_id = uuid.UUID(int=int.from_bytes(client_id_bin))

    if client_id == 0:
        for ws in connections.values():
            await ws.send_json(msg_bin.decode())

    else:
        await connections[client_id].send_json(msg_bin.decode())


async def _listen_local_socket(local_socket_path: Path):
    """Listens worker exclusive socket"""
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

        await asyncio.gather(listen())

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

        writer.write(int(client_id).to_bytes(length=128))
        writer.write(msg.encode())

        await writer.drain()
        writer.close()
