#include <NimBLEDevice.h>
#include <WiFi.h>

static WiFiClient client;
static uint32_t lastConnectAttempt = 0;

static void sendRssi(const char* mac, int rssi) {
    char line[160];
    snprintf(line, sizeof(line),
             "{\"type\":\"rssi\",\"sensor_id\":\"%s\",\"mac\":\"%s\","
             "\"anchor\":[%.2f,%.2f,%.2f],\"rssi\":%d}\n",
             SENSOR_ID, mac, ANCHOR_X, ANCHOR_Y, ANCHOR_Z, rssi);
    client.print(line);
}

class ScanCB : public NimBLEAdvertisedDeviceCallbacks {
    void onResult(NimBLEAdvertisedDevice* dev) override {
        if (client.connected())
            sendRssi(dev->getAddress().toString().c_str(), dev->getRSSI());
    }
};

static void ensureLinks() {
    if (WiFi.status() != WL_CONNECTED) {
        WiFi.begin(WIFI_SSID, WIFI_PASS);
        while (WiFi.status() != WL_CONNECTED) delay(250);
    }
    uint32_t now = millis();
    if (!client.connected() && now - lastConnectAttempt > 2000) {
        lastConnectAttempt = now;
        if (client.connect(TRACKER_HOST, TRACKER_PORT)) {
#ifdef BRIDGE_TOKEN
            client.print("{\"auth\":\"" BRIDGE_TOKEN "\"}\n");
#endif
        }
    }
}

void setup() {
    Serial.begin(115200);
    WiFi.mode(WIFI_STA);
    ensureLinks();
    NimBLEDevice::init("");
    NimBLEScan* scan = NimBLEDevice::getScan();
    scan->setAdvertisedDeviceCallbacks(new ScanCB(), true);
    scan->setActiveScan(false);
    scan->setInterval(100);
    scan->setWindow(60);
    scan->start(0, nullptr, false);  // continuous
}

void loop() {
    ensureLinks();
    delay(500);
}
