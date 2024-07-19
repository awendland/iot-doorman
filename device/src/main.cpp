#include <Arduino.h>

#include <WiFi.h>
#include <WiFiMulti.h>
#include <WiFiClientSecure.h>

#include <ArduinoJson.h>
#include <WebSocketsClient.h>

const char* ssid = "Skylight";
const char* password = "photosynthesis";
const int relayPin = 16; // GPIO pin connected to the relay
const int doorbellRingPin = 32; // GPIO pin connected to the doorbell input
const char* serverHost = "20warren.alexwendland.com";
const uint16_t serverPort = 443;
#define USE_SERIAL Serial

WiFiMulti WiFiMulti;
WebSocketsClient webSocket;
char jsonPayload[256];

void setClock() {
    configTime(0, 0, "pool.ntp.org", "time.nist.gov");

    USE_SERIAL.print(F("Waiting for NTP time sync: "));
    time_t nowSecs = time(nullptr);
    while(nowSecs < 8 * 3600 * 2) {
        delay(500);
        USE_SERIAL.print(F("."));
        yield();
        nowSecs = time(nullptr);
    }

    USE_SERIAL.println();
    struct tm timeinfo;
    gmtime_r(&nowSecs, &timeinfo);
    USE_SERIAL.print(F("Current time: "));
    USE_SERIAL.print(asctime(&timeinfo));
}

void hexdump(const void *mem, uint32_t len, uint8_t cols = 16) {
	const uint8_t* src = (const uint8_t*) mem;
	USE_SERIAL.printf("\n[HEXDUMP] Address: 0x%08X len: 0x%X (%d)", (ptrdiff_t)src, len, len);
	for(uint32_t i = 0; i < len; i++) {
		if(i % cols == 0) {
			USE_SERIAL.printf("\n[0x%08X] 0x%08X: ", (ptrdiff_t)src, i);
		}
		USE_SERIAL.printf("%02X ", *src);
		src++;
	}
	USE_SERIAL.printf("\n");
}

void handleWebSocketText(uint8_t *payload, size_t length) {
	USE_SERIAL.printf("[WSin] text: %s\n", payload);

	// parse JSON payload
	DynamicJsonDocument doc(256);
	deserializeJson(doc, payload, length);

	// check if the received message is an unlock command
	if (!doc.containsKey("type")) {
		USE_SERIAL.println("[WSin] no 'type' key in JSON payload");
		return;
	}
	if (doc["type"] != "device.cmd") {
		USE_SERIAL.println("[WSin] 'type' key is not 'device.cmd' in JSON payload");
		return;
	}
	if (!doc.containsKey("cmd")) {
		USE_SERIAL.println("[WSin] no 'cmd' key in JSON payload");
		return;
	}

	if (doc["cmd"] == "unlock") {
		USE_SERIAL.println("[WSin] unlock command received");
		int duration = 5;
		if (doc.containsKey("duration")) {
			duration = doc["duration"];
		}
		USE_SERIAL.printf("[WSin] unlock for %d seconds\n", duration);
		digitalWrite(16, HIGH);
		delay(duration * 1000);
		digitalWrite(16, LOW);
	} else {
		USE_SERIAL.printf("[WSin] unknown command: %s\n", doc["cmd"]);
	}
}

void webSocketEvent(WStype_t type, uint8_t * payload, size_t length) {

	switch(type) {
		case WStype_DISCONNECTED:
			USE_SERIAL.printf("[WSc] Disconnected!\n");
			break;
		case WStype_CONNECTED:
			USE_SERIAL.printf("[WSc] Connected to url: %s\n", payload);

			// send message to server when Connected
            // Construct the JSON payload manually
            sprintf(jsonPayload, "{\"type\":\"device.status\",\"status\":\"connected\",\"timestamp_ntp\":%ld}", time(nullptr));
			webSocket.sendTXT(jsonPayload);
			break;
		case WStype_TEXT:
			handleWebSocketText(payload, length);
			// send message to server
			// webSocket.sendTXT("message here");
			break;
		case WStype_BIN:
			USE_SERIAL.printf("[WSc] get binary length: %u\n", length);
			hexdump(payload, length);

			// send data to server
			// webSocket.sendBIN(payload, length);
			break;
		case WStype_ERROR:			
		case WStype_FRAGMENT_TEXT_START:
		case WStype_FRAGMENT_BIN_START:
		case WStype_FRAGMENT:
		case WStype_FRAGMENT_FIN:
			break;
	}

}

int lastAnalogueValue = -1;
int ringCount = 0;
unsigned long ringStartTime = 0;
bool lastDoorbellIsRinging = false;
const int DOORBELL_RING_COUNT_PERIOD_MS = 500;
const int DOORBELL_RING_COUNT_THRESHOLD = 10;
const int DOORBELL_RING_ANALOG_THRESHOLD = 200;

void checkDoorbell() {
	int analogValue = analogRead(doorbellRingPin);
	unsigned long now = millis();
	if (analogValue != lastAnalogueValue) {
		USE_SERIAL.printf("[doorbell] change analogValue=%d\n", analogValue, time(nullptr));
		lastAnalogueValue = analogValue;
	}

	if (analogValue > DOORBELL_RING_ANALOG_THRESHOLD) {
		if (ringCount == 0) {
			ringStartTime = now;
		}
		ringCount++;
	}
	bool isRinging = ringCount >= DOORBELL_RING_COUNT_THRESHOLD && now - ringStartTime <= DOORBELL_RING_COUNT_PERIOD_MS;
	if (isRinging != lastDoorbellIsRinging) {
		USE_SERIAL.printf("[doorbell] state changed: doorbellIsRinging=%d\n", isRinging);
		sprintf(jsonPayload, "{\"type\":\"device.status\",\"status\":\"ring.%s\",\"timestamp\":%ld}", isRinging ? "start" : "stop", now);
		webSocket.sendTXT(jsonPayload);
		lastDoorbellIsRinging = isRinging;
	}
	if (now - ringStartTime > DOORBELL_RING_COUNT_PERIOD_MS) {
		ringCount = 0;
	}
}

void setup() {
	USE_SERIAL.begin(115200);

	pinMode(relayPin, OUTPUT);
	pinMode(doorbellRingPin, INPUT);
	digitalWrite(relayPin, LOW); // Relay off

	USE_SERIAL.setDebugOutput(true);

	USE_SERIAL.println();
	USE_SERIAL.println();
	USE_SERIAL.println();

	for(uint8_t t = 4; t > 0; t--) {
		USE_SERIAL.printf("[SETUP] BOOT WAIT %d...\n", t);
		USE_SERIAL.flush();
		delay(1000);
	}

	WiFiMulti.addAP(ssid, password);

	//WiFi.disconnect();
	while(WiFiMulti.run() != WL_CONNECTED) {
		delay(100);
	}
    Serial.println("WiFi connected");
    Serial.print("IP address: ");
    Serial.println(WiFi.localIP());

    setClock();

	// server address, port and URL
	webSocket.beginSSL(serverHost, serverPort, "/ws/device");

	// event handler
	webSocket.onEvent(webSocketEvent);

	// use HTTP Basic Authorization
	webSocket.setAuthorization("device", "niYmTfkJ9c2k6XSD5y6LrC7Wcrpute");

	// try ever 5000 again if connection has failed
	webSocket.setReconnectInterval(5000);

	// Ping every 15s, wait 3s for response, if failed 2 times then disconnect
	webSocket.enableHeartbeat(15000, 3000, 2);
}

void loop() {
	webSocket.loop();
	checkDoorbell();
}
