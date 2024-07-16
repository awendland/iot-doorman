import asyncio
from datetime import datetime
import os
from pathlib import Path
from typing import Annotated, Literal, Optional, Union
from fastapi import (
    FastAPI,
    Form,
    HTTPException,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from starlette.types import Message
import structlog
import secrets
import base64
from cachetools import TTLCache

logger: structlog.stdlib.BoundLogger = structlog.get_logger()
logger.info("starting server", git_version_hash=os.getenv("GIT_VERSION_HASH"))

app = FastAPI()


async def device_security(websocket: WebSocket):
    username = "device"
    password = "niYmTfkJ9c2k6XSD5y6LrC7Wcrpute"
    auth_header = websocket.headers.get("Authorization")
    if not auth_header:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    try:
        scheme, credentials = auth_header.split()
        if scheme.lower() != "basic":
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        decoded = base64.b64decode(credentials).decode("ascii")
        in_username, _, in_password = decoded.partition(":")
        is_correct_username = secrets.compare_digest(
            in_username.encode("utf8"), username.encode("utf8")
        )
        is_correct_password = secrets.compare_digest(
            in_password.encode("utf8"), password.encode("utf8")
        )
        if not (is_correct_username and is_correct_password):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        return username
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return


class UserSessions:
    def __init__(self):
        self.sessions = TTLCache(maxsize=100, ttl=30 * 24 * 60 * 60)

    def login(self, username: str, password: str) -> Optional[str]:
        if username == "tenant" and password == "95sZG4wPjL8FDT":
            session_id = base64.b32encode(secrets.token_bytes(10)).decode()
            self.sessions[session_id] = username
            return session_id
        return None

    def check(self, session_id: Optional[str]) -> Optional[str]:
        if not session_id:
            return None
        return self.sessions.get(session_id, None)


user_sessions = UserSessions()


class DeviceCommandUnlock(BaseModel):
    type: Literal["device.cmd"] = "device.cmd"
    cmd: Literal["unlock"] = "unlock"
    duration: int = Field(default=5, ge=1, le=30)


DeviceCommand = DeviceCommandUnlock
AnnotatedDeviceCommand = Annotated[DeviceCommand, Field(discriminator="cmd")]


class DeviceStatusDisconnected(BaseModel):
    type: Literal["device.status"] = "device.status"
    status: Literal["disconnected"] = "disconnected"


class DeviceStatusConnected(BaseModel):
    type: Literal["device.status"] = "device.status"
    status: Literal["connected"] = "connected"
    rough_time: Optional[int] = None


class DeviceStatusLastCommand(BaseModel):
    type: Literal["device.status"] = "device.status"
    status: Literal["last_command"] = "last_command"
    last_command: DeviceCommand


DeviceStatus = Union[
    DeviceStatusConnected, DeviceStatusDisconnected, DeviceStatusLastCommand
]
AnnotatedDeviceStatus = Annotated[DeviceStatus, Field(discriminator="status")]
DeviceStatusAdapter: TypeAdapter[AnnotatedDeviceStatus] = TypeAdapter(
    AnnotatedDeviceStatus
)


DeviceMessage = Union[DeviceStatus, DeviceCommand]


class ClientRequestHistory(BaseModel):
    type: Literal["client.request_history"] = "client.request_history"
    max_entries: int = 8


class ClientResponseHistory(BaseModel):
    type: Literal["client.response_history"] = "client.response_history"
    history: list[tuple[datetime, DeviceMessage]]


class ClientSendCommand(BaseModel):
    type: Literal["client.send_command"] = "client.send_command"
    command: DeviceCommand


ClientRequest = Union[ClientRequestHistory, ClientSendCommand]
AnnotatedClientRequest = Annotated[ClientRequest, Field(discriminator="type")]
ClientRequestAdapter: TypeAdapter[AnnotatedClientRequest] = TypeAdapter(
    AnnotatedClientRequest
)


class WebSocketU:
    id: str
    c: WebSocket

    def __init__(
        self, websocket: WebSocket, type: Union[Literal["device"], Literal["client"]]
    ) -> None:
        self.id = f"{type}_{base64.b32encode(secrets.token_bytes(10)).decode()}"
        self.c = websocket
        _og_send = websocket.send

        async def send(message: Message) -> None:
            logger.debug("sending message", message=message, id=self.id)
            return await _og_send(message)

        self.c.send = send


class ConnectionManager:
    def __init__(self):
        self.active_device: Optional[WebSocketU] = None
        self.active_clients: list[WebSocketU] = []
        self.history: list[tuple[datetime, DeviceMessage]] = [
            (datetime.now(), DeviceStatusDisconnected())
        ]

    async def connect_device(self, websocket: WebSocketU):
        await websocket.c.accept()
        self.active_device = websocket
        await self.broadcast_device_status(DeviceStatusConnected())

    async def connect_client(self, websocket: WebSocketU):
        await websocket.c.accept()
        self.active_clients.append(websocket)

    async def disconnect_device(self):
        self.active_device = None
        await self.broadcast_device_status(DeviceStatusDisconnected())

    async def disconnect_client(self, websocket: WebSocketU):
        self.active_clients.remove(websocket)

    async def broadcast_device_status(self, status: DeviceStatus):
        self.history.append((datetime.now(), status))
        status_msg = status.model_dump_json()
        await asyncio.gather(
            *(connection.c.send_text(status_msg) for connection in self.active_clients)
        )

    async def send_device_command(self, cmd: DeviceCommand):
        if self.active_device is None:
            logger.info("no active device to send command to", cmd=cmd)
            return
        await self.active_device.c.send_text(cmd.model_dump_json())
        await self.broadcast_device_status(DeviceStatusLastCommand(last_command=cmd))


manager = ConnectionManager()


@app.websocket("/ws/device")
async def ws_device(raw_websocket: WebSocket):
    username = await device_security(raw_websocket)
    if not username:
        return
    websocket = WebSocketU(raw_websocket, "device")
    log = logger.bind(device_id=websocket.id)
    await manager.connect_device(websocket)
    try:
        while True:
            data = await websocket.c.receive_text()
            log.debug("received data from device", data=data)
            try:
                device_status = DeviceStatusAdapter.validate_json(data)
                await manager.broadcast_device_status(device_status)
            except ValidationError as e:
                log.warn("unable to parse device status", error=e)
                await websocket.c.send_json(
                    {"error": "unable to parse request", "errors": e.json()}
                )
    except WebSocketDisconnect as e:
        log.info("device disconnected", error=e)
        await manager.disconnect_device()


@app.post("/ws/client_auth")
def client_auth_endpoint(
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    response: Response,
):
    session_id = user_sessions.login(username, password)
    if not session_id:
        return HTTPException(status_code=401, detail="Authentication failed")
    response.set_cookie(key="session_id", value=session_id)
    return ""


@app.websocket("/ws/client")
async def ws_client(raw_websocket: WebSocket):
    username = user_sessions.check(raw_websocket.cookies.get("session_id"))
    if not username:
        return HTTPException(
            status_code=401,
            detail="Missing session_id cookie with authenticated session. Call POST /ws/client_auth to authenticate.",
        )
    websocket = WebSocketU(raw_websocket, "client")
    log = logger.bind(device_id=websocket.id)
    await manager.connect_client(websocket)
    try:
        while True:
            data = await websocket.c.receive_text()
            log.debug("received data from client", data=data)
            try:
                client_request = ClientRequestAdapter.validate_json(data)
                if client_request.type == "client.send_command":
                    await manager.send_device_command(client_request.command)
                elif client_request.type == "client.request_history":
                    history = manager.history[-client_request.max_entries :]
                    log.debug("sending history to client", history=history)
                    await websocket.c.send_text(
                        ClientResponseHistory(history=history).model_dump_json()
                    )
            except ValidationError as e:
                log.warn("unable to parse client request", error=e)
                await websocket.c.send_json(
                    {"error": "unable to parse request", "errors": e.json()}
                )
    except WebSocketDisconnect as e:
        log.info("client disconnected", error=e)
        await manager.disconnect_client(websocket)


app.mount(
    "/",
    StaticFiles(directory=Path(__file__).parent / "static", html=True),
    name="static",
)
