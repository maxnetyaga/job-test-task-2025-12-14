import asyncio
import contextlib
import datetime
import json
import logging
import os
import signal
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import filelock
import psutil
from fastapi import (
    Body,
    Depends,
    FastAPI,
    HTTPException,
    WebSocket,
    status,
)
from fastapi.encoders import jsonable_encoder
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.websockets import WebSocketState
from pydantic import BaseModel, Field
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


uvicorn_logger = logging.getLogger("uvicorn")
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
for handler in uvicorn_logger.handlers:
    handler.setFormatter(
        ProcessColorFormatter(
            "%(asctime)s | PID=%(process)s | %(levelname)s | %(filename)s:%(lineno)d | %(message)s"
        )
    )
    logger.addHandler(handler)

################################################################################

# In seconds
STATS_UPDATE_RETRY_INTERVAL = 0.1
PROC_ID = os.getpid()

tempdir = (
    Path(tempfile.gettempdir())
    / "netyaga-test-task-2025-12-14"
    / datetime.datetime.fromtimestamp(psutil.boot_time()).strftime("%d%m%Y%H%M%S")
    / f"{os.getppid()}"
)
tempdir.mkdir(exist_ok=True, parents=True)
local_socket_path = tempdir / f"worker-{PROC_ID}"

# Global state
stats_lock = filelock.FileLock(tempdir / "stats.lock")
pending_stats_update_task: asyncio.Task | None = None
shutdown_requested_at = None
connections: dict[uuid.UUID, WebSocket] = {}
loop = None

# Config
port = os.environ.get("PORT") or 12696
stats_path = Path(os.environ.get("STATS_PATH") or "./stats.json")
shutdown_timeout = int(os.environ.get("SHUTDOWN_TIMEOUT") or 0) or 1800
admin_username = os.environ.get("ADMIN_USERNAME")
admin_password = os.environ.get("ADMIN_PASSWORD")
if not admin_username or not admin_password:
    raise Exception(
        f"Admin's username and password must be provided. Got: {admin_username=}, {admin_password=}"
    )


type PID = int


class ClientsPerProcess(BaseModel):
    count: int = 0
    clients: list[uuid.UUID] = Field(default_factory=list)


class Stats(BaseModel):
    clients_per_process: dict[PID, ClientsPerProcess] = Field(default_factory=dict)
    total: int = 0


async def update_stats(stats_path: Path | str):
    global pending_stats_update_task  # noqa: PLW0603
    stats_path = Path(stats_path)
    try:
        with stats_lock:
            logger.debug("Acquired stats file lock")
            stats: Stats = (
                Stats.model_validate_json(stats_path.read_text(encoding="utf-8"))
                if stats_path.exists()
                else Stats()
            )

            prev_per_process_client_count = 0
            if PROC_ID not in stats.clients_per_process:
                stats.clients_per_process[PROC_ID] = ClientsPerProcess()
            else:
                prev_per_process_client_count = stats.clients_per_process[PROC_ID].count

            stats.clients_per_process[PROC_ID].count = len(connections)
            stats.clients_per_process[PROC_ID].clients = list(connections.keys())

            total_delta = len(connections) - prev_per_process_client_count
            stats.total += total_delta

            # FastApi jsonable_encoder to treat UUIDs
            stats_path.write_text(
                json.dumps(jsonable_encoder(stats), indent=2, ensure_ascii=False)
            )

    except filelock.Timeout:
        await asyncio.sleep(STATS_UPDATE_RETRY_INTERVAL)
        pending_stats_update_task = asyncio.create_task(update_stats(stats_path))


def update_stats_safe(stats_path: Path | str):
    """Update stats non-blocking"""
    global pending_stats_update_task
    if pending_stats_update_task is not None and not pending_stats_update_task.done():
        return

    logger.debug("Created task to update stats")
    pending_stats_update_task = asyncio.create_task(update_stats(stats_path))


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


async def _cleanup():
    local_socket_path.unlink(missing_ok=True)
    logger.info("Worker's been shut down 💤")


async def _wait_for_users_disconnect():
    logger.info("len(connections)=%s", len(connections))
    while len(connections) != 0:
        await asyncio.sleep(1)


async def _gracefull_shutdown(timeout):
    global shutdown_requested_at
    shutdown_requested_at = datetime.datetime.now()

    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(_wait_for_users_disconnect(), timeout)

    assert loop
    await _cleanup()
    loop.stop()


def _log_shutdown_info():
    if shutdown_requested_at is None:
        raise Exception("Can be used only during shutdown!")

    minutes_till_shutdown = (
        shutdown_timeout - (datetime.datetime.now() - shutdown_requested_at).seconds
    ) // 60

    logger.info("Shutdown is pending...")
    logger.info("Shutting down in %s minutes", minutes_till_shutdown)
    logger.info(
        "%d active connection left: [%s]",
        len(connections),
        ", ".join([str(x) for x in connections]),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global loop  # noqa: PLW0603
    loop = asyncio.get_event_loop()

    signal.signal(
        signal.SIGINT,
        lambda _, __: asyncio.create_task(_gracefull_shutdown(shutdown_timeout))
        if not shutdown_requested_at
        else _log_shutdown_info(),
    )
    signal.signal(
        signal.SIGTERM,
        lambda _, __: asyncio.create_task(_gracefull_shutdown(shutdown_timeout))
        if not shutdown_requested_at
        else _log_shutdown_info(),
    )

    stats_path.unlink(missing_ok=True)

    await _listen_local_socket(local_socket_path=local_socket_path)

    yield

    # Actually may never accure
    await _cleanup()


app = FastAPI(lifespan=lifespan)
security = HTTPBasic()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if shutdown_requested_at is not None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service doesn't accept new connections",
        )

    try:
        await websocket.accept()
        client_id = uuid.uuid4()
        connections[client_id] = websocket

        logger.info("Client %s connected!", client_id)
        await websocket.send_text(f"Your id: {client_id}")
        update_stats_safe(stats_path)

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
        update_stats_safe(stats_path)


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
