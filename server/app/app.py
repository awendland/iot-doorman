import asyncio
from datetime import datetime
from typing import Annotated, Literal, Optional, Union
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from starlette.types import Message
import structlog
import secrets
import base64
from fastapi.security import HTTPBasic, HTTPBasicCredentials

logger: structlog.stdlib.BoundLogger = structlog.get_logger()

app = FastAPI()

security = HTTPBasic()


def gen_security(username: str, password: str):
    def security_middleware(
        credentials: Annotated[HTTPBasicCredentials, Depends(security)],
    ):
        is_correct_username = secrets.compare_digest(
            credentials.username.encode("utf8"), username.encode("utf8")
        )
        is_correct_password = secrets.compare_digest(
            credentials.password.encode("utf8"), password.encode("utf8")
        )
        if not (is_correct_username and is_correct_password):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect username or password",
                headers={"WWW-Authenticate": "Basic"},
            )
        return credentials.username

    return security_middleware


@app.get("/")
async def root():
    return HTMLResponse("""\
<html>
<head>
    <script>
    {
        var socket = new WebSocket(`ws${window.location.protocol === "https:" ? "s" : ""}://${window.location.host}/ws/client`);
        var _history = [];

        socket.onopen = function(event) {
            console.log("WebSocket connection established.");
            requestHistory();
        };

        socket.onmessage = function(event) {
            console.log("Received message:", event.data);
            var message = JSON.parse(event.data);
            if (message.type === "device.status") {
                _history.push([new Date(), message]);
            } else if (message.type === "client.response_history") {
                _history = message.history;
            }
            updateHistory(message.history);
        };

        socket.onclose = function(event) {
            console.log("WebSocket connection closed.");
        };

        function sendCommand() {
            var command = {
                type: "client.send_command",
                command: {
                    type: "device.cmd",
                    cmd: "unlock",
                    duration: 5
                }
            };
            socket.send(JSON.stringify(command));
        }

        function requestHistory() {
            var request = {
                type: "client.request_history",
                max_entries: 8
            };
            socket.send(JSON.stringify(request));
        }

        function updateHistory(history) {
            var historyElement = document.getElementById("history");
            historyElement.textContent = "";
            var firstStatus;
            for (var i = _history.length - 1; i >= 0; i--) {
                var entry = _history[i];
                var timestamp = new Date(entry[0]);
                if (!firstStatus && entry[1].type === "device.status") {
                    firstStatus = entry[1].status;
                }
                var historyItem = timestamp.toLocaleString() + ": " + JSON.stringify(entry[1]);
                historyElement.textContent += historyItem + "\\n";
            }
            var statusElement = document.getElementById("status");
            statusElement.textContent = "Status: " + firstStatus;
        }
    }
    </script>
</head>
<body>
    <button onclick="sendCommand()">Unlock</button>
    <pre id="status">Status: </pre>
    <pre id="history"></pre>
</body>
</html>
""")


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

device_security = gen_security("device", "niYmTfkJ9c2k6XSD5y6LrC7Wcrpute")


@app.websocket("/ws/device")
async def ws_device(
    raw_websocket: WebSocket, username: Annotated[str, Depends(device_security)]
):
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


client_security = gen_security("tenant", "95sZG4wPjL8FDT")


@app.websocket("/ws/client")
async def ws_client(
    raw_websocket: WebSocket, username: Annotated[str, Depends(client_security)]
):
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
