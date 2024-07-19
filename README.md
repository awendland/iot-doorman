# iot-doorman

- [x] Setup subproject device/ using PlatformIO (<https://docs.platformio.org/en/latest/tutorials/index.html>)
- [x] Setup go (?) project for server/
- [ ] Figure out how to record wiring diagram for board
- [x] Secure websocket from device/ to server. Use configuration loaded into device.
- [x] Add standard CI tooling
- [x] Setup end-to-end testing which ensures the device/ and server/ communicate correctly.
- [ ] Add push notifications for doorbell ring <https://github.com/mdn/serviceworker-cookbook/tree/master/push-subscription-management>
- [ ] Use PlatformIO unit testing, and consider <https://docs.wokwi.com/wokwi-ci/getting-started>.

## Wiring Diagram

_The capacitor is not used, as of 2024-07-19._

```text
  12V doorbell signal
         |
        R1 (10kΩ)
         |
         +--- ESP32 GPIO (e.g., GPIO 32) ----> (analogRead)
         |             |
        R2 (5.1kΩ)    +
         |             |
        GND         Capacitor (10μF)
                      |
                     GND
```
